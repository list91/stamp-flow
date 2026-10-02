"""Карточки ревью для Telegram (HTML parse_mode)"""

from __future__ import annotations

import html
import json
import re
from pathlib import Path
from typing import Any

from .context import format_salary, html_to_text
from .detector import Verdict
from .lock import pass_running
from .questionnaire import Answers, Questionnaire

TG_LIMIT = 4096
# запас под подпись и HTML-разметку подсветки
CHUNK_LIMIT = 3500

STEP_TITLES = {"letter": "письмо", "questionnaire": "анкета"}


def esc(text: Any) -> str:
    return html.escape(str(text or ""), quote=False)


_progress_dir: Path | None = None


def set_progress_dir(path: Any) -> None:
    global _progress_dir
    _progress_dir = Path(path) if path else None


def progress_file() -> Path | None:
    return _progress_dir / "review-progress.json" if _progress_dir else None


def write_progress(data: dict[str, Any] | None) -> None:
    """Прогресс кругов для меток в боте (общий файл: обход и бот — разные процессы)."""
    f = progress_file()
    if not f:
        return
    try:
        if data is None:
            f.unlink(missing_ok=True)
        else:
            f.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def _progress_suffix(rid: str) -> str:
    f = progress_file()
    try:
        data = json.loads(f.read_text(encoding="utf-8")) if f and f.exists() else None
    except (OSError, ValueError):
        return ""
    if not data or rid not in (data.get("done") or {}):
        return ""
    # обход убили/он упал, не убрав файл: счётчик устарел и только путает
    if _progress_dir is not None and not pass_running(_progress_dir):
        return ""
    quota = data.get("quota") or 0
    done = data["done"][rid]
    counter = f"{done}/{quota}" if quota else f"{done}"
    return f" · 🔎 {counter} · круг {data.get('round')}/{data.get('rounds')}"


def resume_tag(resume_id: str | None, titles: dict[str, str]) -> str:
    """«📄 2/4 · Название · 🔎 3/8 · круг 1/2» — резюме, его номер и счётчик вакансий круга."""
    if not resume_id:
        return ""
    rid = str(resume_id).removeprefix("dry:")
    ids = list(titles)
    if rid in titles:
        return f"📄 {ids.index(rid) + 1}/{len(ids)} · {esc(titles[rid])}{_progress_suffix(rid)}"
    return f"📄 резюме {esc(rid[:8])}"


def split_text(text: str, limit: int = CHUNK_LIMIT) -> list[str]:
    """Режет текст по абзацам ➜ строкам ➜ словам, каждый кусок ≤ limit."""
    text = (text or "").strip()
    if not text:
        return []
    chunks: list[str] = []
    current = ""

    def flush() -> None:
        nonlocal current
        if current.strip():
            chunks.append(current.strip())
        current = ""

    def pieces(block: str, seps: list[str]) -> list[str]:
        if len(block) <= limit:
            return [block]
        if not seps:
            return [block[i : i + limit] for i in range(0, len(block), limit)]
        sep, rest = seps[0], seps[1:]
        out: list[str] = []
        for part in block.split(sep):
            out.extend(pieces(part, rest) if len(part) > limit else [part])
        return out

    for paragraph in text.split("\n\n"):
        for piece in pieces(paragraph, ["\n", " "]):
            joiner = "\n\n" if current else ""
            if len(current) + len(joiner) + len(piece) > limit:
                flush()
                joiner = ""
            current += joiner + piece
    flush()
    return chunks


def highlight(text: str, quotes: list[str]) -> str:
    """Экранирует текст и подсвечивает цитаты инъекций."""
    escaped = esc(text)
    for quote in sorted({q for q in quotes if q and len(q) >= 3}, key=len, reverse=True):
        pattern = re.escape(esc(quote)).replace(r"\ ", r"\s+")
        escaped = re.sub(
            pattern,
            lambda m: f"<u><b>{m.group(0)}</b></u>",
            escaped,
            flags=re.IGNORECASE,
        )
    return escaped


def header(item_id: int, vacancy: dict[str, Any], resume_title: str, steps: list[str], dry_run: bool) -> str:
    employer = (vacancy.get("employer") or {}).get("name") or "—"
    url = vacancy.get("alternate_url") or f"https://hh.ru/vacancy/{vacancy.get('id')}"
    route = " ➜ ".join(STEP_TITLES.get(s, s) for s in steps) or "—"
    lines = [
        f"🆕 <b>Ревью #{item_id}</b>" + (" <i>(dry-run)</i>" if dry_run else ""),
        f"🏢 {esc(employer)}",
        f'💼 <a href="{esc(url)}">{esc(vacancy.get("name"))}</a>',
        f"💰 {esc(format_salary(vacancy))}",
    ]
    if resume_title:
        lines.append(f"📄 Резюме: {esc(resume_title)}")
    lines.append(f"🧭 Шаги: {esc(route)}")
    return "\n".join(lines)


