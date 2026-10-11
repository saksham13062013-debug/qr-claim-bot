"""Application settings stored in database with env fallbacks."""

from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import Setting

settings = get_settings()

# Keys managed in DB
MANAGED_KEYS = {
    "default_reward_cents",
    "min_withdrawal_cents",
    "max_withdrawal_cents",
    "force_join_enabled",
    "claim_approval_required",
    "qr_default_expiry_hours",
    "maintenance_mode",
    "bot_welcome_message",
}


async def get_setting(session: AsyncSession, key: str) -> Optional[str]:
    result = await session.execute(select(Setting).where(Setting.key == key))
    row = result.scalar_one_or_none()
    return row.value if row else None


async def get_setting_int(session: AsyncSession, key: str, default: int) -> int:
    val = await get_setting(session, key)
    if val is None:
        return default
    try:
        return int(val)
    except ValueError:
        return default


async def get_setting_bool(session: AsyncSession, key: str, default: bool) -> bool:
    val = await get_setting(session, key)
    if val is None:
        return default
    return val.lower() in ("true", "1", "yes")


async def set_setting(
    session: AsyncSession,
    key: str,
    value: str,
    admin_id: Optional[int] = None,
    description: Optional[str] = None,
) -> Setting:
    result = await session.execute(select(Setting).where(Setting.key == key))
    row = result.scalar_one_or_none()
    if row:
        row.value = value
        row.updated_by_admin_id = admin_id
        if description:
            row.description = description
    else:
        row = Setting(
            key=key,
            value=value,
            description=description,
            updated_by_admin_id=admin_id,
        )
        session.add(row)
    await session.flush()
    return row


async def get_all_settings(session: AsyncSession) -> dict[str, Any]:
    """Return effective settings (DB overrides env)."""
    result = await session.execute(select(Setting))
    db_map = {s.key: s.value for s in result.scalars().all()}

    return {
        "default_reward_cents": int(
            db_map.get("default_reward_cents", settings.default_reward_cents)
        ),
        "min_withdrawal_cents": int(
            db_map.get("min_withdrawal_cents", settings.min_withdrawal_cents)
        ),
        "max_withdrawal_cents": int(
            db_map.get("max_withdrawal_cents", settings.max_withdrawal_cents)
        ),
        "force_join_enabled": (
            db_map.get("force_join_enabled", str(settings.force_join_enabled)).lower()
            in ("true", "1", "yes")
        ),
        "claim_approval_required": (
            db_map.get(
                "claim_approval_required", str(settings.claim_approval_required)
            ).lower()
            in ("true", "1", "yes")
        ),
        "qr_default_expiry_hours": int(
            db_map.get(
                "qr_default_expiry_hours", settings.qr_default_expiry_hours
            )
        ),
        "maintenance_mode": (
            db_map.get("maintenance_mode", str(settings.maintenance_mode)).lower()
            in ("true", "1", "yes")
        ),
        "auto_approve_enabled": (
            db_map.get("auto_approve_enabled", "false").lower() in ("1", "true", "yes")
        ),
        "auto_approve_max_usd": float(db_map.get("auto_approve_max_usd", "0") or 0),
        "bot_welcome_message": db_map.get(
            "bot_welcome_message", settings.bot_welcome_message
        ),
    }


async def seed_default_settings(session: AsyncSession) -> None:
    """Seed settings table from env if empty."""
    defaults = {
        "default_reward_cents": str(settings.default_reward_cents),
        "min_withdrawal_cents": str(settings.min_withdrawal_cents),
        "max_withdrawal_cents": str(settings.max_withdrawal_cents),
        "force_join_enabled": str(settings.force_join_enabled).lower(),
        "claim_approval_required": str(settings.claim_approval_required).lower(),
        "qr_default_expiry_hours": str(settings.qr_default_expiry_hours),
        "maintenance_mode": str(settings.maintenance_mode).lower(),
        "bot_welcome_message": settings.bot_welcome_message,
        "auto_approve_enabled": "false",
        "auto_approve_max_usd": "0",
    }
    for key, value in defaults.items():
        existing = await get_setting(session, key)
        if existing is None:
            await set_setting(session, key, value, description=f"Default {key}")
