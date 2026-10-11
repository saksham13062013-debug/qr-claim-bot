"""FastAPI admin API and authentication routes."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
    Response,
    status,
)
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.db import get_db
from app.models import (
    Admin,
    AdminRole,
    AuditAction,
    AuditLog,
    ClaimStatus,
    QRClaim,
    QRCode,
    QRStatus,
    User,
    UserStatus,
    Withdrawal,
    WithdrawalStatus,
)
from app.security import (
    create_access_token,
    decode_access_token,
    generate_csrf_token,
    has_permission,
    verify_csrf_token,
)
from app.services import (
    admin_service,
    claim_service,
    dashboard_service,
    force_join_service,
    qr_service,
    reset_service,
    settings_service,
    user_service,
    wallet_service,
    withdrawal_service,
)
from app.services.audit_service import write_audit

logger = logging.getLogger(__name__)
settings = get_settings()

router = APIRouter()
security = HTTPBearer(auto_error=False)

# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

COOKIE_NAME = "access_token"
CSRF_COOKIE = "csrf_token"


async def get_current_admin(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
    session: AsyncSession = Depends(get_db),
) -> Admin:
    token = None
    if credentials:
        token = credentials.credentials
    if not token:
        token = request.cookies.get(COOKIE_NAME)
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")

    payload = decode_access_token(token)
    if not payload or "sub" not in payload:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token")

    admin = await admin_service.get_admin_by_id(session, int(payload["sub"]))
    if not admin or not admin.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Admin inactive")
    return admin


def require_perm(permission: str):
    async def _dep(admin: Admin = Depends(get_current_admin)):
        if not has_permission(admin.role, permission, admin.permissions):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Permission denied")
        return admin

    return _dep


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: str
    username: str
    csrf_token: str


class QRCreateRequest(BaseModel):
    reward_usd: float = Field(..., gt=0)
    quantity: int = Field(1, ge=1, le=500)
    expiry_hours: Optional[int] = Field(None, ge=1)
    campaign_name: Optional[str] = None
    description: Optional[str] = None


class ClaimActionRequest(BaseModel):
    reason: Optional[str] = None


class WithdrawalActionRequest(BaseModel):
    reason: Optional[str] = None
    payment_reference: Optional[str] = None


class BalanceAdjustRequest(BaseModel):
    amount_usd: float
    reason: str = Field(..., min_length=3)


class ChannelCreateRequest(BaseModel):
    channel_id: int
    title: str
    username: Optional[str] = None
    invite_url: Optional[str] = None
    is_required: bool = True
    is_active: bool = True


class ChannelUpdateRequest(BaseModel):
    title: Optional[str] = None
    username: Optional[str] = None
    invite_url: Optional[str] = None
    is_required: Optional[bool] = None
    is_active: Optional[bool] = None


class SettingsUpdateRequest(BaseModel):
    default_reward_usd: Optional[float] = None
    min_withdrawal_usd: Optional[float] = None
    max_withdrawal_usd: Optional[float] = None
    force_join_enabled: Optional[bool] = None
    claim_approval_required: Optional[bool] = None
    qr_default_expiry_hours: Optional[int] = None
    maintenance_mode: Optional[bool] = None
    bot_welcome_message: Optional[str] = None


class ResetRequest(BaseModel):
    confirmation_text: str


class PostQRRequest(BaseModel):
    chat_id: Optional[int] = None  # optional; if omitted just marks as POSTED


# ---------------------------------------------------------------------------
# Auth endpoints
# ---------------------------------------------------------------------------

@router.post("/auth/login", response_model=LoginResponse)
async def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_db),
):
    client_ip = request.client.host if request.client else None
    ua = request.headers.get("user-agent")
    admin = await admin_service.authenticate_admin(
        session,
        body.username,
        body.password,
        ip_address=client_ip,
        user_agent=ua,
    )
    if not admin:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid credentials")

    token = create_access_token(
        {"sub": str(admin.id), "role": admin.role.value, "username": admin.username}
    )
    csrf = generate_csrf_token()

    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        httponly=True,
        secure=settings.is_production,
        samesite="lax",
        max_age=settings.jwt_expire_minutes * 60,
    )
    response.set_cookie(
        key=CSRF_COOKIE,
        value=csrf,
        httponly=False,
        secure=settings.is_production,
        samesite="lax",
        max_age=settings.jwt_expire_minutes * 60,
    )
    return LoginResponse(
        access_token=token,
        role=admin.role.value,
        username=admin.username,
        csrf_token=csrf,
    )


@router.post("/auth/logout")
async def logout(
    request: Request,
    response: Response,
    admin: Admin = Depends(get_current_admin),
    session: AsyncSession = Depends(get_db),
):
    await write_audit(
        session,
        action=AuditAction.ADMIN_LOGOUT,
        actor_admin_id=admin.id,
        ip_address=request.client.host if request.client else None,
    )
    response.delete_cookie(COOKIE_NAME)
    response.delete_cookie(CSRF_COOKIE)
    return {"ok": True}


@router.get("/me")
async def me(admin: Admin = Depends(get_current_admin)):
    return {
        "id": admin.id,
        "username": admin.username,
        "role": admin.role.value,
        "display_name": admin.display_name,
        "permissions": admin.permissions or {},
    }


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@router.get("/admin/dashboard")
async def dashboard(
    days: int = 30,
    admin: Admin = Depends(require_perm("dashboard.view")),
    session: AsyncSession = Depends(get_db),
):
    stats = await dashboard_service.get_dashboard_stats(session)
    charts = await dashboard_service.get_chart_data(session, days=days)
    return {"stats": stats, "charts": charts}


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

@router.get("/admin/users")
async def list_users(
    q: Optional[str] = None,
    offset: int = 0,
    limit: int = 50,
    admin: Admin = Depends(require_perm("users.view")),
    session: AsyncSession = Depends(get_db),
):
    query = select(User)
    if q:
        if q.isdigit():
            query = query.where(
                (User.telegram_id == int(q)) | (User.username.ilike(f"%{q}%"))
            )
        else:
            query = query.where(
                (User.username.ilike(f"%{q}%"))
                | (User.first_name.ilike(f"%{q}%"))
                | (User.last_name.ilike(f"%{q}%"))
            )
    count_q = select(func.count()).select_from(query.subquery())
    total = (await session.execute(count_q)).scalar() or 0
    result = await session.execute(
        query.order_by(User.created_at.desc()).offset(offset).limit(limit)
    )
    users = result.scalars().all()
    return {
        "total": total,
        "items": [
            {
                "id": u.id,
                "telegram_id": u.telegram_id,
                "username": u.username,
                "first_name": u.first_name,
                "last_name": u.last_name,
                "status": u.status.value,
                "balance_cents": u.balance_cents,
                "total_earned_cents": u.total_earned_cents,
                "created_at": u.created_at.isoformat() if u.created_at else None,
                "last_seen_at": u.last_seen_at.isoformat() if u.last_seen_at else None,
            }
            for u in users
        ],
    }


@router.get("/admin/users/{user_id}")
async def get_user(
    user_id: int,
    admin: Admin = Depends(require_perm("users.view")),
    session: AsyncSession = Depends(get_db),
):
    user = await user_service.get_user_by_id(session, user_id)
    if not user:
        raise HTTPException(404, "User not found")
    return {
        "id": user.id,
        "telegram_id": user.telegram_id,
        "username": user.username,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "status": user.status.value,
        "balance_cents": user.balance_cents,
        "pending_cents": user.pending_cents,
        "total_earned_cents": user.total_earned_cents,
        "created_at": user.created_at.isoformat() if user.created_at else None,
        "last_seen_at": user.last_seen_at.isoformat() if user.last_seen_at else None,
    }


@router.post("/admin/users/{user_id}/disable")
async def disable_user(
    user_id: int,
    admin: Admin = Depends(require_perm("users.disable")),
    session: AsyncSession = Depends(get_db),
):
    user = await user_service.get_user_by_id(session, user_id)
    if not user:
        raise HTTPException(404, "User not found")
    await user_service.set_user_status(session, user, UserStatus.DISABLED)
    await write_audit(
        session,
        action=AuditAction.USER_DISABLED,
        actor_admin_id=admin.id,
        target_type="user",
        target_id=str(user.id),
    )
    return {"ok": True}


@router.post("/admin/users/{user_id}/enable")
async def enable_user(
    user_id: int,
    admin: Admin = Depends(require_perm("users.disable")),
    session: AsyncSession = Depends(get_db),
):
    user = await user_service.get_user_by_id(session, user_id)
    if not user:
        raise HTTPException(404, "User not found")
    await user_service.set_user_status(session, user, UserStatus.ACTIVE)
    await write_audit(
        session,
        action=AuditAction.USER_ENABLED,
        actor_admin_id=admin.id,
        target_type="user",
        target_id=str(user.id),
    )
    return {"ok": True}


@router.post("/admin/users/{user_id}/adjust-balance")
async def adjust_balance(
    user_id: int,
    body: BalanceAdjustRequest,
    admin: Admin = Depends(require_perm("users.adjust_balance")),
    session: AsyncSession = Depends(get_db),
):
    user = await user_service.get_user_by_id(session, user_id)
    if not user:
        raise HTTPException(404, "User not found")
    amount_cents = int(round(body.amount_usd * 100))
    # Lock user
    result = await session.execute(
        select(User).where(User.id == user_id).with_for_update()
    )
    user = result.scalar_one()
    try:
        tx = await wallet_service.adjust_balance(
            session,
            user=user,
            amount_cents=amount_cents,
            reason=body.reason,
            admin_id=admin.id,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {
        "ok": True,
        "new_balance_cents": user.balance_cents,
        "transaction_id": tx.id,
    }


# ---------------------------------------------------------------------------
# QR Codes
# ---------------------------------------------------------------------------

@router.get("/admin/qrs")
async def list_qrs(
    status: Optional[str] = None,
    campaign: Optional[str] = None,
    offset: int = 0,
    limit: int = 50,
    admin: Admin = Depends(require_perm("qr.view")),
    session: AsyncSession = Depends(get_db),
):
    st = QRStatus(status) if status else None
    items, total = await qr_service.list_qrs(
        session, status=st, campaign=campaign, offset=offset, limit=limit
    )
    return {
        "total": total,
        "items": [
            {
                "id": q.id,
                "code": q.code,
                "reward_cents": q.reward_cents,
                "status": q.status.value,
                "campaign_name": q.campaign_name,
                "description": q.description,
                "created_at": q.created_at.isoformat() if q.created_at else None,
                "posted_at": q.posted_at.isoformat() if q.posted_at else None,
                "claimed_at": q.claimed_at.isoformat() if q.claimed_at else None,
                "expires_at": q.expires_at.isoformat() if q.expires_at else None,
                "claimed_by_user_id": q.claimed_by_user_id,
            }
            for q in items
        ],
    }


@router.post("/admin/qrs")
async def create_qrs(
    body: QRCreateRequest,
    admin: Admin = Depends(require_perm("qr.create")),
    session: AsyncSession = Depends(get_db),
):
    reward_cents = int(round(body.reward_usd * 100))
    expires_at = None
    if body.expiry_hours:
        expires_at = datetime.now(timezone.utc) + timedelta(hours=body.expiry_hours)
    try:
        codes = await qr_service.create_qr_codes(
            session,
            reward_cents=reward_cents,
            quantity=body.quantity,
            expires_at=expires_at,
            campaign_name=body.campaign_name,
            description=body.description,
            admin_id=admin.id,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {
        "created": len(codes),
        "codes": [
            {
                "id": c.id,
                "code": c.code,
                "reward_cents": c.reward_cents,
                "status": c.status.value,
                "expires_at": c.expires_at.isoformat() if c.expires_at else None,
            }
            for c in codes
        ],
    }


@router.post("/admin/qrs/{qr_id}/post")
async def post_qr(
    qr_id: int,
    body: PostQRRequest = PostQRRequest(),
    admin: Admin = Depends(require_perm("qr.post")),
    session: AsyncSession = Depends(get_db),
):
    qr = await qr_service.get_qr_by_id(session, qr_id)
    if not qr:
        raise HTTPException(404, "QR not found")
    try:
        qr = await qr_service.mark_qr_posted(session, qr, admin.id)
    except ValueError as e:
        raise HTTPException(400, str(e))

    # Optionally post to Telegram chat
    message_text = None
    if body.chat_id:
        from app.bot import get_bot
        from app.bot import claim_button

        bot = get_bot()
        if bot:
            try:
                msg = (
                    f"🎁 <b>NEW QR REWARD</b>\n\n"
                    f"🎟 QR: <code>{qr.code}</code>\n"
                    f"💰 Reward: ${qr.reward_cents / 100:.2f}\n"
                    f"🔥 One user only!"
                )
                await bot.send_message(
                    chat_id=body.chat_id,
                    text=msg,
                    reply_markup=claim_button(qr.code),
                )
                message_text = "Posted to Telegram"
            except Exception as exc:
                logger.warning("Failed to post QR to Telegram: %s", exc)
                message_text = f"Marked POSTED but Telegram send failed: {type(exc).__name__}"

    return {
        "ok": True,
        "status": qr.status.value,
        "message": message_text or "Marked as POSTED",
    }


@router.post("/admin/qrs/{qr_id}/reclaim")
async def reclaim_qr(
    qr_id: int,
    admin: Admin = Depends(require_perm("qr.reclaim")),
    session: AsyncSession = Depends(get_db),
):
    qr = await qr_service.get_qr_by_id(session, qr_id)
    if not qr:
        raise HTTPException(404, "QR not found")
    try:
        qr = await qr_service.reclaim_qr(session, qr, admin.id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "status": qr.status.value}


@router.post("/admin/qrs/{qr_id}/disable")
async def disable_qr(
    qr_id: int,
    admin: Admin = Depends(require_perm("qr.disable")),
    session: AsyncSession = Depends(get_db),
):
    qr = await qr_service.get_qr_by_id(session, qr_id)
    if not qr:
        raise HTTPException(404, "QR not found")
    await qr_service.disable_qr(session, qr, admin.id)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Claims
# ---------------------------------------------------------------------------

@router.get("/admin/claims")
async def list_claims(
    status: Optional[str] = None,
    offset: int = 0,
    limit: int = 50,
    admin: Admin = Depends(require_perm("claims.view")),
    session: AsyncSession = Depends(get_db),
):
    st = ClaimStatus(status) if status else None
    items, total = await claim_service.list_claims(
        session, status=st, offset=offset, limit=limit
    )
    return {
        "total": total,
        "items": [
            {
                "id": c.id,
                "qr_code": c.qr_code.code if c.qr_code else None,
                "user_id": c.user_id,
                "username": c.user.username if c.user else None,
                "telegram_id": c.user.telegram_id if c.user else None,
                "reward_cents": c.reward_cents,
                "status": c.status.value,
                "created_at": c.created_at.isoformat() if c.created_at else None,
                "approved_at": c.approved_at.isoformat() if c.approved_at else None,
                "rejected_at": c.rejected_at.isoformat() if c.rejected_at else None,
                "rejection_reason": c.rejection_reason,
                "reviewed_by_admin_id": c.reviewed_by_admin_id,
            }
            for c in items
        ],
    }


@router.post("/admin/claims/{claim_id}/approve")
async def approve_claim(
    claim_id: int,
    admin: Admin = Depends(require_perm("claims.approve")),
    session: AsyncSession = Depends(get_db),
):
    try:
        claim = await claim_service.approve_claim(session, claim_id, admin.id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "status": claim.status.value}


@router.post("/admin/claims/{claim_id}/reject")
async def reject_claim(
    claim_id: int,
    body: ClaimActionRequest,
    admin: Admin = Depends(require_perm("claims.reject")),
    session: AsyncSession = Depends(get_db),
):
    if not body.reason:
        raise HTTPException(400, "Reason required")
    try:
        claim = await claim_service.reject_claim(
            session, claim_id, admin.id, body.reason
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "status": claim.status.value}


@router.post("/admin/claims/{claim_id}/hold")
async def hold_claim(
    claim_id: int,
    admin: Admin = Depends(require_perm("claims.hold")),
    session: AsyncSession = Depends(get_db),
):
    try:
        claim = await claim_service.hold_claim(session, claim_id, admin.id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "status": claim.status.value}


# ---------------------------------------------------------------------------
# Withdrawals
# ---------------------------------------------------------------------------

@router.get("/admin/withdrawals")
async def list_withdrawals(
    status: Optional[str] = None,
    offset: int = 0,
    limit: int = 50,
    admin: Admin = Depends(require_perm("withdrawals.view")),
    session: AsyncSession = Depends(get_db),
):
    st = WithdrawalStatus(status) if status else None
    items, total = await withdrawal_service.list_withdrawals(
        session, status=st, offset=offset, limit=limit
    )
    return {
        "total": total,
        "items": [
            {
                "id": w.id,
                "user_id": w.user_id,
                "username": w.user.username if w.user else None,
                "telegram_id": w.user.telegram_id if w.user else None,
                "amount_cents": w.amount_cents,
                "status": w.status.value,
                "created_at": w.created_at.isoformat() if w.created_at else None,
                "paid_at": w.paid_at.isoformat() if w.paid_at else None,
                "rejected_at": w.rejected_at.isoformat() if w.rejected_at else None,
                "rejection_reason": w.rejection_reason,
                "payment_reference": w.payment_reference,
            }
            for w in items
        ],
    }


@router.post("/admin/withdrawals/{wid}/pay")
async def pay_withdrawal(
    wid: int,
    body: WithdrawalActionRequest = WithdrawalActionRequest(),
    admin: Admin = Depends(require_perm("withdrawals.pay")),
    session: AsyncSession = Depends(get_db),
):
    try:
        w = await withdrawal_service.mark_withdrawal_paid(
            session, wid, admin.id, body.payment_reference
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "status": w.status.value}


@router.post("/admin/withdrawals/{wid}/reject")
async def reject_withdrawal(
    wid: int,
    body: WithdrawalActionRequest,
    admin: Admin = Depends(require_perm("withdrawals.reject")),
    session: AsyncSession = Depends(get_db),
):
    if not body.reason:
        raise HTTPException(400, "Reason required")
    try:
        w = await withdrawal_service.reject_withdrawal(
            session, wid, admin.id, body.reason
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "status": w.status.value}


@router.post("/admin/withdrawals/{wid}/hold")
async def hold_withdrawal(
    wid: int,
    admin: Admin = Depends(require_perm("withdrawals.hold")),
    session: AsyncSession = Depends(get_db),
):
    try:
        w = await withdrawal_service.hold_withdrawal(session, wid, admin.id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "status": w.status.value}


# ---------------------------------------------------------------------------
# Channels
# ---------------------------------------------------------------------------

@router.get("/admin/channels")
async def list_channels(
    admin: Admin = Depends(require_perm("channels.view")),
    session: AsyncSession = Depends(get_db),
):
    channels = await force_join_service.list_channels(session)
    return {
        "items": [
            {
                "id": c.id,
                "channel_id": c.channel_id,
                "username": c.username,
                "title": c.title,
                "invite_url": c.invite_url,
                "is_active": c.is_active,
                "is_required": c.is_required,
            }
            for c in channels
        ]
    }


@router.post("/admin/channels")
async def create_channel(
    body: ChannelCreateRequest,
    admin: Admin = Depends(require_perm("channels.manage")),
    session: AsyncSession = Depends(get_db),
):
    ch = await force_join_service.create_channel(
        session,
        channel_id=body.channel_id,
        title=body.title,
        username=body.username,
        invite_url=body.invite_url,
        is_required=body.is_required,
        is_active=body.is_active,
    )
    await write_audit(
        session,
        action=AuditAction.CHANNEL_CREATED,
        actor_admin_id=admin.id,
        target_type="channel",
        target_id=str(ch.id),
        metadata={"channel_id": body.channel_id, "title": body.title},
    )
    return {"ok": True, "id": ch.id}


@router.patch("/admin/channels/{channel_id}")
async def update_channel(
    channel_id: int,
    body: ChannelUpdateRequest,
    admin: Admin = Depends(require_perm("channels.manage")),
    session: AsyncSession = Depends(get_db),
):
    from app.models import ForceJoinChannel

    result = await session.execute(
        select(ForceJoinChannel).where(ForceJoinChannel.id == channel_id)
    )
    ch = result.scalar_one_or_none()
    if not ch:
        raise HTTPException(404, "Channel not found")
    await force_join_service.update_channel(
        session,
        ch,
        title=body.title,
        username=body.username,
        invite_url=body.invite_url,
        is_required=body.is_required,
        is_active=body.is_active,
    )
    await write_audit(
        session,
        action=AuditAction.CHANNEL_UPDATED,
        actor_admin_id=admin.id,
        target_type="channel",
        target_id=str(ch.id),
    )
    return {"ok": True}


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

@router.get("/admin/settings")
async def get_settings_endpoint(
    admin: Admin = Depends(require_perm("settings.view")),
    session: AsyncSession = Depends(get_db),
):
    return await settings_service.get_all_settings(session)


@router.put("/admin/settings")
async def update_settings(
    body: SettingsUpdateRequest,
    admin: Admin = Depends(require_perm("settings.edit")),
    session: AsyncSession = Depends(get_db),
):
    mapping = {
        "default_reward_usd": ("default_reward_cents", lambda v: str(int(round(v * 100)))),
        "min_withdrawal_usd": ("min_withdrawal_cents", lambda v: str(int(round(v * 100)))),
        "max_withdrawal_usd": ("max_withdrawal_cents", lambda v: str(int(round(v * 100)))),
        "force_join_enabled": ("force_join_enabled", lambda v: str(v).lower()),
        "claim_approval_required": ("claim_approval_required", lambda v: str(v).lower()),
        "qr_default_expiry_hours": ("qr_default_expiry_hours", str),
        "maintenance_mode": ("maintenance_mode", lambda v: str(v).lower()),
        "bot_welcome_message": ("bot_welcome_message", str),
    }
    for field, (key, conv) in mapping.items():
        val = getattr(body, field, None)
        if val is not None:
            await settings_service.set_setting(
                session, key, conv(val), admin_id=admin.id
            )
    await write_audit(
        session,
        action=AuditAction.SETTINGS_CHANGED,
        actor_admin_id=admin.id,
        target_type="settings",
        metadata=body.model_dump(exclude_none=True),
    )
    return await settings_service.get_all_settings(session)


# ---------------------------------------------------------------------------
# Audit logs
# ---------------------------------------------------------------------------

@router.get("/admin/audit-logs")
async def audit_logs(
    offset: int = 0,
    limit: int = 50,
    action: Optional[str] = None,
    admin: Admin = Depends(require_perm("audit.view")),
    session: AsyncSession = Depends(get_db),
):
    q = select(AuditLog)
    if action:
        q = q.where(AuditLog.action == AuditAction(action))
    count_q = select(func.count()).select_from(q.subquery())
    total = (await session.execute(count_q)).scalar() or 0
    result = await session.execute(
        q.order_by(AuditLog.created_at.desc()).offset(offset).limit(limit)
    )
    logs = result.scalars().all()
    return {
        "total": total,
        "items": [
            {
                "id": a.id,
                "action": a.action.value,
                "actor_admin_id": a.actor_admin_id,
                "actor_user_id": a.actor_user_id,
                "target_type": a.target_type,
                "target_id": a.target_id,
                "metadata": a.metadata_,
                "ip_address": a.ip_address,
                "created_at": a.created_at.isoformat() if a.created_at else None,
            }
            for a in logs
        ],
    }


# ---------------------------------------------------------------------------
# Reset (OWNER only)
# ---------------------------------------------------------------------------

@router.post("/admin/reset/activity")
async def reset_activity(
    body: ResetRequest,
    admin: Admin = Depends(require_perm("reset.activity")),
    session: AsyncSession = Depends(get_db),
):
    try:
        result = await reset_service.reset_activity(
            session,
            owner_admin_id=admin.id,
            confirmation_text=body.confirmation_text,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    return result


@router.post("/admin/reset/qr-data")
async def reset_qr_data(
    body: ResetRequest,
    admin: Admin = Depends(require_perm("reset.qr_data")),
    session: AsyncSession = Depends(get_db),
):
    try:
        result = await reset_service.reset_qr_data(
            session,
            owner_admin_id=admin.id,
            confirmation_text=body.confirmation_text,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    return result
