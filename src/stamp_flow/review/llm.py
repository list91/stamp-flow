"""Единый LLM-драйвер для ревью (письма, анкеты, детектор инъекций).

Сейчас реализован один драйвер — ``opencode_ssh``: модель вызывается на
удалённом хосте через SSH, где уже крутится ``opencode serve`` (по умолчанию
127.0.0.1:4096 на самом хосте). По SSH на хост уходит небольшой скрипт на
stdlib Python, который говорит с HTTP API OpenCode локально и возвращает
одну JSON-строку с результатом.

Почему не SSH-туннель: каждый вызов — отдельный короткий ``ssh``, не нужно
держать и переподнимать туннель, не занимается локальный порт.

Безопасность: free tier OpenCode отвечает 403, если из запроса убрать
инструменты (``tools: false`` или ``permission: deny``). Поэтому сессия
создаётся с ``permission: ask`` на всё, а каждый запрос на инструмент
скрипт тут же отклоняет. Попытки вызвать инструмент возвращаются в
``LLMResult.tool_attempts`` — для текста вакансии это сильный признак
промпт-инъекции.

Отклонять нужно по потоку событий ``/event``, а не по списку ``GET /permission``:
когда модель пробует ``webfetch`` (ссылка на тестовое в вакансии), у запроса
в metadata поле ``timeout`` = undefined, сервер не может его сериализовать и
отвечает 400 на КАЖДЫЙ ``GET /permission``, пока запрос не закрыт (так
может встать вся модель). Событие же JSON-сериализуется без undefined и приходит.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import subprocess
import time
from dataclasses import dataclass, field, fields
from typing import Any, Callable, Mapping, Protocol

from hh_applicant_tool.ai.base import AIError

logger = logging.getLogger(__package__)

RESULT_MARK = "@@LLM_RESULT@@"

# Добавляется к системному промпту при повторе после «инструмент завис»: у модели нет
# ни инструментов, ни интернета, ссылки из текста вакансии ей открывать нечем.
NO_TOOLS_HINT = (
    "\n\nВАЖНО: у тебя нет инструментов и доступа в интернет. Ссылки из текста "
    "(тестовое задание, gist, сайт компании) не открывай и не пытайся прочитать — "
    "работай только с текстом выше и ответь обычным текстом."
)

LogHook = Callable[[str, dict[str, Any]], None]


class LLMError(AIError):
    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class LLMDriver(Protocol):
    def complete(
        self,
        system: str,
        user: str,
        *,
        temperature: float | None = None,
    ) -> str: ...


@dataclass
class LLMResult:
    text: str
    elapsed: float
    tool_attempts: list[dict[str, Any]]
    session_id: str | None
    attempts: int


# Выполняется на удалённом хосте: `python3 -` получает этот текст через stdin.
# Запрос вшит в base64 — никакого экранирования пользовательского текста
# через shell. Только stdlib, вывод — одна ASCII-строка с маркером.
REMOTE_SCRIPT = r'''
import base64, json, sys, threading, time, urllib.error, urllib.request

REQ = json.loads(base64.b64decode("__REQUEST_B64__").decode("utf-8"))
BASE = REQ["api_url"].rstrip("/")
MARK = "__RESULT_MARK__"
REJECT_MESSAGE = "Tools are disabled. Answer with plain text only."
# инструмент, висящий дольше этого без ответа на разрешение, считаем залипшим
TOOL_STALL = float(REQ.get("tool_stall", 25))


class CallError(Exception):
    def __init__(self, message, retryable):
        Exception.__init__(self, message)
        self.retryable = retryable


def call(method, path, body=None, timeout=30):
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        BASE + path,
        method=method,
        data=data,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:500]
        raise CallError(
            "HTTP %s %s %s: %s" % (e.code, method, path, detail),
            e.code in (408, 429, 500, 502, 503, 504),
        )
    except (urllib.error.URLError, OSError) as e:
        raise CallError("%s %s: %r" % (method, path, e), True)
    return json.loads(raw) if raw.strip() else None


def error_message(err):
    if not isinstance(err, dict):
        return str(err)
    data = err.get("data") or {}
    return "%s: %s" % (
        err.get("name", "Error"),
        data.get("message") or json.dumps(err)[:500],
    )


def tool_parts(messages):
    found = []
    for m in messages:
        for p in m.get("parts") or []:
            if p.get("type") == "tool":
                found.append({
                    "tool": p.get("tool"),
                    "status": (p.get("state") or {}).get("status"),
                })
    return found


# --- запросы разрешений на инструменты -------------------------------------
# Каждый запрос отклоняем сразу. Список GET /permission для этого НЕ годится:
# если у чужого/нашего запроса в metadata есть поле со значением undefined
# (webfetch без timeout), сервер не может его сериализовать и отвечает 400 на
# КАЖДЫЙ GET /permission, пока запрос висит. Поэтому главный путь — поток
# событий /event (JSON.stringify молча выбрасывает undefined), список — лишь
# подстраховка, и его ошибка не считается ошибкой вызова.
state = {"sid": None}
lock = threading.Lock()
stop = threading.Event()
asked = {}  # id запроса -> {"permission", "patterns", "rejected", "error"}
sse = {"connected": False, "error": None}


def reject(pid, info):
    try:
        call("POST", "/permission/" + pid + "/reply",
             {"reply": "reject", "message": REJECT_MESSAGE}, timeout=10)
        info["rejected"] = True
    except CallError as e:
        info["rejected"] = False
        info["error"] = str(e)[:200]


def on_asked(props):
    pid = props.get("id")
    if not pid or props.get("sessionID") != state["sid"]:
        return
    with lock:
        if pid in asked:
            return
        info = {
            "permission": props.get("permission"),
            "patterns": props.get("patterns"),
            "rejected": False,
            "error": None,
        }
        asked[pid] = info
    reject(pid, info)


def sse_loop():
    while not stop.is_set():
        try:
            req = urllib.request.Request(
                BASE + "/event", headers={"Accept": "text/event-stream"}
            )
            with urllib.request.urlopen(req, timeout=25) as resp:
                sse["connected"] = True
                for raw in resp:
                    if stop.is_set():
                        return
                    line = raw.decode("utf-8", "replace").strip()
                    if not line.startswith("data:"):
                        continue
                    try:
                        ev = json.loads(line[5:].strip())
                    except ValueError:
                        continue
                    if isinstance(ev, dict) and ev.get("type") == "permission.asked":
                        on_asked(ev.get("properties") or {})
        except Exception as e:
            sse["error"] = repr(e)[:150]
        sse["connected"] = False
        if not stop.is_set():
            time.sleep(0.5)


out = {
    "ok": False,
    "text": "",
    "error": None,
    "retryable": False,
    "tool_attempts": [],
    "session_id": None,
    "elapsed": 0.0,
    "permission_list_error": None,
    "sse_error": None,
}
t0 = time.time()
sid = None
finished = False
reader = threading.Thread(target=sse_loop)
reader.daemon = True
reader.start()
# подписка должна быть до промпта, иначе событие можно пропустить
wait_until = time.time() + 3.0
while not sse["connected"] and time.time() < wait_until:
    time.sleep(0.05)
try:
    sid = call("POST", "/session", {
        "title": REQ["title"],
        "permission": [{"permission": "*", "pattern": "*", "action": "ask"}],
    })["id"]
    out["session_id"] = sid
    state["sid"] = sid

    body = {
        "model": {"providerID": REQ["provider_id"], "modelID": REQ["model_id"]},
        "parts": [{"type": "text", "text": REQ["user"]}],
    }
    if REQ.get("system"):
        body["system"] = REQ["system"]
    if REQ.get("agent"):
        body["agent"] = REQ["agent"]
    call("POST", "/session/" + sid + "/prompt_async", body)

    deadline = t0 + float(REQ["timeout"])
    tool_since = {}
    while True:
        try:
            for p in call("GET", "/permission") or []:
                on_asked(p)
        except CallError as e:
            out["permission_list_error"] = str(e)[:300]
        for pid, info in list(asked.items()):
            if not info["rejected"]:
                reject(pid, info)
        try:
            for q in call("GET", "/question") or []:
                if q.get("sessionID") == sid:
                    call("POST", "/question/" + q["id"] + "/reject", {})
        except CallError:
            pass

        status = (call("GET", "/session/status") or {}).get(sid) or {"type": "idle"}
        messages = call("GET", "/session/" + sid + "/message") or []
        attempts = [
            {"permission": i["permission"], "patterns": i["patterns"]}
            for i in asked.values()
        ]
        last = messages[-1] if messages else None
        if last and (last.get("info") or {}).get("role") == "assistant":
            info = last["info"]
            if info.get("error"):
                err = info["error"]
                out["error"] = error_message(err)
                out["retryable"] = bool((err.get("data") or {}).get("isRetryable"))
                out["tool_attempts"] = attempts + tool_parts(messages)
                finished = True
                break
            completed = (info.get("time") or {}).get("completed")
            if completed and status.get("type") == "idle":
                out["text"] = "".join(
                    p.get("text", "")
                    for p in last.get("parts") or []
                    if p.get("type") == "text" and not p.get("synthetic")
                ).strip()
                out["tool_attempts"] = attempts + tool_parts(messages)
                out["ok"] = True
                finished = True
                break

        # инструмент завис в ожидании разрешения, которое мы не смогли отклонить
        # (событие пропущено, список не отдаётся): ждать 300 с бессмысленно
        now = time.time()
        stuck = []
        for m in messages:
            for p in m.get("parts") or []:
                if p.get("type") != "tool":
                    continue
                if (p.get("state") or {}).get("status") in ("pending", "running"):
                    key = p.get("callID") or p.get("id")
                    tool_since.setdefault(key, now)
                    if now - tool_since[key] > TOOL_STALL:
                        stuck.append(p.get("tool"))
        if stuck:
            out["error"] = "tool_blocked: %s" % ", ".join(sorted(set(str(s) for s in stuck)))
            out["retryable"] = True
            out["tool_attempts"] = attempts + tool_parts(messages)
            break

        if now > deadline:
            out["error"] = "timeout after %ss" % REQ["timeout"]
            out["retryable"] = True
            break
        time.sleep(float(REQ.get("poll_interval", 1.0)))
except CallError as e:
    out["error"] = str(e)
    out["retryable"] = e.retryable
except Exception as e:
    out["error"] = repr(e)
    out["retryable"] = True
finally:
    stop.set()
    out["sse_error"] = sse["error"] if not sse["connected"] else None
    if sid and not finished:
        try:
            call("POST", "/session/" + sid + "/abort", {})
        except Exception:
            pass
    # Сессию удаляем после успешного ответа, если в конфиге delete_session=true
    # (по умолчанию). Иначе сессии копятся в OpenCode.
    if sid and finished and REQ.get("delete_session"):
        try:
            call("DELETE", "/session/" + sid)
        except Exception:
            pass
    out["elapsed"] = round(time.time() - t0, 2)
    sys.stdout.write(MARK + json.dumps(out, ensure_ascii=True) + "\n")
    sys.stdout.flush()
'''


@dataclass
class OpencodeSshDriver:
    """Модель OpenCode на удалённом хосте через SSH."""

    host: str
    user: str = ""
    port: int = 22
    key_file: str | None = None
    # обязательный параметр: "provider/model", как его знает ваш OpenCode
    model: str = ""
    # адрес `opencode serve` на удалённом хосте (виден только с самого хоста)
    api_url: str = "http://127.0.0.1:4096"
    agent: str | None = None
    timeout: float = 300.0
    connect_timeout: float = 10.0
    poll_interval: float = 1.0
    # сколько секунд инструмент может висеть без ответа на разрешение, прежде чем вызов считается залипшим
    tool_stall: float = 25.0
    retries: int = 3
    # паузы между повторами (обрывы SSH и таймауты обычно проходят за минуту)
    retry_delays: list[float] = field(default_factory=lambda: [5.0, 15.0, 45.0])
    ssh_bin: str = "ssh"
    ssh_options: list[str] = field(default_factory=list)
    remote_python: str = "python3"
    # удалять сессию OpenCode после ответа (иначе сессии копятся)
    delete_session: bool = True
    title: str = "stamp-flow"
    log_hook: LogHook | None = field(default=None, repr=False)
    sleep: Callable[[float], None] = field(default=time.sleep, repr=False)

    CONFIG_EXCLUDES = frozenset({"log_hook", "sleep"})

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> OpencodeSshDriver:
        allowed = {f.name for f in fields(cls)} - cls.CONFIG_EXCLUDES
        params = {k: v for k, v in config.items() if k != "driver"}
        unknown = sorted(set(params) - allowed)
        if unknown:
            raise ValueError(
                f"Неизвестные параметры llm-драйвера opencode_ssh: {', '.join(unknown)}"
            )
        if not params.get("host"):
            raise ValueError("Для llm-драйвера opencode_ssh нужен параметр 'host'")
        if not params.get("user"):
            raise ValueError("Для llm-драйвера opencode_ssh нужен параметр 'user' (SSH-пользователь)")
        if not params.get("model"):
            raise ValueError(
                "Для llm-драйвера opencode_ssh нужен параметр 'model' в виде 'provider/model'"
            )
        if params.get("key_file"):
            params["key_file"] = os.path.expanduser(params["key_file"])
        return cls(**params)

    @property
    def provider_id(self) -> str:
        return self._split_model()[0]

    @property
    def model_id(self) -> str:
        return self._split_model()[1]

    def _split_model(self) -> tuple[str, str]:
        provider, sep, model = self.model.partition("/")
        if not sep or not provider or not model:
            raise ValueError(
                f"model должен быть в виде 'provider/model', получено: {self.model!r}"
            )
        return provider, model

    def ssh_command(self) -> list[str]:
        cmd = [
            self.ssh_bin,
            "-p",
            str(self.port),
            "-o",
            "BatchMode=yes",
            "-o",
            f"ConnectTimeout={int(self.connect_timeout)}",
            "-o",
            "ServerAliveInterval=15",
        ]
        if self.key_file:
            cmd += ["-i", self.key_file]
        cmd += list(self.ssh_options)
        cmd += [(f"{self.user}@{self.host}" if self.user else self.host), f"{self.remote_python} -"]
        return cmd

    def build_script(self, system: str, user: str) -> str:
        request = {
            "api_url": self.api_url,
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "agent": self.agent,
            "system": system,
            "user": user,
            "title": self.title,
            "timeout": self.timeout,
            "poll_interval": self.poll_interval,
            "tool_stall": self.tool_stall,
            "delete_session": self.delete_session,
        }
        encoded = base64.b64encode(
            json.dumps(request, ensure_ascii=False).encode("utf-8")
        ).decode("ascii")
        return REMOTE_SCRIPT.replace("__REQUEST_B64__", encoded).replace(
            "__RESULT_MARK__", RESULT_MARK
        )

    def complete(
        self,
        system: str,
        user: str,
        *,
        temperature: float | None = None,
    ) -> str:
        if temperature is not None:
            logger.debug("opencode_ssh: temperature не поддерживается, игнорирую")
        return self.run(system, user).text

    def run(self, system: str, user: str) -> LLMResult:
        self._emit(
            "llm_request",
            {
                "driver": "opencode_ssh",
                "host": self.host,
                "model": self.model,
                "system": system,
                "user": user,
            },
        )
        script = self.build_script(system, user)
        last_error: LLMError | None = None
        hinted = False

        for attempt in range(1, self.retries + 2):
            try:
                data = self._execute(script)
            except LLMError as ex:
                last_error = ex
            else:
                if data.get("permission_list_error"):
                    self._note_permission_list_broken(data.get("permission_list_error"))
                if data.get("ok"):
                    result = LLMResult(
                        text=data.get("text") or "",
                        elapsed=float(data.get("elapsed") or 0.0),
                        tool_attempts=list(data.get("tool_attempts") or []),
                        session_id=data.get("session_id"),
                        attempts=attempt,
                    )
                    if result.tool_attempts:
                        logger.warning(
                            "opencode_ssh: модель пыталась вызвать инструменты "
                            "(отклонено): %s",
                            result.tool_attempts,
                        )
                    self._emit(
                        "llm_response",
                        {
                            "text": result.text,
                            "elapsed": result.elapsed,
                            "tool_attempts": result.tool_attempts,
                            "session_id": result.session_id,
                            "attempts": attempt,
                        },
                    )
                    return result
                last_error = LLMError(
                    f"OpenCode: {data.get('error') or 'unknown error'}",
                    retryable=bool(data.get("retryable")),
                )

            logger.warning(
                "opencode_ssh: попытка %d/%d не удалась: %s",
                attempt,
                self.retries + 1,
                last_error,
            )
            if "tool_blocked" in str(last_error) and not hinted and attempt <= self.retries:
                # модель потянулась к инструменту (ссылка в тексте) и застряла: тот же
                # запрос споткнётся так же — повторяем сразу, с напоминанием «инструментов нет»
                hinted = True
                script = self.build_script(system + NO_TOOLS_HINT, user)
                self._emit("llm_retry_no_tools", {"error": str(last_error)})
                continue
            if not last_error.retryable or attempt > self.retries:
                break
            delays = self.retry_delays or [5.0]
            self.sleep(float(delays[min(attempt - 1, len(delays) - 1)]))

        assert last_error is not None
        self._emit(
            "llm_error",
            {"error": str(last_error), "retryable": last_error.retryable},
        )
        raise last_error

    _permission_list_warned = False

    def _note_permission_list_broken(self, detail: str | None) -> None:
        """GET /permission на сервере сломан (закрепившийся запрос без timeout). Работаем по /event."""
        if self._permission_list_warned:
            return
        self._permission_list_warned = True
        logger.warning(
            "opencode_ssh: GET /permission не отдаёт список (%s) — отклоняю запросы инструментов "
            "по потоку событий; сломанный запрос пропадёт при перезапуске opencode на удалённом хосте",
            (detail or "")[:160],
        )
        self._emit("llm_permission_list_broken", {"detail": (detail or "")[:300]})

    def _execute(self, script: str) -> dict[str, Any]:
        try:
            proc = subprocess.run(
                self.ssh_command(),
                input=script,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout + self.connect_timeout + 60,
            )
        except subprocess.TimeoutExpired as ex:
            raise LLMError(f"ssh: таймаут {ex.timeout:.0f}с", retryable=True) from ex
        except OSError as ex:
            raise LLMError(f"ssh не запустился: {ex}") from ex

        for line in reversed(proc.stdout.splitlines()):
            if line.startswith(RESULT_MARK):
                try:
                    return json.loads(line[len(RESULT_MARK) :])
                except ValueError as ex:
                    raise LLMError(f"битый ответ с удалённого хоста: {ex}") from ex

        stderr = (proc.stderr or "").strip()[-500:]
        # 255 — ошибка самого ssh (сеть, ключ, хост), остальное — удалённая сторона
        raise LLMError(
            f"ssh {self.user}@{self.host} вернул код {proc.returncode}"
            + (f": {stderr}" if stderr else ""),
            retryable=proc.returncode == 255,
        )

    def health(self, timeout: float = 20.0) -> tuple[bool, str]:
        """Хост и OpenCode живы? (SSH + HTTP к api_url). Никогда не бросает."""
        script = (
            "import json,sys,urllib.request\n"
            f"base={self.api_url.rstrip('/')!r}\n"
            "for path in ('/session/status',):\n"
            "    try:\n"
            "        urllib.request.urlopen(base+path, timeout=8).read(); print('OK'); sys.exit(0)\n"
            "    except Exception as e:\n"
            "        err=repr(e)\n"
            "print('FAIL '+err)\n"
        )
        try:
            proc = subprocess.run(
                self.ssh_command(),
                input=script,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout + self.connect_timeout,
            )
        except Exception as ex:  # noqa: BLE001
            return False, f"ssh: {type(ex).__name__}: {ex}"
        out = (proc.stdout or "").strip().splitlines()
        if out and out[-1] == "OK":
            return True, "ok"
        detail = (out[-1] if out else "") or (proc.stderr or "").strip()[-300:]
        return False, detail or f"ssh код {proc.returncode}"

    def _emit(self, event: str, payload: dict[str, Any]) -> None:
        logger.debug("%s %s", event, payload)
        if self.log_hook:
            try:
                self.log_hook(event, payload)
            except Exception:
                logger.exception("opencode_ssh: log_hook упал")


DRIVERS: dict[str, Any] = {
    "opencode_ssh": OpencodeSshDriver,
}


def create_llm_driver(
    config: Mapping[str, Any],
    *,
    log_hook: LogHook | None = None,
) -> LLMDriver:
    name = config.get("driver", "opencode_ssh")
    try:
        cls = DRIVERS[name]
    except KeyError:
        raise ValueError(
            f"Неизвестный llm-драйвер {name!r}. Доступны: {', '.join(DRIVERS)}"
        ) from None
    driver = cls.from_config(config)
    if log_hook is not None:
        driver.log_hook = log_hook
    return driver
