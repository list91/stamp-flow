"""Драйвер ревью: карточки в Telegram ➜ решения пользователя.

Поток без очереди: обход стоит на вакансии, пока её карточку не решат. Строго по одной. Письмо: апрув / эдит / реджект. Анкета: промпт в LLM
или ручная правка `N: текст`, апрув и реджект в любой момент. Реджект на любом
шаге пропускает всю вакансию.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Protocol

from . import render
from .context import build_vacancy_text
from .detector import Verdict
from .generator import GenerationError, Generator
from .letters import clean_letter
from .models import ItemState, ReviewItem
from .questionnaire import (
    Questionnaire,
    apply_manual_edits,
    diff_answers,
    parse_manual_edits,
)
from .store import ReviewStore, TransitionError

logger = logging.getLogger(__package__)

OFFSET_KEY = "review.telegram.offset"
KEYBOARD_KEY = "review.telegram.keyboard_msg"

HELP = (
    "Команды: /current — повторить текущую карточку, /queue — что открыто.\n"
    "Письмо: ✅ апрув / ✏️ эдит / ❌ реджект.\n"
    "Анкета: «N: текст» — ручная правка, любой другой текст — промпт для LLM."
)


class Chat(Protocol):
    def send_message(self, chat_id: int | str, text: str, reply_markup: dict | None = None) -> int: ...
    def remove_keyboard(self, chat_id: int | str, message_id: int) -> None: ...
    def answer_callback(self, callback_id: str, text: str | None = None) -> None: ...


class ReviewBot:
    def __init__(
        self,
        store: ReviewStore,
        chat: Chat,
        chat_id: int | str,
        generator: Generator,
        submit: Callable[[ReviewItem], str],
        settings: Any | None = None,
        resume_titles: dict[str, str] | None = None,
        notifier: Any | None = None,
        captcha: Any | None = None,
        waits: Any | None = None,
        checks: Any | None = None,
        attention: Any | None = None,
    ):
        self.waits = waits
        self.checks = checks
        self.attention = attention  # бот уведомлений (отдельный токен): ревью / капча / долгая пауза
        self.store = store
        self.chat = chat
        self.chat_id = chat_id
        self.generator = generator
        self.submit = submit
        self.settings = settings
        self.resume_titles = resume_titles or {}
        self.fresh_chat = False  # бот сменился: открытую карточку показать целиком, а не только шаг
        self._shown_ids: list[int] = []  # сообщения карточки, пока черновик ещё пишется
        self.awaiting_edit: int | None = None  # item_id, ждём текст письма
        self.needs_resume = True  # после старта вернуть кнопки активной карточке
        self.notifier = notifier
        self.captcha = captcha
        self._ctx: ReviewItem | None = None  # вакансия, о которой сейчас пишем
        # сообщение с активными кнопками (переживает перезапуск бота)
        saved = settings.get_value(KEYBOARD_KEY) if settings is not None else None
        self.keyboard_msg: int | None = int(saved) if saved else None

    # ---------------------------------------------------------------- utils

    def log(self, type: str, item: ReviewItem | None, **payload: Any) -> None:
        if item is not None:
            self._ctx = item
        self.store.log_event(
            type,
            payload,
            item_id=item.id if item else None,
            resume_id=item.resume_id if item else None,
            vacancy_id=item.vacancy_id if item else None,
        )

    def _tag(self) -> str:
        item = self._ctx or self.store.active()
        if not item:
            return ""
        tag = render.resume_tag(item.resume_id, self.resume_titles)
        try:  # оценка релевантности остаётся в каждом сообщении о вакансии
            score = self.store.relevance_score(item.resume_id, item.vacancy_id)
        except Exception:  # noqa: BLE001
            score = None
        return f"{tag} · 🎯 {score:.2f}" if tag and score is not None else tag

    def _tagged(self, text: str) -> str:
        tag = self._tag()
        return f"<b>{tag}</b>\n{text}" if tag else text

    def send(self, messages: list[str], keyboard: dict | None = None) -> list[int]:
        ids = []
        if messages:
            messages = [self._tagged(messages[0]), *messages[1:]]
        for i, text in enumerate(messages):
            last = i == len(messages) - 1
            ids.append(self.chat.send_message(self.chat_id, text, keyboard if last else None))
        if keyboard and ids:
            self._drop_keyboard()
            self._set_keyboard_msg(ids[-1])
        return ids

    def _set_keyboard_msg(self, message_id: int | None) -> None:
        self.keyboard_msg = message_id
        if self.settings is not None:
            if message_id is None:
                self.settings.delete_value(KEYBOARD_KEY)
            else:
                self.settings.set_value(KEYBOARD_KEY, str(message_id))

    def say(self, text: str) -> int:
        return self.chat.send_message(self.chat_id, self._tagged(text))

    def _delete(self, *message_ids: int | None) -> None:
        """Убрать служебные сообщения («пишу черновик…», откат призрачной карточки)."""
        delete = getattr(self.chat, "delete_message", None)
        if not callable(delete):
            return
        for mid in message_ids:
            if mid is not None:
                try:
                    delete(self.chat_id, mid)
                except Exception:  # noqa: BLE001 — уборка не должна ронять бота
                    logger.debug("delete_message %s", mid, exc_info=True)

    def _drop_keyboard(self) -> None:
        if self.keyboard_msg is not None:
            self.chat.remove_keyboard(self.chat_id, self.keyboard_msg)
            self._set_keyboard_msg(None)

    @staticmethod
    def questionnaire(item: ReviewItem) -> Questionnaire:
        return Questionnaire.from_test(item.test_json or {})

    @staticmethod
    def vacancy_text(item: ReviewItem) -> str:
        return build_vacancy_text(item.vacancy_json or {})

    # ---------------------------------------------------------------- очередь

    def tick(self) -> ReviewItem | None:
        """Если нет активной карточки — показать следующую из очереди."""
        active = self.store.active()
        if active is not None:
            return active
        if self.keyboard_msg is not None:
            self._drop_keyboard()  # открытой карточки нет (её сняли) — старые кнопки не нужны
        for _ in range(5):
            item = self.store.next_queued()
            if item is None:
                return None
            try:
                item = self.store.set_state(item, ItemState.IN_REVIEW, reason="card")
            except TransitionError:
                # карточку успели снять или забрать параллельно — берём следующую
                continue
            self.show_card(item)
            return item
        return None

    def show_card(self, item: ReviewItem) -> None:
        self._ctx = item
        self._shown_ids = []
        verdict = Verdict.from_dict(item.verdict_json)
        messages = render.card_messages(
            item.id,
            item.vacancy_json or {},
            self.resume_titles.get(item.resume_id.removeprefix("dry:"), ""),
            item.steps,
            verdict,
            item.dry_run,
        )
        self._shown_ids = self.send(messages)
        self.log("card_sent", item, messages=len(messages))
        self.show_step(item)

    def _still_open(self, item: ReviewItem) -> bool:
        cur = self.store.get(item.id)
        return cur is not None and cur.state is ItemState.IN_REVIEW

    def show_step(self, item: ReviewItem, changed: set[int] | None = None, notes: list[str] | None = None) -> None:
        self._ctx = item
        item = self.prepare_step(item)
        if item is None:
            return
        if not self._still_open(item):
            # пока писался черновик, карточку сняли/решили в другом месте: не оставляем
            # в чате «призрак» (текст вакансии без кнопок)
            self.log("card_discarded", item, reason="state_changed_while_drafting")
            self._delete(*getattr(self, "_shown_ids", []))
            self._shown_ids = []
            return
        self._shown_ids = []
        step = item.current_step
        keyboard = render.keyboard(item.id, item.step_idx, step)
        if step == "letter":
            messages = render.letter_message(item.letter_draft or "")
        else:
            messages = render.questionnaire_message(
                self.questionnaire(item), item.answers_json or {}, changed, notes
            )
        self.send(messages, keyboard)
        self.log("step_sent", item, step=step, step_idx=item.step_idx)

    def prepare_step(self, item: ReviewItem) -> ReviewItem | None:
        """Черновик текущего шага (LLM). None — если LLM не ответила."""
        step = item.current_step
        working: int | None = None  # служебное «пишу…» — убираем, когда черновик готов
        try:
            if step == "letter" and not item.letter_draft:
                working = self.say("⏳ Пишу черновик письма…")
                letter = self.generator.letter(item.resume_text, self.vacancy_text(item))
                item = self.store.update(item, letter_draft=letter)
                self.log("letter_draft", item, letter=letter)
            elif step == "questionnaire" and item.answers_json is None:
                working = self.say("⏳ Заполняю анкету…")
                answers, notes = self.generator.fill(
                    self.questionnaire(item), item.resume_text, self.vacancy_text(item)
                )
                item = self.store.update(item, answers_draft_json=answers, answers_json=answers)
                self.log("answers_draft", item, answers=answers, notes=notes)
            self._delete(working)
        except (GenerationError, Exception) as ex:  # noqa: BLE001 — LLM/сеть
            self._delete(working)
            logger.exception("Черновик #%s (%s): %s", item.id, step, ex)
            self.log("error", item, where="draft", step=step, error=f"{type(ex).__name__}: {ex}")
            # одно сообщение вместо двух; кнопка «Повторить» вместо тупика с бесполезным «Апрув»
            self.send(
                [
                    "⚠️ <b>Модель не ответила — черновика нет.</b>\n"
                    "🔁 Повторить — запросить ещё раз · ❌ реджект — пропустить вакансию."
                ],
                render.retry_keyboard(item.id, item.step_idx),
            )
            return None
        return item

    def on_start(self) -> None:
        """После перезапуска: вернуть кнопки незакрытой карточке."""
        item = self.store.active()
        if item is None:
            return
        if self.fresh_chat:
            # чат нового бота пуст: шапки карточки (текст вакансии) здесь нет — присылаем целиком
            self.fresh_chat = False
            self.say(f"🔀 Это новый бот — присылаю открытую карточку #{item.id} целиком.")
            self.show_card(item)
            return
        if self.keyboard_msg is not None and (item.letter_draft or item.answers_json):
            # кнопки у текущей карточки уже есть — не дублируем
            return
        self.say(f"🔄 Бот перезапущен, продолжаем #{item.id}.")
        self.show_step(item)

    # --------------------------------------------------------------- апдейты

    def handle_update(self, update: dict[str, Any]) -> None:
        self._ctx = None
        if cb := update.get("callback_query"):
            chat_id = ((cb.get("message") or {}).get("chat") or {}).get("id")
            if str(chat_id) != str(self.chat_id):
                return
            self.handle_callback(cb)
        elif msg := update.get("message"):
            if str((msg.get("chat") or {}).get("id")) != str(self.chat_id):
                return
            self.handle_message(msg.get("text") or "")

    def handle_callback(self, cb: dict[str, Any]) -> None:
        if self.waits is not None and (cb.get("data") or "").startswith("wait:"):
            self.chat.answer_callback(cb["id"], self.waits.handle_callback(cb["data"]))
            self.waits.poll()
            return
        if self.checks is not None and (cb.get("data") or "").startswith("chk:"):
            self.chat.answer_callback(cb["id"], self.checks.handle_callback(cb["data"]))
            self.checks.poll()
            return
        if self.captcha is not None and (cb.get("data") or "").startswith("cap:"):
            self.chat.answer_callback(cb["id"])
            self.captcha.handle_callback(cb["data"])
            return
        parsed = render.parse_callback(cb.get("data") or "")
        active = self.store.active()
        if (
            parsed is None
            or active is None
            or parsed[0] != active.id
            or parsed[1] != active.step_idx
        ):
            self.chat.answer_callback(cb["id"], "Карточка устарела")
            return
        _, _, action = parsed
        self.chat.answer_callback(cb["id"])
        item = active
        step = item.current_step

        if action == "rj":
            self.log("user_reject", item, step=step)
            self.reject(item)
        elif action == "rt":
            self.log("user_retry_draft", item, step=step)
            self._drop_keyboard()
            self.show_step(item)  # черновика нет — prepare_step запросит его заново
        elif action == "ed" and step == "letter":
            self.awaiting_edit = item.id
            self.log("user_edit_pressed", item, step=step)
            self.say("✏️ Пришли текст письма одним сообщением — уйдёт он.")
        elif action == "ap":
            if step == "letter":
                if not item.letter_draft:
                    self.say("Черновика нет: /current — повторить генерацию.")
                    return
                item = self.store.update(item, letter_final=clean_letter(item.letter_draft))
                self.log("user_approve", item, step=step, letter=item.letter_final)
            else:
                q = self.questionnaire(item)
                answers = item.answers_json or {}
                if missing := q.missing(answers):
                    self.say(
                        "⚠️ Нет ответа на: "
                        + ", ".join(map(str, missing))
                        + ". Допиши «N: текст» или ❌ реджект."
                    )
                    return
                self.log("user_approve", item, step=step, answers=answers, form=q.to_form(answers))
            self.advance(item)

    def handle_message(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        if text.startswith("/"):
            return self.command(text.split()[0].lower())
        # пока открыта капча, любой текст — ответ на неё
        if self.captcha is not None and self.captcha.handle_message(text):
            return

        item = self.store.active()
        if item is None:
            self.say("Сейчас нечего ревьюить. " + HELP)
            return
        step = item.current_step

        if step == "letter":
            if self.awaiting_edit != item.id:
                self.say("Нажми ✏️ Эдит, чтобы заменить письмо своим текстом.")
                return
            # человек копирует сообщение с черновиком целиком (там метка резюме, «✉️ Письмо (черновик)»
            # и подсказка про кнопки) и присылает обратно — служебные строки в письмо не пускаем
            cleaned = clean_letter(text)
            if not cleaned:
                self.say("⚠️ После удаления служебных строк письмо пустое. Пришли текст письма ещё раз.")
                return  # awaiting_edit остаётся: ждём нормальный текст
            self.awaiting_edit = None
            item = self.store.update(item, letter_final=cleaned)
            self.log("user_edit_text", item, step=step, draft=item.letter_draft, letter=cleaned,
                     raw=text if cleaned != text else None, cleaned=cleaned != text)
            note = "✅ Письмо принято."
            if cleaned != text:
                note += " 🧹 Служебные строки бота (метка резюме, «✉️ Письмо (черновик)», подсказка) убрал — в hh уйдёт только текст."
            self.say(note)
            self.advance(item)
            return

        q = self.questionnaire(item)
        old = item.answers_json or {}
        edits = parse_manual_edits(text)
        if edits is not None:
            new, changed, errors = apply_manual_edits(q, old, edits)
            self.log("manual_edit", item, message=text, edits=edits, changed=sorted(changed), errors=errors)
        else:
            self.say("⏳ Правлю анкету…")
            self.log("user_prompt", item, prompt=text)
            try:
                new, errors = self.generator.revise(
                    q, old, text, item.resume_text, self.vacancy_text(item)
                )
            except Exception as ex:  # noqa: BLE001
                logger.exception("Правка анкеты #%s: %s", item.id, ex)
                self.log("error", item, where="revise", error=f"{type(ex).__name__}: {ex}")
                self.say(f"⚠️ LLM не ответила ({type(ex).__name__}). Попробуй ещё раз или «N: текст».")
                return
            changed = diff_answers(old, new)
        if not changed:
            self.say("Ничего не изменилось. " + ("; ".join(errors) if errors else ""))
            return
        item = self.store.update(item, answers_json=new)
        self.log("answers_updated", item, changed=sorted(changed), answers=new)
        self.show_step(item, changed=changed, notes=errors)

    def command(self, cmd: str) -> None:
        if cmd == "/current":
            item = self.store.active()
            if item is None:
                if self.tick() is None:
                    self.say("Открытых карточек нет.")
                return
            self.show_card(item) if item.step_idx == 0 else self.show_step(item)
        elif cmd == "/queue":
            queued = len(self.store.by_state(ItemState.QUEUED))
            active = self.store.active()
            waiting = len(self.store.by_state(ItemState.APPROVED, ItemState.FAILED_RETRYABLE))
            self.say(
                f"Открыта: {'#' + str(active.id) if active else '—'}. "
                + (f"Ждёт показа: {queued}. " if queued else "")
                + f"Ждут отправки: {waiting}."
            )
        else:
            self.say(HELP)

    # ------------------------------------------------------------- переходы

    def reject(self, item: ReviewItem) -> None:
        self._drop_keyboard()
        self.awaiting_edit = None
        self.store.set_state(item, ItemState.REJECTED, reason=f"user reject at {item.current_step}")
        self.say(f"❌ #{item.id} пропущена.")
        self.next_or_empty()

    def advance(self, item: ReviewItem) -> None:
        self._drop_keyboard()
        self.awaiting_edit = None
        if item.step_idx + 1 < len(item.steps):
            item = self.store.set_state(
                item, ItemState.IN_REVIEW, reason="next step", step_idx=item.step_idx + 1
            )
            self.show_step(item)
            return
        item = self.store.set_state(item, ItemState.APPROVED, reason="all steps approved")
        self.say(f"📤 #{item.id} одобрена, отправляю…")
        try:
            result = self.submit(item)
        except Exception as ex:  # noqa: BLE001
            logger.exception("Отправка #%s: %s", item.id, ex)
            self.log("error", item, where="submit", error=f"{type(ex).__name__}: {ex}")
            result = f"ошибка: {ex}"
        v = item.vacancy_json or {}
        link = render.esc(v.get("alternate_url") or f"https://hh.ru/vacancy/{item.vacancy_id}")
        text = SUBMIT_TEXT.get(result, f"ℹ️ #{item.id}: {result}").format(id=item.id)
        self.say(f"{text}\n{render.esc(v.get('name') or '')} {link}".strip())
        self.next_or_empty()

    def next_or_empty(self) -> None:
        # решение принято: обход (он стоит и ждёт) сам идёт дальше — отдельное
        # «очередь пуста» не нужно, очереди нет
        self._ctx = None
        self.tick()

    # ----------------------------------------------------------------- цикл

    def poll_once(self, client: Any, timeout: int = 25) -> None:
        if self.notifier is not None:
            self.notifier.run()
        if self.attention is not None:
            self.attention.run()  # никогда не бросает: пинги не должны ронять основной бот
        if self.captcha is not None:
            self.captcha.poll()
            if self.captcha.active:
                timeout = min(timeout, 3)  # обратный отсчёт под картинкой капчи
        if self.waits is not None:
            try:
                self.waits.poll()
            except Exception:  # noqa: BLE001 — отсчёт не должен ронять бота
                logger.exception("wait view")
            # короткий long-poll: новое ожидание появляется в чате за ≤5 с
            timeout = min(timeout, 5)
        if self.checks is not None:
            try:
                self.checks.poll()
            except Exception:  # noqa: BLE001 — спиннер не должен ронять бота
                logger.exception("check view")
            if self.checks.active():
                timeout = min(timeout, 2)  # спиннер живее
        if self.needs_resume:
            self.on_start()  # TelegramError ➜ повторим в следующем цикле
            self.needs_resume = False
        # сначала показать очередь, потом ждать ответов
        self.tick()
        offset = self._offset()
        updates = client.get_updates(offset, timeout=timeout)
        for update in updates:
            self._save_offset(update["update_id"] + 1)
            try:
                self.handle_update(update)
            except Exception as ex:  # noqa: BLE001 — бот не должен падать
                logger.exception("update %s", update.get("update_id"))
                self.store.log_event("error", {"where": "update", "error": f"{type(ex).__name__}: {ex}"})
        self.tick()

    def _offset(self) -> int | None:
        if self.settings is None:
            return getattr(self, "_mem_offset", None)
        value = self.settings.get_value(OFFSET_KEY)
        return int(value) if value else None

    def _save_offset(self, offset: int) -> None:
        if self.settings is None:
            self._mem_offset = offset
        else:
            self.settings.set_value(OFFSET_KEY, str(offset))


SUBMIT_TEXT = {
    "submitted": "✅ #{id} отправлена в hh.",
    "dry_run": "🧪 #{id}: dry-run, в hh ничего не ушло.",
    "deferred": "⏳ #{id} уйдёт в hh, когда откроется окно работы.",
    "retry": "⚠️ #{id}: hh не принял, повторю при следующем обходе.",
    "limit": "⛔ #{id}: лимит откликов hh, повторю позже.",
    "captcha": "🧩 #{id}: hh просит капчу — картинку пришлю, после неё отклик повторится.",
    "hourly_limit": "⏳ #{id}: лимит откликов в час — уйдёт в следующем обходе.",
    "stale": "🗄 #{id}: вакансия в архиве или только через форму работодателя — пропущена.",
    "requeued": "🔁 #{id}: анкета изменилась, пришлю на ревью заново.",
    "gave_up": "🛑 #{id}: не удалось отправить после нескольких попыток.",
}
