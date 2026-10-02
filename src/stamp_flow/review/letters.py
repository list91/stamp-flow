"""Письмо работодателю не должно нести служебные строки бота.

Как они попадали в письмо: бот присылал черновик одним сообщением «метка резюме · ✉️ Письмо
(черновик) · текст · подсказка про кнопки». Человек копирует сообщение целиком, правит
и присылает обратно — вместе с шапкой. Так письмо и ушло работодателю.

Защита тройная: письмо в боте теперь отдельное «голое» сообщение (render.letter_message),
текст чистится при приёме правки и ещё раз перед отправкой в hh (submit.py).
"""

from __future__ import annotations

import re

# Служебные строки бота: метка резюме («📄 1/4 · Название · 🔎 2/4 · круг 1/2 · 🎯 0.74»),
# заголовок «✉️ Письмо (черновик) — …» и подсказки про кнопки. Признаки узкие, чтобы
# не задеть настоящее письмо: эмодзи или слова сами по себе служебными не считаются.
_SERVICE = (
    re.compile(r"^\s*📄\s*(?:\d+\s*/\s*\d+\s*·|резюме\b)"),
    re.compile(r"^\s*✉️?\s*Письмо\s*(?:\(черновик\).*)?$", re.I),
    re.compile(r"апрув\s*—\s*уйд[её]т\s+как\s+есть", re.I),
    re.compile(r"эдит\s*—\s*пришл", re.I),
    re.compile(r"Пришли\s+свой\s+текст\s+письма", re.I),
)


def _is_service(line: str) -> bool:
    return not line.strip() or any(p.search(line) for p in _SERVICE)


def clean_letter(text: str | None) -> str:
    """Письмо без служебных строк бота сверху и снизу."""
    lines = (text or "").strip().splitlines()
    while lines and _is_service(lines[0]):
        lines.pop(0)
    while lines and _is_service(lines[-1]):
        lines.pop()
    return "\n".join(lines).strip()


def has_service_lines(text: str | None) -> bool:
    """Есть ли в тексте то, что clean_letter убрал бы."""
    return clean_letter(text) != (text or "").strip()
