"""apply-review: анкета вакансии (загрузка и отправка через апстрим), текст резюме для LLM."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock
from urllib.parse import parse_qs

import pytest
import requests

from stamp_flow.operations.apply_review import Operation
from stamp_flow.review.context import build_resume_text

TEST_DATA = {
    "uidPk": "uid-1",
    "guid": "guid-1",
    "startTime": "123",
    "required": "true",
    "name": "Анкета",
    "description": "",
    "tasks": [
        {
            "id": 11,
            "description": "Готовы к удалёнке?",
            "candidateSolutions": [{"id": "1", "text": "Нет"}, {"id": "2", "text": "Да"}],
        },
        {"id": 13, "description": "Расскажите о себе", "candidateSolutions": []},
    ],
}


class FakeHh(requests.adapters.BaseAdapter):
    """Вместо сети: запоминает запросы и отвечает JSON."""

    def __init__(self):
        super().__init__()
        self.sent: list[requests.PreparedRequest] = []

    def send(self, request, **kwargs):
        self.sent.append(request)
        response = requests.Response()
        response.status_code = 200
        response._content = b'{"success": "true"}'
        response.url = request.url
        response.request = request
        return response

    def close(self):
        pass


def _make_op():
    """Операция с настоящей requests.Session: апстрим отправляет анкету именно через неё."""
    adapter = FakeHh()
    session = requests.Session()
    session.mount("https://", adapter)
    op = Operation()
    op.cover_letter_ai = None
    op.tool = SimpleNamespace(session=session, xsrf_token="xsrf")
    op._get_vacancy_tests = MagicMock(return_value={"42": TEST_DATA})
    return op, adapter


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    # пауза перед отправкой живёт в коде апстрима
    monkeypatch.setattr("time.sleep", lambda *_: None)


def _form(request):
    return {k: v[0] for k, v in parse_qs(request.body).items()}


def test_command_has_no_upstream_aliases():
    assert Operation.__aliases__ == []


def test_load_vacancy_test_returns_page_and_test():
    op, _ = _make_op()
    page, test = op._load_vacancy_test(42)
    assert test is TEST_DATA
    assert "vacancyId=42" in page and page.startswith("https://hh.ru/applicant/vacancy_response?")


def test_load_vacancy_test_errors():
    op, _ = _make_op()
    op._get_vacancy_tests.return_value = None
    with pytest.raises(ValueError, match="не найдена"):
        op._load_vacancy_test(42)
    op._get_vacancy_tests.return_value = {"1": TEST_DATA}
    with pytest.raises(ValueError, match="не найдена"):
        op._load_vacancy_test(42)


def test_submit_replaces_upstream_autoanswers_with_ours():
    op, hh = _make_op()
    result = op._submit_vacancy_test(
        vacancy_id=42,
        resume_hash="r1",
        letter="L",
        test_data=TEST_DATA,
        answers={"task_11": "1", "task_13_text": "Мой ответ"},
    )

    assert result["success"] == "true"
    assert result["_http_status"] == 200
    (request,) = hh.sent
    assert request.method == "POST"
    assert request.url == "https://hh.ru/applicant/vacancy_response/popup"
    form = _form(request)
    # ответы наши, а не автоответ апстрима («Да» -> 2)
    assert form["task_11"] == "1"
    assert form["task_13_text"] == "Мой ответ"
    assert form["letter"] == "L"
    assert form["resume_hash"] == "r1"
    assert form["vacancy_id"] == "42"
    assert form["_xsrf"] == "xsrf"
    assert "vacancyId=42" in request.headers["Referer"]
    # метод сессии возвращён на место
    assert "request" not in vars(op.tool.session)


def test_submit_drops_autoanswers_for_tasks_we_did_not_answer():
    op, hh = _make_op()
    op._submit_vacancy_test(42, "r1", "", TEST_DATA, {"task_11": "1"})
    form = _form(hh.sent[0])
    assert form["task_11"] == "1"
    assert "task_13_text" not in form


def test_submit_restores_session_on_error():
    op, hh = _make_op()
    hh.send = MagicMock(side_effect=requests.ConnectionError("down"))
    with pytest.raises(requests.ConnectionError):
        op._submit_vacancy_test(42, "r1", "", TEST_DATA, {})
    assert "request" not in vars(op.tool.session)


def test_submit_refuses_when_upstream_form_is_not_a_dict():
    """Если апстрим начнёт слать анкету иначе, автоответы не должны уйти молча."""
    op, hh = _make_op()

    def odd_upstream(vacancy_id, resume_hash, letter=""):
        return op.tool.session.post("https://hh.ru/applicant/vacancy_response/popup", json={"x": 1}).json()

    op._solve_vacancy_test = odd_upstream
    with pytest.raises(RuntimeError, match="неожиданном виде"):
        op._submit_vacancy_test(42, "r1", "", TEST_DATA, {"task_11": "1"})
    assert hh.sent == []  # в сеть ничего не ушло
    assert "request" not in vars(op.tool.session)


def test_submit_fails_loudly_if_answers_were_not_substituted():
    op, hh = _make_op()
    op._solve_vacancy_test = lambda vacancy_id, resume_hash, letter="": {"success": "true"}
    with pytest.raises(RuntimeError, match="без подмены ответов"):
        op._submit_vacancy_test(42, "r1", "", TEST_DATA, {"task_11": "1"})


def test_build_resume_text_format():
    text = build_resume_text(
        {
            "title": "Python dev",
            "skills": "О себе",
            "skill_set": ["FastAPI", "PostgreSQL"],
            "experience": [
                {
                    "company": "ACME",
                    "position": "Backend",
                    "start": "2024-01-01",
                    "end": None,
                    "description": "SaaS",
                }
            ],
        }
    )
    assert text == (
        "Должность: Python dev\n"
        "\n## О себе\n"
        "О себе\n"
        "\n## Навыки\n"
        "FastAPI, PostgreSQL\n"
        "\n## Опыт работы\n"
        "\n- ACME\n"
        "  Должность: Backend\n"
        "  Период: 2024-01-01 - по настоящее время\n"
        "  Описание:\n"
        "  SaaS"
    )