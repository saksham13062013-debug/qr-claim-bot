"""Owner-only destructive reset operations."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AuditAction,
    AuditLog,
    Notification,
    QRClaim,
    QRCode,
    ResetRecord,
    WalletTransaction,
    Withdrawal,
)
from app.services.audit_service import write_audit


async def reset_activity(
    session: AsyncSession,
    *,
    owner_admin_id: int,
    confirmation_text: str,
) -> dict[str, Any]:
    """
    Reset all activity data (claims, transactions, withdrawals, notifications).
    Keeps users, admins, QR codes structure, settings, channels.
    Requires exact confirmation text.
    """
    expected = "RESET ALL ACTIVITIES"
    if confirmation_text.strip() != expected:
        raise ValueError("CONFIRMATION_MISMATCH")

    # Counts before delete for audit
    claims_count = (
        await session.execute(select(text("count(*)")).select_from(QRClaim))
    ).scalar() or 0
    tx_count = (
        await session.execute(select(text("count(*)")).select_from(WalletTransaction))
    ).scalar() or 0
    wd_count = (
        await session.execute(select(text("count(*)")).select_from(Withdrawal))
    ).scalar() or 0

    # Delete in dependency order
    await session.execute(delete(Notification))
    await session.execute(delete(WalletTransaction))
    await session.execute(delete(Withdrawal))
    await session.execute(delete(QRClaim))

    # Reset user balances
    await session.execute(
        text(
            "UPDATE users SET balance_cents = 0, pending_cents = 0, "
            "total_earned_cents = 0, updated_at = NOW()"
        )
    )

    # Reset QR statuses that were claimed
    await session.execute(
        text(
            """
            UPDATE qr_codes
            SET status = 'AVAILABLE',
                claimed_by_user_id = NULL,
                claimed_at = NULL,
                approved_at = NULL,
                posted_at = NULL
            WHERE status IN ('PENDING', 'APPROVED', 'CLAIMED', 'REJECTED', 'RECLAIMED')
            """
        )
    )

    record = ResetRecord(
        owner_admin_id=owner_admin_id,
        reset_type="ACTIVITY",
        confirmation_text=confirmation_text.strip(),
        metadata_={
            "claims_deleted": claims_count,
            "transactions_deleted": tx_count,
            "withdrawals_deleted": wd_count,
        },
    )
    session.add(record)

    await write_audit(
        session,
        action=AuditAction.RESET_ACTIVITY,
        actor_admin_id=owner_admin_id,
        target_type="system",
        target_id="activity",
        metadata={
            "claims_deleted": claims_count,
            "transactions_deleted": tx_count,
            "withdrawals_deleted": wd_count,
        },
    )
    await session.flush()
    return {
        "reset_type": "ACTIVITY",
        "claims_deleted": claims_count,
        "transactions_deleted": tx_count,
        "withdrawals_deleted": wd_count,
    }


async def reset_qr_data(
    session: AsyncSession,
    *,
    owner_admin_id: int,
    confirmation_text: str,
) -> dict[str, Any]:
    """
    Reset all QR codes and related claims.
    Requires exact confirmation text.
    """
    expected = "RESET ALL QR DATA"
    if confirmation_text.strip() != expected:
        raise ValueError("CONFIRMATION_MISMATCH")

    claims_count = (
        await session.execute(select(text("count(*)")).select_from(QRClaim))
    ).scalar() or 0
    qr_count = (
        await session.execute(select(text("count(*)")).select_from(QRCode))
    ).scalar() or 0

    # Unlink transactions from claims first
    await session.execute(
        text("UPDATE wallet_transactions SET claim_id = NULL WHERE claim_id IS NOT NULL")
    )
    await session.execute(delete(QRClaim))
    await session.execute(delete(QRCode))

    record = ResetRecord(
        owner_admin_id=owner_admin_id,
        reset_type="QR_DATA",
        confirmation_text=confirmation_text.strip(),
        metadata_={"qrs_deleted": qr_count, "claims_deleted": claims_count},
    )
    session.add(record)

    await write_audit(
        session,
        action=AuditAction.RESET_QR_DATA,
        actor_admin_id=owner_admin_id,
        target_type="system",
        target_id="qr_data",
        metadata={"qrs_deleted": qr_count, "claims_deleted": claims_count},
    )
    await session.flush()
    return {
        "reset_type": "QR_DATA",
        "qrs_deleted": qr_count,
        "claims_deleted": claims_count,
    }
