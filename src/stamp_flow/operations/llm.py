from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import TYPE_CHECKING

from hh_applicant_tool.main import BaseNamespace, BaseOperation
from stamp_flow.review.llm import LLMError, OpencodeSshDriver, create_llm_driver

if TYPE_CHECKING:
    from hh_applicant_tool.main import HHApplicantTool

logger = logging.getLogger(__package__)

CONFIG_EXAMPLE = (
    '{"llm": {"driver": "opencode_ssh", "host": "<ssh-хост>", '
    '"user": "<ssh-пользователь>", "key_file": "~/.ssh/<ключ>", '
    '"model": "<provider>/<model>"}}'
)


class Namespace(BaseNamespace):
    prompt: str | None
    system: str | None
    meta: bool


class Operation(BaseOperation):
    """Запрос к LLM через драйвер из секции `llm` конфига.

    По умолчанию драйвер opencode_ssh: модель OpenCode на удалённом хосте по SSH.
    """

    def setup_parser(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "prompt",
            nargs="?",
            help="Текст запроса. Если не указан — читается из stdin",
        )
        parser.add_argument("-s", "--system", help="Системный промпт")
        parser.add_argument(
            "--meta",
            action="store_true",
            help="Вывести в stderr время, сессию и попытки вызова инструментов",
        )

    def run(self, tool: HHApplicantTool, args: Namespace) -> int | None:
        config = tool.config.get("llm")
        if not config:
            raise ValueError(
                f"В конфиге нет секции 'llm'. Пример: {CONFIG_EXAMPLE}"
            )

        prompt = args.prompt if args.prompt is not None else sys.stdin.read()
        if not prompt.strip():
            raise ValueError("Пустой запрос")

        driver = create_llm_driver(config)
        try:
            if isinstance(driver, OpencodeSshDriver):
                result = driver.run(args.system or "", prompt)
                text = result.text
                meta = {
                    "elapsed": result.elapsed,
                    "attempts": result.attempts,
                    "session_id": result.session_id,
                    "tool_attempts": result.tool_attempts,
                }
            else:
                text = driver.complete(args.system or "", prompt)
                meta = {}
        except LLMError as ex:
            logger.error("LLM: %s", ex)
            return 1

        print(text)
        if args.meta:
            print(json.dumps(meta, ensure_ascii=False), file=sys.stderr)
        return None
