"""Dashboard statistics and analytics."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    ClaimStatus,
    QRClaim,
    QRCode,
    QRStatus,
    User,
    WalletTransaction,
    Withdrawal,
    WithdrawalStatus,
    TransactionType,
)


async def get_dashboard_stats(
    session: AsyncSession,
    *,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
) -> dict[str, Any]:
    """Aggregate dashboard cards."""

    async def _count(model, *filters):
        q = select(func.count()).select_from(model)
        for f in filters:
            q = q.where(f)
        if since:
            q = q.where(model.created_at >= since)
        if until:
            q = q.where(model.created_at <= until)
        return (await session.execute(q)).scalar() or 0

    total_users = await _count(User)
    total_qrs = await _count(QRCode)
    available_qrs = (
        await session.execute(
            select(func.count())
            .select_from(QRCode)
            .where(QRCode.status.in_([QRStatus.AVAILABLE, QRStatus.POSTED]))
        )
    ).scalar() or 0

    pending_claims = (
        await session.execute(
            select(func.count())
            .select_from(QRClaim)
            .where(QRClaim.status == ClaimStatus.PENDING)
        )
    ).scalar() or 0

    approved_claims = (
        await session.execute(
            select(func.count())
            .select_from(QRClaim)
            .where(QRClaim.status == ClaimStatus.APPROVED)
        )
    ).scalar() or 0

    rejected_claims = (
        await session.execute(
            select(func.count())
            .select_from(QRClaim)
            .where(QRClaim.status == ClaimStatus.REJECTED)
        )
    ).scalar() or 0

    total_rewards_cents = (
        await session.execute(
            select(func.coalesce(func.sum(WalletTransaction.amount_cents), 0)).where(
                WalletTransaction.type == TransactionType.REWARD
            )
        )
    ).scalar() or 0

    pending_withdrawals = (
        await session.execute(
            select(func.count())
            .select_from(Withdrawal)
            .where(Withdrawal.status == WithdrawalStatus.PENDING)
        )
    ).scalar() or 0

    total_paid_cents = (
        await session.execute(
            select(func.coalesce(func.sum(Withdrawal.amount_cents), 0)).where(
                Withdrawal.status == WithdrawalStatus.PAID
            )
        )
    ).scalar() or 0

    return {
        "total_users": total_users,
        "total_qr_codes": total_qrs,
        "available_qr": available_qrs,
        "pending_claims": pending_claims,
        "approved_claims": approved_claims,
        "rejected_claims": rejected_claims,
        "total_rewards_cents": int(total_rewards_cents),
        "total_rewards_usd": round(int(total_rewards_cents) / 100, 2),
        "pending_withdrawals": pending_withdrawals,
        "total_paid_cents": int(total_paid_cents),
        "total_paid_usd": round(int(total_paid_cents) / 100, 2),
    }


async def get_chart_data(
    session: AsyncSession,
    *,
    days: int = 30,
) -> dict[str, Any]:
    """Daily series for charts."""
    since = datetime.now(timezone.utc) - timedelta(days=days)

    # Claims per day
    claims_q = await session.execute(
        text(
            """
            SELECT date_trunc('day', created_at AT TIME ZONE 'UTC')::date AS day,
                   COUNT(*) AS cnt
            FROM qr_claims
            WHERE created_at >= :since
            GROUP BY 1
            ORDER BY 1
            """
        ),
        {"since": since},
    )
    claims_per_day = [
        {"date": str(r.day), "count": r.cnt} for r in claims_q.fetchall()
    ]

    # Rewards (approved) per day
    rewards_q = await session.execute(
        text(
            """
            SELECT date_trunc('day', approved_at AT TIME ZONE 'UTC')::date AS day,
                   COALESCE(SUM(reward_cents), 0) AS total
            FROM qr_claims
            WHERE status = 'APPROVED' AND approved_at >= :since
            GROUP BY 1
            ORDER BY 1
            """
        ),
        {"since": since},
    )
    rewards_per_day = [
        {"date": str(r.day), "cents": int(r.total)} for r in rewards_q.fetchall()
    ]

    # New users
    users_q = await session.execute(
        text(
            """
            SELECT date_trunc('day', created_at AT TIME ZONE 'UTC')::date AS day,
                   COUNT(*) AS cnt
            FROM users
            WHERE created_at >= :since
            GROUP BY 1
            ORDER BY 1
            """
        ),
        {"since": since},
    )
    new_users = [
        {"date": str(r.day), "count": r.cnt} for r in users_q.fetchall()
    ]

    # Withdrawals
    wd_q = await session.execute(
        text(
            """
            SELECT date_trunc('day', created_at AT TIME ZONE 'UTC')::date AS day,
                   COUNT(*) AS cnt,
                   COALESCE(SUM(amount_cents), 0) AS total
            FROM withdrawals
            WHERE created_at >= :since
            GROUP BY 1
            ORDER BY 1
            """
        ),
        {"since": since},
    )
    withdrawals = [
        {"date": str(r.day), "count": r.cnt, "cents": int(r.total)}
        for r in wd_q.fetchall()
    ]

    return {
        "claims_per_day": claims_per_day,
        "rewards_per_day": rewards_per_day,
        "new_users": new_users,
        "withdrawals": withdrawals,
    }
