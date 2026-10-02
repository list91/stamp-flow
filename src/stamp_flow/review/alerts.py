"""Все ошибки процесса → в Telegram-чат бота (для отладки).

Ставится в main.run() для любой команды, если в config.json заданы
review.notify (или review.telegram) bot_token и chat_id. Ловит:
  * любые записи логгера hh_applicant_tool уровня ERROR и выше
    (в т.ч. logger.exception из _run_operation — с полным traceback);
  * непойманные исключения (sys.excepthook / threading.excepthook).

Гарантии «работает в любом случае»:
  * шлёт напрямую в Bot API (не зависит от review-bot, БД и очереди);
  * сам никогда не бросает исключений и не логирует через тот же логгер
    (нет рекурсии);
  * если Telegram недоступен — пишет в config/review-log/alerts-undelivered.jsonl;
  * одинаковые ошибки не спамят: повтор в течение 30 минут копится и
    приходит счётчиком со следующим сообщением;
  * секреты из config (токены, ключи) вырезаются из текста.
"""

from __future__ import annotations

import html
import json
import logging
import os
import socket
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

TG_LIMIT = 4096
DEDUP_SECONDS = 30 * 60
STATE_FILE = ".alerts-state.json"
UNDELIVERED_FILE = "alerts-undelivered.jsonl"

_guard = threading.local()


def _esc(text: str) -> str:
    return html.escape(str(text), quote=False)


def _collect_secrets(config: dict[str, Any]) -> list[str]:
    found: list[str] = []

    def walk(value: Any, key: str = "") -> None:
        if isinstance(value, dict):
            for k, v in value.items():
                walk(v, str(k).lower())
        elif isinstance(value, list):
            for v in value:
                walk(v, key)
        elif isinstance(value, str) and len(value) >= 8 and any(
            s in key for s in ("token", "key", "secret", "password")
        ):
            found.append(value)

    walk(config)
    return sorted(set(found), key=len, reverse=True)


class TelegramAlerter:
    def __init__(
        self,
        token: str,
        chat_id: int | str,
        state_dir: Path,
        secrets: list[str] | None = None,
        proxy_url: str | None = None,
        context: str = "",
        session: requests.Session | None = None,
        clock=time.time,
    ):
        self.token = token
        self.chat_id = chat_id
        self.state_dir = Path(state_dir)
        self.secrets = [s for s in (secrets or []) if s]
        self.context = context
        self.session = session or requests.Session()
        if proxy_url:
            self.session.proxies = {"http": proxy_url, "https": proxy_url}
        self.clock = clock
        self._lock = threading.Lock()

    # ------------------------------------------------------------ helpers

    def redact(self, text: str) -> str:
        for secret in self.secrets:
            text = text.replace(secret, "***")
        return text

    def _state_path(self) -> Path:
        return self.state_dir / STATE_FILE

    def _load_state(self) -> dict[str, Any]:
        try:
            return json.loads(self._state_path().read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}

    def _save_state(self, state: dict[str, Any]) -> None:
        try:
            now = self.clock()
            # чистим старое, чтобы файл не рос
            state = {k: v for k, v in state.items() if now - v.get("last", 0) < 7 * 86400}
            self._state_path().write_text(json.dumps(state), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass

    def _undelivered(self, record: dict[str, Any]) -> None:
        try:
            folder = self.state_dir / "review-log"
            folder.mkdir(parents=True, exist_ok=True)
            with (folder / UNDELIVERED_FILE).open("a", encoding="utf-8") as fp:
                fp.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:  # noqa: BLE001
            try:
                sys.stderr.write(f"[alerts] не доставлено: {record}\n")
            except Exception:  # noqa: BLE001
                pass

    # -------------------------------------------------------------- отправка

    def format(self, title: str, message: str, tb: str | None, repeats: int = 0) -> str:
        head = (
            f"🚨 <b>ОШИБКА</b> · {_esc(self.context)}\n"
            f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} · {_esc(socket.gethostname())} · pid {os.getpid()}\n"
        )
        if repeats:
            head += f"🔁 до этого повторялась ещё {repeats} раз(а)\n"
        body = f"<b>{_esc(title)}</b>\n{_esc(message)}"
        text = head + body
        if tb:
            room = TG_LIMIT - len(text) - 40
            tb = tb if len(tb) <= room else "…" + tb[-max(room, 0):]
            if room > 100:
                text += f"\n<pre>{_esc(tb)}</pre>"
        return text[:TG_LIMIT]

    def send(self, title: str, message: str, tb: str | None = None, key: str | None = None) -> bool:
        """Никогда не бросает. True — отправлено, False — подавлено/не доставлено."""
        if getattr(_guard, "active", False):
            return False
        _guard.active = True
        try:
            with self._lock:
                title, message = self.redact(title), self.redact(message)
                tb = self.redact(tb) if tb else None
                key = key or f"{title}|{message}"[:300]
                state = self._load_state()
                entry = state.get(key) or {}
                now = self.clock()
                if entry and now - entry.get("last_sent", 0) < DEDUP_SECONDS:
                    entry["suppressed"] = entry.get("suppressed", 0) + 1
                    entry["last"] = now
                    state[key] = entry
                    self._save_state(state)
                    return False
                repeats = entry.get("suppressed", 0)
                text = self.format(title, message, tb, repeats)
                ok = self._post(text)
                if ok:
                    state[key] = {"last_sent": now, "last": now, "suppressed": 0}
                    self._save_state(state)
                else:
                    self._undelivered(
                        {
                            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                            "context": self.context,
                            "title": title,
                            "message": message,
                            "traceback": tb,
                        }
                    )
                return ok
        except Exception:  # noqa: BLE001 — алертер не имеет права ронять процесс
            return False
        finally:
            _guard.active = False

    def _post(self, text: str) -> bool:
        try:
            r = self.session.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                json={
                    "chat_id": self.chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
                timeout=10,
            )
            if r.ok:
                return True
            # битая HTML-разметка и т.п. — пробуем простым текстом
            r = self.session.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                json={"chat_id": self.chat_id, "text": html.unescape(
                    text.replace("<b>", "").replace("</b>", "").replace("<pre>", "\n").replace("</pre>", "")
                )[:TG_LIMIT]},
                timeout=10,
            )
            return bool(r.ok)
        except Exception:  # noqa: BLE001
            return False

    # ------------------------------------------------------------ исключения

    def exception(self, exc: BaseException, where: str = "") -> bool:
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        title = f"{type(exc).__module__}.{type(exc).__qualname__}".removeprefix("builtins.")
        if where:
            title = f"{where}: {title}"
        return self.send(title, str(exc) or repr(exc), tb, key=f"{title}|{exc}"[:300])


