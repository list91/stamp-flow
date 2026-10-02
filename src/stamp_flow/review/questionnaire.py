"""Анкета (тест) вакансии: модель, ответы, ручные правки `N: текст`, diff.

Ответы хранятся JSON-совместимо: {"1": {"text": "..."}, "2": {"choice": ["id"]}}
(ключ — номер вопроса с 1, choice — id вариантов hh).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .context import html_to_text

Answers = dict[str, dict[str, Any]]


@dataclass
class Option:
    n: int
    id: str
    text: str


@dataclass
class Question:
    n: int
    task_id: str
    text: str
    options: list[Option] = field(default_factory=list)
    multiple: bool = False

    @property
    def is_choice(self) -> bool:
        return bool(self.options)

    def option_by_n(self, n: int) -> Option | None:
        return next((o for o in self.options if o.n == n), None)


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() == "true"


@dataclass
class Questionnaire:
    questions: list[Question]
    name: str = ""
    description: str = ""

    @classmethod
    def from_test(cls, test: dict[str, Any]) -> Questionnaire:
        questions = []
        for n, task in enumerate(test.get("tasks") or [], 1):
            options = [
                Option(n=i, id=str(s["id"]), text=html_to_text(s.get("text", "")))
                for i, s in enumerate(task.get("candidateSolutions") or [], 1)
            ]
            questions.append(
                Question(
                    n=n,
                    task_id=str(task["id"]),
                    text=html_to_text(task.get("description") or ""),
                    options=options,
                    multiple=_truthy(task.get("multiple")),
                )
            )
        return cls(
            questions=questions,
            name=test.get("name") or "",
            description=html_to_text(test.get("description") or ""),
        )

    def get(self, n: int) -> Question | None:
        return next((q for q in self.questions if q.n == n), None)

    @property
    def task_ids(self) -> list[str]:
        return [q.task_id for q in self.questions]

    # -------------------------------------------------------------- форма hh

    def to_form(self, answers: Answers) -> dict[str, Any]:
        """Ответы ➜ поля формы hh (`task_<id>` / `task_<id>_text`)."""
        form: dict[str, Any] = {}
        for q in self.questions:
            answer = answers.get(str(q.n)) or {}
            if q.is_choice:
                choice = [str(c) for c in answer.get("choice") or []]
                if not choice:
                    continue
                form[f"task_{q.task_id}"] = choice if q.multiple else choice[0]
            else:
                text = (answer.get("text") or "").strip()
                if text:
                    form[f"task_{q.task_id}_text"] = text
        return form

    def missing(self, answers: Answers) -> list[int]:
        """Номера вопросов без ответа."""
        return [n for n in (q.n for q in self.questions) if not _has_answer(answers.get(str(n)))]

    # ---------------------------------------------------------------- рендер

    def answer_text(self, q: Question, answer: dict[str, Any] | None) -> str:
        answer = answer or {}
        if q.is_choice:
            chosen = {str(c) for c in answer.get("choice") or []}
            picked = [o for o in q.options if o.id in chosen]
            return ", ".join(f"{o.n}) {o.text}" for o in picked) or "—"
        return (answer.get("text") or "").strip() or "—"

    def render(self, answers: Answers, changed: set[int] | None = None) -> str:
        changed = changed or set()
        blocks = []
        for q in self.questions:
            mark = "✏️ " if q.n in changed else ""
            lines = [f"{mark}{q.n}. {q.text}"]
            if q.is_choice:
                kind = "несколько вариантов" if q.multiple else "один вариант"
                lines.append(
                    f"   варианты ({kind}): "
                    + "; ".join(f"{o.n}) {o.text}" for o in q.options)
                )
            answer = self.answer_text(q, answers.get(str(q.n)))
            warn = " ⚠️ нужен ответ" if answer == "—" else ""
            lines.append(f"   ➜ {answer}{warn}")
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks)

    def for_llm(self) -> str:
        """Описание анкеты для промпта генерации."""
        blocks = []
        for q in self.questions:
            if q.is_choice:
                kind = "можно несколько" if q.multiple else "один вариант"
                opts = "\n".join(f"   {o.n}) {o.text}" for o in q.options)
                blocks.append(f"{q.n}. {q.text}\n   [выбор, {kind}]\n{opts}")
            else:
                blocks.append(f"{q.n}. {q.text}\n   [свободный ответ]")
        return "\n\n".join(blocks)

    def answers_for_llm(self, answers: Answers) -> str:
        return "\n".join(
            f"{q.n}. {self.answer_text(q, answers.get(str(q.n)))}"
            for q in self.questions
        )

    # ----------------------------------------------------- ответы модели

    def parse_llm_answers(self, data: Any) -> tuple[Answers, list[str]]:
        """JSON модели ➜ ответы. Возвращает (ответы, ошибки)."""
        items = data.get("answers") if isinstance(data, dict) else data
        if not isinstance(items, list):
            raise ValueError("ожидался список answers")
        answers: Answers = {}
        errors: list[str] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                n = int(item.get("n"))
            except (TypeError, ValueError):
                continue
            q = self.get(n)
            if q is None:
                errors.append(f"нет вопроса №{n}")
                continue
            if q.is_choice:
                raw = item.get("choice")
                raw = raw if isinstance(raw, list) else [raw]
                ids = []
                for value in raw:
                    opt = _resolve_option(q, value)
                    if opt is None:
                        errors.append(f"№{n}: нет варианта {value!r}")
                    else:
                        ids.append(opt.id)
                if ids:
                    answers[str(n)] = {"choice": ids if q.multiple else ids[:1]}
            else:
                text = str(item.get("text") or "").strip()
                if text:
                    answers[str(n)] = {"text": text}
        return answers, errors


def _has_answer(answer: dict[str, Any] | None) -> bool:
    if not answer:
        return False
    return bool(answer.get("choice")) or bool((answer.get("text") or "").strip())


def _resolve_option(q: Question, value: Any) -> Option | None:
    if isinstance(value, int) or (isinstance(value, str) and value.strip().isdigit()):
        return q.option_by_n(int(value))
    text = str(value or "").strip().casefold()
    return next((o for o in q.options if o.text.casefold() == text), None)


# ------------------------------------------------------ ручные правки N: текст

_NUMBERED = re.compile(r"^\s*(\d{1,3})\s*[:.)]\s?(.*)$")


def parse_manual_edits(message: str) -> dict[int, str] | None:
    """`3: текст` (по строке на правку) ➜ {3: "текст"}.

    Первая непустая строка должна начинаться с номера, иначе это не ручная
    правка, а промпт для LLM (возвращается None). Строки без номера —
    продолжение предыдущего ответа.
    """
    edits: dict[int, list[str]] = {}
    current: int | None = None
    for line in (message or "").splitlines():
        m = _NUMBERED.match(line)
        if m:
            current = int(m.group(1))
            edits[current] = [m.group(2).strip()]
        elif current is None:
            if line.strip():
                return None
        else:
            edits[current].append(line.rstrip())
    if not edits:
        return None
    return {n: "\n".join(lines).strip() for n, lines in edits.items()}


def apply_manual_edits(
    questionnaire: Questionnaire, answers: Answers, edits: dict[int, str]
) -> tuple[Answers, set[int], list[str]]:
    """Дословная замена ответов. Для вопросов с вариантами — номера `2` / `1,3`
    или точный текст варианта."""
    result = {k: dict(v) for k, v in answers.items()}
    changed: set[int] = set()
    errors: list[str] = []
    for n, value in edits.items():
        q = questionnaire.get(n)
        if q is None:
            errors.append(f"нет вопроса №{n}")
            continue
        if not value:
            errors.append(f"№{n}: пустой ответ")
            continue
        if q.is_choice:
            parts = [p.strip() for p in re.split(r"[,;]", value) if p.strip()]
            if not q.multiple:
                parts = [value.strip()] if not value.strip().isdigit() else parts[:1]
            options = [_resolve_option(q, p) for p in parts]
            if not options or any(o is None for o in options):
                errors.append(
                    f"№{n}: укажи номер варианта ("
                    + ", ".join(f"{o.n}) {o.text}" for o in q.options)
                    + ")"
                )
                continue
            result[str(n)] = {"choice": [o.id for o in options]}
        else:
            result[str(n)] = {"text": value}
        changed.add(n)
    return result, changed, errors


def diff_answers(old: Answers, new: Answers) -> set[int]:
    keys = set(old) | set(new)
    return {int(k) for k in keys if (old.get(k) or {}) != (new.get(k) or {})}
