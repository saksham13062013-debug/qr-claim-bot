"""Notification creation and Telegram delivery helpers."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Notification, NotificationType

logger = logging.getLogger(__name__)

_bot = None


def set_bot(bot) -> None:
    global _bot
    _bot = bot


async def create_notification(
    session: AsyncSession,
    *,
    user_id: Optional[int],
    type_: NotificationType,
    title: str,
    message: str,
    metadata: Optional[dict[str, Any]] = None,
    send_telegram: bool = True,
    photo_bytes: Optional[bytes] = None,
    photo_filename: str = "image.jpg",
) -> Notification:
    notif = Notification(
        user_id=user_id,
        type=type_,
        title=title,
        message=message,
        metadata_=metadata,
        telegram_sent=False,
    )
    session.add(notif)
    await session.flush()

    if send_telegram and user_id and _bot is not None:
        try:
            from sqlalchemy import select

            from aiogram.types import BufferedInputFile

            from app.models import User

            result = await session.execute(
                select(User.telegram_id).where(User.id == user_id)
            )
            tg_id = result.scalar_one_or_none()
            if tg_id:
                try:
                    if photo_bytes:
                        photo = BufferedInputFile(
                            photo_bytes, filename=photo_filename or "image.jpg"
                        )
                        await _bot.send_photo(
                            chat_id=tg_id,
                            photo=photo,
                            caption=message[:1024],
                            parse_mode="HTML",
                        )
                    else:
                        await _bot.send_message(
                            chat_id=tg_id, text=message, parse_mode="HTML"
                        )
                except Exception:
                    plain = (
                        message.replace("<b>", "")
                        .replace("</b>", "")
                        .replace("<code>", "")
                        .replace("</code>", "")
                        .replace("<i>", "")
                        .replace("</i>", "")
                    )
                    try:
                        if photo_bytes:
                            from aiogram.types import BufferedInputFile

                            photo = BufferedInputFile(
                                photo_bytes, filename=photo_filename or "image.jpg"
                            )
                            await _bot.send_photo(
                                chat_id=tg_id, photo=photo, caption=plain[:1024]
                            )
                        else:
                            await _bot.send_message(chat_id=tg_id, text=plain)
                    except Exception:
                        await _bot.send_message(chat_id=tg_id, text=plain)
                notif.telegram_sent = True
                notif.sent_at = datetime.now(timezone.utc)
                await session.flush()
        except Exception as exc:
            logger.warning(
                "Failed to send Telegram notification to user %s: %s",
                user_id,
                type(exc).__name__,
            )
    return notif
