"""Admin REST API + auth."""

from __future__ import annotations

import csv
import io
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.db import get_db
from app.models import (
    Admin,
    AuditAction,
    AuditLog,
    ClaimStatus,
    QRClaim,
    QRCode,
    QRStatus,
    User,
    UserStatus,
    WithdrawalStatus,
)
from app.security import (
    create_access_token,
    decode_access_token,
    generate_csrf_token,
    has_permission,
    hash_password,
    verify_password,
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


# ---- Schemas ----


class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    access_token: str
    role: str
    username: str
    csrf_token: str = ""


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
    title: str
    channel_id: int
    username: Optional[str] = None
    invite_url: Optional[str] = None
    is_required: bool = True


class ChannelUpdateRequest(BaseModel):
    title: Optional[str] = None
    chat_id: Optional[str] = None
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
    auto_approve_enabled: Optional[bool] = None
    auto_approve_max_usd: Optional[float] = None


class ResetRequest(BaseModel):
    confirmation_text: str


class PostQRRequest(BaseModel):
    chat_id: Optional[str] = None


class BroadcastRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=4000)
    only_active: bool = True


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str = Field(..., min_length=8)


class SchedulePostRequest(BaseModel):
    chat_id: str
    scheduled_at: str


class TicketReplyRequest(BaseModel):
    reply: str = Field(..., min_length=1)
    close: bool = False


# ---- Auth ----


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
        session, body.username, body.password, ip_address=client_ip, user_agent=ua
    )
    if not admin:
        await write_audit(
            session,
            action=AuditAction.ADMIN_LOGIN_FAILED,
            metadata={"username": body.username},
            ip_address=client_ip,
            user_agent=ua,
        )
        try:
            from app.bot import get_bot

            bot = get_bot()
            if bot and settings.owner_telegram_id:
                await bot.send_message(
                    settings.owner_telegram_id,
                    f"Failed admin login\nUser: {body.username}\nIP: {client_ip}",
                )
        except Exception:
            pass
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
        path="/",
    )
    response.set_cookie(
        key=CSRF_COOKIE,
        value=csrf,
        httponly=False,
        secure=settings.is_production,
        samesite="lax",
        max_age=settings.jwt_expire_minutes * 60,
        path="/",
    )
    return LoginResponse(
        access_token=token,
        role=admin.role.value,
        username=admin.username,
        csrf_token=csrf,
    )


@router.post("/auth/logout")
async def logout(
    response: Response,
    admin: Admin = Depends(get_current_admin),
    session: AsyncSession = Depends(get_db),
):
    await write_audit(
        session, action=AuditAction.ADMIN_LOGOUT, actor_admin_id=admin.id
    )
    response.delete_cookie(COOKIE_NAME)
    response.delete_cookie(CSRF_COOKIE)
    return {"ok": True}


@router.get("/auth/me")
async def me(admin: Admin = Depends(get_current_admin)):
    return {
        "id": admin.id,
        "username": admin.username,
        "role": admin.role.value,
        "is_active": admin.is_active,
    }


# ---- Dashboard / users ----


@router.get("/admin/dashboard")
async def dashboard(
    days: int = 30,
    admin: Admin = Depends(require_perm("dashboard.view")),
    session: AsyncSession = Depends(get_db),
):
    from datetime import datetime, timedelta, timezone
    since = datetime.now(timezone.utc) - timedelta(days=days)
    stats = await dashboard_service.get_dashboard_stats(session, since=since)
    charts = await dashboard_service.get_chart_data(session, days=days)
    return {"stats": stats, "charts": charts, **charts}


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
            query = query.where(User.username.ilike(f"%{q}%"))
    total = (
        await session.execute(select(func.count()).select_from(query.subquery()))
    ).scalar() or 0
    items = (
        await session.execute(
            query.order_by(User.created_at.desc()).offset(offset).limit(limit)
        )
    ).scalars().all()
    return {
        "total": total,
        "items": [
            {
                "id": u.id,
                "telegram_id": u.telegram_id,
                "username": u.username,
                "first_name": u.first_name,
                "last_name": u.last_name,
                "balance_cents": u.balance_cents,
                "status": u.status.value,
                "created_at": u.created_at.isoformat() if u.created_at else None,
            }
            for u in items
        ],
    }


