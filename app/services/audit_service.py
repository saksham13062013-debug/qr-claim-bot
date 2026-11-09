"""Audit logging service."""

from __future__ import annotations

from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditAction, AuditLog


async def write_audit(
    session: AsyncSession,
    *,
    action: AuditAction,
    actor_admin_id: Optional[int] = None,
    actor_user_id: Optional[int] = None,
    target_type: Optional[str] = None,
    target_id: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
) -> AuditLog:
    """Write an audit log entry. Never log secrets."""
    # Sanitize metadata – strip any accidental secrets
    safe_meta = None
    if metadata:
        safe_meta = {
            k: v
            for k, v in metadata.items()
            if k.lower()
            not in ("password", "token", "secret", "jwt", "bot_token", "api_key")
        }

    entry = AuditLog(
        actor_admin_id=actor_admin_id,
        actor_user_id=actor_user_id,
        action=action,
        target_type=target_type,
        target_id=target_id,
        metadata_=safe_meta,
        ip_address=ip_address,
        user_agent=user_agent[:512] if user_agent else None,
    )
    session.add(entry)
    await session.flush()
    return entry
