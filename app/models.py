"""SQLAlchemy 2.x async models for QR Claim & Rewards Bot."""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import (
    LargeBinary,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Base declarative class."""

    pass


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class UserStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"
    BANNED = "BANNED"


class QRStatus(str, enum.Enum):
    AVAILABLE = "AVAILABLE"
    POSTED = "POSTED"
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    CLAIMED = "CLAIMED"
    EXPIRED = "EXPIRED"
    RECLAIMED = "RECLAIMED"
    DISABLED = "DISABLED"


class ClaimStatus(str, enum.Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    HELD = "HELD"
    CANCELLED = "CANCELLED"


class TransactionType(str, enum.Enum):
    REWARD = "REWARD"
    WITHDRAWAL = "WITHDRAWAL"
    WITHDRAWAL_REFUND = "WITHDRAWAL_REFUND"
    ADJUSTMENT = "ADJUSTMENT"
    REVERSAL = "REVERSAL"


class WithdrawalStatus(str, enum.Enum):
    PENDING = "PENDING"
    PAID = "PAID"
    REJECTED = "REJECTED"
    HELD = "HELD"
    CANCELLED = "CANCELLED"


class AdminRole(str, enum.Enum):
    OWNER = "OWNER"
    SUPER_ADMIN = "SUPER_ADMIN"
    MANAGER = "MANAGER"
    MODERATOR = "MODERATOR"


class NotificationType(str, enum.Enum):
    QR_POSTED = "QR_POSTED"
    CLAIM_SUBMITTED = "CLAIM_SUBMITTED"
    CLAIM_APPROVED = "CLAIM_APPROVED"
    CLAIM_REJECTED = "CLAIM_REJECTED"
    WITHDRAWAL_SUBMITTED = "WITHDRAWAL_SUBMITTED"
    WITHDRAWAL_PAID = "WITHDRAWAL_PAID"
    WITHDRAWAL_REJECTED = "WITHDRAWAL_REJECTED"
    WITHDRAWAL_HELD = "WITHDRAWAL_HELD"
    ADMIN_ANNOUNCEMENT = "ADMIN_ANNOUNCEMENT"
    SYSTEM = "SYSTEM"


class AuditAction(str, enum.Enum):
    ADMIN_LOGIN = "ADMIN_LOGIN"
    ADMIN_LOGOUT = "ADMIN_LOGOUT"
    ADMIN_LOGIN_FAILED = "ADMIN_LOGIN_FAILED"
    QR_CREATED = "QR_CREATED"
    QR_POSTED = "QR_POSTED"
    QR_RECLAIMED = "QR_RECLAIMED"
    QR_DISABLED = "QR_DISABLED"
    QR_ENABLED = "QR_ENABLED"
    CLAIM_CREATED = "CLAIM_CREATED"
    CLAIM_APPROVED = "CLAIM_APPROVED"
    CLAIM_REJECTED = "CLAIM_REJECTED"
    CLAIM_HELD = "CLAIM_HELD"
    BALANCE_ADJUSTED = "BALANCE_ADJUSTED"
    WITHDRAWAL_CREATED = "WITHDRAWAL_CREATED"
    WITHDRAWAL_PAID = "WITHDRAWAL_PAID"
    WITHDRAWAL_REJECTED = "WITHDRAWAL_REJECTED"
    WITHDRAWAL_HELD = "WITHDRAWAL_HELD"
    ROLE_CHANGED = "ROLE_CHANGED"
    SETTINGS_CHANGED = "SETTINGS_CHANGED"
    USER_DISABLED = "USER_DISABLED"
    USER_ENABLED = "USER_ENABLED"
    CHANNEL_CREATED = "CHANNEL_CREATED"
    CHANNEL_UPDATED = "CHANNEL_UPDATED"
    CHANNEL_DELETED = "CHANNEL_DELETED"
    RESET_ACTIVITY = "RESET_ACTIVITY"
    RESET_QR_DATA = "RESET_QR_DATA"
    PERMISSION_CHANGED = "PERMISSION_CHANGED"
    TICKET_CREATED = "TICKET_CREATED"
    TICKET_REPLIED = "TICKET_REPLIED"
    BROADCAST_SENT = "BROADCAST_SENT"
    QR_CLONED = "QR_CLONED"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False, index=True)
    username: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, index=True)
    first_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    last_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    status: Mapped[UserStatus] = mapped_column(
        Enum(UserStatus, name="user_status", create_constraint=True),
        default=UserStatus.ACTIVE,
        nullable=False,
        index=True,
    )
    balance_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False, server_default="0")
    pending_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False, server_default="0")
    total_earned_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    last_seen_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    claims: Mapped[list["QRClaim"]] = relationship("QRClaim", back_populates="user")
    transactions: Mapped[list["WalletTransaction"]] = relationship(
        "WalletTransaction", back_populates="user"
    )
    withdrawals: Mapped[list["Withdrawal"]] = relationship("Withdrawal", back_populates="user")
    notifications: Mapped[list["Notification"]] = relationship(
        "Notification", back_populates="user"
    )

    __table_args__ = (
        CheckConstraint("balance_cents >= 0", name="ck_users_balance_non_negative"),
        CheckConstraint("pending_cents >= 0", name="ck_users_pending_non_negative"),
        CheckConstraint("total_earned_cents >= 0", name="ck_users_total_earned_non_negative"),
    )


class Admin(Base):
    __tablename__ = "admins"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[AdminRole] = mapped_column(
        Enum(AdminRole, name="admin_role", create_constraint=True),
        nullable=False,
        index=True,
    )
    telegram_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True, unique=True)
    display_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    failed_login_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    locked_until: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # Manager-level optional permissions (JSON flags)
    permissions: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True, default=dict)


class QRCode(Base):
    __tablename__ = "qr_codes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    reward_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[QRStatus] = mapped_column(
        Enum(QRStatus, name="qr_status", create_constraint=True),
        default=QRStatus.AVAILABLE,
        nullable=False,
        index=True,
    )
    campaign_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, index=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_by_admin_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("admins.id", ondelete="SET NULL"), nullable=True
    )
    claimed_by_user_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    posted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    claimed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    approved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    image_png: Mapped[Optional[bytes]] = mapped_column(LargeBinary, nullable=True)
    scheduled_post_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    scheduled_chat_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    claims: Mapped[list["QRClaim"]] = relationship("QRClaim", back_populates="qr_code")
    created_by: Mapped[Optional["Admin"]] = relationship("Admin", foreign_keys=[created_by_admin_id])
    claimed_by: Mapped[Optional["User"]] = relationship("User", foreign_keys=[claimed_by_user_id])

    __table_args__ = (
        CheckConstraint("reward_cents > 0", name="ck_qr_reward_positive"),
        Index("ix_qr_codes_status_expires", "status", "expires_at"),
    )


class QRClaim(Base):
    __tablename__ = "qr_claims"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    qr_code_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("qr_codes.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    reward_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[ClaimStatus] = mapped_column(
        Enum(ClaimStatus, name="claim_status", create_constraint=True),
        default=ClaimStatus.PENDING,
        nullable=False,
        index=True,
    )
    rejection_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    reviewed_by_admin_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("admins.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    approved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    rejected_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # Idempotency key for approval
    approval_reference: Mapped[Optional[str]] = mapped_column(
        String(64), unique=True, nullable=True
    )

    qr_code: Mapped["QRCode"] = relationship("QRCode", back_populates="claims")
    user: Mapped["User"] = relationship("User", back_populates="claims")
    reviewer: Mapped[Optional["Admin"]] = relationship(
        "Admin", foreign_keys=[reviewed_by_admin_id]
    )

    __table_args__ = (
        # One successful claim per QR (enforced also by QR status + locking)
        UniqueConstraint("qr_code_id", name="uq_qr_claims_qr_code_id"),
        CheckConstraint("reward_cents > 0", name="ck_claim_reward_positive"),
    )


class WalletTransaction(Base):
    __tablename__ = "wallet_transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    type: Mapped[TransactionType] = mapped_column(
        Enum(TransactionType, name="transaction_type", create_constraint=True),
        nullable=False,
        index=True,
    )
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    balance_before_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    balance_after_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    reference: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    admin_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("admins.id", ondelete="SET NULL"), nullable=True
    )
    claim_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("qr_claims.id", ondelete="SET NULL"), nullable=True
    )
    withdrawal_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("withdrawals.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )

    user: Mapped["User"] = relationship("User", back_populates="transactions")
    admin: Mapped[Optional["Admin"]] = relationship("Admin")

    __table_args__ = (
        CheckConstraint("amount_cents != 0", name="ck_tx_amount_nonzero"),
    )


class Withdrawal(Base):
    __tablename__ = "withdrawals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[WithdrawalStatus] = mapped_column(
        Enum(WithdrawalStatus, name="withdrawal_status", create_constraint=True),
        default=WithdrawalStatus.PENDING,
        nullable=False,
        index=True,
    )
    rejection_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    payment_reference: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    reviewed_by_admin_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("admins.id", ondelete="SET NULL"), nullable=True
    )
    # Idempotency
    payment_reference_key: Mapped[Optional[str]] = mapped_column(
        String(64), unique=True, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    paid_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    rejected_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped["User"] = relationship("User", back_populates="withdrawals")
    reviewer: Mapped[Optional["Admin"]] = relationship(
        "Admin", foreign_keys=[reviewed_by_admin_id]
    )

    __table_args__ = (
        CheckConstraint("amount_cents > 0", name="ck_withdrawal_amount_positive"),
    )


class ForceJoinChannel(Base):
    __tablename__ = "force_join_channels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    channel_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    username: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    invite_url: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    is_required: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    type: Mapped[NotificationType] = mapped_column(
        Enum(NotificationType, name="notification_type", create_constraint=True),
        nullable=False,
        index=True,
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    is_read: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    metadata_: Mapped[Optional[dict]] = mapped_column("metadata", JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    telegram_sent: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    user: Mapped[Optional["User"]] = relationship("User", back_populates="notifications")


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    actor_admin_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("admins.id", ondelete="SET NULL"), nullable=True, index=True
    )
    actor_user_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    action: Mapped[AuditAction] = mapped_column(
        Enum(AuditAction, name="audit_action", create_constraint=True),
        nullable=False,
        index=True,
    )
    target_type: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    target_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    metadata_: Mapped[Optional[dict]] = mapped_column("metadata", JSONB, nullable=True)
    ip_address: Mapped[Optional[str]] = mapped_column(String(45), nullable=True)
    user_agent: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )

    actor_admin: Mapped[Optional["Admin"]] = relationship(
        "Admin", foreign_keys=[actor_admin_id]
    )


class Setting(Base):
    __tablename__ = "settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    updated_by_admin_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("admins.id", ondelete="SET NULL"), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class ResetRecord(Base):
    """Immutable record of destructive reset operations."""

    __tablename__ = "reset_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_admin_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("admins.id", ondelete="RESTRICT"), nullable=False
    )
    reset_type: Mapped[str] = mapped_column(String(64), nullable=False)
    confirmation_text: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    metadata_: Mapped[Optional[dict]] = mapped_column("metadata", JSONB, nullable=True)


class TicketStatus(str, enum.Enum):
    OPEN = "OPEN"
    IN_PROGRESS = "IN_PROGRESS"
    CLOSED = "CLOSED"


class SupportTicket(Base):
    __tablename__ = "support_tickets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[TicketStatus] = mapped_column(
        Enum(TicketStatus, name="ticket_status", create_constraint=True),
        default=TicketStatus.OPEN,
        nullable=False,
        index=True,
    )
    admin_reply: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    handled_by_admin_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("admins.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    user: Mapped["User"] = relationship("User")