@router.get("/admin/users/{user_id}")
async def get_user(
    user_id: int,
    admin: Admin = Depends(require_perm("users.view")),
    session: AsyncSession = Depends(get_db),
):
    user = await session.get(User, user_id)
    if not user:
        raise HTTPException(404, "User not found")
    return {
        "id": user.id,
        "telegram_id": user.telegram_id,
        "username": user.username,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "balance_cents": user.balance_cents,
        "pending_cents": user.pending_cents,
        "total_earned_cents": user.total_earned_cents,
        "status": user.status.value,
        "created_at": user.created_at.isoformat() if user.created_at else None,
    }


@router.post("/admin/users/{user_id}/disable")
async def disable_user(
    user_id: int,
    admin: Admin = Depends(require_perm("users.edit")),
    session: AsyncSession = Depends(get_db),
):
    user = await session.get(User, user_id)
    if not user:
        raise HTTPException(404, "User not found")
    user.status = UserStatus.DISABLED
    session.add(user)
    await session.commit()
    await write_audit(
        session,
        action=AuditAction.USER_DISABLED,
        actor_admin_id=admin.id,
        target_type="user",
        target_id=str(user_id),
    )
    return {"ok": True}


@router.post("/admin/users/{user_id}/enable")
async def enable_user(
    user_id: int,
    admin: Admin = Depends(require_perm("users.edit")),
    session: AsyncSession = Depends(get_db),
):
    user = await session.get(User, user_id)
    if not user:
        raise HTTPException(404, "User not found")
    user.status = UserStatus.ACTIVE
    session.add(user)
    await session.commit()
    await write_audit(
        session,
        action=AuditAction.USER_ENABLED,
        actor_admin_id=admin.id,
        target_type="user",
        target_id=str(user_id),
    )
    return {"ok": True}


@router.post("/admin/users/{user_id}/adjust-balance")
async def adjust_balance(
    user_id: int,
    body: BalanceAdjustRequest,
    admin: Admin = Depends(require_perm("users.edit")),
    session: AsyncSession = Depends(get_db),
):
    result = await session.execute(
        select(User).where(User.id == user_id).with_for_update()
    )
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(404, "User not found")
    cents = int(round(body.amount_usd * 100))
    await wallet_service.adjust_balance(
        session, user=user, amount_cents=cents, reason=body.reason, admin_id=admin.id
    )
    await session.commit()
    return {"ok": True, "balance_cents": user.balance_cents}


