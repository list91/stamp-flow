"""Хранилище ревью: очередь вакансий + append-only журнал событий.

Журнал пишется в две копии: таблица `review_events` (UPDATE/DELETE запрещены
триггерами в schema.sql) и `review-log/YYYY-MM.jsonl` (только дозапись).
Методов удаления здесь нет намеренно.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .models import JSON_FIELDS, TRANSITIONS, ItemState, ReviewItem

logger = logging.getLogger(__package__)

SCHEMA_PATH = Path(__file__).with_name("schema_review.sql")

# settings[HOT_KEY] = id вакансии, по которой обход ждёт решения (показывается первой)
HOT_KEY = "review_hot"

# Ключи, значения которых никогда не попадают в журнал
SECRET_KEYS = frozenset(
    {
        "token",
        "bot_token",
        "access_token",
        "refresh_token",
        "api_key",
        "password",
        "client_secret",
        "authorization",
    }
)

# Поля review_items, которые можно менять через update()
UPDATABLE_FIELDS = frozenset(
    {
        "steps",
        "step_idx",
        "verdict_json",
        "vacancy_json",
        "resume_text",
        "test_json",
        "letter_draft",
        "letter_final",
        "answers_draft_json",
        "answers_json",
        "tg_message_ids",
    }
)


class TransitionError(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def redact(value: Any) -> Any:
    """Убирает секреты из payload перед записью в журнал."""
    if isinstance(value, dict):
        return {
            k: (
                "***"
                if isinstance(k, str) and k.lower() in SECRET_KEYS
                else redact(v)
            )
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    return value


class ReviewStore:
    def __init__(
        self,
        conn: sqlite3.Connection,
        log_dir: Path | None = None,
        run_id: str | None = None,
    ):
        self.conn = conn
        self.log_dir = Path(log_dir) if log_dir else None
        self.run_id = run_id
        # Таблицы ревью живут в той же БД, что и апстрим, но схему ведём сами.
        self.conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))

    # ------------------------------------------------------------ журнал

    def log_event(
        self,
        type: str,
        payload: dict[str, Any] | None = None,
        *,
        item_id: int | None = None,
        resume_id: str | None = None,
        vacancy_id: int | str | None = None,
    ) -> None:
        ts = _now()
        safe_payload = redact(payload or {})
        vacancy_id = int(vacancy_id) if vacancy_id is not None else None
        self.conn.execute(
            "INSERT INTO review_events"
            " (ts, run_id, item_id, resume_id, vacancy_id, type, payload)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                ts,
                self.run_id,
                item_id,
                resume_id,
                vacancy_id,
                type,
                _dumps(safe_payload),
            ),
        )
        self.conn.commit()
        self._append_jsonl(
            {
                "ts": ts,
                "run_id": self.run_id,
                "item_id": item_id,
                "resume_id": resume_id,
                "vacancy_id": vacancy_id,
                "type": type,
                "payload": safe_payload,
            }
        )

    def _append_jsonl(self, record: dict[str, Any]) -> None:
        if not self.log_dir:
            return
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            path = self.log_dir / f"{record['ts'][:7]}.jsonl"
            with path.open("a", encoding="utf-8") as fp:
                fp.write(_dumps(record) + "\n")
                fp.flush()
        except OSError as ex:
            # SQLite-копия уже записана — не роняем обход из-за зеркала
            logger.error("Не удалось дописать review-log: %s", ex)

    def events(self, **filters: Any) -> list[dict[str, Any]]:
        sql = "SELECT * FROM review_events"
        if filters:
            sql += " WHERE " + " AND ".join(f"{k} = :{k}" for k in filters)
        sql += " ORDER BY id"
        cur = self.conn.execute(sql, filters)
        cols = [c[0] for c in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]  # noqa: B905
        for row in rows:
            row["payload"] = json.loads(row["payload"])
        return rows

    # ------------------------------------------------------------ очередь

    def enqueue(
        self,
        *,
        resume_id: str,
        vacancy_id: int | str,
        steps: list[str],
        vacancy: dict[str, Any],
        resume_text: str = "",
        verdict: Any = None,
        test: Any = None,
        dry_run: bool = False,
    ) -> ReviewItem:
        cur = self.conn.execute(
            "INSERT INTO review_items (resume_id, vacancy_id, state, steps,"
            " dry_run, verdict_json, vacancy_json, resume_text, test_json)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                resume_id,
                int(vacancy_id),
                ItemState.QUEUED.value,
                _dumps(steps),
                int(dry_run),
                None if verdict is None else _dumps(verdict),
                _dumps(vacancy),
                resume_text,
                None if test is None else _dumps(test),
            ),
        )
        self.conn.commit()
        item_id = cur.lastrowid
        self.log_event(
            "enqueued",
            {"steps": steps, "dry_run": dry_run, "verdict": verdict},
            item_id=item_id,
            resume_id=resume_id,
            vacancy_id=vacancy_id,
        )
        return self.get(item_id)

    def get(self, item_id: int) -> ReviewItem | None:
        return next(self._select("id = ?", (item_id,)), None)

    def find(self, resume_id: str, vacancy_id: int | str) -> ReviewItem | None:
        return next(
            self._select(
                "resume_id = ? AND vacancy_id = ?", (resume_id, int(vacancy_id))
            ),
            None,
        )

    def is_known(self, resume_id: str, vacancy_id: int | str) -> bool:
        return self.find(resume_id, vacancy_id) is not None

    def relevance_score(
        self, resume_id: str, vacancy_id: int | str, min_rubric: int = 1
    ) -> float | None:
        """Ранее выставленная моделью оценка релевантности (последняя), если есть."""
        row = self.conn.execute(
            "SELECT payload FROM review_events WHERE type = 'relevance_scored'"
            " AND resume_id = ? AND vacancy_id = ? ORDER BY id DESC LIMIT 1",
            (resume_id, int(vacancy_id)),
        ).fetchone()
        if not row:
            return None
        payload = json.loads(row[0] or "{}")
        # оценка по старым критериям (без rubric) не считается; ручные решения — да
        if not payload.get("manual") and int(payload.get("rubric") or 1) < min_rubric:
            return None
        score = payload.get("score")
        return float(score) if score is not None else None

    CAPTCHA_DONE = ("captcha_solved", "captcha_skipped", "captcha_error", "captcha_timeout")

    def captcha_unresolved(self, max_age: float = 3 * 3600) -> dict[str, Any] | None:
        """Последняя капча hh, которую ещё не решили, не отложили и не закрыли по таймауту.

        Моложе max_age (старая — следующий обход получит свежую). Возвращает
        {"id", "ts", "payload"} или None. Общий критерий: по нему обход пропускает запуск,
        бот блокирует «пропустить ожидание», а бот уведомлений держит своё сообщение.
        """
        row = self.conn.execute(
            "SELECT id, ts, payload FROM review_events WHERE type = 'captcha_required'"
            " ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if not row:
            return None
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(row[1])).total_seconds()
        except (TypeError, ValueError):
            return None
        if age > max_age:
            return None
        if self.captcha_outcome(row[0]) is not None:
            return None
        return {"id": row[0], "ts": row[1], "payload": json.loads(row[2] or "{}")}

    def captcha_outcome(self, event_id: int) -> str | None:
        """Чем кончилась капча после события event_id: captcha_solved / _skipped / _error / _timeout."""
        marks = ", ".join("?" for _ in self.CAPTCHA_DONE)
        row = self.conn.execute(
            f"SELECT type FROM review_events WHERE id > ? AND type IN ({marks}) ORDER BY id LIMIT 1",
            (event_id, *self.CAPTCHA_DONE),
        ).fetchone()
        return row[0] if row else None

    def is_known_any(self, vacancy_id: int | str, dry_run: bool) -> bool:
        """Вакансия уже в очереди/журнале от любого резюме (одна карточка на вакансию)."""
        op = "LIKE" if dry_run else "NOT LIKE"
        if next(self._select(f"vacancy_id = ? AND resume_id {op} 'dry:%'", (int(vacancy_id),)), None):
            return True
        # авто-отклик без письма (без записи в очереди) от другого резюме
        for (payload,) in self.conn.execute(
            "SELECT payload FROM review_events WHERE type = 'auto_applied' AND vacancy_id = ?",
            (int(vacancy_id),),
        ):
            if bool(json.loads(payload or "{}").get("dry_run")) == dry_run:
                return True
        return False

    def by_state(self, *states: ItemState) -> list[ReviewItem]:
        marks = ", ".join("?" for _ in states)
        return list(
            self._select(
                f"state IN ({marks})", tuple(s.value for s in states)
            )
        )

    def next_queued(self) -> ReviewItem | None:
        # вакансия, на которой обход стоит и ждёт решения, показывается первой
        hot = self._hot_item()
        if hot is not None:
            return hot
        return next(
            self._select("state = ?", (ItemState.QUEUED.value,)), None
        )

    def prioritize(self, item_id: int) -> None:
        """Показать эту вакансию следующей (обход ждёт по ней решения пользователя)."""
        self.conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (HOT_KEY, str(int(item_id))),
        )
        self.conn.commit()

    def clear_priority(self, item_id: int | None = None) -> None:
        if item_id is None:
            self.conn.execute("DELETE FROM settings WHERE key = ?", (HOT_KEY,))
        else:
            self.conn.execute(
                "DELETE FROM settings WHERE key = ? AND value = ?", (HOT_KEY, str(int(item_id)))
            )
        self.conn.commit()

    def _hot_item(self) -> ReviewItem | None:
        row = self.conn.execute("SELECT value FROM settings WHERE key = ?", (HOT_KEY,)).fetchone()
        try:
            item = self.get(int(row[0])) if row else None
        except (TypeError, ValueError):
            return None
        return item if item is not None and item.state is ItemState.QUEUED else None

    def active(self) -> ReviewItem | None:
        return next(
            self._select("state = ?", (ItemState.IN_REVIEW.value,)), None
        )

    def count_open(self) -> int:
        return len(self.by_state(ItemState.QUEUED, ItemState.IN_REVIEW))

    def cancel_open(self, reason: str, *, dry_run: bool | None = None) -> int:
        """Снять все незакрытые карточки (queued / in_review) без решения. Вернёт их число.

        Вакансии остаются «известными» (повторно на ревью не придут). Журнал не
        трогаем: каждая отмена — событие state_changed.
        """
        done = 0
        for _ in range(10):  # бот мог показать карточку между чтением и отменой
            items = [
                i for i in self.by_state(ItemState.QUEUED, ItemState.IN_REVIEW)
                if dry_run is None or i.dry_run == dry_run
            ]
            if not items:
                break
            for item in items:
                try:
                    self.set_state(item, ItemState.CANCELLED, reason=reason)
                    done += 1
                except TransitionError:
                    continue
        self.clear_priority()
        if done:
            self.log_event("review_queue_cancelled", {"count": done, "reason": reason})
        return done

    def _select(self, where: str, params: tuple) -> Iterator[ReviewItem]:
        cur = self.conn.execute(
            f"SELECT * FROM review_items WHERE {where} ORDER BY id", params
        )
        cols = [c[0] for c in cur.description]
        for row in cur.fetchall():
            yield ReviewItem.from_row(dict(zip(cols, row)))  # noqa: B905

    # ------------------------------------------------------------ изменения

    def set_state(
        self,
        item: ReviewItem | int,
        new_state: ItemState,
        reason: str | None = None,
        **fields: Any,
    ) -> ReviewItem:
        current = self.get(item if isinstance(item, int) else item.id)
        if current is None:
            raise TransitionError(f"review item not found: {item}")
        new_state = ItemState(new_state)
        if new_state not in TRANSITIONS[current.state]:
            raise TransitionError(
                f"{current.state.value} -> {new_state.value} запрещён"
            )
        # атомарно: «было именно это состояние». Иначе параллельный процесс (бот, обход,
        # скрипт) мог сменить его между проверкой и записью — и переход «воскресил» бы
        # отменённую карточку (так призрак и появился)
        changed = self._update(
            current.id, _expect_state=current.state.value, state=new_state.value, **fields
        )
        if not changed:
            raise TransitionError(
                f"#{current.id}: состояние изменилось параллельно "
                f"(ждали {current.state.value} -> {new_state.value})"
            )
        self.log_event(
            "state_changed",
            {
                "from": current.state.value,
                "to": new_state.value,
                "reason": reason,
            },
            item_id=current.id,
            resume_id=current.resume_id,
            vacancy_id=current.vacancy_id,
        )
        return self.get(current.id)

    def update(self, item: ReviewItem | int, **fields: Any) -> ReviewItem:
        item_id = item if isinstance(item, int) else item.id
        unknown = set(fields) - UPDATABLE_FIELDS
        if unknown:
            raise ValueError(f"нельзя менять поля: {sorted(unknown)}")
        self._update(item_id, **fields)
        return self.get(item_id)

    def _update(self, item_id: int, _expect_state: str | None = None, **fields: Any) -> int:
        """UPDATE карточки; с _expect_state — только если она всё ещё в этом состоянии.
        Возвращает число изменённых строк."""
        if not fields:
            return 0
        values = {
            k: (_dumps(v) if k in JSON_FIELDS and v is not None else v)
            for k, v in fields.items()
        }
        assignments = ", ".join(f"{k} = :{k}" for k in values)
        params: dict[str, Any] = {**values, "_id": item_id}
        where = "id = :_id"
        if _expect_state is not None:
            where += " AND state = :_expect"
            params["_expect"] = _expect_state
        cur = self.conn.execute(f"UPDATE review_items SET {assignments} WHERE {where}", params)
        self.conn.commit()
        return cur.rowcount
