"""QR code creation, posting, claiming with atomic guarantees."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, Sequence

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import (
    AuditAction,
    ClaimStatus,
    NotificationType,
    QRClaim,
    QRCode,
    QRStatus,
)
from app.security import generate_qr_code, generate_reference
from app.services.audit_service import write_audit
from app.services.notification_service import create_notification

logger = logging.getLogger(__name__)


async def create_qr_codes(
    session: AsyncSession,
    *,
    reward_cents: int,
    quantity: int,
    expires_at: Optional[datetime],
    campaign_name: Optional[str],
    description: Optional[str],
    admin_id: int,
    app_name: Optional[str] = None,
) -> list[QRCode]:
    """Create one or more cryptographically secure QR codes."""
    if reward_cents <= 0:
        raise ValueError("Reward must be positive")
    if quantity < 1 or quantity > 500:
        raise ValueError("Quantity must be between 1 and 500")

    app_clean = (app_name or "").strip()[:128] or None

    codes: list[QRCode] = []
    for _ in range(quantity):
        # Retry on extremely unlikely collision
        for attempt in range(5):
            code_str = generate_qr_code()
            existing = await session.execute(
                select(QRCode.id).where(QRCode.code == code_str)
            )
            if existing.scalar_one_or_none() is None:
                break
        else:
            raise RuntimeError("Failed to generate unique QR code")

        qr = QRCode(
            code=code_str,
            reward_cents=reward_cents,
            status=QRStatus.AVAILABLE,
            campaign_name=campaign_name,
            app_name=app_clean,
            description=description,
            created_by_admin_id=admin_id,
            expires_at=expires_at,
        )
        session.add(qr)
        codes.append(qr)

    await session.flush()

    await write_audit(
        session,
        action=AuditAction.QR_CREATED,
        actor_admin_id=admin_id,
        target_type="qr_batch",
        target_id=str(codes[0].id) if codes else None,
        metadata={
            "count": quantity,
            "reward_cents": reward_cents,
            "campaign": campaign_name,
            "codes": [c.code for c in codes],
        },
    )
    return codes


async def get_qr_by_code(session: AsyncSession, code: str) -> Optional[QRCode]:
    result = await session.execute(
        select(QRCode).where(QRCode.code == code.upper().strip())
    )
    return result.scalar_one_or_none()


async def get_qr_by_id(session: AsyncSession, qr_id: int) -> Optional[QRCode]:
    result = await session.execute(select(QRCode).where(QRCode.id == qr_id))
    return result.scalar_one_or_none()


async def mark_qr_posted(
    session: AsyncSession,
    qr: QRCode,
    admin_id: int,
) -> QRCode:
    if qr.status not in (QRStatus.AVAILABLE, QRStatus.RECLAIMED):
        raise ValueError(f"Cannot post QR in status {qr.status.value}")
    qr.status = QRStatus.POSTED
    qr.posted_at = datetime.now(timezone.utc)
    await session.flush()
    await write_audit(
        session,
        action=AuditAction.QR_POSTED,
        actor_admin_id=admin_id,
        target_type="qr_code",
        target_id=str(qr.id),
        metadata={"code": qr.code},
    )
    return qr


async def claim_qr_atomic(
    session: AsyncSession,
    *,
    code: str,
    user_id: int,
) -> tuple[QRClaim, QRCode]:
    """
    Atomically claim a QR using row-level locking.

    Guarantees only one successful claim even under concurrent requests.
    """
    # Lock the QR row
    result = await session.execute(
        select(QRCode)
        .where(QRCode.code == code.upper().strip())
        .with_for_update()
    )
    qr = result.scalar_one_or_none()
    if qr is None:
        raise ValueError("QR_NOT_FOUND")

    now = datetime.now(timezone.utc)

    # Status checks
    if qr.status in (QRStatus.CLAIMED, QRStatus.PENDING, QRStatus.APPROVED):
        raise ValueError("QR_ALREADY_CLAIMED")
    if qr.status in (QRStatus.DISABLED, QRStatus.REJECTED, QRStatus.EXPIRED):
        raise ValueError(f"QR_{qr.status.value}")
    if qr.status not in (QRStatus.AVAILABLE, QRStatus.POSTED, QRStatus.RECLAIMED):
        raise ValueError(f"QR_INVALID_STATUS_{qr.status.value}")

    # Expiry
    if qr.expires_at and qr.expires_at < now:
        qr.status = QRStatus.EXPIRED
        await session.flush()
        raise ValueError("QR_EXPIRED")

    # Check no existing claim (unique constraint also protects)
    existing_claim = await session.execute(
        select(QRClaim).where(QRClaim.qr_code_id == qr.id)
    )
    if existing_claim.scalar_one_or_none():
        raise ValueError("QR_ALREADY_CLAIMED")

    # Create claim
    claim = QRClaim(
        qr_code_id=qr.id,
        user_id=user_id,
        reward_cents=qr.reward_cents,
        status=ClaimStatus.PENDING,
    )
    session.add(claim)

    # Update QR
    qr.status = QRStatus.PENDING
    qr.claimed_by_user_id = user_id
    qr.claimed_at = now

    await session.flush()

    await write_audit(
        session,
        action=AuditAction.CLAIM_CREATED,
        actor_user_id=user_id,
        target_type="qr_claim",
        target_id=str(claim.id),
        metadata={"qr_code": qr.code, "reward_cents": qr.reward_cents},
    )

    app_label = (getattr(qr, "app_name", None) or "").strip()
    title = f"Task #{qr.id}" + (f" · {app_label}" if app_label else "")
    await create_notification(
        session,
        user_id=user_id,
        type_=NotificationType.CLAIM_SUBMITTED,
        title="Claim submitted",
        message=(
            f"<b>Submitted</b> · {title}\n"
            f"Reward: <b>${qr.reward_cents / 100:.2f}</b>\n"
            f"Status: Pending review\n"
            f"We will notify you here after approval."
        ),
        metadata={"claim_id": claim.id, "qr_code": qr.code},
        send_telegram=False,
    )

    return claim, qr


async def reclaim_qr(
    session: AsyncSession,
    qr: QRCode,
    admin_id: int,
    *,
    invalidate_claim: bool = True,
) -> QRCode:
    """Reclaim a QR – OWNER / SUPER_ADMIN (or permitted MANAGER)."""
    if qr.status not in (
        QRStatus.PENDING,
        QRStatus.APPROVED,
        QRStatus.CLAIMED,
        QRStatus.POSTED,
        QRStatus.AVAILABLE,
    ):
        raise ValueError(f"Cannot reclaim QR in status {qr.status.value}")

    old_status = qr.status.value
    old_claim_id = None

    if invalidate_claim:
        result = await session.execute(
            select(QRClaim).where(QRClaim.qr_code_id == qr.id)
        )
        claim = result.scalar_one_or_none()
        if claim and claim.status == ClaimStatus.PENDING:
            claim.status = ClaimStatus.CANCELLED
            old_claim_id = claim.id

    qr.status = QRStatus.RECLAIMED
    qr.claimed_by_user_id = None
    qr.claimed_at = None
    qr.approved_at = None
    await session.flush()

    await write_audit(
        session,
        action=AuditAction.QR_RECLAIMED,
        actor_admin_id=admin_id,
        target_type="qr_code",
        target_id=str(qr.id),
        metadata={
            "code": qr.code,
            "old_status": old_status,
            "invalidated_claim_id": old_claim_id,
        },
    )
    return qr


async def disable_qr(
    session: AsyncSession, qr: QRCode, admin_id: int
) -> QRCode:
    qr.status = QRStatus.DISABLED
    await session.flush()
    await write_audit(
        session,
        action=AuditAction.QR_DISABLED,
        actor_admin_id=admin_id,
        target_type="qr_code",
        target_id=str(qr.id),
        metadata={"code": qr.code},
    )
    return qr


async def list_qrs(
    session: AsyncSession,
    *,
    status: Optional[QRStatus] = None,
    campaign: Optional[str] = None,
    offset: int = 0,
    limit: int = 50,
) -> tuple[Sequence[QRCode], int]:
    from sqlalchemy import func

    q = select(QRCode).options(
        selectinload(QRCode.claimed_by),
        selectinload(QRCode.created_by),
    )
    count_q = select(func.count()).select_from(QRCode)
    if status:
        q = q.where(QRCode.status == status)
        count_q = count_q.where(QRCode.status == status)
    if campaign:
        q = q.where(QRCode.campaign_name.ilike(f"%{campaign}%"))
        count_q = count_q.where(QRCode.campaign_name.ilike(f"%{campaign}%"))

    total = (await session.execute(count_q)).scalar() or 0
    q = q.order_by(QRCode.created_at.desc()).offset(offset).limit(limit)
    result = await session.execute(q)
    return result.scalars().all(), total