class TelegramErrorHandler(logging.Handler):
    """Логи ERROR+ → Telegram (с traceback, если есть exc_info)."""

    def __init__(self, alerter: TelegramAlerter):
        super().__init__(level=logging.ERROR)
        self.alerter = alerter

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
            tb = None
            title = f"{record.levelname} в {record.name}:{record.funcName}:{record.lineno}"
            # logger.error(...) без exc_info внутри except: берём активное исключение
            exc_info = record.exc_info if record.exc_info and record.exc_info[1] is not None else sys.exc_info()
            if exc_info[1] is not None:
                exc = exc_info[1]
                tb = "".join(traceback.format_exception(*exc_info))
                title = f"{title} — {type(exc).__qualname__}"
            self.alerter.send(title, message, tb, key=f"{record.name}:{record.lineno}|{message}"[:300])
        except Exception:  # noqa: BLE001
            pass


def install(tool: Any, logger: logging.Logger) -> TelegramAlerter | None:
    """Подключить алерты к процессу. Никогда не бросает."""
    try:
        config = dict(tool.config or {})
        review = config.get("review") or {}
        main = review.get("telegram") or {}
        # ошибки — тоже «требует участия»: идут в бот уведомлений (если он есть), а не в рабочий чат;
        # и не зависят от основного бота, чей токен как раз может быть причиной сбоя
        tg = review.get("notify") or main
        token = tg.get("bot_token")
        chat_id = tg.get("chat_id") or main.get("chat_id")
        if not token or not chat_id:
            return None
        command = " ".join(os.path.basename(a) if i == 0 else a for i, a in enumerate(sys.argv)) or "hh-applicant-tool"
        alerter = TelegramAlerter(
            token,
            chat_id,
            state_dir=Path(tool.config_path),
            secrets=_collect_secrets(config),
            proxy_url=tg.get("proxy_url"),
            context=command[:200],
        )
        if not any(isinstance(h, TelegramErrorHandler) for h in logger.handlers):
            logger.addHandler(TelegramErrorHandler(alerter))

        previous_hook = sys.excepthook

        def excepthook(exc_type, exc, tb):
            if not issubclass(exc_type, KeyboardInterrupt):
                alerter.exception(exc.with_traceback(tb), where="непойманное исключение")
            previous_hook(exc_type, exc, tb)

        sys.excepthook = excepthook

        previous_thread_hook = threading.excepthook

        def thread_hook(args):
            if args.exc_value is not None and not isinstance(args.exc_value, KeyboardInterrupt):
                alerter.exception(args.exc_value, where=f"поток {getattr(args.thread, 'name', '?')}")
            previous_thread_hook(args)

        threading.excepthook = thread_hook
        return alerter
    except Exception:  # noqa: BLE001
        return None
