"""Тексты для LLM: резюме, вакансия, style.md."""

from __future__ import annotations

import html
import json
import re
from pathlib import Path
from typing import Any


def build_resume_text(full_resume: dict[str, Any]) -> str:
    """Полное резюме (`GET /resumes/{id}`) в текст для LLM: должность, о себе, навыки, опыт."""
    lines: list[str] = []

    if title := full_resume.get("title"):
        lines.append(f"Должность: {title}")

    if about := full_resume.get("skills"):
        lines += ["", "## О себе", about]

    if skill_set := full_resume.get("skill_set"):
        lines += ["", "## Навыки", ", ".join(skill_set)]

    if full_resume.get("experience"):
        lines += ["", "## Опыт работы"]
        for job in full_resume["experience"]:
            period_end = job.get("end") or "по настоящее время"
            lines += [
                "",
                f"- {job.get('company') or 'Не указано'}",
                f"  Должность: {job.get('position') or 'Не указано'}",
                f"  Период: {job.get('start') or ''} - {period_end}",
            ]
            if details := job.get("description"):
                lines += ["  Описание:", f"  {details}"]

    return "\n".join(lines)


_BLOCK_END = re.compile(
    r"</(p|div|h[1-6]|ul|ol|table|tr|blockquote)\s*>", re.IGNORECASE
)
_LI_START = re.compile(r"<li[^>]*>", re.IGNORECASE)
_BR = re.compile(r"<br\s*/?>", re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>")
_HIDDEN_BLOCK = re.compile(
    r"<(script|style)[^>]*>.*?</\1\s*>", re.IGNORECASE | re.DOTALL
)


def html_to_text(content: str | None) -> str:
    """HTML описания вакансии → текст с абзацами и пунктами списков."""
    if not content:
        return ""
    text = _HIDDEN_BLOCK.sub("", content)
    text = _BR.sub("\n", text)
    text = _LI_START.sub("\n• ", text)
    text = _BLOCK_END.sub("\n\n", text)
    text = _TAG.sub("", text)
    text = html.unescape(text).replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
    text = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def format_salary(vacancy: dict[str, Any]) -> str:
    salary = vacancy.get("salary_range") or vacancy.get("salary") or {}
    if not salary:
        return "не указана"
    lo, hi = salary.get("from"), salary.get("to")
    currency = salary.get("currency") or ""
    if lo and hi:
        value = f"{lo:,}–{hi:,}".replace(",", " ")
    elif lo:
        value = f"от {lo:,}".replace(",", " ")
    elif hi:
        value = f"до {hi:,}".replace(",", " ")
    else:
        return "не указана"
    gross = salary.get("gross")
    suffix = " до вычета" if gross else (" на руки" if gross is False else "")
    return f"{value} {currency}{suffix}".strip()


def _name(value: Any) -> str:
    return value.get("name", "") if isinstance(value, dict) else ""


def build_vacancy_text(vacancy: dict[str, Any]) -> str:
    """Полная вакансия (`GET /vacancies/{id}`) → текст (для LLM и карточки)."""
    parts: list[str] = []
    if vacancy.get("name"):
        parts.append(f"Вакансия: {vacancy['name']}")
    if employer := _name(vacancy.get("employer")):
        parts.append(f"Компания: {employer}")
    parts.append(f"Зарплата: {format_salary(vacancy)}")
    for label, key in (
        ("Город", "area"),
        ("Опыт", "experience"),
        ("График", "schedule"),
        ("Занятость", "employment"),
    ):
        if value := _name(vacancy.get(key)):
            parts.append(f"{label}: {value}")
    formats = [
        _name(f) for f in vacancy.get("work_format") or [] if _name(f)
    ]
    if formats:
        parts.append(f"Формат: {', '.join(formats)}")
    skills = [
        s.get("name") for s in vacancy.get("key_skills") or [] if s.get("name")
    ]
    if skills:
        parts.append(f"Ключевые навыки: {', '.join(skills)}")
    description = html_to_text(
        vacancy.get("description") or vacancy.get("branded_description")
    )
    if description:
        parts.append("")
        parts.append(description)
    return "\n".join(parts)


def load_style(config_path: Path, style_file: str | None = "style.md") -> str:
    """Весь style.md как есть — LLM сама берёт нужное."""
    if not style_file:
        return ""
    path = Path(style_file)
    if not path.is_absolute():
        path = Path(config_path) / path
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def extract_json(text: str) -> Any:
    """Достаёт первый JSON-объект/массив из ответа модели."""
    text = _FENCE.sub("", (text or "").strip())
    decoder = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch in "{[":
            try:
                value, _ = decoder.raw_decode(text[i:])
                return value
            except ValueError:
                continue
    raise ValueError("в ответе модели нет JSON")
