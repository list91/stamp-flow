"""Отправка одобренной вакансии в hh."""

from __future__ import annotations

import logging
import random
from datetime import datetime
from urllib.parse import quote
from typing import TYPE_CHECKING, Any

from hh_applicant_tool.api.errors import (
    ApiError,
    CaptchaRequired,
    LimitExceeded,
    Redirect,
    ResourceNotFound,
)
from .letters import clean_letter, has_service_lines
from .models import ItemState, ReviewItem
from .questionnaire import Questionnaire
from .schedule import WorkHours, is_open
from .store import ReviewStore

if TYPE_CHECKING:
    from hh_applicant_tool.main import HHApplicantTool

logger = logging.getLogger(__package__)

# Сколько раз пробуем отправить одну вакансию, прежде чем бросить
MAX_SUBMIT_FAILURES = 3


class SubmitResult:
    SUBMITTED = "submitted"
    DRY_RUN = "dry_run"
    DEFERRED = "deferred"
    RETRY = "retry"
    LIMIT = "limit"
    CAPTCHA = "captcha"
    HOURLY_LIMIT = "hourly_limit"
    STALE = "stale"
    REQUEUED = "requeued"
    GAVE_UP = "gave_up"


def _make_operation(tool: HHApplicantTool) -> Any:
    from stamp_flow.operations.apply_review import Operation

    op = Operation()
    op.tool = tool
    op.cover_letter_ai = None
    return op


