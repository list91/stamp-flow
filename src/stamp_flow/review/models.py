from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ItemState(StrEnum):
    QUEUED = "queued"
    IN_REVIEW = "in_review"
    APPROVED = "approved"
    SUBMITTING = "submitting"
    SUBMITTED = "submitted"
    REJECTED = "rejected"
    FAILED_RETRYABLE = "failed_retryable"
    STALE = "stale"
    CANCELLED = "cancelled"  # снята без решения (очередь отменена); вакансия остаётся «известной»


# Разрешённые переходы. Терминальные: submitted, rejected, cancelled.
TRANSITIONS: dict[ItemState, frozenset[ItemState]] = {
    ItemState.QUEUED: frozenset({ItemState.IN_REVIEW, ItemState.CANCELLED}),
    ItemState.IN_REVIEW: frozenset(
        {ItemState.IN_REVIEW, ItemState.APPROVED, ItemState.REJECTED, ItemState.CANCELLED}
    ),
    ItemState.APPROVED: frozenset({ItemState.SUBMITTING}),
    ItemState.SUBMITTING: frozenset(
        {
            ItemState.SUBMITTED,
            ItemState.FAILED_RETRYABLE,
            ItemState.STALE,
        }
    ),
    ItemState.FAILED_RETRYABLE: frozenset({ItemState.SUBMITTING}),
    ItemState.STALE: frozenset({ItemState.QUEUED}),
    ItemState.SUBMITTED: frozenset(),
    ItemState.REJECTED: frozenset(),
    ItemState.CANCELLED: frozenset(),
}

# Поля review_items, которые хранятся как JSON
JSON_FIELDS = (
    "steps",
    "verdict_json",
    "vacancy_json",
    "test_json",
    "answers_draft_json",
    "answers_json",
    "tg_message_ids",
)


@dataclass
class ReviewItem:
    id: int
    resume_id: str
    vacancy_id: int
    state: ItemState
    steps: list[str] = field(default_factory=list)
    step_idx: int = 0
    dry_run: bool = False
    verdict_json: Any = None
    vacancy_json: Any = None
    resume_text: str = ""
    test_json: Any = None
    letter_draft: str | None = None
    letter_final: str | None = None
    answers_draft_json: Any = None
    answers_json: Any = None
    tg_message_ids: Any = None
    created_at: str | None = None
    updated_at: str | None = None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> ReviewItem:
        data = dict(row)
        for name in JSON_FIELDS:
            value = data.get(name)
            if isinstance(value, str):
                data[name] = json.loads(value)
        data["state"] = ItemState(data["state"])
        data["dry_run"] = bool(data.get("dry_run"))
        if data.get("steps") is None:
            data["steps"] = []
        return cls(**data)

    @property
    def current_step(self) -> str | None:
        if 0 <= self.step_idx < len(self.steps):
            return self.steps[self.step_idx]
        return None
