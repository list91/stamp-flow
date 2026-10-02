"""Обход по cron в review-режиме (`apply-review`)

Маршруты вакансии:
  a) детектор нашёл инъекцию        → ревью письма (+ анкеты, если есть)
  b) response_letter_required        → ревью письма (+ анкеты)
  b2) описание просит что-то в письме → ревью письма
  c) has_test                        → ревью анкеты
  d) иначе                           → авто-отклик без письма
Отсев (--excluded-filter, --ai-filter, --skip-tests, -f) не применяется.
Ревью (a, b, b2, c) — не очередь, а сплошной поток: вакансия сразу показывается в боте,
обход стоит, пока пользователь не решит (апрув/реджект), и только потом берёт следующую.
Оборванный на ревью обход при старте сперва дорешивает свою карточку.

Надёжность:
  * темп как у человека: случайная пауза между откликами и лимит в час
    (pacing.py) — чтобы hh не включал капчу;
  * капча → обход останавливается, бот присылает картинку, после ответа
    бот сам перезапускает обход с теми же параметрами;
  * модель недоступна → обход ждёт её (не откладывает вакансии);
    если так и не ожила — вакансия уходит на ревью (fail-closed), без
    авто-отклика мимо проверки.
"""

from __future__ import annotations

import json
import logging
import os
import random
import sqlite3
import sys
import threading
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from hh_applicant_tool.api.errors import ApiError, CaptchaRequired, LimitExceeded, Redirect
from . import render
from .checks import CheckBoard
from .context import build_resume_text
from .detector import DETECTOR_VERSION, InjectionDetector, Verdict
from .lock import FileLock, LockTimeout, hh_lock
from .timer import WaitBoard, next_cron_tick
from .models import ItemState
from .relevance import RUBRIC, Relevance, ScoreParseError, letter_demanded, score_relevance
from .pacing import DEFAULT_MAX_PER_HOUR, Pacer, parse_range
from .schedule import is_open
from .services import make_llm, make_store, make_window, new_run_id, review_config
from .store import TransitionError
from .submit import Submitter, SubmitResult

if TYPE_CHECKING:
    from stamp_flow.operations.apply_review import Operation

logger = logging.getLogger(__package__)


DRY_PREFIX = "dry:"
# сколько ждать ожившую модель, секунд (по умолчанию 10 минут)
DEFAULT_LLM_WAIT = 600
# лимит откликов исчерпан: сколько квот вперёд ещё искать вакансии для ревью
LOOKAHEAD = 5
# сколько оценок подряд модель может завалить, прежде чем обход остановится
LLM_FAIL_LIMIT = 3
# блокировка на ревью: как часто смотрим решение, пульс для бота, запас на отправку после апрува
REVIEW_POLL_SEC = 2.0
REVIEW_BEAT_SEC = 5.0
REVIEW_BACKLOG_SEC = 60.0  # как часто пробуем отправить застрявшее на лимите, пока ждём человека
REVIEW_GRACE_SEC = 30.0
APPLY_COMMANDS = ("apply-review",)


def resume_args(argv: list[str] | None = None) -> list[str]:
    """Аргументы после имени команды — чтобы бот перезапустил тот же обход."""
    argv = list(sys.argv[1:] if argv is None else argv)
    for i, arg in enumerate(argv):
        if arg in APPLY_COMMANDS:
            return argv[i + 1 :]
    return []


