"""apply-review: отклики с ревью в Telegram.

Подкласс штатной операции `apply-vacancies` из hh-applicant-tool. Поиск вакансий
и разбор тестов берутся у апстрима как есть, обход по резюме, ревью и отправка
реализованы в `stamp_flow.review`.
"""

from __future__ import annotations

import argparse
import logging
import threading
from typing import Any
from urllib.parse import urlencode

from hh_applicant_tool.operations.apply_vacancies import (
    Operation as ApplyOperation,
)
from hh_applicant_tool.utils.datatypes import VacancyTest

logger = logging.getLogger(__package__)

# Страница отклика на вакансию: с неё берётся анкета (веб-форма, не REST API).
RESPONSE_PAGE = "https://hh.ru/applicant/vacancy_response"
# Адрес, на который апстрим отправляет заполненную анкету (по нему узнаём нужный POST).
SUBMIT_PATH = "/applicant/vacancy_response/popup"


class Operation(ApplyOperation):
    """Отклики с ревью в Telegram: чистые вакансии сразу, остальное через бота."""

    # Алиасы апстримного apply-vacancies (apply, apply-similar) не наследуем: конфликт имён.
    __aliases__: list[str] = []

    def setup_parser(self, parser: argparse.ArgumentParser) -> None:
        super().setup_parser(parser)
        group = parser.add_argument_group("Review-режим (stamp-flow)")
        group.add_argument(
            "--work-hours",
            default=None,
            help="Окно работы HH:MM-HH:MM (можно через полночь). Вне окна обход сразу завершается. По умолчанию review.work_hours из config или круглосуточно",
        )
        group.add_argument(
            "--max-queue",
            type=int,
            default=None,
            help="Не держать в очереди ревью больше N незакрытых вакансий (по умолчанию без ограничения)",
        )
        group.add_argument(
            "--apply-delay",
            default=None,
            help="Случайная пауза между реальными откликами, секунд «MIN-MAX» (по умолчанию review.apply_delay или 60-180; 0 без паузы)",
        )
        group.add_argument(
            "--max-per-hour",
            type=int,
            default=None,
            help="Не больше N реальных откликов за скользящий час (по умолчанию review.max_per_hour или 10; 0 без лимита)",
        )
        group.add_argument(
            "--llm-wait",
            type=float,
            default=None,
            help="Сколько секунд ждать ожившую модель, прежде чем отправлять вакансии на ревью без проверки (по умолчанию 600)",
        )
        group.add_argument(
            "--per-resume",
            default=None,
            help="Квота: сколько новых вакансий обработать с одного резюме за круг: N или «MIN-MAX» (случайно на каждый круг). По умолчанию review.per_resume или без квоты",
        )
        group.add_argument(
            "--resume-delay",
            default=None,
            help="Пауза при смене резюме, секунд «MIN-MAX» (по умолчанию 30-90; 0 без паузы). Пропускается кнопкой «пропустить» в боте",
        )
        group.add_argument(
            "--round-delay",
            default=None,
            help="Пауза между кругами, секунд «MIN-MAX» (по умолчанию 120-300). Пропускается кнопкой «пропустить» в боте",
        )
        group.add_argument(
            "--relevance-min",
            type=float,
            default=None,
            help="Порог релевантности 0..1 (оценка моделью). Ниже порога вакансия пропускается и архивируется. По умолчанию review.relevance_min или без оценки (0)",
        )
        group.add_argument(
            "--block-on-review",
            action=argparse.BooleanOptionalAction,
            default=None,
            help="Обход стоит на каждой вакансии, ушедшей на ревью, пока вы не решите её в боте (поток без очереди). По умолчанию включено; --no-block-on-review нужен только для отладки",
        )
        group.add_argument(
            "--rounds",
            default=None,
            help="Сколько кругов по всем резюме за запуск: N или «MIN-MAX» (случайно на каждый запуск). По умолчанию review.rounds или 1. Стартовое резюме ротируется между запусками",
        )

    # ------------------------------------------------------------- обход

    def _apply_vacancies(self) -> None:
        from stamp_flow.review.runner import run_review_pass

        resumes = [
            r
            for r in self.tool.get_resumes()
            if r["status"]["id"] == "published"
            and (not self.resume_id or r["id"] == self.resume_id)
        ]
        if not resumes:
            logger.warning("Нет опубликованных резюме: обходить нечего")
            return
        run_review_pass(self, resumes)

    # ------------------------------------------------- анкета (тест) вакансии

    # Отправка анкеты подменяет метод сессии на время вызова: блокируем параллельные отправки.
    _submit_lock = threading.Lock()

    @staticmethod
    def _vacancy_response_url(vacancy_id: str | int) -> str:
        query = {
            "vacancyId": vacancy_id,
            "startedWithQuestion": "false",
            "hhtmFrom": "vacancy",
        }
        return f"{RESPONSE_PAGE}?{urlencode(query)}"

    def _load_vacancy_test(
        self, vacancy_id: str | int
    ) -> tuple[str, VacancyTest]:
        """Анкета вакансии со страницы отклика: (адрес страницы, данные анкеты)."""
        page = self._vacancy_response_url(vacancy_id)
        tests = self._get_vacancy_tests(page) or {}
        test = tests.get(str(vacancy_id))
        if test is None:
            raise ValueError(
                f"Анкета вакансии {vacancy_id} не найдена на странице отклика {page}"
            )
        return page, test

    def _submit_vacancy_test(
        self,
        vacancy_id: str | int,
        resume_hash: str,
        letter: str,
        test_data: VacancyTest | None,
        answers: dict[str, Any],
        response_url: str | None = None,
    ) -> dict[str, Any]:
        """Отправляет отклик с анкетой штатным методом апстрима, но с нашими ответами.

        Апстрим сам загружает анкету, собирает форму, ждёт паузу и делает POST. Ответы он
        подбирает автоматически, поэтому перед самой отправкой подменяем в форме поля
        `task_<id>` (выбор варианта) и `task_<id>_text` (открытый ответ) на `answers`.
        Подмена строгая: если форма ушла бы в неожиданном виде или подмена не сработала,
        отправка отменяется ошибкой, а не уходит с автоответами апстрима.
        `test_data` и `response_url` оставлены ради единого интерфейса: апстрим берёт
        свежую анкету сам.
        """
        session = self.tool.session
        session_dict = vars(session)
        had_own_request = "request" in session_dict
        real_request = session.request
        replaced = False

        def request_with_answers(method, url, *args, **kwargs):
            nonlocal replaced
            if str(method).upper() == "POST" and str(url).endswith(SUBMIT_PATH):
                form = kwargs.get("data")
                if not isinstance(form, dict):
                    raise RuntimeError(
                        "Апстрим отправляет анкету в неожиданном виде: ответы не подменены, отправка отменена"
                    )
                for field in [k for k in form if str(k).startswith("task_")]:
                    del form[field]
                form.update(answers)
                replaced = True
            return real_request(method, url, *args, **kwargs)

        with self._submit_lock:
            self.cover_letter_ai = None  # автоответы апстрима без модели
            session.request = request_with_answers
            try:
                result = self._solve_vacancy_test(vacancy_id, resume_hash, letter)
            finally:
                if had_own_request:
                    session.request = real_request
                else:
                    session_dict.pop("request", None)
        if not replaced:
            raise RuntimeError(
                "Анкета ушла без подмены ответов: проверь совместимость с версией hh-applicant-tool"
            )
        return result
