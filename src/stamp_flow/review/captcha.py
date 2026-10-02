"""Капча hh через Telegram: кнопка → картинка с отсчётом → ответ текстом → ввод на странице.

Ссылку на капчу нельзя просто открыть на телефоне: hh.ru-ссылки
перехватывает приложение hh, и страницы с капчей там нет. Поэтому бот сам
открывает страницу в headless-Chromium, шлёт картинку в чат, а ответ
пользователя вводит в ту же страницу. После успеха куки страницы
переносятся в сессию утилиты и сохраняются в cookies.txt — как в штатном
_solve_captcha_async.

Ключ капчи у hh живёт около минуты, поэтому картинка создаётся по кнопке
и висит с обратным отсчётом (см. CaptchaFlow).
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable

logger = logging.getLogger(__package__)

SEL_IMAGE = 'img[data-qa="account-captcha-picture"]'
SEL_INPUT = 'input[data-qa="account-captcha-input"]'
# Сколько секунд после создания ключа ответ ещё принимается. Замерено: ответы через
# ≤48 с проходили, через ≥84 с — нет; картинка по ключу отдаётся до ~90 с, к 120 с — 404.
PICTURE_LIFETIME = 55.0
COUNTDOWN_STEP = 5.0
# Нерешённую капчу закрываем через 3 часа (браузер и кнопку не держим бесконечно);
# поздний текст не теряется: бот сразу покажет свежую картинку.
TIMEOUT_SECONDS = 3 * 60 * 60

CAPTION = "🧩 <b>hh просит капчу</b> — отклики на паузе.\nОтветь сообщением с текстом с картинки."
PROMPT = (
    "🧩 <b>hh просит капчу</b> — отклики на паузе.\n"
    "Картинка живёт около минуты, поэтому покажу её, когда будешь готов: "
    "нажми и сразу отвечай текстом."
)

class CaptchaPage:
    """Открытая страница капчи (sync Playwright, держится между сообщениями)."""

    def __init__(self, url: str, cookies: list[dict[str, Any]] | None = None):
        from playwright.sync_api import sync_playwright

        self.url = url
        self._pw = sync_playwright().start()
        try:
            self._browser = self._pw.chromium.launch(headless=True)
            self._context = self._browser.new_context()
            if cookies:
                # капча решается в ЗАЛОГИНЕННОЙ сессии: иначе после решения
                # анонимные куки браузера затёрли бы веб-сессию hh
                self._context.add_cookies(cookies)
            self.page = self._context.new_page()
            self.page.goto(url, timeout=30000)
        except Exception:
            self.close()
            raise

    def image(self) -> bytes:
        element = self.page.wait_for_selector(SEL_IMAGE, timeout=15000, state="visible")
        return element.screenshot()

    def reload(self) -> bytes:
        self.page.goto(self.url, timeout=30000)
        return self.image()

    def submit(self, text: str, timeout: float = 20.0) -> bool:
        """True — капча принята.

        Успех hh показывает редиректом со страницы /account/captcha. Во время
        редиректа DOM пересоздаётся («Execution context was destroyed»), поэтому
        проверяем в цикле по URL и переживаем навигацию.
        """
        self.page.fill(SEL_INPUT, text.strip())
        self.page.press(SEL_INPUT, "Enter")
        deadline = time.monotonic() + timeout
        stable_on_captcha = 0
        while time.monotonic() < deadline:
            try:
                self.page.wait_for_load_state("domcontentloaded", timeout=5000)
                if "/account/captcha" not in self.page.url:
                    return True
                image = self.page.query_selector(SEL_IMAGE)
                if image is not None and image.is_visible():
                    stable_on_captcha += 1
                    # 3 проверки подряд на странице капчи — ответ не принят
                    if stable_on_captcha >= 3:
                        return False
                else:
                    stable_on_captcha = 0
            except Exception as ex:  # noqa: BLE001 — идёт навигация
                if "context was destroyed" not in str(ex) and "navigat" not in str(ex).lower():
                    raise
                stable_on_captcha = 0
            self.page.wait_for_timeout(700)
        return "/account/captcha" not in self.page.url

    def cookies(self) -> list[dict[str, Any]]:
        return self._context.cookies()

    def close(self) -> None:
        for obj in ("_browser", "_pw"):
            target = getattr(self, obj, None)
            try:
                if target is not None:
                    target.close() if obj == "_browser" else target.stop()
            except Exception:  # noqa: BLE001
                pass


class CaptchaFlow:
    """Встраивается в ReviewBot: ловит события captcha_required и ведёт диалог.

    Ключ капчи у hh живёт около минуты (картинка по ключу отдаётся до ~90 с, ответы,
    принятые позже ~50 с, в журнале — все «неверные»). Раньше бот присылал картинку
    сразу и держал страницу открытой до ответа: пока человек добирался до телефона,
    ключ протухал, и первый ответ ВСЕГДА оказывался «неверным». Теперь:

      * на событие капчи бот шлёт не картинку, а сообщение с кнопкой «Показать капчу»;
      * картинка создаётся по нажатию (свежий ключ) и висит с обратным отсчётом;
      * протухла — картинка убирается, снова кнопка; ответ на протухшую в hh не уходит
        (иначе он сгорел бы как «неверный»), вместо этого сразу свежая картинка;
      * в чате живёт одно сообщение капчи: кнопка ИЛИ картинка.
    """

    LAST_KEY = "review.captcha.last_id"
    MSGS_KEY = "review.captcha.msgs"

    def __init__(
        self,
        store: Any,
        chat: Any,
        chat_id: int | str,
        settings: Any | None,
        on_cookies: Callable[[list[dict[str, Any]]], None],
        page_factory: Callable[[str], Any] = CaptchaPage,
        clock: Callable[[], float] = time.time,
        on_solved: Callable[[dict[str, Any]], None] | None = None,
    ):
        self.store = store
        self.chat = chat
        self.chat_id = chat_id
        self.settings = settings
        self.on_cookies = on_cookies
        self.page_factory = page_factory
        self.on_solved = on_solved
        self.event: dict[str, Any] = {}
        # капча ждёт решения, но живой картинки сейчас нет (ещё не показана / протухла)
        self.pending = False
        self.clock = clock
        self.page: Any | None = None
        self.started_at = 0.0  # когда родился ключ показанной картинки
        self.need_at = 0.0  # когда капча стала нужна (для 3-часового таймаута)
        self.message_id: int | None = None  # картинка с кнопками
        self._last_tick = 0.0
        self._note = ""  # приписка над подписью картинки («Неверно…»), сохраняется при отсчёте
        self._mem_last: int | None = None
        self._mem_msgs: list[int] = []

    # ---------------------------------------------------------------- state

    @property
    def active(self) -> bool:
        """Есть живая картинка (открытая страница капчи)."""
        return self.page is not None

    @property
    def blocking(self) -> bool:
        """Капча ждёт решения (картинка показана или ждёт кнопки) — новые обходы бессмысленны."""
        return self.active or self.pending

    def _last(self) -> int | None:
        if self.settings is None:
            return self._mem_last
        value = self.settings.get_value(self.LAST_KEY)
        return int(value) if value else None

    def _set_last(self, value: int) -> None:
        if self.settings is None:
            self._mem_last = value
        else:
            self.settings.set_value(self.LAST_KEY, str(value))

    def _url(self) -> str | None:
        url = self.event.get("captcha_url") or self.event.get("url")
        return url if url and "/account/captcha" in url else None

    @staticmethod
    def _picture_keyboard() -> dict[str, Any]:
        return {
            "inline_keyboard": [
                [
                    {"text": "🔄 Другая картинка", "callback_data": "cap:new"},
                    {"text": "✖️ Позже", "callback_data": "cap:skip"},
                ]
            ]
        }

    @staticmethod
    def _prompt_keyboard() -> dict[str, Any]:
        return {
            "inline_keyboard": [
                [
                    {"text": "🧩 Показать капчу", "callback_data": "cap:show"},
                    {"text": "✖️ Позже", "callback_data": "cap:skip"},
                ]
            ]
        }

    def _caption(self, note: str = "") -> str:
        left = max(int(PICTURE_LIFETIME - (self.clock() - self.started_at)), 0)
        return (
            f"{note}{CAPTION}\n"
            f"⏳ Ответь за <b>{left} с</b> — потом картинка устареет"
        )

    # ------------------------------------------------- сообщения капчи в чате

    def _msgs(self) -> list[int]:
        """Сообщения капчи, ещё висящие в чате (переживают перезапуск бота)."""
        import json

        if self.settings is None:
            return list(self._mem_msgs)
        try:
            return [int(x) for x in json.loads(self.settings.get_value(self.MSGS_KEY) or "[]")]
        except (TypeError, ValueError):
            return []

    def _save_msgs(self, ids: list[int]) -> None:
        import json

        if self.settings is None:
            self._mem_msgs = list(ids)
        elif ids:
            self.settings.set_value(self.MSGS_KEY, json.dumps(ids))
        else:
            self.settings.delete_value(self.MSGS_KEY)

    def _forget_messages(self) -> None:
        """Убрать прежние сообщения капчи: в чате живое — одно, а не куча мёртвых."""
        delete = getattr(self.chat, "delete_message", None)
        if callable(delete):
            for mid in self._msgs():
                try:
                    delete(self.chat_id, mid)
                except Exception:  # noqa: BLE001 — уборка не должна ломать капчу
                    logger.debug("delete captcha message %s", mid, exc_info=True)
        self._save_msgs([])
        self.message_id = None

    def _send_prompt(self, note: str = "") -> None:
        self._forget_messages()
        mid = self.chat.send_message(self.chat_id, note + PROMPT, self._prompt_keyboard())
        self._save_msgs([mid])

    def _send_image(self, png: bytes, note: str, born: float) -> None:
        self._forget_messages()
        self.started_at = born
        self._last_tick = self.clock()
        self.message_id = self.chat.send_photo(
            self.chat_id, png, self._caption(note), self._picture_keyboard()
        )
        self._save_msgs([self.message_id])
        self._note = note

    # ----------------------------------------------------------------- poll

    def _restore(self) -> None:
        """После перезапуска бота: нерешённая капча → кнопка заново (страница жила в прошлом процессе)."""
        import json

        from datetime import datetime, timezone

        row = self.store.conn.execute(
            "SELECT id, payload, ts FROM review_events WHERE type = 'captcha_required' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if not row:
            return
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(row[2])).total_seconds()
        except (TypeError, ValueError):
            age = 0
        if age > TIMEOUT_SECONDS:
            return  # старая капча — следующий обход получит свежую
        if self.store.captcha_outcome(row[0]) is not None:
            return
        data = json.loads(row[1] or "{}")
        self.event = data
        if not self._url():
            return
        self._set_last(row[0])
        self._need(data, note="🔄 Бот перезапущен — капча всё ещё не решена.\n", age=age)

    def _need(self, data: dict[str, Any], note: str = "", age: float = 0.0) -> None:
        """Капча нужна: закрыть прежнюю страницу, вместо картинки показать кнопку."""
        self._close_page()
        self.event = data
        self.pending = True
        self.need_at = self.clock() - age
        self.store.log_event("captcha_prompt", {"url": self._url() or ""})
        self._send_prompt(note)

    def poll(self) -> None:
        """Новая капча из журнала → кнопка; живая картинка → обратный отсчёт и протухание."""
        if not getattr(self, "_restored", False):
            self._restored = True
            try:
                self._restore()
            except Exception:  # noqa: BLE001
                logger.exception("restore captcha")
        if self.blocking and self.need_at and self.clock() - self.need_at > TIMEOUT_SECONDS:
            self._close_page()
            self._forget_messages()
            self.pending = False
            self.need_at = 0.0
            self.store.log_event("captcha_timeout", {})
        self.tick()
        last = self._last()
        if last is None:
            row = self.store.conn.execute("SELECT COALESCE(MAX(id),0) FROM review_events").fetchone()
            self._set_last(int(row[0]))
            return
        rows = self.store.conn.execute(
            "SELECT id, payload FROM review_events WHERE id > ? AND type = 'captcha_required' ORDER BY id",
            (last,),
        ).fetchall()
        if not rows:
            return
        import json

        event_id, payload = rows[-1]  # важна только последняя
        self._set_last(event_id)
        data = json.loads(payload or "{}")
        url = data.get("captcha_url") or data.get("url")
        if url and "/account/captcha" not in url:
            logger.error("captcha_required без ссылки на капчу: %s", data)
            return
        if url:
            if self.active:
                # hh выдал новую капчу — старая картинка уже не решит её
                self.store.log_event("captcha_replaced", {})
            self._need(data)

    def tick(self) -> None:
        """Обратный отсчёт под картинкой; протухла — картинка убирается, снова кнопка."""
        if not self.active or self.message_id is None:
            return
        age = self.clock() - self.started_at
        if age >= PICTURE_LIFETIME:
            self._expire()
            return
        if self.clock() - self._last_tick >= COUNTDOWN_STEP:
            self._last_tick = self.clock()
            edit = getattr(self.chat, "edit_caption", None)
            if callable(edit):
                try:
                    edit(self.chat_id, self.message_id, self._caption(getattr(self, "_note", "")), self._picture_keyboard())
                except Exception:  # noqa: BLE001 — отсчёт не должен ронять бота
                    logger.debug("captcha countdown", exc_info=True)

    def _expire(self) -> None:
        self.store.log_event("captcha_picture_expired", {"age": round(self.clock() - self.started_at)})
        self._close_page()
        self.pending = True
        self._send_prompt("⌛ Картинка устарела (ключ капчи живёт около минуты).\n")

    # ---------------------------------------------------------------- показ

    def start(self, url: str, note: str = "") -> None:
        """Свежая картинка: страница открывается заново, ключ рождается сейчас."""
        born = self.clock()  # до загрузки страницы: запас в несколько секунд, а не наоборот
        try:
            self.page = self.page_factory(url)
            png = self.page.image()
        except Exception as ex:  # noqa: BLE001
            logger.exception("Не удалось открыть капчу: %s", ex)
            self._close_page()
            self.pending = True
            self._send_prompt(f"⚠️ Не смог открыть капчу hh ({type(ex).__name__}). Нажми ещё раз.\n")
            return
        self.pending = False
        if not self.need_at:  # показ без предшествующего _need (тесты, ручной вызов)
            self.need_at = born
        self.store.log_event("captcha_shown", {"url": url})
        self._send_image(png, note, born)

    # ------------------------------------------------------------- ответы

    def handle_message(self, text: str) -> bool:
        """True — сообщение было ответом на капчу."""
        if not text or text.startswith("/"):
            return False
        if not self.active:
            url = self._url()
            if self.pending and url:
                if self.store.active() is not None:
                    # открыта карточка ревью: текст — правка письма/анкеты, а не капча;
                    # капчу человек запросит кнопкой «Показать капчу»
                    return False
                # человек у телефона и пишет — не заставляем ещё жать кнопку
                self.start(url, note="Вот свежая картинка — ответь сразу.\n")
                return True
            return False
        age = self.clock() - self.started_at
        if age > PICTURE_LIFETIME:
            # ответ пришёл на протухшую картинку: hh отклонил бы его как «неверный»
            self.store.log_event("captcha_answer_stale", {"age": round(age), "text_len": len(text)})
            url = self._url() or getattr(self.page, "url", "")
            self._close_page()
            self.start(url, note="⌛ Картинка успела устареть, пока шёл ответ. Вот свежая — ответь сразу.\n")
            return True
        try:
            ok = self.page.submit(text)
        except Exception as ex:  # noqa: BLE001
            logger.exception("Ввод капчи: %s", ex)
            self._finish(f"⚠️ Ошибка при вводе капчи ({type(ex).__name__}). Следующий обход попробует снова.", "captcha_error")
            return True
        self.store.log_event("captcha_answer", {"text": text, "ok": ok, "age": round(age, 1)})
        if ok:
            try:
                self.on_cookies(self.page.cookies())
            except Exception as ex:  # noqa: BLE001
                logger.exception("Куки после капчи: %s", ex)
            resumed = False
            if self.on_solved is not None:
                try:
                    resumed = bool(self.on_solved(self.event))
                except Exception as ex:  # noqa: BLE001
                    logger.exception("Продолжение после капчи: %s", ex)
            self._finish(
                "✅ Капча пройдена. "
                + ("Продолжаю обход: новый запуск по conf.json." if resumed else "Отклики продолжатся в следующем обходе."),
                "captcha_solved",
            )
            return True
        try:
            born = self.clock() - 2  # страница выдала новый ключ, пока отправлялась форма
            png = self.page.image()
        except Exception as ex:  # noqa: BLE001
            logger.exception("Новая картинка капчи: %s", ex)
            self._finish("⚠️ Не удалось получить новую картинку. Следующий обход попробует снова.", "captcha_error")
            return True
        self._send_image(png, "❌ Неверно. Вот новая картинка — ответь текстом.\n", born)
        return True

    def handle_callback(self, data: str) -> bool:
        if not data.startswith("cap:"):
            return False
        if data == "cap:skip":
            if self.blocking:
                self._finish("⏸ Ок, капча отложена. Следующий обход снова её покажет.", "captcha_skipped")
        elif data == "cap:show":
            url = self._url()
            if self.active:
                return True  # картинка уже на экране
            if self.pending and url:
                self.start(url)
        elif data == "cap:new":
            if not self.active:
                return True
            born = self.clock()
            try:
                png = self.page.reload()
            except Exception as ex:  # noqa: BLE001
                logger.exception("Перезагрузка капчи: %s", ex)
                self._finish("⚠️ Не удалось обновить капчу.", "captcha_error")
                return True
            self._send_image(png, "", born)
        return True

    # ---------------------------------------------------------------- utils

    def _close_page(self) -> None:
        if self.page is not None:
            try:
                self.page.close()
            finally:
                self.page = None

    def _finish(self, text: str, event: str) -> None:
        self.pending = False
        self.need_at = 0.0
        self._close_page()
        self._forget_messages()  # капча закрыта — картинка и кнопки больше не нужны, остаётся итог
        self.store.log_event(event, {})
        self.chat.send_message(self.chat_id, text)
