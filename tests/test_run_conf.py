"""run.py читает conf.json строго: лишний/недостающий ключ — ошибка, флаги берутся из конфига."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _run_module():
    spec = importlib.util.spec_from_file_location("hh_run", ROOT / "run.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_real_conf_is_valid():
    run = _run_module()
    conf = run.load_conf()
    args = run.pass_args(conf)
    assert "--work-format" in args
    # ревью — поток без очереди: отдельного переключателя в конфиге нет
    assert "block_on_review" not in conf
    assert not any("block-on-review" in a for a in args)


def test_conf_is_strict(tmp_path):
    run = _run_module()
    conf = json.loads((ROOT / "conf.json").read_text("utf-8-sig"))
    path = tmp_path / "conf.json"

    path.write_text(json.dumps({**conf, "block_on_review": False}), "utf-8")
    with pytest.raises(run.ConfError):  # лишний ключ
        run.load_conf(path)

    del conf["limits"]
    path.write_text(json.dumps(conf), "utf-8")
    with pytest.raises(run.ConfError):  # нет ключа
        run.load_conf(path)


def test_restart_refuses_only_when_pass_lives_in_that_container(monkeypatch, capsys):
    run = _run_module()
    monkeypatch.setattr(run, "pass_hosts", lambda: ["stamp_flow_cron"])  # обход из cron
    assert run.refuse_if_busy(False, "перезапуск бота", only=("stamp_flow_bot",)) is False  # бота не тронет
    assert run.refuse_if_busy(False, "пересоздание") is True  # пересоздание всех убило бы
    assert "--force" in capsys.readouterr().err
    assert run.refuse_if_busy(True, "пересоздание") is False  # --force
    monkeypatch.setattr(run, "pass_hosts", lambda: [])
    assert run.refuse_if_busy(False, "пересоздание") is False


def test_crontab_is_generated_from_conf(tmp_path, monkeypatch):
    run = _run_module()
    conf = run.load_conf()
    monkeypatch.setattr(run, "SCHEDULE", tmp_path / "config" / "crontab")
    run.write_crontab(conf)
    text = (tmp_path / "config" / "crontab").read_text("utf-8")
    assert "\r" not in text and text.endswith("\n")
    assert f"*/{conf['cron_every_min']} * * * *" in text
    assert "python /app/run.py pass" in text
    assert "update-resumes" in text
    assert "%" not in text  # в crontab `%` начинает stdin команды
    assert "hh_applicant_tool" not in text  # только наш модуль
