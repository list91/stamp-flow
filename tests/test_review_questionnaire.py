from __future__ import annotations

import pytest

from stamp_flow.review.questionnaire import (
    Questionnaire,
    apply_manual_edits,
    diff_answers,
    parse_manual_edits,
)

TEST = {
    "name": "Анкета",
    "tasks": [
        {"id": 7, "description": "Грейд?", "multiple": "false",
         "candidateSolutions": [{"id": "a", "text": "Junior"}, {"id": "b", "text": "Middle"}]},
        {"id": 8, "description": "<p>Расскажите о себе</p>", "candidateSolutions": []},
        {"id": 9, "description": "Технологии", "multiple": "true",
         "candidateSolutions": [{"id": "x", "text": "FastAPI"}, {"id": "y", "text": "Django"}, {"id": "z", "text": "PostgreSQL"}]},
    ],
}


@pytest.fixture
def q():
    return Questionnaire.from_test(TEST)


def test_model(q):
    assert [x.n for x in q.questions] == [1, 2, 3]
    assert q.questions[1].text == "Расскажите о себе"
    assert q.questions[2].multiple and q.questions[2].is_choice
    assert q.task_ids == ["7", "8", "9"]


def test_to_form(q):
    answers = {"1": {"choice": ["b"]}, "2": {"text": "Привет"}, "3": {"choice": ["x", "z"]}}
    assert q.to_form(answers) == {"task_7": "b", "task_8_text": "Привет", "task_9": ["x", "z"]}
    assert q.missing({"1": {"choice": ["b"]}}) == [2, 3]


def test_parse_llm_answers(q):
    answers, errors = q.parse_llm_answers(
        {"answers": [{"n": 1, "choice": [2]}, {"n": 2, "text": "Я"}, {"n": 3, "choice": ["FastAPI", 3]}, {"n": 9, "text": "?"}]}
    )
    assert answers == {"1": {"choice": ["b"]}, "2": {"text": "Я"}, "3": {"choice": ["x", "z"]}}
    assert errors == ["нет вопроса №9"]


@pytest.mark.parametrize(
    "message, expected",
    [
        ("2: новый ответ", {2: "новый ответ"}),
        ("1. 2\n3) 1,3", {1: "2", 3: "1,3"}),
        ("2: строка один\nстрока два", {2: "строка один\nстрока два"}),
        ("сделай второй короче", None),
        ("сделай короче\n2: так", None),
        ("", None),
    ],
)
def test_parse_manual_edits(message, expected):
    assert parse_manual_edits(message) == expected


def test_apply_manual_edits(q):
    old = {"1": {"choice": ["a"]}, "2": {"text": "старый"}}
    new, changed, errors = apply_manual_edits(q, old, {1: "2", 2: "новый", 3: "1, 3", 5: "x"})
    assert new["1"] == {"choice": ["b"]}
    assert new["2"] == {"text": "новый"}
    assert new["3"] == {"choice": ["x", "z"]}
    assert changed == {1, 2, 3}
    assert errors == ["нет вопроса №5"]
    assert old["2"] == {"text": "старый"}  # исходник не мутирован


def test_apply_manual_edits_choice_by_text_and_errors(q):
    new, changed, _ = apply_manual_edits(q, {}, {1: "middle"})
    assert new["1"] == {"choice": ["b"]} and changed == {1}
    _, changed, errors = apply_manual_edits(q, {}, {1: "1,2"})
    assert not changed and "укажи номер варианта" in errors[0]


def test_diff_and_render(q):
    old = {"1": {"choice": ["a"]}, "2": {"text": "x"}}
    new = {"1": {"choice": ["b"]}, "2": {"text": "x"}}
    assert diff_answers(old, new) == {1}
    text = q.render(new, changed={1})
    assert "✏️ 1. Грейд?" in text
    assert "➜ 2) Middle" in text
    assert "⚠️ нужен ответ" in text  # вопрос 3 без ответа
