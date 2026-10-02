"""Таймауты обхода, видимые в Telegram, с кнопкой «пропустить».

Обход (cron / хост) пишет текущее ожидание в config/review-wait.json.
Бот показывает одно сообщение с обратным отсчётом и обновляет его;
кнопка ⏭ пишет config/review-wait-skip — ожидание сразу заканчивается,
следующее ожидание снова можно пропустить.

Виды: vacancy (между откликами), resume (смена резюме), round (между
кругами внутри запуска), next_pass (до следующего запуска cron — пропуск
запускает обход сразу).
"""

from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

WAIT_FILE = "review-wait.json"
SKIP_FILE = "review-wait-skip"
KIND_TITLES = {
    "vacancy": "до следующей вакансии",
    "resume": "до смены резюме",
    "round": "до следующего круга",
    "next_pass": "до следующего обхода",
    "review": "решения по ревью",
}
# «Обход стоит и ждёт человека» (kind=review): срока нет, вместо него пульс —
# пока обход жив, он продлевает until; умер — сообщение в боте уберётся само
HOLD_BEAT_SEC = 90


def _write(path: Path, data: str) -> None:
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    tmp.write_text(data, "utf-8")
    os.replace(tmp, path)


class WaitBoard:
    def __init__(self, config_path: Path | str):
        self.dir = Path(config_path)
        self.wait_path = self.dir / WAIT_FILE
        self.skip_path = self.dir / SKIP_FILE

    # ------------------------------------------------------------ состояние

    def current(self) -> dict[str, Any] | None:
        try:
            data = json.loads(self.wait_path.read_text("utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) and data.get("id") else None

    def publish(self, kind: str, seconds: float, label: str = "", extra: dict | None = None) -> str:
        wid = uuid.uuid4().hex[:10]
        until = datetime.now(timezone.utc) + timedelta(seconds=max(seconds, 0))
        data = {"id": wid, "kind": kind, "label": label, "until": until.isoformat(), **(extra or {})}
        _write(self.wait_path, json.dumps(data, ensure_ascii=False))
        return wid

    def hold(self, kind: str, label: str = "", extra: dict | None = None) -> str:
        """Ожидание без срока (решение пользователя); держать живым через beat()."""
        since = datetime.now(timezone.utc).isoformat()
        return self.publish(kind, HOLD_BEAT_SEC, label, {"hold": True, "since": since, **(extra or {})})

    def beat(self, wid: str, **extra: Any) -> None:
        """Пульс: продлить ожидание и обновить поля (например, какая карточка открыта)."""
        cur = self.current()
        if cur and cur.get("id") == wid:
            until = datetime.now(timezone.utc) + timedelta(seconds=HOLD_BEAT_SEC)
            _write(self.wait_path, json.dumps({**cur, **extra, "until": until.isoformat()}, ensure_ascii=False))

    def clear(self, wid: str | None = None) -> None:
        cur = self.current()
        if cur and (wid is None or cur.get("id") == wid):
            try:
                self.wait_path.unlink()
            except OSError:
                pass

    def request_skip(self, wid: str) -> None:
        _write(self.skip_path, wid)

    def skipped(self, wid: str) -> bool:
        try:
            return self.skip_path.read_text("utf-8").strip() == wid
        except OSError:
            return False

    @staticmethod
    def left(data: dict[str, Any]) -> float:
        try:
            until = datetime.fromisoformat(data["until"])
        except (KeyError, ValueError):
            return 0.0
        return (until - datetime.now(timezone.utc)).total_seconds()

    # ----------------------------------------------------------- ожидание

    def wait(
        self,
        seconds: float,
        kind: str,
        label: str = "",
        cancelled: Callable[[], bool] = lambda: False,
        sleep: Callable[[float], None] = time.sleep,
    ) -> float:
        """Ждать seconds, пока не нажали ⏭ или не отменили. Возвращает ожидание."""
        if seconds <= 0:
            return 0.0
        wid = self.publish(kind, seconds, label)
        start = time.monotonic()
        left = seconds
        try:
            while left > 0 and not cancelled():
                if self.skipped(wid):
                    break
                step = min(left, 1.0)
                sleep(step)
                left -= step
        finally:
            self.clear(wid)
        return time.monotonic() - start


class WaitView:
    """Сообщение бота с обратным отсчётом и кнопкой ⏭ Пропустить."""

    STALE = 300  # next_pass просрочен дольше — cron, видимо, не запущен
    DEAD_GRACE = 6.0  # столько секунд без обхода — и его паузы считаем брошенными

    def __init__(self, board: WaitBoard, chat: Any, chat_id: int | str,
                 on_skip_pass: Callable[[dict], None] | None = None,
                 alive: Callable[[], bool] | None = None):
        self.board = board
        self.chat = chat
        self.chat_id = chat_id
        self.on_skip_pass = on_skip_pass
        # идёт ли обход: паузы и «обход стоит» принадлежат ему; обхода нет — они брошены
        self.alive = alive
        self._dead_since: float | None = None
        # причина, по которой новый обход сейчас запускать нельзя (капча)
        self.blocked: Callable[[], str | None] | None = None
        # журнал нажатий ⏭ (store.log_event), чтобы разбирать темп откликов
        self.on_event: Callable[[str, dict], None] | None = None
        self.msg_id: int | None = None
        self.shown: dict[str, Any] | None = None
        self.skipped_ids: set[str] = set()
        # сообщение с отсчётом переживает перезапуск бота
        self._state = self.board.dir / "review-wait-msg.json"
        try:
            saved = json.loads(self._state.read_text("utf-8"))
            self.msg_id, self.shown = saved["msg_id"], saved["shown"]
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def _save(self) -> None:
        try:
            if self.shown is None:
                self._state.unlink(missing_ok=True)
            else:
                _write(self._state, json.dumps({"msg_id": self.msg_id, "shown": self.shown}))
        except OSError:
            pass

    def active(self) -> bool:
        return self.shown is not None

    def _text(self, data: dict[str, Any], left: float) -> str:
        title = KIND_TITLES.get(data.get("kind"), "ожидание")
        label = data.get("label") or ""
        tail = f"\n<i>{label}</i>" if label else ""
        if data.get("kind") == "review":
            try:
                since = datetime.fromisoformat(data["since"])
                waited = (datetime.now(timezone.utc) - since).total_seconds()
            except (KeyError, ValueError):
                waited = 0.0
            active, mine = data.get("active"), data.get("item_id")
            hint = (
                f"\nСейчас открыта другая карточка #{active}: реши её — следом придёт эта."
                if active and active != mine
                else "\nКарточка с кнопками — в чате."
            )
            return (
                f"⏸ Обход стоит — жду твоего решения по ревью: "
                f"<b>{fmt_left(waited)}</b>{tail}{hint}"
            )
        return f"⏳ Ждём {title}: <b>{fmt_left(left)}</b>{tail}"

    @staticmethod
    def _kb(data: dict[str, Any]) -> dict | None:
        if data.get("kind") == "review":
            return None  # решение — кнопки самой карточки; пропускать здесь нечего
        return {
            "inline_keyboard": [[{"text": "⏭ Пропустить ожидание", "callback_data": f"wait:{data['id']}"}]]
        }

    def _finish(self, text: str) -> None:
        # закончившийся отсчёт удаляем, чтобы не засорять чат
        if self.msg_id is not None:
            delete = getattr(self.chat, "delete_message", None)
            if not (callable(delete) and delete(self.chat_id, self.msg_id)):
                self.chat.edit_message(self.chat_id, self.msg_id, text)
        self.msg_id = None
        self.shown = None
        self._save()

    def _pass_dead(self) -> bool:
        if self.alive is None or self.alive():
            self._dead_since = None
            return False
        now = time.monotonic()
        if self._dead_since is None:
            self._dead_since = now
        return now - self._dead_since >= self.DEAD_GRACE

    def poll(self) -> None:
        data = self.board.current()
        if data:
            # процесс обхода умер, не убрав ожидание
            limit = self.STALE if data.get("kind") == "next_pass" else 60
            if self.board.left(data) < -limit:
                self.board.clear(data["id"])
                data = None
            elif data.get("kind") != "next_pass" and self._pass_dead():
                # паузы и «обход стоит» принадлежат обходу; его нет — не показываем
                # отсчёт, который никто не ведёт (next_pass ждёт cron, обхода тогда и нет)
                self.board.clear(data["id"])
                data = None
        if self.shown and (not data or data["id"] != self.shown["id"]):
            wid = self.shown["id"]
            done = "⏭ Ожидание пропущено" if wid in self.skipped_ids else "✅ Ожидание закончилось"
            self._finish(f"{done} ({KIND_TITLES.get(self.shown.get('kind'), '')})")
        if not data:
            return
        left = self.board.left(data)
        if self.shown is None:
            self.shown = data
            self.msg_id = self.chat.send_message(
                self.chat_id, self._text(data, left), self._kb(data)
            )
            self._save()
        else:
            self.chat.edit_message(
                self.chat_id, self.msg_id, self._text(data, left), self._kb(data)
            )

    def handle_callback(self, data: str) -> str:
        wid = data.removeprefix("wait:")
        cur = self.board.current()
        if not cur or cur.get("id") != wid:
            return "Ожидание уже закончилось"
        if cur.get("kind") == "next_pass" and self.blocked and (why := self.blocked()):
            return why
        self.skipped_ids.add(wid)
        if self.on_event:
            self.on_event("wait_skipped", {"kind": cur.get("kind"), "label": cur.get("label"),
                                           "left": round(self.board.left(cur))})
        if cur.get("kind") == "next_pass":
            self.board.clear(wid)
            if self.on_skip_pass:
                self.on_skip_pass(cur)
        else:
            self.board.request_skip(wid)
        return "Пропускаю ⏭"


def fmt_left(seconds: float) -> str:
    s = max(int(seconds), 0)
    m, s = divmod(s, 60)
    return f"{m}:{s:02d}"


def next_cron_tick(now: datetime | None = None, every_min: int = 15, jitter: int = 75) -> float:
    """Секунд до следующего запуска cron */every_min (+ средний jitter)."""
    now = now or datetime.now()
    minute = (now.minute // every_min + 1) * every_min
    nxt = now.replace(second=0, microsecond=0) + timedelta(minutes=minute - now.minute)
    return (nxt - now).total_seconds() + jitter
