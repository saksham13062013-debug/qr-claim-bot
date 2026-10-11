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
    *,
    user: User,
    amount_cents: int,
) -> Withdrawal:
    """
    Create a pending withdrawal and reserve funds by deducting balance.

    Accounting model: deduct on request, refund on rejection.
    """
    if amount_cents < settings.min_withdrawal_cents:
        raise ValueError(
            f"MINIMUM_WITHDRAWAL_{settings.min_withdrawal_cents}"
        )
    if amount_cents > settings.max_withdrawal_cents:
        raise ValueError(
            f"MAXIMUM_WITHDRAWAL_{settings.max_withdrawal_cents}"
        )
    if amount_cents <= 0:
        raise ValueError("INVALID_AMOUNT")

    # Lock user
    result = await session.execute(
        select(User).where(User.id == user.id).with_for_update()
    )
    user = result.scalar_one()

    if user.balance_cents < amount_cents:
        raise ValueError("INSUFFICIENT_BALANCE")

    # Deduct balance (reserve)
    ref = generate_reference("WD")
    await credit_wallet(
        session,
        user=user,
        amount_cents=-amount_cents,
        type_=TransactionType.WITHDRAWAL,
        reference=ref,
        description=f"Withdrawal request #{ref}",
    )

    withdrawal = Withdrawal(
        user_id=user.id,
        amount_cents=amount_cents,
        status=WithdrawalStatus.PENDING,
    )
    session.add(withdrawal)
    await session.flush()

    # Link transaction to withdrawal
    from app.models import WalletTransaction

    tx_result = await session.execute(
        select(WalletTransaction).where(WalletTransaction.reference == ref)
    )
    tx = tx_result.scalar_one()
    tx.withdrawal_id = withdrawal.id

    await write_audit(
        session,
        action=AuditAction.WITHDRAWAL_CREATED,
        actor_user_id=user.id,
        target_type="withdrawal",
        target_id=str(withdrawal.id),
        metadata={"amount_cents": amount_cents, "reference": ref},
    )

    await create_notification(
        session,
        user_id=user.id,
        type_=NotificationType.WITHDRAWAL_SUBMITTED,
        title="Withdrawal Request Submitted",
        message=(
            f"💸 Withdrawal Request Submitted\n\n"
            f"Amount: ${amount_cents / 100:.2f}\n"
            f"Status: Pending"
        ),
        metadata={"withdrawal_id": withdrawal.id},
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
    if w is None:
        raise ValueError("WITHDRAWAL_NOT_FOUND")

    if w.status == WithdrawalStatus.PAID:
        return w  # idempotent

    if w.status not in (WithdrawalStatus.PENDING, WithdrawalStatus.HELD):
        raise ValueError(f"WITHDRAWAL_NOT_PAYABLE_{w.status.value}")

    now = datetime.now(timezone.utc)
    pay_key = generate_reference("PAY")

    w.status = WithdrawalStatus.PAID
    w.reviewed_by_admin_id = admin_id
    w.reviewed_at = now
    w.paid_at = now
    w.payment_reference = payment_reference
    w.payment_reference_key = pay_key

    await session.flush()

    await write_audit(
        session,
        action=AuditAction.WITHDRAWAL_PAID,
        actor_admin_id=admin_id,
        target_type="withdrawal",
        target_id=str(w.id),
        metadata={
            "amount_cents": w.amount_cents,
            "payment_reference": payment_reference,
            "key": pay_key,
        },
    )

    await create_notification(
        session,
        user_id=w.user_id,
        type_=NotificationType.WITHDRAWAL_PAID,
        title="Withdrawal Paid",
        message=(
            f"✅ Withdrawal Paid\n\n"
            f"Amount: ${w.amount_cents / 100:.2f}\n"
            f"Reference: {payment_reference or pay_key}"
        ),
        metadata={"withdrawal_id": w.id},
    )
    return w


async def reject_withdrawal(
    session: AsyncSession,
    withdrawal_id: int,
    admin_id: int,
    reason: str,
) -> Withdrawal:
    if not reason or not reason.strip():
        raise ValueError("REJECTION_REASON_REQUIRED")

    result = await session.execute(
        select(Withdrawal).where(Withdrawal.id == withdrawal_id).with_for_update()
    )
    w = result.scalar_one_or_none()
    if w is None:
        raise ValueError("WITHDRAWAL_NOT_FOUND")

    if w.status == WithdrawalStatus.REJECTED:
        return w  # idempotent – never refund twice

    if w.status not in (WithdrawalStatus.PENDING, WithdrawalStatus.HELD):
        raise ValueError(f"WITHDRAWAL_NOT_REJECTABLE_{w.status.value}")

    # Lock user and refund
    user_result = await session.execute(
        select(User).where(User.id == w.user_id).with_for_update()
    )
    user = user_result.scalar_one()

    refund_ref = generate_reference("WDR")
    await credit_wallet(
        session,
        user=user,
        amount_cents=w.amount_cents,
        type_=TransactionType.WITHDRAWAL_REFUND,
        reference=refund_ref,
        description=f"Refund for rejected withdrawal #{w.id}: {reason.strip()}",
        admin_id=admin_id,
        withdrawal_id=w.id,
    )

    now = datetime.now(timezone.utc)
    w.status = WithdrawalStatus.REJECTED
    w.rejection_reason = reason.strip()
    w.reviewed_by_admin_id = admin_id
    w.reviewed_at = now
    w.rejected_at = now

    await session.flush()

    await write_audit(
        session,
        action=AuditAction.WITHDRAWAL_REJECTED,
        actor_admin_id=admin_id,
        target_type="withdrawal",
        target_id=str(w.id),
        metadata={
            "amount_cents": w.amount_cents,
            "reason": reason.strip(),
            "refund_reference": refund_ref,
        },
    )

    await create_notification(
        session,
        user_id=w.user_id,
        type_=NotificationType.WITHDRAWAL_REJECTED,
        title="Withdrawal Rejected",
        message=(
            f"❌ Withdrawal Rejected\n\n"
            f"Amount: ${w.amount_cents / 100:.2f}\n"
            f"Reason: {reason.strip()}\n\n"
            f"The amount has been returned to your balance."
        ),
        metadata={"withdrawal_id": w.id, "reason": reason.strip()},
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
    if w is None:
        raise ValueError("WITHDRAWAL_NOT_FOUND")

    if w.status == WithdrawalStatus.HELD:
        return w

    if w.status != WithdrawalStatus.PENDING:
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

    await create_notification(
        session,
        user_id=w.user_id,
        type_=NotificationType.WITHDRAWAL_HELD,
        title="Withdrawal On Hold",
        message=(
            f"⏸ Your withdrawal of ${w.amount_cents / 100:.2f} "
            f"has been placed on hold and is under review."
        ),
        metadata={"withdrawal_id": w.id},
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
    count_q = select(func.count()).select_from(Withdrawal)
    if status:
        q = q.where(Withdrawal.status == status)
        count_q = count_q.where(Withdrawal.status == status)

    total = (await session.execute(count_q)).scalar() or 0
    q = q.order_by(Withdrawal.created_at.desc()).offset(offset).limit(limit)
    result = await session.execute(q)
    return result.scalars().all(), total