# ---- QR ----


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
                "claimed_by_username": getattr(
                    getattr(q, "claimed_by", None), "username", None
                ),
                "claimed_by_first_name": getattr(
                    getattr(q, "claimed_by", None), "first_name", None
                ),
                "claimed_by_telegram_id": getattr(
                    getattr(q, "claimed_by", None), "telegram_id", None
                ),
                "has_image": bool(q.image_png),
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
    codes = await qr_service.create_qr_codes(
        session,
        reward_cents=reward_cents,
        quantity=body.quantity,
        expires_at=expires_at,
        campaign_name=body.campaign_name,
        description=body.description,
        admin_id=admin.id,
    )
    await session.commit()
    return {
        "ok": True,
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
    message_text = None
    if body.chat_id:
        from app.bot import claim_button, get_bot

        bot = get_bot()
        if bot:
            try:
                msg = (
                    f"<b>NEW QR REWARD</b>\n\n"
                    f"QR: <code>{qr.code}</code>\n"
                    f"Reward: ${qr.reward_cents / 100:.2f}\n"
                    f"One user only!\n\n"
                    f"Tap <b>CLAIM REWARD</b> — the bot will send you the QR image."
                )
                await bot.send_message(
                    chat_id=body.chat_id,
                    text=msg,
                    reply_markup=claim_button(qr.code),
                )
                message_text = "Posted to Telegram (text only)"
            except Exception as exc:
                logger.warning("Failed to post QR to Telegram: %s", exc)
                message_text = (
                    f"Marked POSTED but Telegram send failed: {type(exc).__name__}"
                )
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


@router.get("/admin/qrs/{qr_id}/image")
async def qr_image(
    qr_id: int,
    admin: Admin = Depends(require_perm("qr.view")),
    session: AsyncSession = Depends(get_db),
):
    from app.services.qr_image_service import claim_payload, generate_qr_png_bytes

    qr = await qr_service.get_qr_by_id(session, qr_id)
    if not qr:
        raise HTTPException(404, "QR not found")
    if qr.image_png:
        png = qr.image_png
    else:
        png = generate_qr_png_bytes(
            claim_payload(qr.code, settings.bot_username or None)
        )
    return Response(
        content=png,
        media_type="image/png",
        headers={"Content-Disposition": f'attachment; filename="{qr.code}.png"'},
    )


@router.post("/admin/qrs/{qr_id}/upload-image")
async def upload_qr_image(
    qr_id: int,
    request: Request,
    admin: Admin = Depends(require_perm("qr.create")),
    session: AsyncSession = Depends(get_db),
):
    form = await request.form()
    file = form.get("file")
    if file is None or not hasattr(file, "read"):
        raise HTTPException(400, "file is required")
    data = await file.read()
    if not data:
        raise HTTPException(400, "Empty file")
    if len(data) > 5 * 1024 * 1024:
        raise HTTPException(400, "File too large (max 5MB)")
    ct = (getattr(file, "content_type", "") or "").lower()
    name = (getattr(file, "filename", "") or "").lower()
    ok_type = ct.startswith("image/") or name.endswith(
        (".png", ".jpg", ".jpeg", ".webp", ".gif")
    )
    if not ok_type:
        raise HTTPException(400, "Only image files allowed (PNG/JPEG/WebP)")
    qr = await qr_service.get_qr_by_id(session, qr_id)
    if not qr:
        raise HTTPException(404, "QR not found")
    qr.image_png = data
    session.add(qr)
    await session.commit()
    return {"ok": True, "qr_id": qr.id, "code": qr.code, "size": len(data)}


@router.post("/admin/qrs/{qr_id}/clone")
async def clone_qr(
    qr_id: int,
    quantity: int = 1,
    admin: Admin = Depends(require_perm("qr.create")),
    session: AsyncSession = Depends(get_db),
):
    quantity = max(1, min(int(quantity or 1), 100))
    src = await qr_service.get_qr_by_id(session, qr_id)
    if not src:
        raise HTTPException(404, "QR not found")
    created = await qr_service.create_qr_codes(
        session,
        reward_cents=src.reward_cents,
        quantity=quantity,
        expires_at=src.expires_at,
        campaign_name=src.campaign_name,
        description=((src.description or "") + " (clone)").strip(),
        admin_id=admin.id,
    )
    if src.image_png and created:
        for c in created:
            c.image_png = src.image_png
            session.add(c)
        await session.commit()
    await write_audit(
        session,
        action=AuditAction.QR_CLONED,
        actor_admin_id=admin.id,
        target_type="qr",
        target_id=str(qr_id),
        metadata={"quantity": quantity},
    )
    return {"ok": True, "created": len(created), "codes": [c.code for c in created]}


@router.post("/admin/qrs/{qr_id}/schedule")
async def schedule_qr_post(
    qr_id: int,
    body: SchedulePostRequest,
    admin: Admin = Depends(require_perm("qr.post")),
    session: AsyncSession = Depends(get_db),
):
    qr = await qr_service.get_qr_by_id(session, qr_id)
    if not qr:
        raise HTTPException(404, "QR not found")
    try:
        when = datetime.fromisoformat(body.scheduled_at.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(400, "Invalid scheduled_at ISO datetime")
    qr.scheduled_chat_id = body.chat_id
    qr.scheduled_post_at = when
    session.add(qr)
    await session.commit()
    return {"ok": True, "scheduled_post_at": when.isoformat(), "chat_id": body.chat_id}


# ---- Claims ----


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
                "first_name": c.user.first_name if c.user else None,
                "last_name": c.user.last_name if c.user else None,
                "telegram_id": c.user.telegram_id if c.user else None,
                "reward_cents": c.reward_cents,
                "status": c.status.value,
                "created_at": c.created_at.isoformat() if c.created_at else None,
                "approved_at": c.approved_at.isoformat() if c.approved_at else None,
                "rejected_at": c.rejected_at.isoformat() if c.rejected_at else None,
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
        await session.commit()
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
        await session.commit()
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "status": claim.status.value}


@router.post("/admin/claims/{claim_id}/hold")
async def hold_claim(
    claim_id: int,
    admin: Admin = Depends(require_perm("claims.approve")),
    session: AsyncSession = Depends(get_db),
):
    try:
        claim = await claim_service.hold_claim(session, claim_id, admin.id)
        await session.commit()
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "status": claim.status.value}


