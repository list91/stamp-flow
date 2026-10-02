#!/usr/bin/env python3
"""Запуск обхода строго по conf.json.

    python run.py          — loop=true: config/crontab из conf + docker compose up -d
                             loop=false: один обход здесь и сейчас
    python run.py pass     — один обход по conf.json (так его зовёт cron)
    python run.py args     — показать аргументы apply-review из conf.json
    python run.py stop     — docker compose down
    python run.py restart  — перезапуск бота; отказывается, пока идёт обход (--force — всё равно)
    python run.py busy     — идёт ли обход (код 0 — идёт, 1 — нет)

Запуск и перезапуск контейнеров тоже отказываются, пока идёт обход: иначе он убивается
на полпути (--force — всё равно).

conf.json читается при каждом обходе: правка значений действует со следующего
обхода; смена loop / cron_every_min — после `python run.py`.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONF = ROOT / "conf.json"
SCHEDULE = ROOT / "config" / "crontab"  # расписание cron-контейнера, создаётся здесь

SCHEMA: dict[str, object] = {
    "loop": bool,
    "cron_every_min": int,
    "review": str,
    "search": {"work_format": str, "order_by": str},
    "rotation": {"per_resume": (int, str), "rounds": (int, str)},
    "timeouts_sec": {"apply_delay": str, "resume_delay": str, "round_delay": str},
    "limits": {"max_per_hour": int},
    "relevance": {"apply_from": (int, float)},
}
INT_RANGE = re.compile(r"^(\d+)(?:-(\d+))?$")
RANGE = re.compile(r"^\d+(\.\d+)?(-\d+(\.\d+)?)?$")


class ConfError(ValueError):
    pass


def _check(data: dict, schema: dict, path: str = "") -> None:
    extra = set(data) - set(schema)
    missing = set(schema) - set(data)
    if extra:
        raise ConfError(f"лишние ключи {path or 'в корне'}: {sorted(extra)}")
    if missing:
        raise ConfError(f"нет ключей {path or 'в корне'}: {sorted(missing)}")
    for key, kind in schema.items():
        value, where = data[key], f"{path}{key}"
        if isinstance(kind, dict):
            if not isinstance(value, dict):
                raise ConfError(f"{where}: нужен объект")
            _check(value, kind, where + ".")
        elif kind is int and (not isinstance(value, int) or isinstance(value, bool)):
            raise ConfError(f"{where}: нужно целое число, а не {value!r}")
        elif isinstance(kind, tuple):
            if isinstance(value, bool) or not isinstance(value, kind):
                raise ConfError(f"{where}: нужно число или «MIN-MAX», а не {value!r}")
        elif not isinstance(value, kind):
            raise ConfError(f"{where}: нужен {kind.__name__}, а не {value!r}")


def load_conf(path: Path = CONF) -> dict:
    try:
        data = json.loads(path.read_text("utf-8-sig"))
    except (OSError, ValueError) as ex:
        raise ConfError(f"не читается {path}: {ex}") from None
    _check(data, SCHEMA)
    if data["review"] != "telegram":
        raise ConfError("review: поддерживается только \"telegram\"")
    if not 1 <= data["cron_every_min"] <= 59:
        raise ConfError("cron_every_min: от 1 до 59")
    for key in ("per_resume", "rounds"):
        value = str(data["rotation"][key]).replace(" ", "")
        m = INT_RANGE.match(value)
        if not m or int(m.group(1)) < 1 or (m.group(2) and int(m.group(2)) < int(m.group(1))):
            raise ConfError(f"rotation.{key}: целое ≥1 или «MIN-MAX» (MIN ≤ MAX), а не {value!r}")
    if data["limits"]["max_per_hour"] < 0:
        raise ConfError("limits.max_per_hour: не меньше 0 (0 — без лимита)")
    apply_from = data["relevance"]["apply_from"]
    if isinstance(apply_from, bool) or not 0 <= apply_from <= 1:
        raise ConfError("relevance.apply_from: число от 0 до 1 (0 — без оценки релевантности)")
    for key, value in data["timeouts_sec"].items():
        if not RANGE.match(value.replace(" ", "")):
            raise ConfError(f"timeouts_sec.{key}: формат «MIN-MAX» или «N» секунд, а не {value!r}")
    return data


def pass_args(conf: dict) -> list[str]:
    """conf.json → аргументы apply-review (всё явно, без умолчаний)."""
    t, r, s = conf["timeouts_sec"], conf["rotation"], conf["search"]
    return [
        "--work-format", s["work_format"],
        "--order-by", s["order_by"],
        "--per-resume", str(r["per_resume"]).replace(" ", ""),
        "--rounds", str(r["rounds"]).replace(" ", ""),
        "--apply-delay", t["apply_delay"].replace(" ", ""),
        "--resume-delay", t["resume_delay"].replace(" ", ""),
        "--round-delay", t["round_delay"].replace(" ", ""),
        "--max-per-hour", str(conf["limits"]["max_per_hour"]),
        "--relevance-min", str(conf["relevance"]["apply_from"]),
    ]


def run_pass(conf: dict) -> int:
    cmd = [sys.executable, "-m", "stamp_flow", "--config-dir", str(ROOT / "config"),
           "apply-review", *pass_args(conf)]
    print("▶", " ".join(cmd[3:]), flush=True)
    env = {
        **os.environ,
        "HH_LOOP": "1" if conf["loop"] else "0",
        "HH_CRON_EVERY_MIN": str(conf["cron_every_min"]),
    }
    return subprocess.call(cmd, env=env)


def crontab_text(conf: dict) -> str:
    """Расписание cron-контейнера: подъём резюме раз в 5 часов и обход каждые N минут."""
    log = ">> /var/log/stamp-flow.log 2>&1"
    return (
        "SHELL=/bin/bash\n"
        "PATH=/usr/local/bin:/usr/bin:/bin\n"
        "# Файл создаёт run.py по conf.json. Руками не править.\n"
        f"0 */5 * * * sleep $(shuf -i 60-300 -n 1) && python -m stamp_flow --config-dir /app/config update-resumes {log}\n"
        f"*/{conf['cron_every_min']} * * * * sleep $(shuf -i 30-120 -n 1) && "
        f"flock -n /tmp/pass.lock python /app/run.py pass {log}\n"
    )


def write_crontab(conf: dict) -> None:
    SCHEDULE.parent.mkdir(parents=True, exist_ok=True)
    SCHEDULE.write_text(crontab_text(conf), "utf-8", newline="\n")


def compose(*args: str) -> int:
    return subprocess.call(["docker", "compose", *args], cwd=ROOT)


def local_busy() -> bool:
    """Идёт ли обход — по его lock (запускать внутри контейнера)."""
    sys.path.insert(0, str(ROOT / "src"))
    from stamp_flow.review.lock import pass_running

    return pass_running(ROOT / "config")


CONTAINERS = ("stamp_flow_cron", "stamp_flow_bot")


def pass_hosts() -> list[str]:
    """В каких контейнерах сейчас крутится обход (процесс apply-review)."""
    hosts = []
    for name in CONTAINERS:
        r = subprocess.run(
            ["docker", "exec", name, "sh", "-c", "ps aux | grep -v grep | grep -q apply-review"],
            capture_output=True,
        )
        if r.returncode == 0:
            hosts.append(name)
    return hosts


def refuse_if_busy(force: bool, what: str, only: tuple[str, ...] = CONTAINERS) -> bool:
    """True — отказаться: в перезапускаемом контейнере идёт обход, и перезапуск его убьёт."""
    if force:
        return False
    hosts = [h for h in pass_hosts() if h in only]
    if not hosts:
        return False
    print(
        f"⛔ Идёт обход (контейнер {', '.join(hosts)}) — {what} убьёт его на полпути "
        "(останутся брошенные спиннеры и счётчики).\n"
        "   Дождись конца обхода (в боте «⏳ Ждём до следующего обхода») или добавь --force.",
        file=sys.stderr,
    )
    return True


def main(argv: list[str]) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    force = "--force" in argv
    argv = [a for a in argv if a != "--force"]
    cmd = argv[0] if argv else "start"
    if cmd == "stop":
        return compose("down")
    if cmd == "busy":  # 0 — обход идёт, 1 — нет (для скриптов и для docker exec)
        busy = local_busy()
        print("обход идёт" if busy else "обхода нет")
        return 0 if busy else 1
    if cmd == "restart":  # безопасный перезапуск бота (обход из cron живёт в другом контейнере)
        if refuse_if_busy(force, "перезапуск бота", only=("stamp_flow_bot",)):
            return 3
        return compose("restart", "bot")
    try:
        conf = load_conf()
    except ConfError as ex:
        print(f"❌ conf.json: {ex}", file=sys.stderr)
        return 2
    if cmd == "args":
        print(" ".join(pass_args(conf)))
        return 0
    if cmd == "pass":
        return run_pass(conf)
    if cmd != "start":
        print(__doc__)
        return 2
    if conf["loop"]:
        if refuse_if_busy(force, "пересоздание контейнеров"):
            return 3
        write_crontab(conf)
        print(f"🔁 loop: обход каждые {conf['cron_every_min']} мин по conf.json → docker compose up -d")
        return compose("up", "-d", "--build", "--force-recreate")
    print("▶ loop=false: один обход")
    compose("up", "-d", "--no-deps", "bot")  # бот нужен для ревью и капчи; cron не поднимаем
    return run_pass(conf)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
