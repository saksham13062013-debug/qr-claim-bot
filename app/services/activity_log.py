"""Global + per-group activity logging to a Telegram channel."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from aiogram import Bot
from aiogram.enums import ParseMode

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


async def log_event(
    bot: Optional[Bot],
    event_type: str,
    text: str,
    *,
    group_log_chat_id: Optional[int] = None,
) -> None:
    """
    Send a log line to:
    1) LOG_CHANNEL_ID (global, from env) if set
    2) group-specific log channel if provided
    """
    if not bot:
        try:
            from app.bot import get_bot
            bot = get_bot()
        except Exception:
            bot = None
    if not bot:
        return

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    body = (
        f"<b>LOG</b> · <code>{event_type}</code>\n"
        f"<i>{stamp}</i>\n\n"
        f"{text}"
    )[:4000]

    targets: list[int] = []
    global_id = getattr(settings, "log_channel_id", None)
    if global_id:
        try:
            targets.append(int(global_id))
        except Exception:
            pass
    if group_log_chat_id:
        try:
            targets.append(int(group_log_chat_id))
        except Exception:
            pass

    for cid in dict.fromkeys(targets):
        try:
            await bot.send_message(cid, body, parse_mode=ParseMode.HTML)
        except Exception as exc:
            logger.warning("log_event failed chat=%s: %s", cid, exc)
