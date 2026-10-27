"""Admin authentication, bootstrap and RBAC helpers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import Admin, AdminRole, AuditAction
from app.security import hash_password, verify_password
from app.services.audit_service import write_audit

settings = get_settings()


async def bootstrap_owner(session: AsyncSession) -> Admin:
    """Create the initial OWNER admin from environment variables if none exists."""
    result = await session.execute(
        select(Admin).where(Admin.role == AdminRole.OWNER)
    )
    owner = result.scalar_one_or_none()
    if owner:
        return owner

    # Check if username already taken by another role
    result = await session.execute(
        select(Admin).where(Admin.username == settings.admin_username)
    )
    existing = result.scalar_one_or_none()
    if existing:
        existing.role = AdminRole.OWNER
        existing.password_hash = hash_password(settings.admin_password)
        existing.is_active = True
        existing.telegram_id = settings.owner_telegram_id
        await session.flush()
        return existing

    owner = Admin(
        username=settings.admin_username,
        password_hash=hash_password(settings.admin_password),
        role=AdminRole.OWNER,
        telegram_id=settings.owner_telegram_id,
        display_name="Owner",
        is_active=True,
    )
    session.add(owner)
    await session.flush()
    return owner


async def authenticate_admin(
    session: AsyncSession,
    username: str,
    password: str,
    *,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
) -> Optional[Admin]:
    result = await session.execute(
        select(Admin).where(Admin.username == username)
    )
    admin = result.scalar_one_or_none()

    if not admin or not admin.is_active:
        await write_audit(
            session,
            action=AuditAction.ADMIN_LOGIN_FAILED,
            metadata={"username": username, "reason": "not_found_or_inactive"},
            ip_address=ip_address,
            user_agent=user_agent,
        )
        return None

    # Check lockout
    now = datetime.now(timezone.utc)
    if admin.locked_until and admin.locked_until > now:
        await write_audit(
            session,
            action=AuditAction.ADMIN_LOGIN_FAILED,
            actor_admin_id=admin.id,
            metadata={"reason": "locked"},
            ip_address=ip_address,
            user_agent=user_agent,
        )
        return None

    if not verify_password(password, admin.password_hash):
        admin.failed_login_attempts += 1
        if admin.failed_login_attempts >= settings.max_login_attempts:
            admin.locked_until = now + timedelta(minutes=settings.lockout_minutes)
            admin.failed_login_attempts = 0
        await session.flush()
        await write_audit(
            session,
            action=AuditAction.ADMIN_LOGIN_FAILED,
            actor_admin_id=admin.id,
            metadata={"reason": "bad_password"},
            ip_address=ip_address,
            user_agent=user_agent,
        )
        return None

    # Success
    admin.failed_login_attempts = 0
    admin.locked_until = None
    admin.last_login_at = now
    await session.flush()

    await write_audit(
        session,
        action=AuditAction.ADMIN_LOGIN,
        actor_admin_id=admin.id,
        ip_address=ip_address,
        user_agent=user_agent,
    )
    return admin


async def get_admin_by_id(session: AsyncSession, admin_id: int) -> Optional[Admin]:
    result = await session.execute(select(Admin).where(Admin.id == admin_id))
    return result.scalar_one_or_none()
