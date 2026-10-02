"""Окно работы `--work-hours HH:MM-HH:MM` (можно через полночь)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta

_RANGE = re.compile(r"^\s*(\d{1,2})(?::(\d{2}))?\s*-\s*(\d{1,2})(?::(\d{2}))?\s*$")


@dataclass(frozen=True)
class WorkHours:
    start: time
    end: time

    @classmethod
    def parse(cls, value: str | None) -> WorkHours | None:
        """None / пустая строка → окна нет (работаем круглосуточно)."""
        if value is None or not str(value).strip():
            return None
        m = _RANGE.match(str(value))
        if not m:
            raise ValueError(
                f"Неверное окно работы {value!r}, нужно HH:MM-HH:MM, например 09:00-21:00"
            )
        h1, m1, h2, m2 = m.groups()
        try:
            start = time(int(h1) % 24 if int(h1) == 24 else int(h1), int(m1 or 0))
            end = time(0, 0) if int(h2) == 24 else time(int(h2), int(m2 or 0))
        except ValueError as ex:
            raise ValueError(f"Неверное окно работы {value!r}: {ex}") from ex
        if start == end:
            raise ValueError(f"Пустое окно работы {value!r}")
        return cls(start, end)

    def contains(self, moment: datetime) -> bool:
        t = moment.time().replace(second=0, microsecond=0)
        if self.start < self.end:
            return self.start <= t < self.end
        # через полночь: 22:00-06:00
        return t >= self.start or t < self.end

    def next_start(self, moment: datetime) -> datetime:
        candidate = moment.replace(
            hour=self.start.hour, minute=self.start.minute, second=0, microsecond=0
        )
        if candidate <= moment:
            candidate += timedelta(days=1)
        return candidate

    def __str__(self) -> str:
        return f"{self.start:%H:%M}-{self.end:%H:%M}"


def is_open(window: WorkHours | None, moment: datetime | None = None) -> bool:
    return window is None or window.contains(moment or datetime.now())