class Submitter:
    def __init__(
        self,
        tool: HHApplicantTool,
        store: ReviewStore,
        window: WorkHours | None = None,
        operation: Any | None = None,
        now=datetime.now,
        pacer: Any | None = None,
    ):
        self.tool = tool
        self.store = store
        self.window = window
        self.op = operation or _make_operation(tool)
        self.now = now
        self.pacer = pacer

    def failures(self, item: ReviewItem) -> int:
        return len(
            [e for e in self.store.events(item_id=item.id) if e["type"] == "submit_failed"]
        )

    def submit(self, item: ReviewItem) -> str:
        item = self.store.get(item.id)
        if item.state not in (ItemState.APPROVED, ItemState.FAILED_RETRYABLE):
            raise ValueError(f"#{item.id}: нельзя отправить из состояния {item.state}")

        if not item.dry_run and not is_open(self.window, self.now()):
            self.store.log_event(
                "submit_deferred",
                {"window": str(self.window)},
                item_id=item.id,
                vacancy_id=item.vacancy_id,
            )
            return SubmitResult.DEFERRED

        if not item.dry_run and self.pacer is not None and self.pacer.hourly_limit_reached():
            self.store.log_event(
                "submit_deferred",
                {"reason": "hourly_limit", "max_per_hour": self.pacer.max_per_hour},
                item_id=item.id,
                vacancy_id=item.vacancy_id,
            )
            return SubmitResult.HOURLY_LIMIT

        if self.failures(item) >= MAX_SUBMIT_FAILURES:
            self.store.log_event(
                "submit_gave_up", {}, item_id=item.id, vacancy_id=item.vacancy_id
            )
            return SubmitResult.GAVE_UP

        item = self.store.set_state(item, ItemState.SUBMITTING)
        ctx = {"item_id": item.id, "resume_id": item.resume_id, "vacancy_id": item.vacancy_id}

        if item.dry_run:
            self.store.log_event(
                "submit_skipped_dry_run",
                {"letter": item.letter_final, "answers": item.answers_json},
                **ctx,
            )
            self.store.set_state(item, ItemState.SUBMITTED, reason="dry_run")
            return SubmitResult.DRY_RUN

        try:
            return self._submit_live(item, ctx)
        except LimitExceeded as ex:
            return self._retry(item, ctx, f"лимит откликов hh: {ex}", limit=True)
        except ResourceNotFound:
            self.store.set_state(item, ItemState.STALE, reason="вакансия удалена")
            return SubmitResult.STALE
        except CaptchaRequired as ex:
            self.store.log_event("captcha_required", {"captcha_url": ex.captcha_url}, **ctx)
            self._retry(item, ctx, "hh требует капчу", limit=True)
            return SubmitResult.CAPTCHA
        except (ApiError, ValueError, OSError) as ex:
            return self._retry(item, ctx, f"{type(ex).__name__}: {ex}")

    def _submit_live(self, item: ReviewItem, ctx: dict[str, Any]) -> str:
        vacancy = self.tool.api_client.get(f"/vacancies/{item.vacancy_id}")
        if vacancy.get("archived"):
            self.store.set_state(item, ItemState.STALE, reason="vacancy archived")
            return SubmitResult.STALE

        letter = item.letter_final or ""
        if letter and has_service_lines(letter):
            # последний рубеж: служебные строки бота (метка резюме, «✉️ Письмо (черновик)»,
            # подсказка) работодателю уходить не должны, как бы они ни попали в текст
            cleaned = clean_letter(letter)
            self.store.log_event("letter_cleaned", {"before": letter, "after": cleaned}, **ctx)
            if not cleaned and "letter" in item.steps:
                return self._retry(item, ctx, "письмо пустое после удаления служебных строк")
            letter = cleaned

        if "questionnaire" in item.steps:
            response_url, test = self.op._load_vacancy_test(item.vacancy_id)
            q_old = Questionnaire.from_test(item.test_json or {})
            q_new = Questionnaire.from_test(test)
            if q_old.task_ids != q_new.task_ids:
                self.store.set_state(item, ItemState.STALE, reason="questionnaire changed")
                self.store.set_state(item, ItemState.QUEUED, reason="re-review new questionnaire")
                self.store.update(
                    item,
                    test_json=test,
                    vacancy_json=vacancy,
                    step_idx=0,
                    answers_draft_json=None,
                    answers_json=None,
                )
                return SubmitResult.REQUEUED
            form = q_new.to_form(item.answers_json or {})
            self.store.log_event(
                "submit_request", {"kind": "questionnaire", "letter": letter, "form": form}, **ctx
            )
            result = self.op._submit_vacancy_test(
                vacancy_id=item.vacancy_id,
                resume_hash=item.resume_id,
                letter=letter,
                test_data=test,
                answers=form,
                response_url=response_url,
            )
            self.store.log_event("submit_response", {"result": result}, **ctx)
            if isinstance(result, dict) and result.get("success") == "true":
                self.store.set_state(item, ItemState.SUBMITTED)
                return SubmitResult.SUBMITTED
            captcha = result.get("hhcaptcha") if isinstance(result, dict) else None
            if isinstance(captcha, dict) and captcha.get("isBot") and captcha.get("captchaState"):
                # веб-форма анкеты отвечает не CaptchaRequired, а {"hhcaptcha": {...}}
                url = f"https://hh.ru/account/captcha?state={quote(str(captcha['captchaState']), safe='')}"
                self.store.log_event("captcha_required", {"captcha_url": url}, **ctx)
                self._retry(item, ctx, "hh просит капчу (анкета)", limit=True)
                return SubmitResult.CAPTCHA
            error = result.get("error") if isinstance(result, dict) else None
            if error == "negotiations-limit-exceeded":
                return self._retry(item, ctx, "лимит откликов hh", limit=True)
            return self._retry(item, ctx, f"анкета не принята: {error or result}")

        params = {"resume_id": item.resume_id, "vacancy_id": item.vacancy_id, "message": letter}
        self.store.log_event("submit_request", {"kind": "negotiation", **params}, **ctx)
        try:
            res = self.tool.api_client.post("/negotiations", params, delay=random.uniform(1, 3))
        except Redirect:
            self.store.log_event("submit_response", {"redirect": True}, **ctx)
            self.store.set_state(item, ItemState.STALE, reason="отклик только через форму работодателя")
            return SubmitResult.STALE
        self.store.log_event("submit_response", {"result": res}, **ctx)
        self.store.set_state(item, ItemState.SUBMITTED)
        return SubmitResult.SUBMITTED

    def _retry(
        self, item: ReviewItem, ctx: dict[str, Any], error: str, limit: bool = False
    ) -> str:
        (logger.warning if limit else logger.error)("review-submit #%s: %s", item.id, error)
        # лимит hh — не вина вакансии, в счётчик неудач не идёт
        self.store.log_event("submit_limit" if limit else "submit_failed", {"error": error}, **ctx)
        self.store.set_state(self.store.get(item.id), ItemState.FAILED_RETRYABLE, reason=error)
        return SubmitResult.LIMIT if limit else SubmitResult.RETRY
