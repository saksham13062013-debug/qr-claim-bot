"""Force-join channel membership checks."""

from __future__ import annotations

import logging
from typing import Optional, Sequence

from aiogram import Bot
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramAPIError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import ForceJoinChannel

logger = logging.getLogger(__name__)
settings = get_settings()


async def get_active_required_channels(
    session: AsyncSession,
) -> Sequence[ForceJoinChannel]:
    result = await session.execute(
        select(ForceJoinChannel).where(
            ForceJoinChannel.is_active.is_(True),
            ForceJoinChannel.is_required.is_(True),
        )
    )
    return result.scalars().all()


async def check_membership(
    bot: Bot,
    telegram_user_id: int,
    channels: Sequence[ForceJoinChannel],
) -> tuple[bool, list[ForceJoinChannel]]:
    """
    Returns (all_joined, missing_channels).
    Handles Telegram API errors safely.
    """
    if not channels:
        return True, []

    missing: list[ForceJoinChannel] = []
    for ch in channels:
        try:
            member = await bot.get_chat_member(
                chat_id=ch.channel_id, user_id=telegram_user_id
            )
            status = member.status
            if status in (
                ChatMemberStatus.LEFT,
                ChatMemberStatus.KICKED,
            ):
                missing.append(ch)
            # creator, administrator, member, restricted (if not kicked) are OK
        except TelegramAPIError as exc:
            logger.warning(
                "Membership check failed for channel %s user %s: %s",
                ch.channel_id,
                telegram_user_id,
                exc,
            )
            # Treat API errors as "not joined" to be safe
            missing.append(ch)
        except Exception as exc:
            logger.exception(
                "Unexpected error checking membership channel %s: %s",
                ch.channel_id,
                exc,
            )
            missing.append(ch)

    return len(missing) == 0, list(missing)


async def is_force_join_enabled(session: AsyncSession) -> bool:
    """Check global setting, falling back to env config."""
    from app.services.settings_service import get_setting

    val = await get_setting(session, "force_join_enabled")
    if val is not None:
        return val.lower() in ("true", "1", "yes")
    return settings.force_join_enabled


async def list_channels(
    session: AsyncSession,
) -> Sequence[ForceJoinChannel]:
    result = await session.execute(
        select(ForceJoinChannel).order_by(ForceJoinChannel.id)
    )
    return result.scalars().all()


async def create_channel(
    session: AsyncSession,
    *,
    channel_id: int,
    title: str,
    username: Optional[str] = None,
    invite_url: Optional[str] = None,
    is_required: bool = True,
    is_active: bool = True,
) -> ForceJoinChannel:
    ch = ForceJoinChannel(
        channel_id=channel_id,
        title=title,
        username=username,
        invite_url=invite_url,
        is_required=is_required,
        is_active=is_active,
    )
    session.add(ch)
    await session.flush()
    return ch


async def update_channel(
    session: AsyncSession,
    channel: ForceJoinChannel,
    **kwargs,
) -> ForceJoinChannel:
    for k, v in kwargs.items():
        if hasattr(channel, k) and v is not None:
            setattr(channel, k, v)
    await session.flush()
    return channel
