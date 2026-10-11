"""Claim approval, rejection and hold with idempotent wallet credit."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import (
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


async def get_claim_by_id(
    session: AsyncSession, claim_id: int
) -> Optional[QRClaim]:
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
    """
    Approve a pending claim and credit the user's wallet exactly once.

    Idempotent: if already approved, returns the existing claim without
    double-crediting (enforced by status check + unique approval_reference).
    """
    # Lock claim row
    result = await session.execute(
        select(QRClaim)
        .where(QRClaim.id == claim_id)
        .with_for_update()
    )
    claim = result.scalar_one_or_none()
    if claim is None:
        raise ValueError("CLAIM_NOT_FOUND")

    if claim.status == ClaimStatus.APPROVED:
        # Already approved – idempotent success
        return claim

    if claim.status != ClaimStatus.PENDING:
        raise ValueError(f"CLAIM_NOT_PENDING_{claim.status.value}")

    # Lock related QR
    qr_result = await session.execute(
        select(QRCode).where(QRCode.id == claim.qr_code_id).with_for_update()
    )
    qr = qr_result.scalar_one_or_none()
    if qr is None:
        raise ValueError("QR_NOT_FOUND")

    # Lock user for wallet update
    user_result = await session.execute(
        select(User).where(User.id == claim.user_id).with_for_update()
    )
    user = user_result.scalar_one_or_none()
    if user is None:
        raise ValueError("USER_NOT_FOUND")

    now = datetime.now(timezone.utc)
    approval_ref = generate_reference("APR")

    # Credit wallet (creates transaction with unique reference)
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
        metadata={
            "qr_code": qr.code,
            "user_id": user.id,
            "reward_cents": claim.reward_cents,
            "reference": approval_ref,
        },
    )

    await create_notification(
        session,
        user_id=user.id,
        type_=NotificationType.CLAIM_APPROVED,
        title="Reward Approved!",
        message=(
            f"🎉 Reward Approved!\n\n"
            f"🎟 QR: {qr.code}\n"
            f"💰 Added: ${claim.reward_cents / 100:.2f}\n\n"
            f"Your wallet has been credited."
        ),
        metadata={"claim_id": claim.id, "qr_code": qr.code},
    )

    return claim


async def reject_claim(
    session: AsyncSession,
    claim_id: int,
    admin_id: int,
    reason: str,
) -> QRClaim:
    if not reason or not reason.strip():
        raise ValueError("REJECTION_REASON_REQUIRED")

    result = await session.execute(
        select(QRClaim).where(QRClaim.id == claim_id).with_for_update()
    )
    claim = result.scalar_one_or_none()
    if claim is None:
        raise ValueError("CLAIM_NOT_FOUND")

    if claim.status == ClaimStatus.REJECTED:
        return claim  # idempotent

    if claim.status not in (ClaimStatus.PENDING, ClaimStatus.HELD):
        raise ValueError(f"CLAIM_NOT_REJECTABLE_{claim.status.value}")

    qr_result = await session.execute(
        select(QRCode).where(QRCode.id == claim.qr_code_id).with_for_update()
    )
    qr = qr_result.scalar_one_or_none()

    now = datetime.now(timezone.utc)
    claim.status = ClaimStatus.REJECTED
    claim.rejection_reason = reason.strip()
    claim.reviewed_by_admin_id = admin_id
    claim.reviewed_at = now
    claim.rejected_at = now

    if qr:
        qr.status = QRStatus.REJECTED

    await session.flush()

    await write_audit(
        session,
        action=AuditAction.CLAIM_REJECTED,
        actor_admin_id=admin_id,
        target_type="qr_claim",
        target_id=str(claim.id),
        metadata={
            "qr_code": qr.code if qr else None,
            "reason": reason.strip(),
        },
    )

    await create_notification(
        session,
        user_id=claim.user_id,
        type_=NotificationType.CLAIM_REJECTED,
        title="Claim Rejected",
        message=(
            f"❌ Claim Rejected\n\n"
            f"QR: {qr.code if qr else 'N/A'}\n\n"
            f"Reason:\n{reason.strip()}"
        ),
        metadata={"claim_id": claim.id, "reason": reason.strip()},
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

    if claim.status == ClaimStatus.HELD:
        return claim

    if claim.status != ClaimStatus.PENDING:
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
    count_q = select(func.count()).select_from(QRClaim)
    if status:
        q = q.where(QRClaim.status == status)
        count_q = count_q.where(QRClaim.status == status)

    total = (await session.execute(count_q)).scalar() or 0
    q = q.order_by(QRClaim.created_at.desc()).offset(offset).limit(limit)
    result = await session.execute(q)
    return result.scalars().all(), total


async def try_auto_approve(
    session: AsyncSession,
    claim_id: int,
) -> Optional[QRClaim]:
    """If auto-approve is enabled and reward <= max, approve as system (admin_id=None path uses 0)."""
    from app.services import settings_service

    enabled = await settings_service.get_setting_bool(session, "auto_approve_enabled", False)
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

    # Approve with system admin id 0 not allowed by FK – use first OWNER admin
    from app.models import Admin, AdminRole
    owner = (
        await session.execute(
            select(Admin).where(Admin.role == AdminRole.OWNER).limit(1)
        )
    ).scalar_one_or_none()
    if not owner:
        return None
    return await approve_claim(session, claim_id, owner.id)
