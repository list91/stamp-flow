from __future__ import annotations

import argparse
import logging
import subprocess
import sys
import time
from pathlib import Path
import importlib.util

from stamp_flow.review import botbind
from stamp_flow.review.attention import AttentionNotifier
from stamp_flow.review.botbind import BOT_ID_KEY
from stamp_flow.review.checks import CheckBoard, CheckView
from stamp_flow.review.lock import FileLock, LockTimeout
from stamp_flow.review.timer import next_cron_tick
from stamp_flow.review.timer import WaitBoard, WaitView
from typing import TYPE_CHECKING

from hh_applicant_tool.main import BaseNamespace, BaseOperation
from stamp_flow.review.driver import ReviewBot
from stamp_flow.review.generator import Generator
from stamp_flow.review.models import ItemState, ReviewItem
from stamp_flow.review.captcha import CaptchaFlow, CaptchaPage
from stamp_flow.review.cookies import add_browser_cookies, jar_to_browser_cookies
from stamp_flow.review.notify import Notifier
from stamp_flow.review.services import make_llm, make_store, make_style, new_run_id, review_config
from stamp_flow.review.telegram import TelegramClient, TelegramError
from .review_submit import RESULT_MARK

if TYPE_CHECKING:
    from hh_applicant_tool.main import HHApplicantTool

logger = logging.getLogger(__package__)


class Namespace(BaseNamespace):
    once: bool
    poll_timeout: int


