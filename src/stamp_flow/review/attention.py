"""Бот уведомлений: только то, что требует участия человека.

Ничего не принимает и ни на что не отвечает — работа (карточки, капча, лента) идёт в
основном боте, сюда приходит короткий «пинг» со звуком и кнопкой «Открыть бота».

Не по событиям, а по состоянию: каждый цикл сверяем, что сейчас ждёт человека, и держим
по одному сообщению на каждое такое «требует участия»:

  📝 ревью        — пока карточка не решена (обход при этом стоит);
  🧩 капча        — пока не решена;
  ⏳ пауза до следующего обхода — самая долгая из пауз (тихо, исчезает вместе с паузой).

Решённое переписывается в итог («✅ Ревью решено …»), так что по чату видно, что ждёт
прямо сейчас. Забытое ревью напоминает о себе новым сообщением раз в 20 минут.
Состояние сообщений хранится в БД: перезапуск бота ничего не дублирует.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from .models import ItemState
from .render import esc, resume_tag

logger = logging.getLogger(__package__)

STATE_KEY = "review.attn.state"
REMIND_EVERY_SEC = 20 * 60
DONE_TTL_SEC = 24 * 3600  # решённые строки живут сутки, потом чат чистится
EDIT_MIN_SEC = 60.0  # текст пинга обновляем не чаще раза в минуту
RETRY_AFTER_ERROR_SEC = 30.0

STEP_WORDS = {"letter": "письмо", "questionnaire": "анкета"}
ITEM_DONE_WORDS = {
    ItemState.APPROVED: "✅ Ревью решено: апрув",
    ItemState.SUBMITTING: "✅ Ревью решено: апрув",
    ItemState.SUBMITTED: "✅ Ревью решено: отправлено в hh",
    ItemState.FAILED_RETRYABLE: "✅ Ревью решено: апрув (отправка повторится)",
    ItemState.REJECTED: "❌ Ревью решено: реджект",
    ItemState.CANCELLED: "🧹 Ревью снято без решения",
    ItemState.STALE: "🗄 Вакансия устарела",
}
CAPTCHA_DONE_WORDS = {
    "captcha_solved": "✅ Капча решена",
    "captcha_skipped": "⏸ Капча отложена",
    "captcha_error": "⚠️ Капча: ошибка ввода",
    "captcha_timeout": "⌛ Капча не решена за 3 часа",
}


@dataclass
class Want:
    """Что должно висеть в чате уведомлений прямо сейчас."""

    text: str
    title: str  # короткая строка для итога («вакансия — компания»)
    keyboard: dict[str, Any] | None = None
    silent: bool = False
    remind: str | None = None  # текст напоминания; None — не напоминать


def _minutes(seconds: float) -> int:
    return max(int(seconds // 60), 0)


class AttentionNotifier:
    def __init__(
        self,
        store: Any,
        chat: Any,
        chat_id: int | str,
        settings: Any | None,
        titles: dict[str, str] | None,
        board: Any,
        *,
        open_url: str | None = None,
        open_name: str | None = None,
        bot_id: int | str | None = None,
        clock: Callable[[], float] = time.time,
        remind_every: float = REMIND_EVERY_SEC,
    ):
        self.store = store
        self.chat = chat
        self.chat_id = chat_id
        self.settings = settings
        self.titles = titles or {}
        self.board = board  # WaitBoard: пауза до обхода и «обход стоит на ревью»
        self.open_url = open_url
        self.open_name = open_name
        self.bot_id = str(bot_id) if bot_id is not None else "default"
        self.clock = clock
        self.remind_every = remind_every
        self._mem: dict[str, Any] | None = None
        self._retry_at = 0.0

    # ------------------------------------------------------------ состояние

    def _load(self) -> dict[str, Any]:
        raw = self._mem if self.settings is None else self.settings.get_value(STATE_KEY)
        state = raw if isinstance(raw, dict) else {}
        # сообщения принадлежат конкретному боту: сменили бота — старые id недействительны
        if state.get("bot") != self.bot_id:
            state = {"bot": self.bot_id, "hello": False}
        state.setdefault("live", {})
        state.setdefault("done", [])
        return state

    def _save(self, state: dict[str, Any]) -> None:
        if self.settings is None:
            self._mem = state
        else:
            self.settings.set_value(STATE_KEY, state)

    # ------------------------------------------------------------------ цикл

    def run(self) -> None:
        """Сверить, что ждёт человека, с тем, что уже показано. Никогда не бросает."""
        now = self.clock()
        if now < self._retry_at:
            return
        try:
            self._sync(now)
        except Exception as ex:  # noqa: BLE001 — уведомления не должны ронять бота
            self._retry_at = now + RETRY_AFTER_ERROR_SEC
            logger.warning("Бот уведомлений: %s (повтор через %d с)", ex, RETRY_AFTER_ERROR_SEC)
            logger.debug("attention", exc_info=True)

    def _sync(self, now: float) -> None:
        state = self._load()
        self._hello(state)
        wants = self._wants(now)
        live: dict[str, dict[str, Any]] = state["live"]

        for key in [k for k in live if k not in wants]:
            entry = live.pop(key)
            self._resolve(key, entry, state, now)
            self._save(state)

        for key, want in wants.items():
            entry = live.get(key)
            if entry is None:
                mid = self.chat.send_message(self.chat_id, want.text, want.keyboard, silent=want.silent)
                live[key] = {"id": mid, "sent": now, "edited": now, "text": want.text, "title": want.title}
                self._save(state)
                continue
            if want.text != entry.get("text") and now - entry.get("edited", 0) >= EDIT_MIN_SEC:
                self.chat.edit_message(self.chat_id, entry["id"], want.text, want.keyboard)
                entry.update(text=want.text, edited=now, title=want.title)
                self._save(state)
            if want.remind:
                last = max(entry.get("sent", 0), (entry.get("remind") or {}).get("ts", 0))
                if now - last >= self.remind_every:
                    self._remind(entry, want, now)
                    self._save(state)

        cutoff = now - DONE_TTL_SEC
        old = [d for d in state["done"] if d.get("ts", 0) < cutoff]
        if old:
            for d in old:
                self._delete(d["id"])
            state["done"] = [d for d in state["done"] if d.get("ts", 0) >= cutoff]
            self._save(state)

    def _hello(self, state: dict[str, Any]) -> None:
        if state.get("hello"):
            return
        where = f"«{self.open_name}»" if self.open_name else "основном боте"
        text = (
            "🔔 <b>Бот уведомлений подключён</b>\n"
            "Сюда приходит только то, что требует тебя: ревью, капча и долгая пауза "
            f"до следующего обхода. Работа идёт в {where}."
        )
        self.chat.send_message(self.chat_id, text, self._keyboard(), silent=True)
        state["hello"] = True
        self._save(state)

    def _keyboard(self) -> dict[str, Any] | None:
        if not self.open_url:
            return None
        return {"inline_keyboard": [[{"text": "▶️ Открыть бота", "url": self.open_url}]]}

    def _delete(self, message_id: int | None) -> None:
        delete = getattr(self.chat, "delete_message", None)
        if message_id is not None and callable(delete):
            try:
                delete(self.chat_id, message_id)
            except Exception:  # noqa: BLE001
                logger.debug("delete notify message %s", message_id, exc_info=True)

    # ---------------------------------------------------- что ждёт человека

    def _wants(self, now: float) -> dict[str, Want]:
        wants: dict[str, Want] = {}
        for item in self.store.by_state(ItemState.QUEUED, ItemState.IN_REVIEW):
            if item.dry_run:
                continue  # демо-карточки (review-demo) — не работа
            wants[f"review:{item.id}"] = self._review_want(item, now)
        event = self.store.captcha_unresolved()
        if event:
            wants[f"captcha:{event['id']}"] = self._captcha_want(event, now)
        wait = self.board.current()
        if wait and wait.get("kind") == "next_pass" and self._left(wait, now) > -60:
            wants[f"wait:{wait['id']}"] = self._wait_want(wait, now)
        return wants

    @staticmethod
    def _left(wait: dict[str, Any], now: float) -> float:
        try:
            return datetime.fromisoformat(wait["until"]).timestamp() - now
        except (KeyError, ValueError):
            return 0.0

    def _review_want(self, item: Any, now: float) -> Want:
        v = item.vacancy_json or {}
        name = esc(v.get("name") or f"вакансия {item.vacancy_id}")
        employer = (v.get("employer") or {}).get("name")
        url = v.get("alternate_url") or f"https://hh.ru/vacancy/{item.vacancy_id}"
        title = f'<a href="{esc(url)}">{name}</a>' + (f" — {esc(employer)}" if employer else "")
        steps = " + ".join(STEP_WORDS.get(s, s) for s in item.steps) or "ревью"
        head = resume_tag(item.resume_id, self.titles)
        try:
            score = self.store.relevance_score(item.resume_id, item.vacancy_id)
        except Exception:  # noqa: BLE001
            score = None
        if head and score is not None:
            head += f" · 🎯 {score:.2f}"
        wait = self.board.current()
        blocked = bool(wait and wait.get("kind") == "review" and wait.get("item_id") == item.id)
        if blocked:
            try:
                since = datetime.fromisoformat(wait["since"]).timestamp()
            except (KeyError, ValueError):
                since = now
            try:
                # «since» обнуляется при каждом новом запуске обхода; карточка же висит с момента создания
                created = datetime.fromisoformat(str(item.created_at)).replace(tzinfo=timezone.utc).timestamp()
                since = min(since, created)
            except (TypeError, ValueError):
                pass
            mins = _minutes(now - since)
            status = f"⏸ Обход стоит и ждёт тебя{f' уже {mins} мин' if mins else ''}"
        elif item.state is ItemState.IN_REVIEW:
            status = "Карточка открыта в основном боте — жду твоего решения"
        else:
            status = "Ждёт показа в основном боте"
        text = f"📝 <b>Нужно ревью</b> · {steps}\n{title}\n" + (f"{head}\n" if head else "") + status
        remind = f"⏰ <b>Всё ещё жду решения</b>\n{title}" if blocked else None
        return Want(text=text, title=title, keyboard=self._keyboard(), remind=remind)

    def _captcha_want(self, event: dict[str, Any], now: float) -> Want:
        p = event.get("payload") or {}
        where = f"\nНа вакансии: {esc(p['name'])}" if p.get("name") else ""
        text = (
            "🧩 <b>hh просит капчу</b> — отклики на паузе"
            f"{where}\n"
            "Открой бота и нажми «Показать капчу»: картинка живёт около минуты, отвечай сразу."
        )
        return Want(
            text=text,
            title="капча hh",
            keyboard=self._keyboard(),
            remind="⏰ <b>Капча всё ещё не решена</b> — отклики стоят",
        )

    def _wait_want(self, wait: dict[str, Any], now: float) -> Want:
        try:
            end = datetime.fromisoformat(wait["until"]).astimezone().strftime("%H:%M")
        except (KeyError, ValueError):
            end = "?"
        text = f"⏳ Пауза до следующего обхода — до {end}"
        return Want(text=text, title="пауза до следующего обхода", silent=True)

    # ------------------------------------------------------------ итоги

    def _remind(self, entry: dict[str, Any], want: Want, now: float) -> None:
        prev = entry.get("remind")
        if prev:
            self._delete(prev["id"])
        mid = self.chat.send_message(self.chat_id, want.remind, self._keyboard())
        entry["remind"] = {"id": mid, "ts": now}

    def _resolve(self, key: str, entry: dict[str, Any], state: dict[str, Any], now: float) -> None:
        kind, _, ident = key.partition(":")
        if entry.get("remind"):
            self._delete(entry["remind"]["id"])
        if kind == "wait":
            self._delete(entry["id"])  # пауза кончилась — уведомление больше не нужно
            return
        title = entry.get("title") or ""
        if kind == "review":
            item = self.store.get(int(ident))
            word = ITEM_DONE_WORDS.get(item.state, "ℹ️ Ревью закрыто") if item else "ℹ️ Ревью закрыто"
        else:
            outcome = self.store.captcha_outcome(int(ident))
            word = CAPTCHA_DONE_WORDS.get(outcome or "", "⌛ Капча закрыта")
        text = f"{word}\n{title}" if title and kind == "review" else word
        stamp = datetime.now().strftime("%H:%M")
        self.chat.edit_message(self.chat_id, entry["id"], f"{text}\n<i>{stamp}</i>")
        state["done"].append({"id": entry["id"], "ts": now})