class ReviewPass:
    def __init__(
        self,
        op: Operation,
        store=None,
        detector: InjectionDetector | None = None,
        window=None,
        now=datetime.now,
        pacer: Pacer | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self.op = op
        self.tool = op.tool
        self.args = op.args
        self.dry_run = bool(getattr(op, "dry_run", False))
        cfg = review_config(self.tool)
        self.relevance_profile = str(cfg.get("relevance_profile") or "")
        self.store = store or make_store(self.tool, new_run_id("pass"))
        self.window = (
            window
            if window is not None
            else make_window(self.tool, getattr(self.args, "work_hours", None))
        )
        self.max_queue = getattr(self.args, "max_queue", None) or cfg.get("max_queue")
        self.max_responses = getattr(op, "max_responses", None)
        if detector is None:
            use_llm = bool(cfg.get("injection_llm", True))
            llm = make_llm(self.tool, self.store) if use_llm else None
            detector = InjectionDetector(llm, use_llm=use_llm)
        self.detector = detector
        self.now = now
        self.sleep = sleep
        self.monotonic = monotonic
        self.llm_wait = float(
            getattr(self.args, "llm_wait", None) or cfg.get("llm_wait", DEFAULT_LLM_WAIT)
        )
        if pacer is None:
            max_per_hour = getattr(self.args, "max_per_hour", None)
            if max_per_hour is None:
                max_per_hour = cfg.get("max_per_hour", DEFAULT_MAX_PER_HOUR)
            pacer = Pacer(
                self.store,
                delay=parse_range(getattr(self.args, "apply_delay", None) or cfg.get("apply_delay")),
                max_per_hour=max_per_hour,
                cancelled=self._cancelled,
            )
        self.board = WaitBoard(self.tool.config_path)
        self.checks = CheckBoard(self.tool.config_path)
        if getattr(pacer, "waiter", "absent") is None:
            pacer.waiter = lambda left: self.board.wait(
                left, "vacancy", "пауза между откликами", self._cancelled, self.sleep
            )
        self.pacer = pacer
        self.resume_delay = parse_range(
            getattr(self.args, "resume_delay", None) or cfg.get("resume_delay"), "30-90"
        )
        self.round_delay = parse_range(
            getattr(self.args, "round_delay", None) or cfg.get("round_delay"), "120-300"
        )
        rel = getattr(self.args, "relevance_min", None)
        if rel is None:
            rel = cfg.get("relevance_min") or 0
        self.relevance_min = float(rel)
        # поток без очереди: обход стоит на каждой вакансии, ушедшей на ревью,
        # пока пользователь не решит (--no-block-on-review — только для отладки)
        block = getattr(self.args, "block_on_review", None)
        if block is None:
            block = cfg.get("block_on_review", True)
        self.block_on_review = bool(block)
        self._own_texts: dict[str, str] = {}
        self._progress: dict[str, Any] = {}
        render.set_progress_dir(getattr(self.tool, "config_path", None))
        self._letter_flag: set[str] = set()  # вакансии, где модель увидела просьбу про письмо
        self.stats: Counter[str] = Counter()
        self.stop = False
        # модель не ожила за llm_wait — дальше не ждём, сразу fail-closed
        self.llm_dead = False
        self._llm_fail_streak = 0  # оценок подряд без ответа модели (см. _llm_failed)

    # ------------------------------------------------------------------ run

    def run(self, resumes: list[dict[str, Any]]) -> Counter[str]:
        if not is_open(self.window, self.now()):
            self.store.log_event("pass_skipped_off_hours", {"window": str(self.window)})
            print(f"🌙 Вне окна работы ({self.window}), обход пропущен")
            return self.stats

        self.store.log_event(
            "pass_started",
            {
                "dry_run": self.dry_run,
                "window": str(self.window) if self.window else None,
                "max_queue": self.max_queue,
                "max_responses": self.max_responses,
                "max_per_hour": self.pacer.max_per_hour,
                "apply_delay": list(self.pacer.delay),
                "resumes": [r.get("id") for r in resumes],
                "args": resume_args(),
            },
        )
        if self._captcha_unresolved():
            self.store.log_event("pass_skipped_captcha", {})
            print("🧩 Капча hh не решена — обход пропущен; после ответа в боте он запустится сам")
            return self.stats
        lock = FileLock(Path(self.tool.config_path) / ".hh-pass.lock", timeout=0)
        try:
            lock.acquire()
        except LockTimeout:
            print("⏭ Обход уже идёт в другом процессе — пропускаю")
            return self.stats
        # cron-обход стартовал: отсчёт «до следующего обхода» больше не нужен
        cur = self.board.current()
        if cur and cur.get("kind") == "next_pass":
            self.board.clear(cur["id"])
        try:
            self._wait_for_llm("старт обхода")
            self._submit_backlog()
            self._finish_open_reviews(resumes)
            self._run_rounds(resumes)
        finally:
            lock.release()
            render.write_progress(None)
            self.store.log_event("pass_finished", {**self.stats, "dry_run": self.dry_run})
            # до следующего запуска cron (только loop=true); ⏭ в боте запустит обход сразу
            # при нерешённой капче cron-обходы всё равно пропускаются, а после ответа
            # бот сам запустит обход, поэтому отсчёт только вводит в заблуждение
            if (
                not self._cancelled()
                and os.environ.get("HH_LOOP", "1") != "0"
                and not self._captcha_unresolved()
            ):
                every = int(os.environ.get("HH_CRON_EVERY_MIN") or 15)
                self.board.publish(
                    "next_pass", next_cron_tick(every_min=every), "следующий обход по cron",
                    {"args": resume_args()},
                )
        print(
            "📝 Обход завершён: "
            + ", ".join(f"{k}={v}" for k, v in sorted(self.stats.items()))
        )
        return self.stats

    def _captcha_unresolved(self, max_age: float = 3 * 3600) -> bool:
        """Недавняя капча без решения/отказа — обход упрётся в неё и сбросит картинку."""
        return self.store.captcha_unresolved(max_age) is not None

    def _cancelled(self) -> bool:
        event = getattr(self.op, "_cancel_event", None)
        return bool(event and event.is_set())

    # --------------------------------------------------------------- модель

    def _wait_for_llm(self, where: str) -> bool:
        """Ждать, пока модель оживёт (до llm_wait секунд). True — жива."""
        llm = getattr(self.detector, "llm", None)
        health = getattr(llm, "health", None)
        if not self.detector.use_llm or not callable(health) or self.llm_dead:
            return not self.llm_dead
        ok, detail = health()
        if ok:
            return True
        logger.error(
            "Модель недоступна (%s): %s — жду до %d мин, вакансии не пропускаю",
            where, detail, int(self.llm_wait // 60),
        )
        self.store.log_event("llm_unavailable", {"where": where, "detail": detail})
        deadline = self.monotonic() + self.llm_wait
        delay = 15.0
        while self.monotonic() < deadline and not self._cancelled():
            self.sleep(min(delay, max(deadline - self.monotonic(), 0)))
            delay = min(delay * 2, 120.0)
            ok, detail = health()
            if ok:
                self.store.log_event("llm_recovered", {"where": where})
                logger.warning("Модель снова доступна (%s)", where)
                return True
        self.llm_dead = True
        logger.error(
            "Модель так и не ожила за %d мин (%s): новые вакансии идут на ревью без авто-откликов",
            int(self.llm_wait // 60), detail,
        )
        self.store.log_event("llm_dead", {"where": where, "detail": detail})
        return False

    # -------------------------------------------------------------- backlog

    def _submit_backlog(self, skip: int | None = None) -> None:
        """Отправить одобренное, но не ушедшее (лимит в час, сбой, окно работы).

        skip — карточка, по которой обход сейчас ждёт решения: её отправкой занят бот.
        """
        submitter = Submitter(
            self.tool, self.store, self.window, operation=self.op, pacer=self.pacer
        )
        for item in self.store.by_state(ItemState.APPROVED, ItemState.FAILED_RETRYABLE):
            if item.id == skip:
                continue
            if self.pacer.hourly_limit_reached():
                self.stats["backlog_hourly_limit"] += 1
                return
            if not item.dry_run:
                self.pacer.wait_before_apply()
            try:
                with hh_lock(self.tool.config_path):
                    result = submitter.submit(item)
            except TransitionError:
                # параллельно её уже отправляет бот (review-submit): второй раз не шлём
                self.stats["backlog_taken_by_bot"] += 1
                continue
            self.stats[f"backlog_{result}"] += 1
            if result in (SubmitResult.LIMIT, SubmitResult.CAPTCHA):
                self.stop = True
                return

    def _retry_backlog_while_waiting(self, item_id: int) -> None:
        """Обход стоит и ждёт человека, а одобренное раньше письмо застряло на лимите в час.
        Лимит скользящий: как только освободился — отправляем, не дожидаясь конца ожидания."""
        try:
            pending = [
                i for i in self.store.by_state(ItemState.APPROVED, ItemState.FAILED_RETRYABLE)
                if i.id != item_id and not i.dry_run
            ]
        except sqlite3.OperationalError:
            return
        if not pending or self.pacer.hourly_limit_reached():
            return
        try:
            self._submit_backlog(skip=item_id)
            self.stats["backlog_while_waiting"] += 1
        except Exception:  # noqa: BLE001 — ожидание человека не должно падать из-за отправки
            logger.exception("Отправка отложенного во время ожидания ревью")

    # ----------------------------------------------------------- по резюме

    def _rotation_file(self) -> Path:
        name = "review-rotation-dry.json" if self.dry_run else "review-rotation.json"
        return Path(self.tool.config_path) / name

    def _rotate(self, resumes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Ротация: каждый запуск начинается со следующего резюме."""
        if len(resumes) < 2:
            return resumes
        path = self._rotation_file()
        try:
            start = int(json.loads(path.read_text("utf-8")).get("next", 0))
        except (OSError, ValueError, AttributeError):
            start = 0
        start %= len(resumes)
        try:
            path.write_text(json.dumps({"next": (start + 1) % len(resumes)}), "utf-8")
        except OSError as ex:
            logger.warning("Не удалось сохранить ротацию резюме: %s", ex)
        return resumes[start:] + resumes[:start]

    def _load_texts(self, resumes: list[dict[str, Any]]) -> dict[str, str]:
        texts: dict[str, str] = {}
        for resume in resumes:
            rid = resume["id"]
            try:
                texts[rid] = build_resume_text(self.tool.api_client.get(f"/resumes/{rid}"))
            except ApiError as ex:
                logger.error("Не удалось получить резюме %s: %s", rid, ex, exc_info=True)
                texts[rid] = resume.get("title") or ""
        return texts

    @staticmethod
    def _pooled_text(current_id: str, texts: dict[str, str]) -> str:
        """Текущее резюме — главное; остальные — общий пул опыта."""
        others = [t for rid, t in texts.items() if rid != current_id and t]
        text = texts.get(current_id, "")
        if not others:
            return text
        return (
            "=== ТЕКУЩЕЕ РЕЗЮМЕ (откликаемся им, пиши в его пользу) ===\n"
            + text
            + "\n\n=== ДРУГИЕ МОИ РЕЗЮМЕ (общий опыт; бери факты, только если они усиливают текущее) ===\n"
            + "\n\n---\n\n".join(others)
        )

    def _run_rounds(self, resumes: list[dict[str, Any]]) -> None:
        cfg = review_config(self.tool)
        per_resume_rng = int_range(getattr(self.args, "per_resume", None) or cfg.get("per_resume") or 0)
        rounds = random.randint(*int_range(getattr(self.args, "rounds", None) or cfg.get("rounds") or 1))
        order = self._rotate(resumes)
        texts = self._load_texts(order)
        self._own_texts = texts
        iters = {r["id"]: iter(self.op._get_vacancies(resume_id=r["id"])) for r in order}
        self.store.log_event(
            "pass_plan",
            {"order": [r["id"] for r in order], "per_resume": list(per_resume_rng), "rounds": rounds},
        )
        started = False
        for rnd in range(1, int(rounds) + 1):
            active = False
            first_in_round = True
            # квота на резюме — случайно на каждый круг
            per_resume = random.randint(*per_resume_rng)
            self.store.log_event("round_plan", {"round": rnd, "rounds": rounds, "per_resume": per_resume})
            print(f"🎲 Круг {rnd}/{rounds}: по {per_resume or '∞'} вакансий с резюме")
            self._progress = {
                "round": rnd, "rounds": int(rounds), "quota": per_resume,
                "done": {r["id"]: 0 for r in order if r["id"] in iters},
            }
            self._save_progress()
            for resume in order:
                if self.stop or self._cancelled():
                    return
                rid = resume["id"]
                if rid not in iters:
                    continue
                # ровно одна пауза перед резюме: круга (первое в круге) или смены резюме
                if started and first_in_round:
                    self._pause(self.round_delay, "round", f"круг {rnd}/{rounds}")
                elif started:
                    self._pause(self.resume_delay, "resume", f"следующее: {resume.get('title')}")
                started = True
                first_in_round = False
                if self.stop or self._cancelled():
                    return
                print(f"🔁 Круг {rnd}/{rounds}: резюме «{resume.get('title')}»")
                if self._run_resume(resume, self._pooled_text(rid, texts), iters[rid], per_resume):
                    active = True
                else:
                    iters.pop(rid)  # вакансии кончились
            self.stats["rounds"] += 1
            if not active:
                return

    def _save_progress(self) -> None:
        if not getattr(self, "dry_run", False):
            render.write_progress(getattr(self, "_progress", None) or None)

    def _mark_progress(self, resume_id: str, number: int) -> None:
        """Номер вакансии, обрабатываемой сейчас (для метки резюме в боте)."""
        progress = getattr(self, "_progress", None)
        if progress and resume_id in progress["done"]:
            quota = progress.get("quota") or 0
            progress["done"][resume_id] = min(number, quota) if quota else number
            self._save_progress()

    def _pause(self, rng: tuple[float, float], kind: str, label: str) -> None:
        lo, hi = rng
        if hi <= 0:
            return
        seconds = random.uniform(lo, hi)
        # один отсчёт вместо двух подряд: покрываем и остаток паузы между
        # откликами, а ближайший отклик после этой паузы не ждёт повторно
        remaining = self.pacer.remaining() if not self.dry_run else 0.0
        seconds = max(seconds, remaining)
        print(f"⏳ {label}: жду {int(seconds)} с")
        self.board.wait(seconds, kind, label, self._cancelled, self.sleep)
        self.pacer.skip_next = True

    def _run_resume(
        self, resume: dict[str, Any], resume_text: str, vacancies, quota: int = 0
    ) -> bool:
        """Обработать до quota новых вакансий. False — вакансии кончились."""
        resume_id = resume["id"]
        done = 0
        deferred = 0
        # при исчерпанном лимите не листаем hh до конца: не больше LOOKAHEAD квот вперёд
        lookahead = (quota or 10) * LOOKAHEAD
        for vacancy in vacancies:
            if self.stop or self._cancelled():
                return True
            self.stats["seen"] += 1
            self._deferred = False
            self._mark_progress(resume_id, done + 1)
            try:
                if self._handle_vacancy(resume, resume_text, vacancy):
                    done += 1
                    self.stats[f"handled_{resume_id}"] += 1
                elif self._deferred:
                    deferred += 1
                    if deferred >= lookahead:
                        self.stats["lookahead_stop"] += 1
                        return True  # вакансии ещё есть — следующий круг/обход
            except _tool_error() as ex:
                # веб-сессия hh (куки) истекла: анкету не загрузить — вакансию
                # не помечаем известной, обход идёт дальше
                self.stats["skip_web_session"] += 1
                if self.stats["skip_web_session"] == 1:
                    logger.error(
                        "Веб-сессия hh истекла (%s): вакансии с анкетой пропускаю до перелогина (stamp-flow auth, при капче -k или -s)",
                        ex,
                    )
                self.store.log_event(
                    "error",
                    {"where": "web_session", "error": str(ex), **brief_vacancy(vacancy)},
                    resume_id=resume_id,
                    vacancy_id=vacancy.get("id"),
                )
                continue
            except LimitExceeded:
                self.store.log_event("hh_limit_exceeded", {}, resume_id=resume_id)
                print("⛔ Лимит откликов hh.ru исчерпан.")
                self.stop = True
            except (ApiError, ValueError, OSError) as ex:
                self.stats["errors"] += 1
                logger.error("Вакансия %s: %s", vacancy.get("alternate_url"), ex, exc_info=True)
                self.store.log_event(
                    "error",
                    {"where": "vacancy", "error": f"{type(ex).__name__}: {ex}", **brief_vacancy(vacancy)},
                    resume_id=resume_id,
                    vacancy_id=vacancy.get("id"),
                )
                done += 1
            if quota and done >= quota:
                return True
        return False

    def _technical_skip(self, vacancy: dict[str, Any]) -> str | None:
        if vacancy.get("relations"):
            return "already_applied"
        if vacancy.get("archived"):
            return "archived"
        if vacancy.get("response_url"):
            return "external_form"
        return None

    def _auto_apply_blocked(self) -> str | None:
        if self.max_responses and self.stats["auto_applied"] >= self.max_responses:
            return "max_responses"
        if not self.dry_run and self.pacer.hourly_limit_reached():
            return "hourly_limit"
        return None

    def _handle_vacancy(
        self, resume: dict[str, Any], resume_text: str, vacancy: dict[str, Any]
    ) -> bool:
        """True — новая вакансия засчитана в квоту резюме."""
        resume_id = resume["id"]
        vacancy_id = vacancy["id"]
        # dry-run живёт в отдельном пространстве: иначе проверочный прогон
        # навсегда пометил бы вакансии «известными» для боевого
        queue_key = f"{DRY_PREFIX}{resume_id}" if self.dry_run else resume_id

        if reason := self._technical_skip(vacancy):
            self.stats[f"skip_{reason}"] += 1
            return False
        if self.store.is_known(queue_key, vacancy_id) or self.store.is_known_any(
            vacancy_id, self.dry_run
        ):
            self.stats["skip_known"] += 1
            return False

        if self.relevance_min:
            cached = self.store.relevance_score(queue_key, vacancy_id, RUBRIC)
            if cached is not None and cached < self.relevance_min:
                # уже оценена и отправлена в архив: без модели и не в квоту
                self.stats["skip_relevance_archived"] += 1
                return False

        needs_review_hint = vacancy.get("has_test") or vacancy.get("response_letter_required")
        if needs_review_hint and self._queue_full():
            self.stats["skip_queue_full"] += 1
            return True
        if not needs_review_hint and (blocked := self._auto_apply_blocked()):
            # отклик без письма сейчас невозможен (лимит): без модели, не в квоту,
            # не помечаем известной — подберёт следующий обход
            self.stats[f"deferred_{blocked}"] += 1
            self._deferred = True
            return False

        try:
            self.tool.storage.vacancies.save(vacancy)
        except Exception as ex:  # noqa: BLE001 — как в штатном обходе
            logger.debug(ex)

        full = self.tool.api_client.get(f"/vacancies/{vacancy_id}")
        if self.relevance_min:
            passed = self._relevance_gate(resume_id, queue_key, resume_text, vacancy, full)
            if passed is None:  # модель не смогла оценить: без оценки не откликаемся
                return False
            if not passed:
                return True  # оценена и заархивирована: идёт в квоту (время обхода предсказуемо)
        test = None
        if vacancy.get("has_test") or full.get("has_test"):
            _, test = self.op._load_vacancy_test(vacancy_id)

        verdict = self._verdict(resume_id, full, test)

        steps: list[str] = []
        asks_letter = str(vacancy_id) in self._letter_flag or letter_demanded(full)
        if asks_letter:
            self.stats["letter_from_description"] += 1
            print("✉️ Описание просит указать что-то в письме → ревью:", vacancy.get("alternate_url"))
        if (
            verdict.suspicious
            or asks_letter
            or full.get("response_letter_required")
            or vacancy.get("response_letter_required")
        ):
            steps.append("letter")
        if test:
            steps.append("questionnaire")

        if steps:
            if self._queue_full():
                self.stats["skip_queue_full"] += 1
                return True
            item = self.store.enqueue(
                resume_id=queue_key,
                vacancy_id=vacancy_id,
                steps=steps,
                vacancy=full,
                resume_text=resume_text,
                verdict=verdict.to_dict(),
                test=test,
                dry_run=self.dry_run,
            )
            self.stats["enqueued"] += 1
            print(f"📨 На ревью ({' + '.join(steps)}):", vacancy.get("alternate_url"))
            self._await_review(item, resume, full)
            return True

        self._auto_apply(resume_id, vacancy, full)
        return True

    def _finish_open_reviews(self, resumes: list[dict[str, Any]]) -> None:
        """Поток без хвостов: карточку, на которой прошлый обход оборвали, дорешиваем
        до любых новых вакансий (обход стоит, пока пользователь не решит)."""
        if not self.block_on_review or self.dry_run:
            return
        titles = {r.get("id"): r.get("title") for r in resumes}
        for item in self.store.by_state(ItemState.QUEUED, ItemState.IN_REVIEW):
            if item.dry_run:
                continue  # демо-карточки (review-demo) — не из потока
            if self._cancelled():
                return
            self.stats["review_resumed"] += 1
            resume = {"id": item.resume_id, "title": titles.get(item.resume_id)}
            self._await_review(item, resume, item.vacancy_json or {})

    def _item_state(self, item_id: int) -> ItemState | None:
        """Состояние карточки; None — временно не прочиталось (БД занята ботом)."""
        try:
            item = self.store.get(item_id)
        except sqlite3.OperationalError:
            return None
        return item.state if item is not None else None

    def _await_review(self, item: Any, resume: dict[str, Any], full: dict[str, Any]) -> None:
        """Обход стоит, пока пользователь не решит по этой вакансии в боте.

        Вакансия показывается в боте первой. Решение — апрув или реджект (любой шаг:
        письмо, анкета). После апрува даём боту секунды на отправку, чтобы два отклика
        не ушли в hh одновременно; лимит в час оставляет её одобренной — тогда не ждём.
        """
        if not self.block_on_review or self.dry_run:
            return
        brief = brief_vacancy(full)
        label = f"{brief['name']} — {brief['employer']}"
        if title := resume.get("title"):
            label += f" ({title})"
        self.store.prioritize(item.id)
        try:
            active = self.store.active()
        except sqlite3.OperationalError:
            active = None
        wid = self.board.hold(
            "review", label, {"item_id": item.id, "active": active.id if active else None}
        )
        self.store.log_event(
            "review_wait_started",
            {"steps": item.steps, **brief},
            item_id=item.id,
            resume_id=item.resume_id,
            vacancy_id=item.vacancy_id,
        )
        print(f"⏸ Обход стоит: жду решения по ревью #{item.id}:", brief["url"], flush=True)
        started = beat = backlog_at = self.monotonic()
        decided_at: float | None = None
        state: ItemState | None = item.state
        outcome = "решено"
        try:
            while True:
                if self._cancelled():
                    outcome = "обход прерван"
                    break
                now = self.monotonic()
                if now - backlog_at >= REVIEW_BACKLOG_SEC:
                    backlog_at = now
                    self._retry_backlog_while_waiting(item.id)
                    if self.stop:  # капча/лимит hh при отправке отложенного
                        outcome = "остановлено при отправке отложенного"
                        break
                cur = self._item_state(item.id)
                if cur is not None:
                    state = cur
                    if cur in (ItemState.APPROVED, ItemState.SUBMITTING):
                        decided_at = now if decided_at is None else decided_at
                        if now - decided_at >= REVIEW_GRACE_SEC:
                            break
                    elif cur not in (ItemState.QUEUED, ItemState.IN_REVIEW):
                        break
                if now - beat >= REVIEW_BEAT_SEC:
                    try:
                        active = self.store.active()
                    except sqlite3.OperationalError:
                        active = None
                    self.board.beat(wid, active=active.id if active else None)
                    beat = now
                self.sleep(REVIEW_POLL_SEC)
        finally:
            waited = int(self.monotonic() - started)
            self.board.clear(wid)
            self.store.clear_priority(item.id)
            self.stats["review_blocked"] += 1
            self.stats["review_wait_sec"] += waited
            self.store.log_event(
                "review_wait_finished",
                {"state": getattr(state, "value", state), "outcome": outcome, "waited": waited},
                item_id=item.id,
                resume_id=item.resume_id,
                vacancy_id=item.vacancy_id,
            )
            print(
                f"▶️ Решение по #{item.id}: {getattr(state, 'value', state)} "
                f"(ждали {waited} с) — обход продолжается",
                flush=True,
            )

    def _llm_failed(self, ex: Exception, cid: str, resume_id: str, brief: dict[str, Any]) -> None:
        """Модель не ответила на оценку. Несколько таких подряд — обход останавливается.

        Без этого обход, у которого модель стала отвечать ошибкой на каждый запрос,
        за минуты пролетал по сотням вакансий: каждая — ~4 секунды, сообщение «проверка
        прервана» в чате, оценка не ставится, квота не заполняется. Дальше каждая вакансия
        падала бы так же, поэтому после LLM_FAIL_LIMIT подряд — стоп с одним понятным сообщением;
        следующий обход по cron повторит попытку с нуля.
        """
        self._llm_fail_streak = getattr(self, "_llm_fail_streak", 0) + 1
        try:
            # итог проверки в чате: «модель не ответила», а не «обход остановился»
            self.checks.add_result(
                cid, {"verdict": "llm_error", "resume_id": resume_id, "score": 0.0, **brief}
            )
        except Exception:  # noqa: BLE001 — подпись в чате не важнее самого обхода
            logger.debug("add_result llm_error", exc_info=True)
        if self._llm_fail_streak >= LLM_FAIL_LIMIT and not self.llm_dead:
            self.llm_dead = True
            self.stop = True
            self.stats["llm_stopped_pass"] += 1
            self.store.log_event(
                "llm_dead",
                {"where": "релевантность", "detail": str(ex)[:300], "streak": self._llm_fail_streak},
            )
            logger.error(
                "Модель не отвечает (%d оценки подряд): %s — обход остановлен, вакансии "
                "проверятся в следующем обходе",
                self._llm_fail_streak, str(ex)[:200],
            )

    def _relevance_gate(
        self,
        resume_id: str,
        queue_key: str,
        resume_text: str,
        vacancy: dict[str, Any],
        full: dict[str, Any],
    ) -> bool | None:
        """True — вакансия подходит; False — ниже порога, в архив; None — оценить не удалось."""
        vacancy_id = vacancy["id"]
        score = self.store.relevance_score(queue_key, vacancy_id, RUBRIC)
        why = "кэш"
        details: dict[str, Any] = {}
        cid = ""
        if score is None:
            if self.llm_dead:
                self.stats["relevance_no_llm"] += 1
                return None
            llm = getattr(self.detector, "llm", None) or make_llm(self.tool, self.store)
            own_text = self._own_texts.get(resume_id) or resume_text
            brief = brief_vacancy(full)
            cid = self.checks.publish(
                {
                    "resume_id": resume_id,
                    "name": brief["name"],
                    "employer": brief["employer"],
                    "url": brief["url"],
                    "stage": "оцениваю релевантность",
                }
            )
            result = None
            keep = False
            try:
                for attempt in (1, 2):
                    try:
                        result = self._score_interruptible(cid, llm, own_text, full)
                        break
                    except ScoreParseError as ex:
                        # модель трижды не дала валидный ответ: сырые ответы — в журнал
                        logger.warning("Оценка релевантности %s не разобрана: %s", vacancy_id, ex)
                        self.stats["relevance_parse_failed"] += 1
                        self.store.log_event(
                            "relevance_parse_failed",
                            {"error": str(ex), "raw": [r[:600] for r in getattr(ex, "raw", [])], **brief},
                            resume_id=queue_key,
                            vacancy_id=vacancy_id,
                        )
                        return None
                    except Exception as ex:  # noqa: BLE001 — не откликаемся без оценки
                        logger.warning("Оценка релевантности %s: %s", vacancy_id, ex)
                        if attempt == 1 and self._wait_for_llm(f"релевантность {vacancy_id}"):
                            continue
                        self.stats["relevance_error"] += 1
                        self._llm_failed(ex, cid, resume_id, brief)
                        return None
                keep = result is not None  # сообщение в боте снимет итог, очистим ниже
                if keep:
                    self._llm_fail_streak = 0
            finally:
                if not keep:
                    self.checks.clear(cid)
            if result is None:
                return None
            score, why, details = result.score, result.why, result.details()
            if result.letter_needed:
                self._letter_flag.add(str(vacancy_id))
            if result.attempts > 1:
                self.stats["relevance_self_corrected"] += 1
            self.store.log_event(
                "relevance_scored",
                {"score": score, "why": why, "threshold": self.relevance_min, **details, **brief},
                resume_id=queue_key,
                vacancy_id=vacancy_id,
            )
        if cid:
            if details.get("manual"):
                outcome = "manual_ok" if score >= self.relevance_min else "manual_no"
            else:
                outcome = "passed" if score >= self.relevance_min else "archived"
            self.checks.add_result(
                cid,
                {
                    "score": score, "why": why, "parts": details.get("parts") or {},
                    "verdict": outcome, "threshold": self.relevance_min,
                    "resume_id": resume_id, **brief_vacancy(full),
                },
            )
            self.checks.clear(cid)
        parts = details.get("parts") or {}
        shown = " ".join(f"{k}={v:g}" for k, v in parts.items())
        verdict = "проходит" if score >= self.relevance_min else "в архив"
        print(
            f"🎯 Релевантность {score:.2f} (порог {self.relevance_min:g}"
            + (f"; {shown}" if shown else "")
            + f") → {verdict}:",
            vacancy.get("alternate_url"),
        )
        if score >= self.relevance_min:
            self.stats["relevance_passed"] += 1
            return True
        self.stats["relevance_archived"] += 1
        self.store.log_event(
            "relevance_skipped",
            {"score": score, "why": why, "threshold": self.relevance_min, **brief_vacancy(full)},
            resume_id=queue_key,
            vacancy_id=vacancy_id,
        )
        return False

    def _score_interruptible(
        self, cid: str, llm: Any, resume_text: str, full: dict[str, Any]
    ) -> Relevance:
        """Оценка моделью; кнопка в боте (апрув/реджект) прерывает ожидание модели."""
        box: dict[str, Any] = {}

        def work() -> None:
            try:
                box["r"] = score_relevance(
                    llm, resume_text, full, preferences=self.relevance_profile
                )
            except Exception as ex:  # noqa: BLE001
                box["e"] = ex

        thread = threading.Thread(target=work, daemon=True)
        thread.start()
        while thread.is_alive():
            thread.join(0.3)
            if verdict := self.checks.decision(cid):
                self.stats[f"manual_{verdict}"] += 1
                if verdict == "ok":
                    return Relevance(1.0, "апрув вручную в боте", manual=True)
                return Relevance(0.0, "реджект вручную в боте", manual=True)
            if self._cancelled():
                raise RuntimeError("обход прерван")
        if "e" in box:
            raise box["e"]
        return box["r"]

    def _queue_full(self) -> bool:
        return bool(self.max_queue) and self.store.count_open() >= int(self.max_queue)

    def _verdict(self, resume_id: str, full: dict[str, Any], test: Any) -> Verdict:
        """Проверка на инъекции; результат кэшируется в журнале (LLM дорогая).

        Модель не ответила → ждём её (до llm_wait) и проверяем ещё раз.
        Так и не ожила → вердикт с ошибкой (suspicious) → вакансия на ревью.
        """
        vacancy_id = int(full["id"])
        cached = [
            e
            for e in self.store.events(type="injection_checked", vacancy_id=vacancy_id)
            if not e["payload"].get("verdict", {}).get("error")
            and e["payload"].get("detector_version") == DETECTOR_VERSION
        ]
        if cached:
            self.stats["verdict_cached"] += 1
            return Verdict.from_dict(cached[-1]["payload"]["verdict"])

        verdict = self.detector.check(full, test)
        if verdict.error and not self.llm_dead:
            if self._wait_for_llm(f"вакансия {vacancy_id}"):
                verdict = self.detector.check(full, test)
                self.stats["llm_retried"] += 1
        if verdict.error:
            self.stats["llm_fail_closed"] += 1

        self.store.log_event(
            "vacancy_new",
            {"vacancy": full, "test": test},
            resume_id=resume_id,
            vacancy_id=vacancy_id,
        )
        self.store.log_event(
            "injection_checked",
            {"verdict": verdict.to_dict(), "detector_version": DETECTOR_VERSION},
            resume_id=resume_id,
            vacancy_id=vacancy_id,
        )
        if verdict.suspicious:
            self.stats["suspicious"] += 1
        return verdict

    def _auto_apply(self, resume_id: str, vacancy: dict[str, Any], full: dict[str, Any]) -> None:
        if blocked := self._auto_apply_blocked():
            self.stats[f"skip_{blocked}"] += 1
            return
        params = {"resume_id": resume_id, "vacancy_id": vacancy["id"], "message": ""}
        ctx = {"resume_id": resume_id, "vacancy_id": vacancy["id"]}
        brief = brief_vacancy(full or vacancy)
        if self.dry_run:
            self.store.log_event("auto_applied", {"dry_run": True, **params, **brief}, **ctx)
            self.stats["auto_applied"] += 1
            print("🧪 [dry-run] Отклик без письма:", vacancy.get("alternate_url"))
            return
        waited = self.pacer.wait_before_apply()
        if self._cancelled():
            return
        try:
            with hh_lock(self.tool.config_path):
                res = self.tool.api_client.post(
                    "/negotiations", params, delay=random.uniform(1, 3)
                )
        except Redirect:
            self.stats["skip_external_form"] += 1
            self.store.log_event("skipped_technical", {"reason": "redirect", **brief}, **ctx)
            return
        except CaptchaRequired as ex:
            self.store.log_event(
                "captcha_required",
                {**brief, "captcha_url": ex.captcha_url, "resume_args": resume_args()},
                **ctx,
            )
            logger.warning("hh требует капчу, обход остановлен: %s", ex.captcha_url)
            print("⛔ hh требует капчу, обход остановлен — картинка придёт в Telegram")
            self.stop = True
            return
        self.store.log_event(
            "auto_applied", {"result": res, **params, **brief, "waited": round(waited)}, **ctx
        )
        self.stats["auto_applied"] += 1
        print("📨 Отклик без письма:", vacancy.get("alternate_url"))


def int_range(value: Any) -> tuple[int, int]:
    """3 / "3" / "2-12" → (min, max) целых."""
    raw = str(value).replace(" ", "")
    lo, _, hi = raw.partition("-")
    try:
        a, b = int(lo), int(hi or lo)
    except ValueError:
        raise ValueError(f"Нужно целое или «MIN-MAX», а не {value!r}") from None
    return (a, b) if a <= b else (b, a)


def _tool_error() -> type[Exception]:
    from hh_applicant_tool.main import Error  # ленивый импорт: main импортирует операции

    return Error


def brief_vacancy(vacancy: dict[str, Any]) -> dict[str, Any]:
    """Короткая подпись вакансии для журнала и уведомлений."""
    return {
        "name": vacancy.get("name"),
        "employer": (vacancy.get("employer") or {}).get("name"),
        "url": vacancy.get("alternate_url") or f"https://hh.ru/vacancy/{vacancy.get('id')}",
    }


def run_review_pass(op: Operation, resumes: list[dict[str, Any]]) -> Counter[str]:
    return ReviewPass(op).run(resumes)
