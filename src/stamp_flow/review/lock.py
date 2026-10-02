"""Межпроцессный lock на действия с hh (обход cron ↔ review-submit)."""

from __future__ import annotations

import os
import time
from pathlib import Path


class LockTimeout(TimeoutError):
    pass


class FileLock:
    def __init__(self, path: Path, timeout: float = 600.0, poll: float = 0.5):
        self.path = Path(path)
        self.timeout = timeout
        self.poll = poll
        self._fp = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fp = open(self.path, "a+b")  # noqa: SIM115
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                _lock(fp)
                break
            except OSError:
                if time.monotonic() > deadline:
                    fp.close()
                    raise LockTimeout(f"lock занят: {self.path}") from None
                time.sleep(self.poll)
        self._fp = fp

    def release(self) -> None:
        if self._fp is not None:
            try:
                _unlock(self._fp)
            finally:
                self._fp.close()
                self._fp = None

    def __enter__(self) -> FileLock:
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()


if os.name == "nt":
    import msvcrt

    def _lock(fp) -> None:
        fp.seek(0)
        msvcrt.locking(fp.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock(fp) -> None:
        fp.seek(0)
        msvcrt.locking(fp.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _lock(fp) -> None:
        fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fp) -> None:
        fcntl.flock(fp.fileno(), fcntl.LOCK_UN)


def hh_lock(config_path: Path, timeout: float = 600.0) -> FileLock:
    return FileLock(Path(config_path) / ".hh-apply.lock", timeout=timeout)


PASS_LOCK = ".hh-pass.lock"


def pass_running(config_path: Path | str) -> bool:
    """Идёт ли сейчас обход (в любом контейнере): его lock занят. Свободен — обхода нет."""
    lock = FileLock(Path(config_path) / PASS_LOCK, timeout=0)
    try:
        lock.acquire()
    except LockTimeout:
        return True
    except OSError:
        return True  # не смогли проверить — считаем, что идёт (безопаснее не трогать)
    lock.release()
    return False
