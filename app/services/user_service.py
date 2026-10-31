"""User registration and management service."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User, UserStatus


async def get_or_create_user(
    session: AsyncSession,
    telegram_id: int,
    username: Optional[str] = None,
    first_name: Optional[str] = None,
    last_name: Optional[str] = None,
) -> User:
    """Get existing user or create a new one. Always uses authenticated telegram_id."""
    result = await session.execute(
        select(User).where(User.telegram_id == telegram_id)
    )
    user = result.scalar_one_or_none()

    now = datetime.now(timezone.utc)
    if user:
        # Update profile fields if changed
        changed = False
        if username is not None and user.username != username:
            user.username = username
            changed = True
        if first_name is not None and user.first_name != first_name:
            user.first_name = first_name
            changed = True
        if last_name is not None and user.last_name != last_name:
            user.last_name = last_name
            changed = True
        user.last_seen_at = now
        if changed:
            user.updated_at = now
        return user

    user = User(
        telegram_id=telegram_id,
        username=username,
        first_name=first_name,
        last_name=last_name,
        status=UserStatus.ACTIVE,
        balance_cents=0,
        pending_cents=0,
        total_earned_cents=0,
        last_seen_at=now,
    )
    session.add(user)
    await session.flush()
    return user


async def get_user_by_telegram_id(
    session: AsyncSession, telegram_id: int
) -> Optional[User]:
    result = await session.execute(
        select(User).where(User.telegram_id == telegram_id)
    )
    return result.scalar_one_or_none()


async def get_user_by_id(session: AsyncSession, user_id: int) -> Optional[User]:
    result = await session.execute(select(User).where(User.id == user_id))
    return result.scalar_one_or_none()


async def set_user_status(
    session: AsyncSession, user: User, status: UserStatus
) -> User:
    user.status = status
    user.updated_at = datetime.now(timezone.utc)
    await session.flush()
    return user
