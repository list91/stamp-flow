"""Тесты opencode_ssh-драйвера без сети: subprocess.run подменяется."""

from __future__ import annotations

import base64
import json
import re
import subprocess
from types import SimpleNamespace

import pytest

from stamp_flow.review import llm
from stamp_flow.review.llm import (
    RESULT_MARK,
    LLMError,
    OpencodeSshDriver,
    create_llm_driver,
)


def _proc(stdout: str = "", returncode: int = 0, stderr: str = ""):
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)


def _ok(text: str = "PONG", **extra) -> str:
    data = {
        "ok": True,
        "text": text,
        "error": None,
        "retryable": False,
        "tool_attempts": [],
        "session_id": "ses_1",
        "elapsed": 1.5,
    } | extra
    return "noise\n" + RESULT_MARK + json.dumps(data) + "\n"


def _fail(error: str, retryable: bool) -> str:
    data = {
        "ok": False,
        "text": "",
        "error": error,
        "retryable": retryable,
        "tool_attempts": [],
        "session_id": "ses_1",
        "elapsed": 0.1,
    }
    return RESULT_MARK + json.dumps(data) + "\n"


@pytest.fixture
def driver():
    return OpencodeSshDriver(
        host="203.0.113.5",
        user="root",
        model="opencode/test-model",
        key_file="/k/id",
        retries=2,
        sleep=lambda s: None,
    )


def _patch_run(monkeypatch, results):
    calls = []
    results = list(results)

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        item = results.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    monkeypatch.setattr(llm.subprocess, "run", fake_run)
    return calls


def test_complete_returns_text(monkeypatch, driver):
    calls = _patch_run(monkeypatch, [_proc(_ok("PONG"))])
    assert driver.complete("sys", "Ответь PONG") == "PONG"
    cmd, kwargs = calls[0]
    assert cmd[0] == "ssh"
    assert "BatchMode=yes" in cmd
    assert cmd[cmd.index("-i") + 1] == "/k/id"
    assert cmd[-2:] == ["root@203.0.113.5", "python3 -"]
    assert kwargs["encoding"] == "utf-8"


def test_request_embedded_as_base64(driver):
    user_text = 'Вакансия"; rm -rf / #\n$(reboot) `id`'
    script = driver.build_script("система", user_text)
    # сырой текст пользователя в скрипт не попадает — только base64
    assert user_text not in script
    b64 = re.search(r'b64decode\("([A-Za-z0-9+/=]+)"\)', script).group(1)
    req = json.loads(base64.b64decode(b64).decode("utf-8"))
    assert req["user"] == user_text
    assert req["system"] == "система"
    assert req["provider_id"] == "opencode"
    assert req["model_id"] == "test-model"
    assert RESULT_MARK in script


def test_remote_script_compiles(driver):
    compile(driver.build_script("s", "u"), "<remote>", "exec")


def test_tool_attempts_are_reported(monkeypatch, driver):
    attempts = [{"permission": "bash", "patterns": ["ls /"]}]
    _patch_run(monkeypatch, [_proc(_ok("нет", tool_attempts=attempts))])
    events = []
    driver.log_hook = lambda e, p: events.append((e, p))
    result = driver.run("", "выполни ls /")
    assert result.tool_attempts == attempts
    assert [e for e, _ in events] == ["llm_request", "llm_response"]
    assert events[1][1]["tool_attempts"] == attempts


def test_retryable_error_is_retried(monkeypatch, driver):
    calls = _patch_run(
        monkeypatch,
        [_proc(_fail("timeout", True)), _proc("", 255, "Connection refused"), _proc(_ok())],
    )
    result = driver.run("", "x")
    assert result.text == "PONG"
    assert result.attempts == 3
    assert len(calls) == 3


def test_non_retryable_error_stops(monkeypatch, driver):
    calls = _patch_run(
        monkeypatch,
        [_proc(_fail("APIError: free tier only within OpenCode", False))],
    )
    events = []
    driver.log_hook = lambda e, p: events.append(e)
    with pytest.raises(LLMError, match="free tier") as exc:
        driver.run("", "x")
    assert exc.value.retryable is False
    assert len(calls) == 1
    assert events[-1] == "llm_error"


def test_retries_exhausted(monkeypatch, driver):
    calls = _patch_run(monkeypatch, [_proc("", 255, "no route")] * 3)
    with pytest.raises(LLMError, match="no route"):
        driver.run("", "x")
    assert len(calls) == 3


def test_missing_marker_non_ssh_error_not_retried(monkeypatch, driver):
    calls = _patch_run(monkeypatch, [_proc("", 127, "python3: not found")])
    with pytest.raises(LLMError, match="127"):
        driver.run("", "x")
    assert len(calls) == 1


def test_ssh_timeout_is_retryable(monkeypatch, driver):
    _patch_run(
        monkeypatch,
        [subprocess.TimeoutExpired("ssh", 5), _proc(_ok("ok"))],
    )
    assert driver.complete("", "x") == "ok"


