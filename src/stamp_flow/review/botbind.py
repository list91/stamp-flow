"""Состояние ревью привязано к конкретному Telegram-боту.

Токен можно сменить (перенос на другого бота). Тогда числа, накопленные в БД и в файлах
рядом, принадлежат СТАРОМУ боту:

  * смещение getUpdates старого бота у нового больше самих обновлений — новый бот молчал бы;
  * id сообщений с кнопками, отсчётами, спиннером и картинками капчи в новом чате указывают
    на чужие или несуществующие сообщения (бот пытался бы их править и удалять).

Поэтому при первом старте с новым ботом всё это сбрасывается, а сообщения, оставленные в
чате старого бота, по возможности убираются (если у нас есть его токен, например он стал
ботом уведомлений).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__package__)

BOT_ID_KEY = "review.telegram.bot_id"
# settings, значения которых имеют смысл только в чате одного бота
SCOPED_SETTINGS = (
    "review.telegram.offset",
    "review.telegram.keyboard_msg",
    "review.captcha.msgs",
)
# файлы с id сообщений бота (отсчёт паузы, спиннер проверки)
SCOPED_FILES = ("review-wait-msg.json", "review-check-msg.json")


def _msg_ids(settings: Any, config_dir: Path) -> list[int]:
    """Сообщения основного бота, которые остались в чате старого."""
    ids: list[int] = []
    kb = settings.get_value("review.telegram.keyboard_msg")
    if kb:
        try:
            ids.append(int(kb))
        except (TypeError, ValueError):
            pass
    try:
        ids += [int(x) for x in json.loads(settings.get_value("review.captcha.msgs") or "[]")]
    except (TypeError, ValueError):
        pass
    for name in SCOPED_FILES:
        try:
            data = json.loads((config_dir / name).read_text("utf-8"))
            if data.get("msg_id"):
                ids.append(int(data["msg_id"]))
        except (OSError, ValueError, TypeError, AttributeError):
            continue
    return sorted(set(ids))


def bind(
    settings: Any,
    config_dir: Path | str,
    bot_id: int | str,
    cleaner: Callable[[list[int]], None] | None = None,
) -> dict[str, Any] | None:
    """Привязать состояние к боту. None — бот тот же; иначе {"previous", "cleaned"} после сброса.

    cleaner(ids) убирает сообщения старого бота из его чата (нужен токен старого бота).
    """
    config_dir = Path(config_dir)
    saved = settings.get_value(BOT_ID_KEY)
    if saved is not None and str(saved) == str(bot_id):
        return None
    ids = _msg_ids(settings, config_dir)
    cleaned = 0
    if cleaner and ids:
        try:
            cleaner(ids)
            cleaned = len(ids)
        except Exception:  # noqa: BLE001 — уборка за старым ботом не должна мешать запуску
            logger.warning("Не удалось убрать сообщения старого бота", exc_info=True)
    for key in SCOPED_SETTINGS:
        settings.delete_value(key)
    for name in SCOPED_FILES:
        try:
            (config_dir / name).unlink(missing_ok=True)
        except OSError:
            pass
    settings.set_value(BOT_ID_KEY, str(bot_id))
    return {"previous": saved, "cleaned": cleaned, "found": len(ids)}
