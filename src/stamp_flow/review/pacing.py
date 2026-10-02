"""Темп откликов «как у человека»: случайная пауза между откликами и лимит в час.

Считается по журналу review_events, поэтому работает через границы процессов
(обход по cron, review-submit из бота, ручной запуск).
"""

from __future__ import annotations

import json
import random
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

DEFAULT_DELAY = "60-180"
DEFAULT_MAX_PER_HOUR = 10


def parse_range(value: Any, default: str = DEFAULT_DELAY) -> tuple[float, float]:
    """«60-180» / «90» / 0 → (min, max) секунд."""
    raw = default if value is None or str(value).strip() == "" else str(value)
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(?:-\s*(\d+(?:\.\d+)?))?\s*", raw)
    if not m:
        raise ValueError(f"Неверная пауза {value!r}, нужно секунды «60-180» или «90»")
    lo = float(m.group(1))
    hi = float(m.group(2)) if m.group(2) else lo
    if hi < lo:
        lo, hi = hi, lo
    return lo, hi


def _parse_ts(ts: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def real_applications(store: Any, since: datetime) -> list[datetime]:
    """Моменты реальных откликов (не dry-run) начиная с since."""
    rows = store.conn.execute(
        "SELECT ts, type, payload FROM review_events"
        " WHERE ts >= ? AND type IN ('auto_applied', 'state_changed')",
        (since.astimezone(timezone.utc).isoformat(timespec="seconds"),),
    ).fetchall()
    result = []
    for ts, type_, payload in rows:
        p = json.loads(payload or "{}")
        if type_ == "auto_applied" and p.get("dry_run"):
            continue
        if type_ == "state_changed" and not (
            p.get("to") == "submitted" and p.get("reason") != "dry_run"
        ):
            continue
        if (dt := _parse_ts(ts)) is not None:
            result.append(dt)
    return sorted(result)


class Pacer:
    def __init__(
        self,
        store: Any,
        delay: tuple[float, float] = (60.0, 180.0),
        max_per_hour: int | None = DEFAULT_MAX_PER_HOUR,
        cancelled: Callable[[], bool] = lambda: False,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        waiter: Callable[[float], float] | None = None,
    ):
        self.waiter = waiter  # видимое ожидание с кнопкой ⏭ (timer.WaitBoard)
        self.store = store
        self.delay = delay
        self.max_per_hour = max_per_hour or None
        self.cancelled = cancelled
        self.sleep = sleep
        self.now = now
        # после паузы резюме/круга (она уже покрыла интервал) ближайший отклик
        # не ждёт повторно — если с тех пор не было других откликов
        self._skip_after: datetime | None = None
        self._skip_armed = False

    @property
    def skip_next(self) -> bool:
        return self._skip_armed

    @skip_next.setter
    def skip_next(self, value: bool) -> None:
        self._skip_armed = bool(value)
        recent = real_applications(self.store, self.now() - timedelta(hours=1)) if value else []
        self._skip_after = recent[-1] if recent else None

    def remaining(self) -> float:
        """Сколько ещё ждать до следующего отклика (случайная цель из delay)."""
        lo, hi = self.delay
        if hi <= 0:
            return 0.0
        recent = real_applications(self.store, self.now() - timedelta(hours=1))
        if not recent:
            return 0.0
        return max(random.uniform(lo, hi) - (self.now() - recent[-1]).total_seconds(), 0.0)

    def hour_count(self) -> int:
        return len(real_applications(self.store, self.now() - timedelta(hours=1)))

    def hourly_limit_reached(self) -> bool:
        return bool(self.max_per_hour) and self.hour_count() >= self.max_per_hour

    def wait_before_apply(self) -> float:
        """Пауза с момента последнего реального отклика. Возвращает секунды ожидания."""
        lo, hi = self.delay
        if hi <= 0:
            return 0.0
        recent = real_applications(self.store, self.now() - timedelta(hours=1))
        if not recent:
            self._skip_armed = False
            return 0.0
        if self._skip_armed:
            self._skip_armed = False
            if recent[-1] == self._skip_after:
                return 0.0  # интервал уже выдержан паузой резюме/круга
        target = random.uniform(lo, hi)
        left = target - (self.now() - recent[-1]).total_seconds()
        if left > 0 and self.waiter is not None:
            return self.waiter(left)
        waited = 0.0
        while left > 0 and not self.cancelled():
            step = min(left, 5.0)
            self.sleep(step)
            waited += step
            left -= step
        return waited
