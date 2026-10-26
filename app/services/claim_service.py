"""Claim approval, rejection and hold with idempotent wallet credit."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import (
    Admin,
    AdminRole,
    AuditAction,
    ClaimStatus,
    NotificationType,
    QRClaim,
    QRCode,
    QRStatus,
    TransactionType,
    User,
)
from app.security import generate_reference
from app.services.audit_service import write_audit
from app.services.notification_service import create_notification
from app.services.wallet_service import credit_wallet

logger = logging.getLogger(__name__)


async def get_claim_by_id(session: AsyncSession, claim_id: int) -> Optional[QRClaim]:
    result = await session.execute(
        select(QRClaim)
        .options(selectinload(QRClaim.qr_code), selectinload(QRClaim.user))
        .where(QRClaim.id == claim_id)
    )
    return result.scalar_one_or_none()


async def approve_claim(
    session: AsyncSession,
    claim_id: int,
    admin_id: int,
) -> QRClaim:
    result = await session.execute(
        select(QRClaim).where(QRClaim.id == claim_id).with_for_update()
    )
    claim = result.scalar_one_or_none()
    if claim is None:
        raise ValueError("CLAIM_NOT_FOUND")
    if claim.status == ClaimStatus.APPROVED:
        return claim
    if claim.status != ClaimStatus.PENDING:
        raise ValueError(f"CLAIM_NOT_PENDING_{claim.status.value}")

    qr = (
        await session.execute(
            select(QRCode).where(QRCode.id == claim.qr_code_id).with_for_update()
        )
    ).scalar_one_or_none()
    if qr is None:
        raise ValueError("QR_NOT_FOUND")

    user = (
        await session.execute(
            select(User).where(User.id == claim.user_id).with_for_update()
        )
    ).scalar_one()

    now = datetime.now(timezone.utc)
    approval_ref = generate_reference("APR")

    await credit_wallet(
        session,
        user=user,
        amount_cents=claim.reward_cents,
        type_=TransactionType.REWARD,
        reference=approval_ref,
        description=f"Reward for QR {qr.code}",
        admin_id=admin_id,
        claim_id=claim.id,
    )

    claim.status = ClaimStatus.APPROVED
    claim.reviewed_by_admin_id = admin_id
    claim.reviewed_at = now
    claim.approved_at = now
    claim.approval_reference = approval_ref

    qr.status = QRStatus.CLAIMED
    qr.approved_at = now

    await session.flush()

    await write_audit(
        session,
        action=AuditAction.CLAIM_APPROVED,
        actor_admin_id=admin_id,
        target_type="qr_claim",
        target_id=str(claim.id),
        metadata={"qr_code": qr.code, "user_id": user.id, "reward_cents": claim.reward_cents},
    )
    app_label = (getattr(qr, "app_name", None) or "").strip()
    title = f"Task #{qr.id}" + (f" · {app_label}" if app_label else "")
    await create_notification(
        session,
        user_id=user.id,
        type_=NotificationType.CLAIM_APPROVED,
        title="Claim approved",
        message=(
            f"<b>Approved</b> · {title}\n"
            f"Reward: <b>${claim.reward_cents / 100:.2f}</b>\n"
            f"Credited to your wallet.\n"
            f"Menu → Balance / Withdraw"
        ),
    )
    return claim


async def reject_claim(
    session: AsyncSession,
    claim_id: int,
    admin_id: int,
    reason: str,
    photo_bytes: Optional[bytes] = None,
    photo_filename: str = "reject.jpg",
) -> QRClaim:
    result = await session.execute(
        select(QRClaim).where(QRClaim.id == claim_id).with_for_update()
    )
    claim = result.scalar_one_or_none()
    if claim is None:
        raise ValueError("CLAIM_NOT_FOUND")
    if claim.status == ClaimStatus.REJECTED:
        return claim
    if claim.status not in (ClaimStatus.PENDING, ClaimStatus.HELD):
        raise ValueError(f"CLAIM_NOT_REJECTABLE_{claim.status.value}")

    qr = (
        await session.execute(
            select(QRCode).where(QRCode.id == claim.qr_code_id).with_for_update()
        )
    ).scalar_one_or_none()

    now = datetime.now(timezone.utc)
    claim.status = ClaimStatus.REJECTED
    claim.reviewed_by_admin_id = admin_id
    claim.reviewed_at = now
    claim.rejected_at = now
    claim.rejection_reason = reason[:500]

    if qr and qr.status in (QRStatus.PENDING, QRStatus.POSTED):
        qr.status = QRStatus.AVAILABLE
        qr.claimed_by_user_id = None
        qr.claimed_at = None

    await session.flush()
    await write_audit(
        session,
        action=AuditAction.CLAIM_REJECTED,
        actor_admin_id=admin_id,
        target_type="qr_claim",
        target_id=str(claim.id),
        metadata={"reason": reason},
    )
    qr_id = qr.id if qr else claim.qr_code_id
    app_label = (getattr(qr, "app_name", None) or "").strip() if qr else ""
    reason_clean = (reason or "No reason").strip()[:300]
    title = f"Task #{qr_id}" + (f" · {app_label}" if app_label else "")
    await create_notification(
        session,
        user_id=claim.user_id,
        type_=NotificationType.CLAIM_REJECTED,
        title="Claim rejected",
        message=(
            f"<b>Rejected</b> · {title}\n"
            f"Reason: {reason_clean}\n"
            f"This claim will not be paid.\n"
            f"Wait for the next task to claim again."
        ),
        photo_bytes=photo_bytes,
        photo_filename=photo_filename or "reject.jpg",
    )
    return claim


async def hold_claim(
    session: AsyncSession,
    claim_id: int,
    admin_id: int,
) -> QRClaim:
    result = await session.execute(
        select(QRClaim).where(QRClaim.id == claim_id).with_for_update()
    )
    claim = result.scalar_one_or_none()
    if claim is None:
        raise ValueError("CLAIM_NOT_FOUND")
    if claim.status not in (ClaimStatus.PENDING, ClaimStatus.HELD):
        raise ValueError(f"CLAIM_NOT_HOLDABLE_{claim.status.value}")
    claim.status = ClaimStatus.HELD
    claim.reviewed_by_admin_id = admin_id
    claim.reviewed_at = datetime.now(timezone.utc)
    await session.flush()
    await write_audit(
        session,
        action=AuditAction.CLAIM_HELD,
        actor_admin_id=admin_id,
        target_type="qr_claim",
        target_id=str(claim.id),
    )
    qr = (
        await session.execute(select(QRCode).where(QRCode.id == claim.qr_code_id))
    ).scalar_one_or_none()
    app_label = (getattr(qr, "app_name", None) or "").strip() if qr else ""
    title = f"Task #{qr.id if qr else claim.qr_code_id}" + (
        f" · {app_label}" if app_label else ""
    )
    await create_notification(
        session,
        user_id=claim.user_id,
        type_=NotificationType.CLAIM_SUBMITTED,
        title="Claim on hold",
        message=(
            f"<b>On hold</b> · {title}\n"
            f"Reward: <b>${claim.reward_cents / 100:.2f}</b>\n"
            f"Under extra review.\n"
            f"You will be notified when decided."
        ),
    )
    return claim


async def list_claims(
    session: AsyncSession,
    *,
    status: Optional[ClaimStatus] = None,
    offset: int = 0,
    limit: int = 50,
) -> tuple[Sequence[QRClaim], int]:
    from sqlalchemy import func

    q = select(QRClaim).options(
        selectinload(QRClaim.qr_code), selectinload(QRClaim.user)
    )
    cq = select(func.count()).select_from(QRClaim)
    if status:
        q = q.where(QRClaim.status == status)
        cq = cq.where(QRClaim.status == status)
    total = (await session.execute(cq)).scalar() or 0
    items = (
        await session.execute(
            q.order_by(QRClaim.created_at.desc()).offset(offset).limit(limit)
        )
    ).scalars().all()
    return items, total


async def try_auto_approve(session: AsyncSession, claim_id: int) -> Optional[QRClaim]:
    from app.services import settings_service

    enabled = await settings_service.get_setting_bool(
        session, "auto_approve_enabled", False
    )
    if not enabled:
        return None
    max_usd = await settings_service.get_setting(session, "auto_approve_max_usd")
    try:
        max_cents = int(round(float(max_usd or "0") * 100))
    except (TypeError, ValueError):
        max_cents = 0
    if max_cents <= 0:
        return None
    claim = await get_claim_by_id(session, claim_id)
    if not claim or claim.status != ClaimStatus.PENDING:
        return None
    if claim.reward_cents > max_cents:
        return None
    owner = (
        await session.execute(select(Admin).where(Admin.role == AdminRole.OWNER).limit(1))
    ).scalar_one_or_none()
    if not owner:
        return None
    return await approve_claim(session, claim_id, owner.id)