def test_from_config(monkeypatch):
    monkeypatch.setenv("HOME", "/home/me")
    monkeypatch.setenv("USERPROFILE", "/home/me")
    d = create_llm_driver(
        {
            "driver": "opencode_ssh",
            "host": "203.0.113.5",
            "user": "root",
            "key_file": "~/.ssh/id",
            "model": "opencode/example-model",
        }
    )
    assert isinstance(d, OpencodeSshDriver)
    assert not d.key_file.startswith("~")
    assert (d.provider_id, d.model_id) == ("opencode", "example-model")


def test_config_errors():
    with pytest.raises(ValueError, match="host"):
        create_llm_driver({"driver": "opencode_ssh"})
    with pytest.raises(ValueError, match="hots"):
        create_llm_driver({"hots": "x", "host": "y"})
    with pytest.raises(ValueError, match="Неизвестный llm-драйвер"):
        create_llm_driver({"driver": "nope", "host": "y"})
    with pytest.raises(ValueError, match="provider/model"):
        OpencodeSshDriver(host="h", model="muse").build_script("", "")


# ------------------------------------------------- сломанный GET /permission: скрипт против фальшивого сервера
# Случай из практики: модель потянулась к webfetch (ссылка на тестовое в вакансии), в metadata запроса поле
# timeout = undefined, сервер отвечает 400 на любой GET /permission, пока запрос висит. Драйвер
# отклонял запросы только по этому списку — и вся модель встала. Здесь настоящий скрипт (тот, что уходит
# на удалённый хост) гоняется против фальшивого OpenCode, который воспроизводит эту поломку.


class FakeOpencode:
    """Минимальный OpenCode: сессии, события SSE, запрос разрешения на webfetch, поломанный список."""

    def __init__(self, *, poison_list=True, sse=True, tool_call=True):
        import queue
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        self.poison_list, self.sse, self.tool_call = poison_list, sse, tool_call
        self.sid = "ses_fake1"
        self.pending, self.replies, self.aborted = {}, [], []
        self.tool_state = None  # None | "running" | "error"
        self.completed = False
        self.events: "queue.Queue[str]" = queue.Queue()
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, code, data):
                raw = json.dumps(data).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _body(self):
                n = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(n) or b"{}")

            def do_GET(self):
                if self.path == "/event":
                    if not fake.sse:
                        return self._json(404, {"error": "no sse"})
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    self.wfile.write(b'data: {"type":"server.connected","properties":{}}\n\n')
                    self.wfile.flush()
                    try:
                        while True:
                            try:
                                ev = fake.events.get(timeout=0.2)
                            except queue.Empty:
                                if fake.stop:
                                    return
                                continue
                            self.wfile.write(b"data: " + ev.encode() + b"\n\n")
                            self.wfile.flush()
                    except (BrokenPipeError, ConnectionError, OSError):
                        return
                if self.path == "/permission":
                    if fake.poison_list == "always" or (fake.poison_list and fake.pending):
                        return self._json(400, {"name": "BadRequest", "data": {
                            "message": 'Expected JSON value, got undefined\n  at [0]["metadata"]["timeout"]',
                            "kind": "Body"}})
                    return self._json(200, [dict(v, id=k) for k, v in fake.pending.items()])
                if self.path == "/question":
                    return self._json(200, [])
                if self.path == "/session/status":
                    busy = fake.tool_state == "running" or (fake.sent and not fake.completed)
                    return self._json(200, {fake.sid: {"type": "busy" if busy else "idle"}})
                if self.path == f"/session/{fake.sid}/message":
                    return self._json(200, fake.messages())
                self._json(404, {"error": self.path})

            def do_POST(self):
                body = self._body()
                if self.path == "/session":
                    return self._json(200, {"id": fake.sid})
                if self.path == f"/session/{fake.sid}/prompt_async":
                    fake.sent = True
                    threading.Thread(target=fake.model_turn, daemon=True).start()
                    return self._json(200, {})
                if self.path.startswith("/permission/") and self.path.endswith("/reply"):
                    pid = self.path.split("/")[2]
                    fake.replies.append((pid, body))
                    fake.pending.pop(pid, None)
                    fake.tool_state = "error"
                    return self._json(200, True)
                if self.path == f"/session/{fake.sid}/abort":
                    fake.aborted.append(self.path)
                    return self._json(200, True)
                self._json(404, {"error": self.path})

        self.sent, self.stop = False, False
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def model_turn(self):
        import time

        time.sleep(0.2)
        if self.tool_call:
            self.tool_state = "running"
            self.pending["per_orphan1"] = {
                "sessionID": self.sid, "permission": "webfetch", "patterns": ["https://gist.github.com/x"],
                "metadata": {"url": "https://gist.github.com/x", "timeout": None},
            }
            self.events.put(json.dumps({"type": "permission.asked", "properties": {
                "id": "per_orphan1", "sessionID": self.sid, "permission": "webfetch",
                "patterns": ["https://gist.github.com/x"], "metadata": {"url": "https://gist.github.com/x"}}}))
            deadline = time.time() + 20
            while self.tool_state == "running" and time.time() < deadline:
                time.sleep(0.05)
            if self.tool_state == "running":
                return  # никто не отклонил: модель так и висит на инструменте
        time.sleep(0.2)
        self.completed = True

    def messages(self):
        parts = [{"type": "text", "text": "ответ по тексту"}] if self.completed else []
        if self.tool_state:
            parts.append({"type": "tool", "tool": "webfetch", "callID": "call_1",
                          "state": {"status": self.tool_state, "input": {"url": "https://gist.github.com/x"}}})
        info = {"role": "assistant", "time": {"completed": 1} if self.completed else {}}
        return [{"info": {"role": "user"}, "parts": [{"type": "text", "text": "q"}]},
                {"info": info, "parts": parts}]

    def close(self):
        self.stop = True
        self.httpd.shutdown()


