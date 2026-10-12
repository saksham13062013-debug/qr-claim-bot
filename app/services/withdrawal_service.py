"""Withdrawal request and admin processing – manual payouts only."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.models import (
    AuditAction,
    NotificationType,
    TransactionType,
    User,
    Withdrawal,
    WithdrawalStatus,
)
from app.security import generate_reference
from app.services.audit_service import write_audit
from app.services.notification_service import create_notification
from app.services.wallet_service import credit_wallet

settings = get_settings()


async def create_withdrawal(
    session: AsyncSession,
    user_id: int,
    amount_cents: int,
    payout_method: Optional[str] = None,
    payout_details: Optional[str] = None,
) -> Withdrawal:
    if amount_cents < settings.min_withdrawal_cents:
        raise ValueError(f"MINIMUM_WITHDRAWAL_{settings.min_withdrawal_cents}")
    if amount_cents > settings.max_withdrawal_cents:
        raise ValueError(f"MAXIMUM_WITHDRAWAL_{settings.max_withdrawal_cents}")
    if amount_cents <= 0:
        raise ValueError("INVALID_AMOUNT")

    result = await session.execute(
        select(User).where(User.id == user_id).with_for_update()
    )
    user = result.scalar_one_or_none()
    if not user:
        raise ValueError("USER_NOT_FOUND")
    if user.balance_cents < amount_cents:
        raise ValueError("INSUFFICIENT_BALANCE")

    ref = generate_reference("WD")
    await credit_wallet(
        session,
        user=user,
        amount_cents=-amount_cents,
        type_=TransactionType.WITHDRAWAL,
        reference=ref,
        description=f"Withdrawal request {ref}",
    )

    method = (payout_method or "").upper() or None
    if method and method not in ("UPI", "BINANCE"):
        method = None

    withdrawal = Withdrawal(
        user_id=user.id,
        amount_cents=amount_cents,
        status=WithdrawalStatus.PENDING,
        payout_method=method,
        payout_details=(payout_details or "")[:255] or None,
    )
    session.add(withdrawal)
    await session.flush()

    await write_audit(
        session,
        action=AuditAction.WITHDRAWAL_CREATED,
        actor_user_id=user.id,
        target_type="withdrawal",
        target_id=str(withdrawal.id),
        metadata={
            "amount_cents": amount_cents,
            "payout_method": method,
        },
    )
    return withdrawal


async def mark_withdrawal_paid(
    session: AsyncSession,
    withdrawal_id: int,
    admin_id: int,
    payment_reference: Optional[str] = None,
) -> Withdrawal:
    result = await session.execute(
        select(Withdrawal).where(Withdrawal.id == withdrawal_id).with_for_update()
    )
    w = result.scalar_one_or_none()
    if not w:
        raise ValueError("WITHDRAWAL_NOT_FOUND")
    if w.status == WithdrawalStatus.PAID:
        return w
    if w.status not in (WithdrawalStatus.PENDING, WithdrawalStatus.HELD):
        raise ValueError(f"WITHDRAWAL_NOT_PAYABLE_{w.status.value}")

    pay_key = payment_reference or generate_reference("PAY")
    w.status = WithdrawalStatus.PAID
    w.reviewed_by_admin_id = admin_id
    w.reviewed_at = datetime.now(timezone.utc)
    w.paid_at = datetime.now(timezone.utc)
    w.payment_reference = payment_reference
    w.payment_reference_key = pay_key
    await session.flush()

    await write_audit(
        session,
        action=AuditAction.WITHDRAWAL_PAID,
        actor_admin_id=admin_id,
        target_type="withdrawal",
        target_id=str(w.id),
        metadata={"payment_reference": payment_reference},
    )
    await create_notification(
        session,
        user_id=w.user_id,
        type_=NotificationType.WITHDRAWAL_PAID,
        title="Withdrawal paid",
        message=f"Your withdrawal of ${w.amount_cents/100:.2f} was marked paid. Ref: {pay_key}",
    )
    return w


async def reject_withdrawal(
    session: AsyncSession,
    withdrawal_id: int,
    admin_id: int,
    reason: str,
) -> Withdrawal:
    result = await session.execute(
        select(Withdrawal).where(Withdrawal.id == withdrawal_id).with_for_update()
    )
    w = result.scalar_one_or_none()
    if not w:
        raise ValueError("WITHDRAWAL_NOT_FOUND")
    if w.status == WithdrawalStatus.REJECTED:
        return w
    if w.status not in (WithdrawalStatus.PENDING, WithdrawalStatus.HELD):
        raise ValueError(f"WITHDRAWAL_NOT_REJECTABLE_{w.status.value}")

    user = (
        await session.execute(
            select(User).where(User.id == w.user_id).with_for_update()
        )
    ).scalar_one()

    ref = generate_reference("WRF")
    await credit_wallet(
        session,
        user=user,
        amount_cents=w.amount_cents,
        type_=TransactionType.WITHDRAWAL_REFUND,
        reference=ref,
        description=f"Withdrawal rejection refund #{w.id}",
        withdrawal_id=w.id,
    )

    w.status = WithdrawalStatus.REJECTED
    w.reviewed_by_admin_id = admin_id
    w.reviewed_at = datetime.now(timezone.utc)
    w.rejection_reason = reason[:500]
    await session.flush()

    await write_audit(
        session,
        action=AuditAction.WITHDRAWAL_REJECTED,
        actor_admin_id=admin_id,
        target_type="withdrawal",
        target_id=str(w.id),
        metadata={"reason": reason},
    )
    await create_notification(
        session,
        user_id=w.user_id,
        type_=NotificationType.WITHDRAWAL_REJECTED,
        title="Withdrawal rejected",
        message=f"Your withdrawal was rejected and refunded. Reason: {reason}",
    )
    return w


async def hold_withdrawal(
    session: AsyncSession,
    withdrawal_id: int,
    admin_id: int,
) -> Withdrawal:
    result = await session.execute(
        select(Withdrawal).where(Withdrawal.id == withdrawal_id).with_for_update()
    )
    w = result.scalar_one_or_none()
    if not w:
        raise ValueError("WITHDRAWAL_NOT_FOUND")
    if w.status not in (WithdrawalStatus.PENDING, WithdrawalStatus.HELD):
        raise ValueError(f"WITHDRAWAL_NOT_HOLDABLE_{w.status.value}")
    w.status = WithdrawalStatus.HELD
    w.reviewed_by_admin_id = admin_id
    w.reviewed_at = datetime.now(timezone.utc)
    await session.flush()
    await write_audit(
        session,
        action=AuditAction.WITHDRAWAL_HELD,
        actor_admin_id=admin_id,
        target_type="withdrawal",
        target_id=str(w.id),
    )
    return w


async def list_withdrawals(
    session: AsyncSession,
    *,
    status: Optional[WithdrawalStatus] = None,
    offset: int = 0,
    limit: int = 50,
) -> tuple[Sequence[Withdrawal], int]:
    from sqlalchemy import func

    q = select(Withdrawal).options(selectinload(Withdrawal.user))
    cq = select(func.count()).select_from(Withdrawal)
    if status:
        q = q.where(Withdrawal.status == status)
        cq = cq.where(Withdrawal.status == status)
    total = (await session.execute(cq)).scalar() or 0
    items = (
        await session.execute(
            q.order_by(Withdrawal.created_at.desc()).offset(offset).limit(limit)
        )
    ).scalars().all()
    return items, total