class Operation(BaseOperation):
    """Telegram-бот ревью: показывает карточки по одной, принимает решения и капчу.

    Уведомления «нужно участие» уходят отдельным ботом (review.notify).
    """

    def setup_parser(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--once", action="store_true", help="Один цикл опроса и выход (отладка)")
        parser.add_argument("--poll-timeout", type=int, default=25, help="Long polling, секунд")

    def run(self, tool: HHApplicantTool, args: Namespace) -> int:
        cfg = review_config(tool)
        tg_cfg = cfg.get("telegram") or {}
        chat_id = tg_cfg.get("chat_id")
        if not chat_id:
            raise ValueError("review.telegram.chat_id не задан в config.json")
        client = TelegramClient(tg_cfg.get("bot_token") or "", proxy_url=tg_cfg.get("proxy_url"))
        me: dict = {}
        for attempt in range(1, 6):  # сеть на старте контейнера может проснуться позже бота
            try:
                me = client.get_me()  # неверный токен — падаем сразу, а не молча «ничего не приходит»
                break
            except TelegramError as ex:
                if attempt == 5 or "Unauthorized" in str(ex):
                    raise
                logger.warning("getMe: %s (попытка %d/5)", ex, attempt)
                time.sleep(5)
        main_username = me.get("username") or ""

        store = make_store(tool, new_run_id("bot"))
        generator = Generator(make_llm(tool, store), make_style(tool))

        # Бот уведомлений (отдельный токен): только то, что требует участия. Не обязателен.
        notify_client = None
        notify_me: dict = {}
        notify_chat = chat_id
        notify_cfg = cfg.get("notify") or {}
        if notify_cfg.get("bot_token"):
            try:
                notify_client = TelegramClient(notify_cfg["bot_token"], proxy_url=notify_cfg.get("proxy_url"))
                notify_me = notify_client.get_me()
                notify_chat = notify_cfg.get("chat_id") or chat_id
                if str(notify_me.get("id")) == str(me.get("id")):
                    logger.warning("review.notify совпадает с review.telegram — отдельный бот уведомлений отключён")
                    notify_client = None
            except TelegramError as ex:
                logger.error("Бот уведомлений недоступен (%s): пинги отключены", ex)
                notify_client = None

        # Токен сменили (перенос на другого бота) — счётчики и id сообщений старого бота сбросить,
        # а оставленное в его чате убрать, если у нас есть его токен (он мог стать ботом уведомлений)
        def clean_old_chat(ids: list[int]) -> None:
            saved = tool.storage.settings.get_value(BOT_ID_KEY)
            if notify_client is not None and str(saved) == str(notify_me.get("id")):
                for mid in ids:
                    notify_client.delete_message(notify_chat, mid)

        switched = botbind.bind(tool.storage.settings, tool.config_path, me["id"], clean_old_chat)
        if switched is not None:
            store.log_event("bot_switched", {**switched, "bot": main_username})
            print(f"🔀 Основной бот теперь @{main_username}: состояние старого бота сброшено ({switched})")
        client.set_commands([
            ("current", "повторить текущую карточку"),
            ("queue", "что открыто и ждёт отправки"),
        ])

        # порядок как в hh (очередь резюме), только опубликованные
        titles = {}
        try:
            titles = {
                r["id"]: r.get("title") or r["id"]
                for r in tool.get_resumes()
                if (r.get("status") or {}).get("id") == "published"
            }
        except Exception:  # noqa: BLE001 — только подписи
            try:
                titles = {r.id: r.title for r in tool.storage.resumes.find()}
            except Exception:  # noqa: BLE001
                pass

        def submit(item: ReviewItem) -> str:
            cmd = [
                sys.executable,
                "-m",
                "stamp_flow",
                "--config-dir",
                str(tool.config_path),
                "--profile",
                ".",
                "review-submit",
                str(item.id),
            ]
            proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900)
            for line in reversed(proc.stdout.splitlines()):
                if line.startswith(RESULT_MARK):
                    return line[len(RESULT_MARK):].strip()
            logger.error(
                "review-submit #%s упал (код %s):\n%s", item.id, proc.returncode, proc.stderr[-3000:]
            )
            store.log_event(
                "error",
                {"where": "review-submit", "code": proc.returncode, "stderr": proc.stderr[-1000:]},
                item_id=item.id,
            )
            return f"ошибка review-submit (код {proc.returncode})"

        def reload_cookies() -> None:
            # свежие куки с диска (auth -m / обходы могли обновить файл)
            try:
                tool.session.cookies.clear()
                tool.session.cookies.load(ignore_discard=True, ignore_expires=True)
            except (OSError, ValueError) as ex:
                logger.warning("Не удалось перечитать cookies: %s", ex)

        def open_captcha(url: str) -> CaptchaPage:
            reload_cookies()
            return CaptchaPage(url, jar_to_browser_cookies(tool.session.cookies))

        def save_cookies(cookies: list[dict]) -> None:
            # куки ЗАЛОГИНЕННОЙ страницы капчи → сессия утилиты → cookies.txt
            reload_cookies()
            added = add_browser_cookies(tool.session.cookies, cookies)
            tool.save_cookies()
            store.log_event("cookies_saved", {"added": added, "total": len(tool.session.cookies)})

        run_py = Path(tool.config_path).resolve().parent / "run.py"

        def start_pass(args: list | None, reason: str, log_name: str) -> None:
            """Обход фоном. Есть run.py — строго по conf.json (перечитывается сейчас)."""
            if run_py.exists():
                cmd = [sys.executable, str(run_py), "pass"]
            else:
                cmd = [
                    sys.executable, "-m", "stamp_flow",
                    "--config-dir", str(tool.config_path), "--profile", ".",
                    "apply-review", *(args or []),
                ]
            log_path = tool.config_path / "review-log" / log_name
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(log_path, "a", encoding="utf-8") as out:
                subprocess.Popen(cmd, stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
            store.log_event(
                "pass_started_by_bot",
                {"cmd": cmd[1:], "reason": reason, "conf": run_py.exists()},
            )

        def skip_to_next_pass(wait: dict) -> None:
            start_pass(wait.get("args"), "wait_skipped", "pass-from-bot.log")

        def resume_after_captcha(event: dict) -> bool:
            """Капча решена → обход заново по conf.json (фоном) или досылка одобренного."""
            args = event.get("resume_args")
            if args is None:
                # капча была при отправке из бота — одобренное дошлёт review-submit
                for item in store.by_state(ItemState.FAILED_RETRYABLE):
                    submit(item)
                return False
            start_pass(args, "captcha_solved", "resume-after-captcha.log")
            store.log_event("pass_resumed", {"conf": run_py.exists()})
            return True

        from stamp_flow.review import render as _render
        from stamp_flow.review.lock import pass_running

        _render.set_progress_dir(tool.config_path)
        # обход мог жить в другом контейнере (cron) или быть убит: смотрим его lock
        alive = lambda: pass_running(tool.config_path)  # noqa: E731
        attention = None
        if notify_client is not None:
            attention = AttentionNotifier(
                store, notify_client, notify_chat, tool.storage.settings, titles, WaitBoard(tool.config_path),
                open_url=f"https://t.me/{main_username}" if main_username else None,
                open_name=f"@{main_username}" if main_username else None,
                bot_id=notify_me.get("id"),
            )
        bot = ReviewBot(
            store=store,
            chat=client,
            chat_id=chat_id,
            generator=generator,
            submit=submit,
            settings=tool.storage.settings,
            resume_titles=titles,
            notifier=Notifier(store, client, chat_id, tool.storage.settings, titles),
            captcha=CaptchaFlow(
                store, client, chat_id, tool.storage.settings,
                on_cookies=save_cookies, on_solved=resume_after_captcha,
                page_factory=open_captcha,
            ),
            waits=WaitView(WaitBoard(tool.config_path), client, chat_id, skip_to_next_pass, alive=alive),
            checks=CheckView(CheckBoard(tool.config_path), client, chat_id, titles, alive=alive),
            attention=attention,
        )
        bot.fresh_chat = switched is not None
        bot.checks.on_event = lambda type_, payload: store.log_event(type_, payload)
        bot.waits.blocked = lambda: (
            "Сначала реши капчу — иначе обход сразу упрётся в неё" if bot.captcha.blocking else None
        )
        bot.waits.on_event = lambda type_, payload: store.log_event(type_, payload)
        store.log_event("bot_started", {})
        print("🤖 review-bot запущен")


        def idle_countdown() -> None:
            """Обход не идёт и отсчёта нет (например, после старта контейнеров) — показать
            «до следующего обхода», чтобы тишина не выглядела как поломка."""
            try:
                if bot.captcha.blocking or bot.waits.board.current():
                    return
                if run_py.exists():
                    spec = importlib.util.spec_from_file_location("hh_run_conf", run_py)
                    mod = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(mod)
                    conf = mod.load_conf()
                    if not conf["loop"]:
                        return
                    every = int(conf["cron_every_min"])
                else:
                    every = 15
                lock = FileLock(Path(tool.config_path) / ".hh-pass.lock", timeout=0)
                try:
                    lock.acquire()
                except LockTimeout:
                    return  # обход идёт
                lock.release()
                bot.waits.board.publish(
                    "next_pass", next_cron_tick(every_min=every), "следующий обход по cron", {"args": []}
                )
            except Exception:  # noqa: BLE001 — отсчёт не должен ронять бота
                logger.debug("idle_countdown", exc_info=True)

        backoff = 1.0
        while True:
            if getattr(args, "_cancel_event", None) and args._cancel_event.is_set():
                break
            try:
                idle_countdown()
                bot.poll_once(client, timeout=args.poll_timeout)
                backoff = 1.0
            except TelegramError as ex:
                logger.warning("Telegram: %s", ex)
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
            if args.once:
                break
        store.log_event("bot_stopped", {})
        return 0
