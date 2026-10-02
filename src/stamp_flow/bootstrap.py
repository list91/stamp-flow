"""Подключение stamp-flow к hh-applicant-tool без правки его кода.

Апстрим собирает команды из каталога собственного пакета `operations` и
настраивает логгер внутри `HHApplicantTool.run`. Здесь, до запуска CLI:

1. каталог `_ext` с нашими командами добавляется в `operations.__path__` и в
   список, который просматривает `main.iter_modules`, поэтому апстрим видит
   `apply-review`, `review-bot`, `review-submit`, `review-demo`, `llm`;
2. сразу после настройки логгера включаются Telegram-алерты об ошибках процесса.

`install()` идемпотентна.
"""

from __future__ import annotations

import itertools
import logging
import pkgutil
from pathlib import Path
from typing import Any

EXT_DIR = Path(__file__).resolve().parent / "_ext"

_installed = False


def install() -> None:
    global _installed
    if _installed:
        return
    _installed = True

    from hh_applicant_tool import main as upstream_main
    from hh_applicant_tool import operations as upstream_ops

    ext = str(EXT_DIR)
    if ext not in upstream_ops.__path__:
        upstream_ops.__path__.append(ext)

    original_iter = upstream_main.iter_modules

    def iter_with_ext(paths: Any = None, prefix: str = ""):
        found = original_iter(paths, prefix)
        # только при сборе списка операций: путь к их каталогу заканчивается на `operations`
        if paths and Path(list(paths)[0]).name == upstream_main.OPERATIONS:
            return itertools.chain(found, pkgutil.iter_modules([ext], prefix))
        return found

    upstream_main.iter_modules = iter_with_ext

    # Алерты ставим после того, как апстрим настроил логгер: до этого момента
    # неизвестен каталог профиля, а значит и config.json с токеном бота.
    original_run = upstream_main.HHApplicantTool.run
    original_setup = upstream_main.setup_logger
    current: dict[str, Any] = {}

    def run(self, argv=None):
        current["tool"] = self
        return original_run(self, argv)

    def setup_logger(logger: logging.Logger, *args: Any, **kwargs: Any):
        result = original_setup(logger, *args, **kwargs)
        tool = current.get("tool")
        if tool is not None:
            from stamp_flow.review.alerts import install as install_alerts

            install_alerts(tool, logger)
        # Логгеры stamp_flow.* не потомки логгера апстрима: даём им те же хэндлеры
        # (файл лога, Telegram-алерты) и уровень, чтобы -v/-vv и ошибки работали и для них.
        own = logging.getLogger("stamp_flow")
        own.setLevel(logger.level)
        for handler in logger.handlers:
            if handler not in own.handlers:
                own.addHandler(handler)
        return result

    upstream_main.HHApplicantTool.run = run
    upstream_main.setup_logger = setup_logger