# ---- Withdrawals ----


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
                "first_name": w.user.first_name if w.user else None,
                "telegram_id": w.user.telegram_id if w.user else None,
                "amount_cents": w.amount_cents,
                "status": w.status.value,
                "created_at": w.created_at.isoformat() if w.created_at else None,
                "paid_at": w.paid_at.isoformat() if w.paid_at else None,
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
        await session.commit()
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
        await session.commit()
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "status": w.status.value}


@router.post("/admin/withdrawals/{wid}/hold")
async def hold_withdrawal(
    wid: int,
    admin: Admin = Depends(require_perm("withdrawals.pay")),
    session: AsyncSession = Depends(get_db),
):
    try:
        w = await withdrawal_service.hold_withdrawal(session, wid, admin.id)
        await session.commit()
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "status": w.status.value}


# ---- Channels / settings / audit / reset ----


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
                "title": c.title,
                "channel_id": c.channel_id,
                "username": c.username,
                "invite_url": c.invite_url,
                "is_required": c.is_required,
                "is_active": c.is_active,
            }
            for c in channels
        ]
    }


@router.post("/admin/channels")
async def create_channel(
    body: ChannelCreateRequest,
    admin: Admin = Depends(require_perm("channels.edit")),
    session: AsyncSession = Depends(get_db),
):
    ch = await force_join_service.create_channel(
        session,
        channel_id=body.channel_id,
        title=body.title,
        username=body.username,
        invite_url=body.invite_url,
        is_required=body.is_required,
    )
    await session.commit()
    return {"ok": True, "id": ch.id}


