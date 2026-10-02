"""Мини-клиент Telegram Bot API на requests (без новых зависимостей)."""

from __future__ import annotations

import logging
import time
from typing import Any

import requests

logger = logging.getLogger(__package__)


class TelegramError(RuntimeError):
    pass


class TelegramClient:
    def __init__(
        self,
        token: str,
        proxy_url: str | None = None,
        session: requests.Session | None = None,
        min_interval: float = 1.0,
    ):
        if not token:
            raise ValueError("review.telegram.bot_token не задан в config.json")
        self._base = f"https://api.telegram.org/bot{token}/"
        self.session = session or requests.Session()
        if proxy_url:
            self.session.proxies = {"http": proxy_url, "https": proxy_url}
        # Telegram: не чаще ~1 сообщения в секунду в один чат
        self.min_interval = min_interval
        self._last_send = 0.0

    def call(self, method: str, http_timeout: float = 30.0, **params: Any) -> Any:
        payload = {k: v for k, v in params.items() if v is not None}
        try:
            r = self.session.post(
                self._base + method, json=payload, timeout=http_timeout
            )
            data = r.json()
        except (requests.RequestException, ValueError) as ex:
            # не светим токен из URL
            raise TelegramError(f"{method}: {type(ex).__name__}") from None
        if not data.get("ok"):
            raise TelegramError(f"{method}: {data.get('description')}")
        return data.get("result")

    def get_updates(self, offset: int | None, timeout: int = 25) -> list[dict[str, Any]]:
        return self.call(
            "getUpdates",
            http_timeout=timeout + 10,
            offset=offset,
            timeout=timeout,
            allowed_updates=["message", "callback_query"],
        )

    def get_me(self) -> dict[str, Any]:
        """Кто этот бот (id, username). Нужен, чтобы заметить смену токена."""
        return self.call("getMe")

    # У переиспользованного бота в меню висят команды прежних хозяев — причём в нескольких
    # областях сразу; для личного чата приоритетнее всего «все личные чаты» + язык. Перекрываем все.
    COMMAND_SCOPES: tuple[dict[str, Any], ...] = (
        {},
        {"language_code": "ru"},
        {"scope": {"type": "all_private_chats"}},
        {"scope": {"type": "all_private_chats"}, "language_code": "ru"},
    )

    def set_commands(self, commands: list[tuple[str, str]]) -> None:
        """Меню команд бота: наши команды вместо чужих во всех областях личного чата."""
        payload = [{"command": c, "description": d} for c, d in commands]
        for scope in self.COMMAND_SCOPES:
            try:
                self.call("setMyCommands", commands=payload, **scope)
            except TelegramError as ex:
                logger.debug("setMyCommands %s: %s", scope, ex)

    def send_message(
        self,
        chat_id: int | str,
        text: str,
        reply_markup: dict[str, Any] | None = None,
        silent: bool = False,
    ) -> int:
        wait = self.min_interval - (time.monotonic() - self._last_send)
        if wait > 0:
            time.sleep(wait)
        result = self.call(
            "sendMessage",
            chat_id=chat_id,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True,
            disable_notification=True if silent else None,
            reply_markup=reply_markup,
        )
        self._last_send = time.monotonic()
        return result["message_id"]

    def edit_caption(
        self,
        chat_id: int | str,
        message_id: int,
        caption: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        """Подпись под фото (обратный отсчёт капчи); «не изменилось» — не ошибка."""
        try:
            self.call(
                "editMessageCaption",
                chat_id=chat_id,
                message_id=message_id,
                caption=caption,
                parse_mode="HTML",
                reply_markup=reply_markup or {"inline_keyboard": []},
            )
        except TelegramError as ex:
            if "not modified" not in str(ex):
                logger.debug("editMessageCaption: %s", ex)

    def send_photo(
        self,
        chat_id: int | str,
        png: bytes,
        caption: str = "",
        reply_markup: dict[str, Any] | None = None,
    ) -> int:
        import json

        data: dict[str, Any] = {"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"}
        if reply_markup:
            data["reply_markup"] = json.dumps(reply_markup)
        try:
            r = self.session.post(
                self._base + "sendPhoto",
                data=data,
                files={"photo": ("captcha.png", png, "image/png")},
                timeout=60,
            )
            result = r.json()
        except (requests.RequestException, ValueError) as ex:
            raise TelegramError(f"sendPhoto: {type(ex).__name__}") from None
        if not result.get("ok"):
            raise TelegramError(f"sendPhoto: {result.get('description')}")
        self._last_send = time.monotonic()
        return result["result"]["message_id"]

    def edit_message(
        self,
        chat_id: int | str,
        message_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        try:
            self.call(
                "editMessageText",
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=reply_markup or {"inline_keyboard": []},
            )
        except TelegramError as ex:
            if "not modified" not in str(ex):
                logger.debug("editMessageText: %s", ex)

    def delete_message(self, chat_id: int | str, message_id: int) -> bool:
        try:
            self.call("deleteMessage", chat_id=chat_id, message_id=message_id)
            return True
        except TelegramError as ex:
            logger.debug("deleteMessage: %s", ex)
            return False

    def remove_keyboard(self, chat_id: int | str, message_id: int) -> None:
        try:
            self.call(
                "editMessageReplyMarkup",
                chat_id=chat_id,
                message_id=message_id,
                reply_markup={"inline_keyboard": []},
            )
        except TelegramError as ex:
            logger.debug("remove_keyboard: %s", ex)

    def answer_callback(self, callback_id: str, text: str | None = None) -> None:
        try:
            self.call("answerCallbackQuery", callback_query_id=callback_id, text=text)
        except TelegramError as ex:
            logger.debug("answerCallbackQuery: %s", ex)
