from datetime import datetime, timezone
from enum import Enum
from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text, JSON, Index, UniqueConstraint, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy import Identity

def now():
    return datetime.now(timezone.utc)

class Base(DeclarativeBase):
    pass

class UserStatus(str, Enum):
    ACTIVE="ACTIVE"; BLOCKED="BLOCKED"
class AdminRole(str, Enum):
    OWNER="OWNER"; SUPER_ADMIN="SUPER_ADMIN"; MANAGER="MANAGER"; MODERATOR="MODERATOR"
class QRStatus(str, Enum):
    DRAFT="DRAFT"; POSTED="POSTED"; AVAILABLE="AVAILABLE"; PENDING="PENDING"; CLAIMED="CLAIMED"; RECLAIMED="RECLAIMED"; EXPIRED="EXPIRED"; DISABLED="DISABLED"
class ClaimStatus(str, Enum):
    PENDING="PENDING"; APPROVED="APPROVED"; REJECTED="REJECTED"; REVOKED="REVOKED"
class TxType(str, Enum):
    QR_REWARD="QR_REWARD"; WITHDRAWAL_REVERSAL="WITHDRAWAL_REVERSAL"; ADMIN_ADJUSTMENT="ADMIN_ADJUSTMENT"
class WithdrawalStatus(str, Enum):
    PENDING="PENDING"; PROCESSING="PROCESSING"; PAID="PAID"; REJECTED="REJECTED"
class ForceJoinStatus(str, Enum):
    ACTIVE="ACTIVE"; DISABLED="DISABLED"

class User(Base):
    __tablename__="users"
    id: Mapped[int]=mapped_column(BigInteger, Identity(), primary_key=True)
    telegram_id: Mapped[int]=mapped_column(BigInteger, unique=True, nullable=False)
    username: Mapped[str|None]=mapped_column(String(255))
    first_name: Mapped[str|None]=mapped_column(String(255))
    status: Mapped[UserStatus]=mapped_column(String(20), default=UserStatus.ACTIVE.value)
    balance_cents: Mapped[int]=mapped_column(Integer, default=0)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    admin=relationship("Admin", back_populates="user", uselist=False)
    claims=relationship("QRClaim", back_populates="user")
    transactions=relationship("WalletTransaction", back_populates="user")
    withdrawals=relationship("Withdrawal", back_populates="user")
    notifications=relationship("Notification", back_populates="user")

class Admin(Base):
    __tablename__="admins"
    id: Mapped[int]=mapped_column(BigInteger, Identity(), primary_key=True)
    user_id: Mapped[int]=mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"), unique=True)
    role: Mapped[str]=mapped_column(String(30), nullable=False)
    permissions: Mapped[dict]=mapped_column(JSON, default=list)
    active: Mapped[bool]=mapped_column(Boolean, default=True)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    user=relationship("User", back_populates="admin")

class QRCode(Base):
    __tablename__="qr_codes"
    id: Mapped[int]=mapped_column(BigInteger, Identity(), primary_key=True)
    code: Mapped[str]=mapped_column(String(40), unique=True, nullable=False)
    reward_cents: Mapped[int]=mapped_column(Integer, nullable=False)
    status: Mapped[str]=mapped_column(String(20), default=QRStatus.DRAFT.value)
    claim_limit: Mapped[int]=mapped_column(Integer, default=1)
    expires_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True))
    created_by_id: Mapped[int]=mapped_column(BigInteger, nullable=False)
    posted_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    claims=relationship("QRClaim", back_populates="qr")

class QRClaim(Base):
    __tablename__="qr_claims"
    id: Mapped[int]=mapped_column(BigInteger, Identity(), primary_key=True)
    qr_id: Mapped[int]=mapped_column(BigInteger, ForeignKey("qr_codes.id", ondelete="RESTRICT"), nullable=False)
    user_id: Mapped[int]=mapped_column(BigInteger, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    status: Mapped[str]=mapped_column(String(20), default=ClaimStatus.PENDING.value)
    reward_cents: Mapped[int]=mapped_column(Integer, nullable=False)
    reason: Mapped[str|None]=mapped_column(Text)
    claimed_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), server_default=func.now())
    reviewed_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True))
    reviewed_by_id: Mapped[int|None]=mapped_column(BigInteger)
    qr=relationship("QRCode", back_populates="claims")
    user=relationship("User", back_populates="claims")
    __table_args__=(Index("ix_claim_user_status","user_id","status"),)

class WalletTransaction(Base):
    __tablename__="wallet_transactions"
    id: Mapped[int]=mapped_column(BigInteger, Identity(), primary_key=True)
    user_id: Mapped[int]=mapped_column(BigInteger, ForeignKey("users.id", ondelete="RESTRICT"))
    type: Mapped[str]=mapped_column(String(30))
    amount_cents: Mapped[int]=mapped_column(Integer)
    balance_before: Mapped[int]=mapped_column(Integer)
    balance_after: Mapped[int]=mapped_column(Integer)
    reference_id: Mapped[str]=mapped_column(String(100), unique=True)
    metadata_json: Mapped[dict]=mapped_column("metadata", JSON, default=dict)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), server_default=func.now())
    user=relationship("User", back_populates="transactions")

class Withdrawal(Base):
    __tablename__="withdrawals"
    id: Mapped[int]=mapped_column(BigInteger, Identity(), primary_key=True)
    user_id: Mapped[int]=mapped_column(BigInteger, ForeignKey("users.id", ondelete="RESTRICT"))
    amount_cents: Mapped[int]=mapped_column(Integer)
    method: Mapped[str]=mapped_column(String(50))
    payment_details: Mapped[dict]=mapped_column(JSON, default=dict)
    status: Mapped[str]=mapped_column(String(20), default=WithdrawalStatus.PENDING.value)
    reviewed_by_id: Mapped[int|None]=mapped_column(BigInteger)
    review_note: Mapped[str|None]=mapped_column(String(300))
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    user=relationship("User", back_populates="withdrawals")

class ForceJoinChannel(Base):
    __tablename__="force_join_channels"
    id: Mapped[int]=mapped_column(BigInteger, Identity(), primary_key=True)
    channel_id: Mapped[int]=mapped_column(BigInteger)
    username: Mapped[str|None]=mapped_column(String(255))
    title: Mapped[str|None]=mapped_column(String(255))
    required: Mapped[bool]=mapped_column(Boolean, default=True)
    status: Mapped[str]=mapped_column(String(20), default=ForceJoinStatus.ACTIVE.value)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), server_default=func.now())

class Notification(Base):
    __tablename__="notifications"
    id: Mapped[int]=mapped_column(BigInteger, Identity(), primary_key=True)
    user_id: Mapped[int]=mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"))
    type: Mapped[str]=mapped_column(String(50))
    message: Mapped[str]=mapped_column(Text)
    status: Mapped[str]=mapped_column(String(20), default="QUEUED")
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), server_default=func.now())
    sent_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True))
    user=relationship("User", back_populates="notifications")

class AuditLog(Base):
    __tablename__="audit_logs"
    id: Mapped[int]=mapped_column(BigInteger, Identity(), primary_key=True)
    actor_id: Mapped[int|None]=mapped_column(BigInteger)
    action: Mapped[str]=mapped_column(String(100))
    target_type: Mapped[str]=mapped_column(String(50))
    target_id: Mapped[str|None]=mapped_column(String(100))
    metadata_json: Mapped[dict]=mapped_column("metadata", JSON, default=dict)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__=(Index("ix_audit_created","created_at"),Index("ix_audit_action_target","action","target_type"))
