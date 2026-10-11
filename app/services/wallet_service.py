"""Wallet operations – all amounts in integer cents."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AuditAction,
    TransactionType,
    User,
    WalletTransaction,
)
from app.security import generate_reference
from app.services.audit_service import write_audit


async def credit_wallet(
    session: AsyncSession,
    *,
    user: User,
    amount_cents: int,
    type_: TransactionType,
    reference: str,
    description: Optional[str] = None,
    admin_id: Optional[int] = None,
    claim_id: Optional[int] = None,
    withdrawal_id: Optional[int] = None,
) -> WalletTransaction:
    """Credit (or debit if negative) user wallet. Caller must hold user row lock."""
    if amount_cents == 0:
        raise ValueError("Amount cannot be zero")

    balance_before = user.balance_cents
    balance_after = balance_before + amount_cents

    if balance_after < 0:
        raise ValueError("INSUFFICIENT_BALANCE")

    user.balance_cents = balance_after
    if amount_cents > 0 and type_ == TransactionType.REWARD:
        user.total_earned_cents += amount_cents
    user.updated_at = datetime.now(timezone.utc)

    tx = WalletTransaction(
        user_id=user.id,
        type=type_,
        amount_cents=amount_cents,
        balance_before_cents=balance_before,
        balance_after_cents=balance_after,
        reference=reference,
        description=description,
        admin_id=admin_id,
        claim_id=claim_id,
        withdrawal_id=withdrawal_id,
    )
    session.add(tx)
    await session.flush()
    return tx


async def adjust_balance(
    session: AsyncSession,
    *,
    user: User,
    amount_cents: int,
    reason: str,
    admin_id: int,
) -> WalletTransaction:
    """Manual balance adjustment by admin. Requires reason."""
    if not reason or not reason.strip():
        raise ValueError("ADJUSTMENT_REASON_REQUIRED")

    ref = generate_reference("ADJ")
    tx = await credit_wallet(
        session,
        user=user,
        amount_cents=amount_cents,
        type_=TransactionType.ADJUSTMENT,
        reference=ref,
        description=reason.strip(),
        admin_id=admin_id,
    )

    await write_audit(
        session,
        action=AuditAction.BALANCE_ADJUSTED,
        actor_admin_id=admin_id,
        target_type="user",
        target_id=str(user.id),
        metadata={
            "amount_cents": amount_cents,
            "reason": reason.strip(),
            "reference": ref,
            "balance_after": user.balance_cents,
        },
    )
    return tx


async def get_transactions(
    session: AsyncSession,
    user_id: int,
    *,
    offset: int = 0,
    limit: int = 50,
) -> tuple[Sequence[WalletTransaction], int]:
    from sqlalchemy import func

    count_q = select(func.count()).select_from(WalletTransaction).where(
        WalletTransaction.user_id == user_id
    )
    total = (await session.execute(count_q)).scalar() or 0

    q = (
        select(WalletTransaction)
        .where(WalletTransaction.user_id == user_id)
        .order_by(WalletTransaction.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    result = await session.execute(q)
    return result.scalars().all(), total


async def get_all_transactions(
    session: AsyncSession,
    *,
    type_: Optional[TransactionType] = None,
    offset: int = 0,
    limit: int = 50,
) -> tuple[Sequence[WalletTransaction], int]:
    from sqlalchemy import func

    q = select(WalletTransaction)
    count_q = select(func.count()).select_from(WalletTransaction)
    if type_:
        q = q.where(WalletTransaction.type == type_)
        count_q = count_q.where(WalletTransaction.type == type_)

    total = (await session.execute(count_q)).scalar() or 0
    q = q.order_by(WalletTransaction.created_at.desc()).offset(offset).limit(limit)
    result = await session.execute(q)
    return result.scalars().all(), total