@router.get("/admin/settings")
async def get_settings_api(
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
        "default_reward_usd": (
            "default_reward_cents",
            lambda v: str(int(round(v * 100))),
        ),
        "min_withdrawal_usd": (
            "min_withdrawal_cents",
            lambda v: str(int(round(v * 100))),
        ),
        "max_withdrawal_usd": (
            "max_withdrawal_cents",
            lambda v: str(int(round(v * 100))),
        ),
        "force_join_enabled": ("force_join_enabled", lambda v: str(v).lower()),
        "claim_approval_required": (
            "claim_approval_required",
            lambda v: str(v).lower(),
        ),
        "qr_default_expiry_hours": ("qr_default_expiry_hours", str),
        "maintenance_mode": ("maintenance_mode", lambda v: str(v).lower()),
        "bot_welcome_message": ("bot_welcome_message", str),
        "auto_approve_enabled": ("auto_approve_enabled", lambda v: str(v).lower()),
        "auto_approve_max_usd": ("auto_approve_max_usd", str),
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
    total = (
        await session.execute(select(func.count()).select_from(q.subquery()))
    ).scalar() or 0
    items = (
        await session.execute(
            q.order_by(AuditLog.created_at.desc()).offset(offset).limit(limit)
        )
    ).scalars().all()
    return {
        "total": total,
        "items": [
            {
                "id": a.id,
                "action": a.action.value if hasattr(a.action, "value") else str(a.action),
                "actor_admin_id": a.actor_admin_id,
                "actor_user_id": a.actor_user_id,
                "target_type": a.target_type,
                "target_id": a.target_id,
                "ip_address": a.ip_address,
                "created_at": a.created_at.isoformat() if a.created_at else None,
            }
            for a in items
        ],
    }


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


@router.post("/admin/broadcast")
async def broadcast(
    body: BroadcastRequest,
    admin: Admin = Depends(require_perm("settings.edit")),
    session: AsyncSession = Depends(get_db),
):
    from app.bot import get_bot

    bot = get_bot()
    if not bot:
        raise HTTPException(503, "Bot not ready")
    q = select(User)
    if body.only_active:
        q = q.where(User.status == UserStatus.ACTIVE)
    users = (await session.execute(q)).scalars().all()
    sent = failed = 0
    for u in users:
        try:
            await bot.send_message(u.telegram_id, body.message, parse_mode="HTML")
            sent += 1
        except Exception:
            failed += 1
    await write_audit(
        session,
        action=AuditAction.BROADCAST_SENT,
        actor_admin_id=admin.id,
        metadata={"sent": sent, "failed": failed},
    )
    return {"ok": True, "sent": sent, "failed": failed, "total": len(users)}


@router.post("/admin/change-password")
async def change_password(
    body: ChangePasswordRequest,
    admin: Admin = Depends(get_current_admin),
    session: AsyncSession = Depends(get_db),
):
    if not verify_password(body.current_password, admin.password_hash):
        raise HTTPException(400, "Current password is incorrect")
    admin.password_hash = hash_password(body.new_password)
    session.add(admin)
    await session.commit()
    return {"ok": True, "message": "Password updated"}


@router.get("/admin/campaigns")
async def campaign_stats(
    admin: Admin = Depends(require_perm("qr.view")),
    session: AsyncSession = Depends(get_db),
):
    rows = (
        await session.execute(
            select(
                QRCode.campaign_name,
                func.count(QRCode.id),
                func.sum(QRCode.reward_cents),
                func.count().filter(QRCode.status == QRStatus.CLAIMED),
                func.count().filter(QRCode.status == QRStatus.AVAILABLE),
            ).group_by(QRCode.campaign_name)
        )
    ).all()
    return {
        "items": [
            {
                "campaign": r[0] or "(none)",
                "total_qrs": r[1],
                "total_reward_cents": int(r[2] or 0),
                "claimed": r[3],
                "available": r[4],
            }
            for r in rows
        ]
    }


@router.get("/admin/tickets")
async def list_tickets(
    status: Optional[str] = None,
    offset: int = 0,
    limit: int = 50,
    admin: Admin = Depends(require_perm("users.view")),
    session: AsyncSession = Depends(get_db),
):
    from app.models import SupportTicket, TicketStatus

    q = select(SupportTicket).options(selectinload(SupportTicket.user))
    cq = select(func.count()).select_from(SupportTicket)
    if status:
        st = TicketStatus(status)
        q = q.where(SupportTicket.status == st)
        cq = cq.where(SupportTicket.status == st)
    total = (await session.execute(cq)).scalar() or 0
    items = (
        await session.execute(
            q.order_by(SupportTicket.created_at.desc()).offset(offset).limit(limit)
        )
    ).scalars().all()
    return {
        "total": total,
        "items": [
            {
                "id": t.id,
                "subject": t.subject,
                "message": t.message,
                "status": t.status.value,
                "admin_reply": t.admin_reply,
                "created_at": t.created_at.isoformat() if t.created_at else None,
                "username": t.user.username if t.user else None,
                "telegram_id": t.user.telegram_id if t.user else None,
                "first_name": t.user.first_name if t.user else None,
            }
            for t in items
        ],
    }


@router.post("/admin/tickets/{tid}/reply")
async def reply_ticket(
    tid: int,
    body: TicketReplyRequest,
    admin: Admin = Depends(require_perm("users.view")),
    session: AsyncSession = Depends(get_db),
):
    from app.bot import get_bot
    from app.models import SupportTicket, TicketStatus

    t = (
        await session.execute(
            select(SupportTicket)
            .options(selectinload(SupportTicket.user))
            .where(SupportTicket.id == tid)
        )
    ).scalar_one_or_none()
    if not t:
        raise HTTPException(404, "Ticket not found")
    t.admin_reply = body.reply
    t.handled_by_admin_id = admin.id
    t.status = TicketStatus.CLOSED if body.close else TicketStatus.IN_PROGRESS
    session.add(t)
    await session.commit()
    bot = get_bot()
    if bot and t.user:
        try:
            await bot.send_message(
                t.user.telegram_id,
                f"<b>Support Reply</b>\n\nTicket #{t.id}: {t.subject}\n\n{body.reply}",
                parse_mode="HTML",
            )
        except Exception:
            pass
    await write_audit(
        session,
        action=AuditAction.TICKET_REPLIED,
        actor_admin_id=admin.id,
        target_type="ticket",
        target_id=str(tid),
    )
    return {"ok": True, "status": t.status.value}


@router.get("/admin/export/claims")
async def export_claims(
    status: Optional[str] = None,
    admin: Admin = Depends(require_perm("claims.view")),
    session: AsyncSession = Depends(get_db),
):
    st = ClaimStatus(status) if status else None
    items, _ = await claim_service.list_claims(
        session, status=st, offset=0, limit=10000
    )
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(
        [
            "id",
            "qr_code",
            "user_id",
            "username",
            "telegram_id",
            "reward_usd",
            "status",
            "created_at",
        ]
    )
    for c in items:
        w.writerow(
            [
                c.id,
                c.qr_code.code if c.qr_code else "",
                c.user_id,
                c.user.username if c.user else "",
                c.user.telegram_id if c.user else "",
                f"{c.reward_cents/100:.2f}",
                c.status.value,
                c.created_at.isoformat() if c.created_at else "",
            ]
        )
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=claims.csv"},
    )


