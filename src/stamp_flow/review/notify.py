"""Краткая лента в Telegram: куда отклик ушёл, куда нет (по журналу review_events).

Бот раз в цикл читает новые события журнала и шлёт одно сжатое сообщение.
Обход по cron в Telegram не пишет — вся доставка здесь.
"""

from __future__ import annotations

import logging
from typing import Any

from .render import esc, resume_tag, split_text

logger = logging.getLogger(__package__)

NOTIFY_KEY = "review.telegram.notify_last_id"


def _link(p: dict[str, Any], vacancy_id: Any) -> str:
    name = p.get("name") or f"вакансия {vacancy_id}"
    employer = p.get("employer")
    url = p.get("url") or f"https://hh.ru/vacancy/{vacancy_id}"
    title = f"{name} — {employer}" if employer else name
    return f'<a href="{esc(url)}">{esc(title)}</a>'


class Notifier:
    def __init__(
        self,
        store: Any,
        chat: Any,
        chat_id: int | str,
        settings: Any | None,
        resume_titles: dict[str, str] | None = None,
    ):
        self.resume_titles = resume_titles or {}
        self.store = store
        self.chat = chat
        self.chat_id = chat_id
        self.settings = settings
        self._mem_last: int | None = None

    # ------------------------------------------------------------ состояние

    def _last_id(self) -> int | None:
        if self.settings is None:
            return self._mem_last
        value = self.settings.get_value(NOTIFY_KEY)
        return int(value) if value else None

    def _save_last_id(self, value: int) -> None:
        if self.settings is None:
            self._mem_last = value
        else:
            self.settings.set_value(NOTIFY_KEY, str(value))

    def _max_id(self) -> int:
        row = self.store.conn.execute("SELECT COALESCE(MAX(id), 0) FROM review_events").fetchone()
        return int(row[0])

    # ---------------------------------------------------------------- лента

    def run(self) -> int:
        """Отправить новые события. Возвращает число строк в сообщении."""
        last = self._last_id()
        if last is None:
            # первый запуск: историю не вываливаем
            self._save_last_id(self._max_id())
            return 0
        rows = self.store.conn.execute(
            "SELECT id, run_id, item_id, vacancy_id, type, payload, resume_id FROM review_events"
            " WHERE id > ? ORDER BY id",
            (last,),
        ).fetchall()
        if not rows:
            return 0
        lines = [line for row in rows if (line := self.format(row))]
        if lines:
            for chunk in split_text("\n".join(lines)):
                self.chat.send_message(self.chat_id, chunk)
        # сдвигаем курсор только после успешной отправки
        self._save_last_id(rows[-1][0])
        return len(lines)

    def format(self, row: tuple) -> str | None:
        line = self._format(row)
        if not line:
            return None
        resume_id = row[6] if len(row) > 6 else None
        if not resume_id and row[2]:
            item = self.store.get(row[2])
            resume_id = item.resume_id if item else None
        tag = resume_tag(resume_id, self.resume_titles)
        if tag and resume_id and row[3]:
            try:  # оценка релевантности в каждой строке ленты
                score = self.store.relevance_score(resume_id, row[3])
            except Exception:  # noqa: BLE001
                score = None
            if score is not None:
                tag = f"{tag} · 🎯 {score:.2f}"
        return f"{line}\n   <i>{tag}</i>" if tag else line

    def _format(self, row: tuple) -> str | None:
        import json

        _, run_id, item_id, vacancy_id, type_, payload = row[:6]
        p = json.loads(payload or "{}")
        from_pass = (run_id or "").startswith("pass-")

        if type_ == "auto_applied":
            if p.get("dry_run"):
                return f"🧪 dry-run, отклик без письма: {_link(p, vacancy_id)}"
            return f"✅ Отклик без письма: {_link(p, vacancy_id)}"
        if type_ == "skipped_technical" and p.get("reason") == "redirect":
            return f"↪️ Только через сайт работодателя: {_link(p, vacancy_id)}"
        if type_ == "error" and vacancy_id:
            if p.get("where") == "draft":
                return None  # карточка сама пишет «Модель не ответила» с кнопкой «Повторить»
            err = p.get("error") or ("капча hh" if p.get("captcha") else "ошибка")
            if str(err).startswith("LLMError"):
                return f"⚠️ Модель не ответила: {_link(p, vacancy_id)}"  # детали — в алерте и журнале
            return f"⚠️ {esc(str(err)[:120])}: {_link(p, vacancy_id)}"
        if type_ == "captcha_required":
            if not p.get("name"):
                return "🧩 hh просит капчу при отправке — картинку пришлю, после неё отправлю"
            return f"🧩 hh просит капчу — отклики на паузе, картинку пришлю отдельно ({_link(p, vacancy_id)})"
        if type_ == "review_queue_cancelled":
            return (
                f"🧹 Очередь ревью отменена: снято карточек {p.get('count', 0)}. "
                "Дальше — поток без очереди: обход встаёт на каждой вакансии, пока не решишь."
            )
        if type_ == "hh_limit_exceeded":
            return "⛔ Лимит откликов hh исчерпан — обход остановлен"
        if type_ == "llm_unavailable":
            return "⏸ Модель не отвечает — обход ждёт её, вакансии не пропускаю"
        if type_ == "llm_recovered":
            return "▶️ Модель снова отвечает — обход продолжается"
        if type_ == "llm_dead":
            if p.get("where") == "релевантность":
                return "🛑 Модель не отвечает — обход остановлен, вакансии проверятся в следующем обходе"
            return "🛑 Модель не ожила — новые вакансии идут на ревью, без авто-откликов"
        # Отправки из бота (review-submit) бот объявляет сам; здесь — только
        # досланные обходом (одобрено вне окна / после лимита)
        submitted = type_ == "state_changed" and p.get("to") == "submitted" and p.get("reason") != "dry_run"

        if from_pass and (submitted or type_ in ("submit_failed", "submit_limit")):
            item = self.store.get(item_id) if item_id else None
            v = (item.vacancy_json if item else None) or {}
            brief = {
                "name": v.get("name"),
                "employer": (v.get("employer") or {}).get("name"),
                "url": v.get("alternate_url"),
            }
            if submitted:
                return f"✅ Отклик после ревью: {_link(brief, vacancy_id)}"
            return f"⚠️ Не ушло ({esc(str(p.get('error'))[:100])}): {_link(brief, vacancy_id)}"
        if type_ == "pass_finished":
            applied, queued, errors = (p.get(k, 0) for k in ("auto_applied", "enqueued", "errors"))
            if not (applied or queued or errors):
                return None
            parts = [f"откликов {applied}", f"на ревью {queued}"]
            if errors:
                parts.append(f"ошибок {errors}")
            deferred = (p.get("deferred_hourly_limit") or 0) + (p.get("skip_hourly_limit") or 0)
            if deferred:
                parts.append(f"отложено лимитом в час {deferred}")
            parts.append(f"просмотрено {p.get('seen', 0)}")
            prefix = "🧪 dry-run обход: " if p.get("dry_run") else "📊 Обход: "
            return prefix + ", ".join(parts)
        return None
