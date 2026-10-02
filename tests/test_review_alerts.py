"""Алерты об ошибках в Telegram: формат, секреты, анти-спам, отказоустойчивость."""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import requests

from stamp_flow.review import alerts
from stamp_flow.review.alerts import TelegramAlerter, TelegramErrorHandler, _collect_secrets


class FakeSession:
    def __init__(self, ok=True, fail=False):
        self.ok, self.fail = ok, fail
        self.posts = []
        self.proxies = {}

    def post(self, url, json=None, timeout=None):
        if self.fail:
            raise requests.ConnectionError("down")
        self.posts.append((url, json))
        return SimpleNamespace(ok=self.ok)


def make(tmp_path, session=None, now=None):
    now = now if now is not None else [1000.0]
    return TelegramAlerter(
        "TOKEN123456", 42, tmp_path, secrets=["sk-secret-key-1"], context="hh-applicant-tool apply-vacancies",
        session=session or FakeSession(), clock=lambda: now[0],
    )


def test_exception_has_type_message_traceback(tmp_path):
    a = make(tmp_path)
    try:
        {}["missing"]
    except KeyError as ex:
        assert a.exception(ex, where="обход")
    text = a.session.posts[0][1]["text"]
    assert text.startswith("🚨 <b>ОШИБКА</b> · hh-applicant-tool apply-vacancies")
    assert "обход: KeyError" in text and "'missing'" in text
    assert "<pre>Traceback" in text and "test_exception_has_type_message_traceback" in text
    assert len(text) <= 4096


def test_secrets_redacted(tmp_path):
    a = make(tmp_path)
    a.send("ValueError", "bad key sk-secret-key-1 used", "tb sk-secret-key-1")
    text = a.session.posts[0][1]["text"]
    assert "sk-secret-key-1" not in text and "***" in text


def test_collect_secrets():
    cfg = {"review": {"telegram": {"bot_token": "123:ABCDEFGH", "chat_id": 1}},
           "llm": {"api_key": "sk-xxxxxxxx", "model": "m"}, "token": {"access_token": "AAAABBBBCCCC"}}
    assert set(_collect_secrets(cfg)) == {"123:ABCDEFGH", "sk-xxxxxxxx", "AAAABBBBCCCC"}


def test_dedup_and_repeat_counter(tmp_path):
    now = [1000.0]
    a = make(tmp_path, now=now)
    assert a.send("E", "same") is True
    assert a.send("E", "same") is False
    assert a.send("E", "same") is False
    now[0] += alerts.DEDUP_SECONDS + 1
    assert a.send("E", "same") is True
    assert "повторялась ещё 2" in a.session.posts[-1][1]["text"]
    # другая ошибка не подавляется
    assert a.send("E", "other") is True


def test_dedup_survives_new_process(tmp_path):
    now = [1000.0]
    make(tmp_path, now=now).send("E", "x")
    second = make(tmp_path, FakeSession(), now=now)  # как новый запуск cron
    assert second.send("E", "x") is False


def test_telegram_down_goes_to_file_and_never_raises(tmp_path):
    a = make(tmp_path, FakeSession(fail=True))
    assert a.send("E", "msg", "tb") is False
    line = (tmp_path / "review-log" / alerts.UNDELIVERED_FILE).read_text(encoding="utf-8")
    assert json.loads(line)["message"] == "msg"


def test_bad_html_falls_back_to_plain(tmp_path):
    session = FakeSession(ok=False)
    a = make(tmp_path, session)
    a.send("E", "x")
    assert len(session.posts) == 2 and "parse_mode" not in session.posts[1][1]


def test_logging_handler_catches_error_with_traceback(tmp_path):
    a = make(tmp_path)
    log = logging.getLogger("hh_applicant_tool.test_alerts")
    log.propagate = False
    handler = TelegramErrorHandler(a)
    log.addHandler(handler)
    try:
        log.warning("не ошибка")
        assert a.session.posts == []
        try:
            _ = 1 / 0
        except ZeroDivisionError:
            log.exception("упало деление")
        text = a.session.posts[-1][1]["text"]
        assert "ERROR в hh_applicant_tool.test_alerts" in text and "ZeroDivisionError" in text
        assert "упало деление" in text and "<pre>Traceback" in text
    finally:
        log.removeHandler(handler)


def test_no_recursion_when_sending_logs_error(tmp_path):
    a = make(tmp_path)
    log = logging.getLogger("hh_applicant_tool.test_recursion")
    log.propagate = False
    handler = TelegramErrorHandler(a)
    log.addHandler(handler)

    original = a._post

    def noisy_post(text):
        log.error("ошибка внутри отправки")
        return original(text)

    a._post = noisy_post
    try:
        log.error("первичная")
        assert len(a.session.posts) == 1
    finally:
        log.removeHandler(handler)


def test_install_without_config_is_noop(tmp_path):
    tool = SimpleNamespace(config={}, config_path=tmp_path)
    assert alerts.install(tool, logging.getLogger("x")) is None
    broken = SimpleNamespace(config=None, config_path=None)
    assert alerts.install(broken, logging.getLogger("x")) is None