@router.get("/admin/export/qrs")
async def export_qrs(
    admin: Admin = Depends(require_perm("qr.view")),
    session: AsyncSession = Depends(get_db),
):
    items, _ = await qr_service.list_qrs(session, offset=0, limit=10000)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(
        ["id", "code", "reward_usd", "status", "campaign", "claimed_by_user_id", "created_at"]
    )
    for q in items:
        w.writerow(
            [
                q.id,
                q.code,
                f"{q.reward_cents/100:.2f}",
                q.status.value,
                q.campaign_name or "",
                q.claimed_by_user_id or "",
                q.created_at.isoformat() if q.created_at else "",
            ]
        )
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=qrs.csv"},
    )


@router.get("/admin/export/withdrawals")
async def export_withdrawals(
    admin: Admin = Depends(require_perm("withdrawals.view")),
    session: AsyncSession = Depends(get_db),
):
    items, _ = await withdrawal_service.list_withdrawals(
        session, offset=0, limit=10000
    )
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(
        ["id", "user_id", "username", "telegram_id", "amount_usd", "status", "created_at"]
    )
    for x in items:
        w.writerow(
            [
                x.id,
                x.user_id,
                x.user.username if x.user else "",
                x.user.telegram_id if x.user else "",
                f"{x.amount_cents/100:.2f}",
                x.status.value,
                x.created_at.isoformat() if x.created_at else "",
            ]
        )
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=withdrawals.csv"},
    )