def _run_remote(fake, **kw):
    import sys

    d = OpencodeSshDriver(host="h", model="opencode/test-model", api_url=fake.url, poll_interval=0.1, tool_stall=1.5, timeout=20, **kw)
    script = d.build_script("sys", "user")
    proc = subprocess.run([sys.executable, "-"], input=script, capture_output=True, text=True, encoding="utf-8", timeout=60)
    line = next(l for l in proc.stdout.splitlines() if l.startswith(RESULT_MARK))
    return json.loads(line[len(RESULT_MARK):])


def test_remote_script_rejects_tool_via_events_when_permission_list_is_broken():
    # «always»: закрепившийся с прошлого раза запрос сидит на сервере, список сломан постоянно
    fake = FakeOpencode(poison_list="always", sse=True)
    try:
        out = _run_remote(fake)
    finally:
        fake.close()
    assert out["ok"] is True and out["text"] == "ответ по тексту"  # модель не встала
    assert out["permission_list_error"] and "HTTP 400" in out["permission_list_error"]  # список сломан, и это учтено
    assert fake.replies and fake.replies[0][0] == "per_orphan1"
    assert fake.replies[0][1]["reply"] == "reject"  # инструмент отклонён по событию
    assert any(a.get("permission") == "webfetch" for a in out["tool_attempts"])  # попытка зафиксирована
    assert not fake.aborted  # вызов завершился штатно, сессию не обрывали
    assert fake.pending == {}  # закрепившегося запроса не осталось — сервер не отравлен


def test_remote_script_fails_fast_when_tool_blocks_and_cannot_be_rejected():
    """Нет потока событий и список сломан: ждать 300 с бессмысленно — быстрый понятный отказ."""
    fake = FakeOpencode(poison_list=True, sse=False)
    try:
        out = _run_remote(fake)
    finally:
        fake.close()
    assert out["ok"] is False and out["error"].startswith("tool_blocked: webfetch")
    assert out["retryable"] is True and out["elapsed"] < 15
    assert fake.aborted  # залипшую сессию оборвали


def test_remote_script_normal_answer_without_tools_is_unchanged():
    fake = FakeOpencode(poison_list=False, sse=True, tool_call=False)
    try:
        out = _run_remote(fake)
    finally:
        fake.close()
    assert out["ok"] is True and out["text"] == "ответ по тексту"
    assert out["permission_list_error"] is None and out["tool_attempts"] == []
    assert fake.replies == []


def test_driver_retries_tool_blocked_once_with_no_tools_hint(monkeypatch, driver):
    scripts = []

    def fake_run(cmd, **kwargs):
        scripts.append(kwargs["input"])
        if len(scripts) == 1:
            return _proc(_fail("tool_blocked: webfetch", True))
        return _proc(_ok("готово"))

    monkeypatch.setattr(llm.subprocess, "run", fake_run)
    events = []
    driver.log_hook = lambda e, p: events.append(e)
    slept = []
    driver.sleep = slept.append
    assert driver.run("система", "текст со ссылкой").text == "готово"
    assert len(scripts) == 2 and slept == []  # повтор сразу, без пауз 5/15/45 с
    req = json.loads(base64.b64decode(re.search(r'b64decode\("([A-Za-z0-9+/=]+)"\)', scripts[1]).group(1)))
    assert "нет инструментов" in req["system"] and req["system"].startswith("система")
    first = json.loads(base64.b64decode(re.search(r'b64decode\("([A-Za-z0-9+/=]+)"\)', scripts[0]).group(1)))
    assert "нет инструментов" not in first["system"]  # обычный запрос подсказку не получает
    assert "llm_retry_no_tools" in events


def test_driver_warns_once_about_broken_permission_list(monkeypatch, driver, caplog):
    _patch_run(monkeypatch, [_proc(_ok("a", permission_list_error="HTTP 400 GET /permission: x")),
                             _proc(_ok("b", permission_list_error="HTTP 400 GET /permission: x"))])
    events = []
    driver.log_hook = lambda e, p: events.append(e)
    with caplog.at_level("WARNING"):
        driver.run("", "1")
        driver.run("", "2")
    assert events.count("llm_permission_list_broken") == 1  # один раз за процесс, а не на каждый вызов
