"""Проверка вакансии, видимая в Telegram: спиннер + кнопки «Апрув» / «Реджект».

Обход пишет текущую проверку в config/review-check.json. Бот показывает одно
сообщение со спиннером и временем, обновляет его; кнопки пишут решение в
config/review-check-decision («<id>:ok» или «<id>:no») — обход подхватывает
его, не дожидаясь модели: ok — вакансия подходит, no — в архив.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from . import render

CHECK_FILE = "review-check.json"
DECISION_FILE = "review-check-decision"
RESULTS_FILE = "review-check-results.jsonl"
KEEP_RESULTS = 200
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
STALE = 900  # проверка старше 15 мин — процесс обхода, видимо, умер
DEAD_GRACE = 6.0  # столько секунд без обхода — и проверку считаем брошенной


def _write(path: Path, data: str) -> None:
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    tmp.write_text(data, "utf-8")
    os.replace(tmp, path)


class CheckBoard:
    def __init__(self, config_path: Path | str):
        self.dir = Path(config_path)
        self.path = self.dir / CHECK_FILE
        self.decision_path = self.dir / DECISION_FILE
        self.results_path = self.dir / RESULTS_FILE

    def current(self) -> dict[str, Any] | None:
        try:
            data = json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) and data.get("id") else None

    def publish(self, info: dict[str, Any]) -> str:
        cid = uuid.uuid4().hex[:10]
        _write(self.path, json.dumps({"id": cid, "started": time.time(), **info}, ensure_ascii=False))
        return cid

    def clear(self, cid: str | None = None) -> None:
        cur = self.current()
        if cur and (cid is None or cur.get("id") == cid):
            try:
                self.path.unlink()
            except OSError:
                pass

    def add_result(self, cid: str, result: dict[str, Any]) -> None:
        """Итог проверки (оценка, критерии, решение) — бот оставит его в чате."""
        try:
            lines = self.results_path.read_text("utf-8").splitlines()
        except OSError:
            lines = []
        lines = lines[-(KEEP_RESULTS - 1):] + [json.dumps({"id": cid, **result}, ensure_ascii=False)]
        _write(self.results_path, "\n".join(lines) + "\n")

    def result(self, cid: str) -> dict[str, Any] | None:
        try:
            lines = self.results_path.read_text("utf-8").splitlines()
        except OSError:
            return None
        for line in reversed(lines[-KEEP_RESULTS:]):
            try:
                data = json.loads(line)
            except ValueError:
                continue
            if data.get("id") == cid:
                return data
        return None

    def decide(self, cid: str, verdict: str) -> None:
        _write(self.decision_path, f"{cid}:{verdict}")

    def decision(self, cid: str) -> str | None:
        try:
            raw = self.decision_path.read_text("utf-8").strip()
        except OSError:
            return None
        head, _, verdict = raw.partition(":")
        return verdict if head == cid and verdict in ("ok", "no") else None


class CheckView:
    """Сообщение бота: «🔎 Проверяю вакансию ⠹ 12 с» + кнопки апрув / реджект."""

    DEAD_GRACE = DEAD_GRACE

    def __init__(
        self,
        board: CheckBoard,
        chat: Any,
        chat_id: int | str,
        titles: dict[str, str],
        alive: Callable[[], bool] | None = None,
    ):
        self.board = board
        self.chat = chat
        self.chat_id = chat_id
        self.titles = titles
        # идёт ли обход, который ведёт проверку; нет — спиннер не должен тикать вечно
        self.alive = alive
        self._dead_since: float | None = None
        self.msg_id: int | None = None
        self.shown_id: str | None = None
        self.decided: dict[str, str] = {}
        self.last: dict[str, Any] | None = None
        self.on_event: Callable[[str, dict], None] | None = None
        self._state = board.dir / "review-check-msg.json"
        try:
            saved = json.loads(self._state.read_text("utf-8"))
            self.msg_id, self.shown_id = saved["msg_id"], saved["shown_id"]
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def _save(self) -> None:
        try:
            if self.shown_id is None:
                self._state.unlink(missing_ok=True)
            else:
                _write(self._state, json.dumps({"msg_id": self.msg_id, "shown_id": self.shown_id}))
        except OSError:
            pass

    def active(self) -> bool:
        return self.shown_id is not None

    def _text(self, data: dict[str, Any], final: bool = False) -> str:
        elapsed = max(int(time.time() - float(data.get("started") or time.time())), 0)
        frame = SPINNER[int(time.time() * 2) % len(SPINNER)]
        tag = render.resume_tag(data.get("resume_id"), self.titles)
        name = render.esc(data.get("name"))
        employer = render.esc(data.get("employer"))
        head = f"<b>{tag}</b>\n" if tag else ""
        line = f"<a href=\"{render.esc(data.get('url'))}\">{name}</a>" if data.get("url") else name
        stage = render.esc(data.get("stage") or "оцениваю релевантность")
        if final:
            return f"{head}✅ Проверка завершена\n{line}"
        return (
            f"{head}🔎 Проверяю вакансию {frame} <b>{elapsed} с</b>\n"
            f"{line}" + (f" — {employer}" if employer else "") + f"\n<i>{stage}</i>"
        )

    @staticmethod
    def _kb(cid: str) -> dict:
        return {
            "inline_keyboard": [[
                {"text": "✅ Апрув (подходит)", "callback_data": f"chk:{cid}:ok"},
                {"text": "❌ Реджект (в архив)", "callback_data": f"chk:{cid}:no"},
            ]]
        }

    def _final_text(self, data: dict[str, Any] | None, result: dict[str, Any] | None) -> str:
        """Итоговое сообщение: остаётся в чате с оценкой и решением, без кнопок."""
        if not result:
            # итога нет: обход убили/он упал/модель не ответила — не выдаём это за успех
            if not data:
                return "⚠️ Проверка прервана"
            tag = render.resume_tag(data.get("resume_id"), self.titles)
            head = f"<b>{tag}</b>\n" if tag else ""
            name = render.esc(data.get("name"))
            line = f"<a href=\"{render.esc(data.get('url'))}\">{name}</a>" if data.get("url") else name
            return f"{head}⚠️ Проверка прервана — обход остановился, вакансия проверится в следующем\n{line}"
        tag = render.resume_tag(result.get("resume_id"), self.titles)
        name = render.esc(result.get("name"))
        line = f"<a href=\"{render.esc(result.get('url'))}\">{name}</a>" if result.get("url") else name
        employer = render.esc(result.get("employer"))
        if result.get("verdict") == "llm_error":
            # оценки нет, но и «обход остановился» неверно: обход жив, не ответила модель
            head = f"<b>{tag}</b>\n" if tag else ""
            who = line + (f" — {employer}" if employer else "")
            return f"{head}⚠️ Модель не ответила — вакансия проверится в следующем обходе\n{who}"
        head = f"<b>{tag} · 🎯 {float(result['score']):.2f}</b>\n" if tag else f"<b>🎯 {float(result['score']):.2f}</b>\n"
        verdicts = {
            "passed": "✅ Подходит",
            "archived": "🗄 В архив",
            "manual_ok": "✅ Апрув вручную",
            "manual_no": "❌ Реджект вручную",
        }
        title = verdicts.get(result.get("verdict"), "🔎 Проверено")
        parts = result.get("parts") or {}
        crit = " · ".join(f"{k} {v:g}" for k, v in parts.items())
        why = render.esc(str(result.get("why") or "")[:220])
        threshold = result.get("threshold")
        out = f"{head}{title} (порог {threshold:g})\n" if threshold is not None else f"{head}{title}\n"
        out += line + (f" — {employer}" if employer else "")
        if crit:
            out += f"\n<i>{crit}</i>"
        if why:
            out += f"\n{why}"
        return out

    def _finish(self) -> None:
        if self.msg_id is not None:
            cid = self.shown_id or ""
            result = self.board.result(cid)
            # сообщение не удаляем: итог и оценка остаются в чате
            self.chat.edit_message(self.chat_id, self.msg_id, self._final_text(self.last, result))
        self.last = None
        self.msg_id = None
        self.shown_id = None
        self._save()

    def _pass_dead(self) -> bool:
        """Обхода нет уже несколько секунд (не мигнувшая проверка на стыке запусков)."""
        if self.alive is None or self.alive():
            self._dead_since = None
            return False
        now = time.time()
        if self._dead_since is None:
            self._dead_since = now
        return now - self._dead_since >= self.DEAD_GRACE

    def poll(self) -> None:
        data = self.board.current()
        if data and time.time() - float(data.get("started") or 0) > STALE:
            self.board.clear(data["id"])
            data = None
        elif data and self._pass_dead():
            # обход убили или он упал, не убрав проверку
            self.board.clear(data["id"])
            data = None
        if self.shown_id and (not data or data["id"] != self.shown_id):
            self._finish()
        if not data:
            return
        if self.shown_id and data and data["id"] == self.shown_id:
            self.last = data
        if self.shown_id is None:
            self.shown_id = data["id"]
            self.last = data
            self.msg_id = self.chat.send_message(self.chat_id, self._text(data), self._kb(data["id"]))
            self._save()
        else:
            self.chat.edit_message(self.chat_id, self.msg_id, self._text(data), self._kb(data["id"]))

    def handle_callback(self, data: str) -> str:
        _, _, rest = data.partition(":")
        cid, _, verdict = rest.partition(":")
        cur = self.board.current()
        if not cur or cur.get("id") != cid or verdict not in ("ok", "no"):
            return "Проверка уже закончилась"
        self.board.decide(cid, verdict)
        if self.on_event:
            self.on_event("check_decided", {"verdict": verdict, "name": cur.get("name"),
                                            "url": cur.get("url")})
        return "Апрув ✅" if verdict == "ok" else "Реджект ❌"
