"""Точка входа: `python -m stamp_flow ...` или `stamp-flow ...`.

Это CLI hh-applicant-tool с дополнительными командами stamp-flow
(`apply-review`, `review-bot`, `review-submit`, `review-demo`, `llm`).
"""

from __future__ import annotations

import sys
from collections.abc import Sequence


def main(argv: Sequence[str] | None = None) -> int | None:
    from stamp_flow.bootstrap import install

    install()

    from hh_applicant_tool.main import main as upstream_main

    return upstream_main(argv)


if __name__ == "__main__":
    sys.exit(main())
