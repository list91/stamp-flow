"""Подключение stamp-flow к апстриму без моков: команды, импорты, запуск через свой модуль."""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock

SRC = Path(__file__).resolve().parent.parent / "src" / "stamp_flow"
COMMANDS = {"apply-review", "review-bot", "review-submit", "review-demo", "llm"}


def test_bootstrap_registers_our_commands_in_upstream_cli():
    from stamp_flow.bootstrap import install

    install()
    from hh_applicant_tool.main import HHApplicantTool

    parser = HHApplicantTool()._parser
    choices = set(parser._subparsers._group_actions[0].choices)
    assert COMMANDS <= choices
    assert "apply-vacancies" in choices  # штатные команды апстрима на месте


def test_every_ext_shim_exposes_operation():
    import importlib

    for shim in (SRC / "_ext").glob("[!_]*.py"):
        module = importlib.import_module(f"stamp_flow.operations.{shim.stem}")
        assert hasattr(module, "Operation"), shim.name


def test_submitter_builds_default_operation_without_mocks():
    from stamp_flow.operations.apply_review import Operation
    from stamp_flow.review.submit import Submitter, _make_operation

    tool = MagicMock()
    assert isinstance(_make_operation(tool), Operation)
    assert isinstance(Submitter(tool, MagicMock()).op, Operation)


def test_no_legacy_cli_module_invocations():
    """Процессы запускаем только через `-m stamp_flow`: у апстримного CLI наших команд нет."""
    pattern = re.compile(r"""["']-m["']\s*,\s*["']hh_applicant_tool["']|-m\s+hh_applicant_tool""")
    root = SRC.parent.parent
    offenders = [
        str(p.relative_to(root))
        for p in [*SRC.rglob("*.py"), root / "run.py"]
        if pattern.search(p.read_text(encoding="utf-8"))
    ]
    assert offenders == []


def test_logger_mirrors_upstream_handlers(tmp_path):
    import logging

    from stamp_flow import bootstrap

    bootstrap.install()
    from hh_applicant_tool import main as upstream_main

    upstream = logging.getLogger("hh_applicant_tool")
    own = logging.getLogger("stamp_flow")
    handler = logging.NullHandler()
    upstream.addHandler(handler)
    try:
        upstream_main.HHApplicantTool  # noqa: B018
        upstream_main.setup_logger(upstream, logging.DEBUG, tmp_path / "log.txt")
        assert handler in own.handlers
        assert own.level == upstream.level
    finally:
        upstream.removeHandler(handler)
        own.removeHandler(handler)