def vacancy_messages(vacancy: dict[str, Any], quotes: list[str]) -> list[str]:
    parts = []
    skills = [s.get("name") for s in vacancy.get("key_skills") or [] if s.get("name")]
    if skills:
        parts.append("Ключевые навыки: " + ", ".join(skills))
    description = html_to_text(vacancy.get("description") or vacancy.get("branded_description"))
    if description:
        parts.append(description)
    chunks = split_text("\n\n".join(parts))
    total = len(chunks)
    return [
        f"📃 <b>Вакансия {i}/{total}</b>\n\n{highlight(chunk, quotes)}"
        for i, chunk in enumerate(chunks, 1)
    ]


def verdict_message(verdict: Verdict) -> str | None:
    if not verdict.suspicious:
        return None
    lines = ["⚠️ <b>Похоже на промпт-инъекцию</b>"]
    for f in verdict.fragments:
        quote = f" «{esc(f.quote)}»" if f.quote else ""
        lines.append(f"• [{esc(f.field)}]{quote} — {esc(f.reason)}")
    if verdict.error:
        lines.append(f"• {esc(verdict.error)} ➜ на ревью на всякий случай")
    lines.append("\nПисьмо ниже сгенерировано без выполнения этих инструкций.")
    return "\n".join(lines)


def card_messages(
    item_id: int,
    vacancy: dict[str, Any],
    resume_title: str,
    steps: list[str],
    verdict: Verdict,
    dry_run: bool = False,
) -> list[str]:
    """Шапка + полный текст вакансии батчами + вердикт (без шага и кнопок)."""
    quotes = [f.quote for f in verdict.fragments if f.source != "tools"]
    messages = [header(item_id, vacancy, resume_title, steps, dry_run)]
    messages += vacancy_messages(vacancy, quotes)
    if msg := verdict_message(verdict):
        messages.append(msg)
    return messages


def letter_message(letter: str, awaiting_edit: bool = False) -> list[str]:
    """Шапка с подсказкой отдельно, письмо — голым текстом отдельными сообщениями.

    Так «Копировать» на сообщении с письмом даёт ровно то, что уйдёт в hh: ни метки резюме,
    ни заголовка, ни подсказки в нём нет (раньше их копировали вместе с текстом, и они
    уходили работодателю).
    """
    body = split_text(letter) or ["—"]
    hint = (
        "✏️ Пришли свой текст письма одним сообщением — уйдёт он."
        if awaiting_edit
        else "✅ апрув — уйдёт как есть · ✏️ эдит — пришлёшь свой текст · ❌ реджект — пропуск вакансии"
    )
    head = f"✉️ <b>Письмо</b> (черновик) — ниже ровно тот текст, что уйдёт в hh\n<i>{hint}</i>"
    return [head] + [esc(b) for b in body]


def questionnaire_message(
    questionnaire: Questionnaire,
    answers: Answers,
    changed: set[int] | None = None,
    notes: list[str] | None = None,
) -> list[str]:
    title = "📝 <b>Анкета</b>"
    if changed:
        title += f" — изменено: {', '.join(str(n) for n in sorted(changed))}"
    text = questionnaire.render(answers, changed)
    chunks = split_text(text) or ["—"]
    messages = [f"{title}\n\n{esc(chunks[0])}"] + [esc(c) for c in chunks[1:]]
    tail = []
    if notes:
        tail.append("⚠️ " + esc("; ".join(notes)))
    tail.append(
        "<i>Правки: «N: текст» — ответ заменится дословно (для выбора — номера вариантов, "
        "например «N: 1,3»). Любой другой текст — промпт для LLM. "
        "✅ апрув — в любой момент · ❌ реджект — пропуск вакансии</i>"
    )
    messages[-1] += "\n\n" + "\n".join(tail)
    return messages


def keyboard(item_id: int, step_idx: int, step: str) -> dict[str, Any]:
    def button(text: str, action: str) -> dict[str, str]:
        return {"text": text, "callback_data": f"r:{item_id}:{step_idx}:{action}"}

    if step == "letter":
        row = [button("✅ Апрув", "ap"), button("✏️ Эдит", "ed"), button("❌ Реджект", "rj")]
    else:
        row = [button("✅ Апрув", "ap"), button("❌ Реджект", "rj")]
    return {"inline_keyboard": [row]}


def retry_keyboard(item_id: int, step_idx: int) -> dict[str, Any]:
    """Черновик не получился (модель не ответила): повторить или пропустить вакансию."""
    return {
        "inline_keyboard": [[
            {"text": "🔁 Повторить", "callback_data": f"r:{item_id}:{step_idx}:rt"},
            {"text": "❌ Реджект", "callback_data": f"r:{item_id}:{step_idx}:rj"},
        ]]
    }


def parse_callback(data: str) -> tuple[int, int, str] | None:
    m = re.fullmatch(r"r:(\d+):(\d+):(ap|ed|rj|rt)", data or "")
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), m.group(3)
