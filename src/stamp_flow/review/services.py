"""Сборка зависимостей ревью из HHApplicantTool (config, БД, LLM)."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from .context import load_style
from .llm import create_llm_driver
from .schedule import WorkHours
from .store import ReviewStore

if TYPE_CHECKING:
    from hh_applicant_tool.main import HHApplicantTool


def review_config(tool: HHApplicantTool) -> dict[str, Any]:
    return tool.config.get("review") or {}


def new_run_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def make_store(tool: HHApplicantTool, run_id: str | None = None) -> ReviewStore:
    tool.storage  # noqa: B018 — применяет schema.sql (таблицы ревью)
    return ReviewStore(tool.db, log_dir=tool.config_path / "review-log", run_id=run_id)


def make_llm(tool: HHApplicantTool, store: ReviewStore | None = None) -> Any:
    config = tool.config.get("llm") or {}
    if not config:
        raise ValueError('В config.json нет секции "llm" (драйвер модели)')

    def log_hook(event: str, payload: dict[str, Any]) -> None:
        if store is not None:
            store.log_event(event, payload)

    return create_llm_driver(config, log_hook=log_hook)


def make_window(tool: HHApplicantTool, cli_value: str | None = None) -> WorkHours | None:
    return WorkHours.parse(cli_value or review_config(tool).get("work_hours"))


def run_conf(tool: HHApplicantTool) -> dict[str, Any] | None:
    """conf.json рядом с run.py (единственный источник параметров обхода) или None.

    Без него отправка из бота (review-submit) жила бы по другим числам, чем обход:
    лимит 10 в час из умолчаний вместо 20 из conf.json — и одобренное письмо
    зависало бы «до следующего обхода», хотя обход давно разрешает больше.
    """
    import importlib.util
    from pathlib import Path

    root = Path(tool.config_path).resolve().parent
    run_py = root / "run.py"
    if not run_py.exists() or not (root / "conf.json").exists():
        return None
    try:
        spec = importlib.util.spec_from_file_location("hh_run_conf", run_py)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.load_conf(root / "conf.json")
    except Exception:  # noqa: BLE001 — битый conf.json не должен ронять отправку
        return None


def make_pacer(tool: HHApplicantTool, store: ReviewStore) -> Any:
    from .pacing import DEFAULT_MAX_PER_HOUR, Pacer, parse_range

    cfg = review_config(tool)
    conf = run_conf(tool)
    if conf:
        delay = conf["timeouts_sec"]["apply_delay"]
        max_per_hour = conf["limits"]["max_per_hour"]
    else:
        delay = cfg.get("apply_delay")
        max_per_hour = cfg.get("max_per_hour", DEFAULT_MAX_PER_HOUR)
    return Pacer(store, delay=parse_range(delay), max_per_hour=max_per_hour)


def make_style(tool: HHApplicantTool) -> str:
    return load_style(tool.config_path, review_config(tool).get("style_file", "style.md"))
