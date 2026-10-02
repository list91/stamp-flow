from __future__ import annotations

import argparse
import logging
from typing import TYPE_CHECKING

from hh_applicant_tool.main import BaseNamespace, BaseOperation
from stamp_flow.review.lock import LockTimeout, hh_lock
from stamp_flow.review.services import make_pacer, make_store, make_window, new_run_id
from stamp_flow.review.submit import Submitter

if TYPE_CHECKING:
    from hh_applicant_tool.main import HHApplicantTool

logger = logging.getLogger(__package__)

RESULT_MARK = "@@REVIEW_SUBMIT@@"


class Namespace(BaseNamespace):
    item_id: int
    work_hours: str | None


class Operation(BaseOperation):
    """Отправить в hh одобренную на ревью вакансию (вызывается review-bot)."""

    def setup_parser(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("item_id", type=int, help="id из review_items")
        parser.add_argument("--work-hours", default=None, help="Окно работы HH:MM-HH:MM")

    def run(self, tool: HHApplicantTool, args: Namespace) -> int:
        store = make_store(tool, new_run_id("submit"))
        item = store.get(args.item_id)
        if item is None:
            print(f"{RESULT_MARK}not_found")
            return 1
        try:
            with hh_lock(tool.config_path):
                result = Submitter(
                    tool, store, make_window(tool, args.work_hours), pacer=make_pacer(tool, store)
                ).submit(item)
        except LockTimeout:
            logger.error("review-submit #%s: lock занят дольше таймаута, отправка отложена", item.id)
            store.log_event("submit_deferred", {"reason": "lock busy"}, item_id=item.id)
            result = "deferred"
        print(f"{RESULT_MARK}{result}")
        return 0
