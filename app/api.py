"""Admin REST API + auth."""

from __future__ import annotations

import asyncio
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
    AdminRole,
    AuditAction,
    AuditLog,
    BroadcastBatch,
    BroadcastDelivery,
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
    expiry_minutes: Optional[int] = Field(None, ge=1, le=525600)
    expiry_hours: Optional[int] = Field(None, ge=1)  # legacy alias
    campaign_name: Optional[str] = None
    app_name: Optional[str] = Field(None, max_length=128)
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
    is_active: bool = True


class ChannelUpdateRequest(BaseModel):
    title: Optional[str] = None
    channel_id: Optional[int] = None
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
    claims_enabled: Optional[bool] = None
    withdrawals_enabled: Optional[bool] = None
    dm_notify_on_post: Optional[bool] = None


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
                "app_name": getattr(q, "app_name", None),
                "description": q.description,
                "created_at": q.created_at.isoformat() if q.created_at else None,
                "posted_at": q.posted_at.isoformat() if q.posted_at else None,
                "claimed_at": q.claimed_at.isoformat() if q.claimed_at else None,
                "expires_at": q.expires_at.isoformat() if q.expires_at else None,
                "created_by_admin_id": q.created_by_admin_id,
                "created_by_username": getattr(
                    getattr(q, "created_by", None), "username", None
                ),
                "claimed_by_user_id": q.claimed_by_user_id,
                "claimed_by_username": getattr(
                    getattr(q, "claimed_by", None), "username", None
                ),
                "claimed_by_first_name": getattr(
                    getattr(q, "claimed_by", None), "first_name", None
                ),
                "claimed_by_last_name": getattr(
                    getattr(q, "claimed_by", None), "last_name", None
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
    if body.expiry_minutes:
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=body.expiry_minutes)
    elif body.expiry_hours:
        expires_at = datetime.now(timezone.utc) + timedelta(hours=body.expiry_hours)
    codes = await qr_service.create_qr_codes(
        session,
        reward_cents=reward_cents,
        quantity=body.quantity,
        expires_at=expires_at,
        campaign_name=body.campaign_name,
        description=body.description,
        admin_id=admin.id,
        app_name=body.app_name,
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
    """Mark QR as POSTED and DM all active users with CLAIM button (no channel post)."""
    qr = await qr_service.get_qr_by_id(session, qr_id)
    if not qr:
        raise HTTPException(404, "QR not found")
    try:
        qr = await qr_service.mark_qr_posted(session, qr, admin.id)
    except ValueError as e:
        raise HTTPException(400, str(e))

    from app.bot import claim_button, get_bot

    dm_on = await settings_service.get_setting_bool(session, "dm_notify_on_post", True)
    bot = get_bot()
    sent = 0
    failed = 0
    if bot and dm_on:
        users = (
            await session.execute(
                select(User).where(User.status == UserStatus.ACTIVE)
            )
        ).scalars().all()
        app_label = (qr.app_name or "QR task").strip()
        exp_line = "No expiry"
        if qr.expires_at:
            exp_line = qr.expires_at.strftime("%H:%M:%S UTC")
        reward_str = f"{qr.reward_cents / 100:.2f}"
        msg = (
            f"<b>New task #{qr.id} · {app_label}</b>\n"
            f"Your reward: <b>${reward_str}</b>\n"
            f"Expires: {exp_line}\n"
            f"Claim before opening the payment link."
        )
        for u in users:
            try:
                await bot.send_message(
                    chat_id=u.telegram_id,
                    text=msg,
                    reply_markup=claim_button(qr.code),
                    parse_mode="HTML",
                )
                sent += 1
            except Exception:
                failed += 1
        message_text = f"Notified users via DM — sent: {sent}, failed: {failed}"
    elif bot and not dm_on:
        message_text = "Marked POSTED (DM notify disabled in settings)"
    else:
        message_text = "Marked POSTED (bot offline — no DMs sent)"

    return {
        "ok": True,
        "status": qr.status.value,
        "message": message_text,
        "sent": sent,
        "failed": failed,
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
    request: Request,
    admin: Admin = Depends(require_perm("claims.reject")),
    session: AsyncSession = Depends(get_db),
):
    """Reject claim. JSON {reason} or multipart: reason + optional photo."""
    reason = ""
    photo_bytes: Optional[bytes] = None
    photo_filename = "reject.jpg"
    ct = (request.headers.get("content-type") or "").lower()
    if "multipart/form-data" in ct:
        form = await request.form()
        reason = str(form.get("reason") or "").strip()
        file = form.get("photo") or form.get("file")
        if file is not None and hasattr(file, "read"):
            data = await file.read()
            if data:
                if len(data) > 5 * 1024 * 1024:
                    raise HTTPException(400, "Photo too large (max 5MB)")
                fct = (getattr(file, "content_type", "") or "").lower()
                fname = (getattr(file, "filename", "") or "").lower()
                ok = fct.startswith("image/") or fname.endswith(
                    (".png", ".jpg", ".jpeg", ".webp", ".gif")
                )
                if not ok:
                    raise HTTPException(400, "Only image files allowed")
                photo_bytes = data
                photo_filename = getattr(file, "filename", None) or "reject.jpg"
    else:
        try:
            body = await request.json()
            reason = str((body or {}).get("reason") or "").strip()
        except Exception:
            reason = ""
    if not reason:
        raise HTTPException(400, "Reason required")
    try:
        claim = await claim_service.reject_claim(
            session,
            claim_id,
            admin.id,
            reason,
            photo_bytes=photo_bytes,
            photo_filename=photo_filename,
        )
        await session.commit()
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {
        "ok": True,
        "status": claim.status.value,
        "photo_sent": bool(photo_bytes),
    }


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
                "payout_method": getattr(w, "payout_method", None),
                "payout_details": getattr(w, "payout_details", None),
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
    try:
        ch = await force_join_service.create_channel(
            session,
            channel_id=body.channel_id,
            title=body.title,
            username=body.username,
            invite_url=body.invite_url,
            is_required=body.is_required,
            is_active=body.is_active,
        )
        await session.commit()
        return {"ok": True, "id": ch.id}
    except Exception as e:
        raise HTTPException(400, f"Failed to add channel: {e}") from e


@router.patch("/admin/channels/{channel_id}")
async def update_channel(
    channel_id: int,
    body: ChannelUpdateRequest,
    admin: Admin = Depends(require_perm("channels.edit")),
    session: AsyncSession = Depends(get_db),
):
    channels = await force_join_service.list_channels(session)
    ch = next((c for c in channels if c.id == channel_id), None)
    if not ch:
        raise HTTPException(404, "Channel not found")
    data = body.model_dump(exclude_none=True)
    try:
        await force_join_service.update_channel(session, ch, **data)
        await session.commit()
        return {"ok": True}
    except Exception as e:
        raise HTTPException(400, f"Failed to update channel: {e}") from e


@router.delete("/admin/channels/{channel_id}")
async def delete_channel(
    channel_id: int,
    admin: Admin = Depends(require_perm("channels.edit")),
    session: AsyncSession = Depends(get_db),
):
    channels = await force_join_service.list_channels(session)
    ch = next((c for c in channels if c.id == channel_id), None)
    if not ch:
        raise HTTPException(404, "Channel not found")
    await session.delete(ch)
    await session.commit()
    return {"ok": True}


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
        "claims_enabled": ("claims_enabled", lambda v: str(v).lower()),
        "withdrawals_enabled": ("withdrawals_enabled", lambda v: str(v).lower()),
        "dm_notify_on_post": ("dm_notify_on_post", lambda v: str(v).lower()),
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


_broadcast_lock = asyncio.Lock()


@router.post("/admin/broadcast")
async def broadcast(
    request: Request,
    admin: Admin = Depends(require_perm("settings.edit")),
    session: AsyncSession = Depends(get_db),
):
    """Broadcast text and optional photo. JSON or multipart (message, only_active, photo)."""
    import asyncio as _asyncio

    from aiogram.types import BufferedInputFile

    from app.bot import get_bot

    message = ""
    only_active = True
    photo_bytes: Optional[bytes] = None
    video_bytes: Optional[bytes] = None
    photo_filename = "broadcast.jpg"
    video_filename = "broadcast.mp4"
    buttons_raw = ""
    ct = (request.headers.get("content-type") or "").lower()
    if "multipart/form-data" in ct:
        form = await request.form()
        message = str(form.get("message") or "").strip()
        only_active = str(form.get("only_active") or "true").lower() in (
            "1",
            "true",
            "yes",
            "on",
        )
        buttons_raw = str(form.get("buttons") or "").strip()
        file = form.get("photo") or form.get("file")
        video = form.get("video")
        if file is not None and hasattr(file, "read"):
            data = await file.read()
            if data:
                if len(data) > 8 * 1024 * 1024:
                    raise HTTPException(400, "Photo too large (max 8MB)")
                fct = (getattr(file, "content_type", "") or "").lower()
                fname = (getattr(file, "filename", "") or "").lower()
                ok = fct.startswith("image/") or fname.endswith(
                    (".png", ".jpg", ".jpeg", ".webp", ".gif")
                )
                if not ok:
                    raise HTTPException(400, "Only image files allowed for photo")
                photo_bytes = data
                photo_filename = getattr(file, "filename", None) or "broadcast.jpg"
        if video is not None and hasattr(video, "read"):
            data = await video.read()
            if data:
                if len(data) > 200 * 1024 * 1024:
                    raise HTTPException(400, "Video too large (max 200MB)")
                fct = (getattr(video, "content_type", "") or "").lower()
                fname = (getattr(video, "filename", "") or "").lower()
                ok = fct.startswith("video/") or fname.endswith(
                    (".mp4", ".mov", ".webm", ".mkv")
                )
                if not ok:
                    raise HTTPException(400, "Only video files allowed")
                video_bytes = data
                video_filename = getattr(video, "filename", None) or "broadcast.mp4"
    else:
        try:
            body = await request.json()
        except Exception:
            body = {}
        message = str((body or {}).get("message") or "").strip()
        only_active = bool((body or {}).get("only_active", True))
        buttons_raw = str((body or {}).get("buttons") or "").strip()

    if not message and not photo_bytes and not video_bytes:
        raise HTTPException(400, "Message, photo, or video required")
    if not message:
        message = " "

    # buttons: "Label|https://url, Label2|https://url2"
    reply_markup = None
    if buttons_raw:
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        parts = []
        for chunk in buttons_raw.replace("\n", ",").split(","):
            chunk = chunk.strip()
            if chunk:
                parts.append(chunk)
        row = []
        for part in parts:
            if "|" not in part:
                continue
            label, url = part.split("|", 1)
            label, url = label.strip()[:64], url.strip()
            if label and url.startswith(("http://", "https://", "tg://")):
                row.append(InlineKeyboardButton(text=label, url=url))
        if row:
            rows = [row[i : i + 2] for i in range(0, len(row), 2)]
            reply_markup = InlineKeyboardMarkup(inline_keyboard=rows)

    if _broadcast_lock.locked():
        raise HTTPException(429, "Broadcast already in progress — wait for it to finish")

    async with _broadcast_lock:
        bot = get_bot()
        if not bot:
            raise HTTPException(503, "Bot is offline — wait a few seconds and retry")
        q = select(User)
        if only_active:
            q = q.where(User.status == UserStatus.ACTIVE)
        users = (await session.execute(q)).scalars().all()
        if not users:
            return {
                "ok": True,
                "sent": 0,
                "failed": 0,
                "total": 0,
                "message": "No users to send",
            }

        batch = BroadcastBatch(
            admin_id=admin.id,
            preview=(message or "")[:255],
            has_photo=bool(photo_bytes),
            has_video=bool(video_bytes),
        )
        session.add(batch)
        await session.flush()
        batch_id = batch.id

        sem = _asyncio.Semaphore(25)
        sent = 0
        failed = 0
        lock_counters = _asyncio.Lock()
        deliveries: list[tuple[int, int]] = []
        caption = message[:1024]

        async def _send_one(uid: int) -> None:
            nonlocal sent, failed
            async with sem:
                try:
                    if video_bytes:
                        video = BufferedInputFile(
                            video_bytes, filename=video_filename
                        )
                        msg = await bot.send_video(
                            chat_id=uid,
                            video=video,
                            caption=caption,
                            parse_mode="HTML",
                            reply_markup=reply_markup,
                        )
                    elif photo_bytes:
                        photo = BufferedInputFile(
                            photo_bytes, filename=photo_filename
                        )
                        msg = await bot.send_photo(
                            chat_id=uid,
                            photo=photo,
                            caption=caption,
                            parse_mode="HTML",
                            reply_markup=reply_markup,
                        )
                    else:
                        msg = await bot.send_message(
                            chat_id=uid,
                            text=message,
                            parse_mode="HTML",
                            reply_markup=reply_markup,
                        )
                    mid = getattr(msg, "message_id", None)
                    async with lock_counters:
                        sent += 1
                        if mid is not None:
                            deliveries.append((uid, int(mid)))
                except Exception:
                    async with lock_counters:
                        failed += 1

        await _asyncio.gather(
            *[_send_one(u.telegram_id) for u in users],
            return_exceptions=True,
        )

        for uid, mid in deliveries:
            session.add(
                BroadcastDelivery(
                    batch_id=batch_id,
                    telegram_id=uid,
                    message_id=mid,
                )
            )
        batch.sent_count = sent
        batch.failed_count = failed
        session.add(batch)

        await write_audit(
            session,
            action=AuditAction.BROADCAST_SENT,
            actor_admin_id=admin.id,
            metadata={
                "sent": sent,
                "failed": failed,
                "total": len(users),
                "has_photo": bool(photo_bytes),
                "has_video": bool(video_bytes),
                "batch_id": batch_id,
            },
        )
        return {
            "ok": True,
            "sent": sent,
            "failed": failed,
            "total": len(users),
            "has_photo": bool(photo_bytes),
            "has_video": bool(video_bytes),
            "batch_id": batch_id,
        }




@router.get("/admin/broadcasts")
async def list_broadcasts(
    admin: Admin = Depends(require_perm("settings.edit")),
    session: AsyncSession = Depends(get_db),
):
    rows = (
        await session.execute(
            select(BroadcastBatch).order_by(BroadcastBatch.created_at.desc()).limit(20)
        )
    ).scalars().all()
    return {
        "items": [
            {
                "id": b.id,
                "preview": b.preview,
                "has_photo": b.has_photo,
                "has_video": b.has_video,
                "sent_count": b.sent_count,
                "failed_count": b.failed_count,
                "deleted": b.deleted,
                "created_at": b.created_at.isoformat() if b.created_at else None,
            }
            for b in rows
        ]
    }


@router.post("/admin/broadcasts/{batch_id}/delete")
async def delete_broadcast(
    batch_id: int,
    admin: Admin = Depends(require_perm("settings.edit")),
    session: AsyncSession = Depends(get_db),
):
    """Delete a broadcast from all user chats (best-effort, Telegram 48h limit)."""
    import asyncio as _asyncio

    from app.bot import get_bot

    batch = (
        await session.execute(
            select(BroadcastBatch).where(BroadcastBatch.id == batch_id)
        )
    ).scalar_one_or_none()
    if not batch:
        raise HTTPException(404, "Broadcast not found")
    if batch.deleted:
        return {"ok": True, "deleted": 0, "failed": 0, "message": "Already deleted"}

    bot = get_bot()
    if not bot:
        raise HTTPException(503, "Bot is offline")

    rows = (
        await session.execute(
            select(BroadcastDelivery).where(
                BroadcastDelivery.batch_id == batch_id,
                BroadcastDelivery.deleted.is_(False),
            )
        )
    ).scalars().all()

    sem = _asyncio.Semaphore(25)
    deleted = 0
    failed = 0
    lock = _asyncio.Lock()

    async def _del_one(row: BroadcastDelivery) -> None:
        nonlocal deleted, failed
        async with sem:
            try:
                await bot.delete_message(chat_id=row.telegram_id, message_id=row.message_id)
                row.deleted = True
                async with lock:
                    deleted += 1
            except Exception:
                async with lock:
                    failed += 1

    await _asyncio.gather(*[_del_one(r) for r in rows], return_exceptions=True)
    batch.deleted = True
    session.add(batch)
    await session.commit()
    return {
        "ok": True,
        "deleted": deleted,
        "failed": failed,
        "total": len(rows),
        "batch_id": batch_id,
    }


@router.post("/admin/broadcasts/delete-last")
async def delete_last_broadcast(
    admin: Admin = Depends(require_perm("settings.edit")),
    session: AsyncSession = Depends(get_db),
):
    batch = (
        await session.execute(
            select(BroadcastBatch)
            .where(BroadcastBatch.deleted.is_(False))
            .order_by(BroadcastBatch.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if not batch:
        raise HTTPException(404, "No broadcast to delete")
    # reuse logic via internal call pattern
    return await delete_broadcast(batch.id, admin, session)




@router.post("/admin/broadcast-channel")
async def broadcast_to_channel(
    request: Request,
    admin: Admin = Depends(require_perm("settings.edit")),
    session: AsyncSession = Depends(get_db),
):
    """Post message/photo/video to a channel/group where the bot is admin."""
    from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup

    from app.bot import get_bot

    bot = get_bot()
    if not bot:
        raise HTTPException(503, "Bot is offline")

    chat_id = ""
    message = ""
    photo_bytes = None
    video_bytes = None
    photo_filename = "ch.jpg"
    video_filename = "ch.mp4"
    buttons_raw = ""
    ct = (request.headers.get("content-type") or "").lower()
    if "multipart/form-data" in ct:
        form = await request.form()
        chat_id = str(form.get("chat_id") or "").strip()
        message = str(form.get("message") or "").strip() or " "
        buttons_raw = str(form.get("buttons") or "").strip()
        file = form.get("photo")
        video = form.get("video")
        if file is not None and hasattr(file, "read"):
            photo_bytes = await file.read()
            photo_filename = getattr(file, "filename", None) or "ch.jpg"
        if video is not None and hasattr(video, "read"):
            video_bytes = await video.read()
            video_filename = getattr(video, "filename", None) or "ch.mp4"
    else:
        try:
            body = await request.json()
        except Exception:
            body = {}
        chat_id = str((body or {}).get("chat_id") or "").strip()
        message = str((body or {}).get("message") or "").strip() or " "
        buttons_raw = str((body or {}).get("buttons") or "").strip()

    if not chat_id:
        raise HTTPException(400, "chat_id required (e.g. -100… or @channel)")

    reply_markup = None
    if buttons_raw:
        row = []
        for part in buttons_raw.replace("\n", ",").split(","):
            part = part.strip()
            if "|" not in part:
                continue
            label, url = part.split("|", 1)
            label, url = label.strip()[:64], url.strip()
            if label and url.startswith(("http://", "https://", "tg://")):
                row.append(InlineKeyboardButton(text=label, url=url))
        if row:
            rows = [row[i : i + 2] for i in range(0, len(row), 2)]
            reply_markup = InlineKeyboardMarkup(inline_keyboard=rows)

    try:
        # resolve @username
        target = chat_id
        if chat_id.startswith("@"):
            pass
        else:
            try:
                target = int(chat_id)
            except ValueError:
                target = chat_id
        if video_bytes:
            msg = await bot.send_video(
                chat_id=target,
                video=BufferedInputFile(video_bytes, filename=video_filename),
                caption=message[:1024],
                parse_mode="HTML",
                reply_markup=reply_markup,
            )
        elif photo_bytes:
            msg = await bot.send_photo(
                chat_id=target,
                photo=BufferedInputFile(photo_bytes, filename=photo_filename),
                caption=message[:1024],
                parse_mode="HTML",
                reply_markup=reply_markup,
            )
        else:
            msg = await bot.send_message(
                chat_id=target,
                text=message,
                parse_mode="HTML",
                reply_markup=reply_markup,
            )
    except Exception as e:
        raise HTTPException(400, f"Channel send failed: {type(e).__name__}: {e}")

    await write_audit(
        session,
        action=AuditAction.BROADCAST_SENT,
        actor_admin_id=admin.id,
        target_type="channel",
        target_id=str(chat_id),
        metadata={"kind": "channel_broadcast", "message_id": getattr(msg, "message_id", None)},
    )
    return {
        "ok": True,
        "message": "Posted to channel",
        "chat_id": str(chat_id),
        "message_id": getattr(msg, "message_id", None),
    }


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


# ---- Direct message / notify one user ----


class DirectMessageRequest(BaseModel):
    telegram_id: int
    message: str = Field(..., min_length=1, max_length=4000)


class NotifyUserRequest(BaseModel):
    telegram_id: int


@router.post("/admin/dm")
async def send_direct_message(
    request: Request,
    admin: Admin = Depends(require_perm("users.view")),
    session: AsyncSession = Depends(get_db),
):
    """DM a user: JSON {telegram_id, message} or multipart with optional photo."""
    from aiogram.types import BufferedInputFile

    from app.bot import get_bot

    bot = get_bot()
    if not bot:
        raise HTTPException(503, "Bot is offline")

    telegram_id = 0
    message = ""
    photo_bytes = None
    photo_filename = "dm.jpg"
    ct = (request.headers.get("content-type") or "").lower()
    if "multipart/form-data" in ct:
        form = await request.form()
        try:
            telegram_id = int(str(form.get("telegram_id") or "0"))
        except ValueError:
            telegram_id = 0
        message = str(form.get("message") or "").strip()
        file = form.get("photo") or form.get("file")
        if file is not None and hasattr(file, "read"):
            data = await file.read()
            if data:
                if len(data) > 8 * 1024 * 1024:
                    raise HTTPException(400, "Photo too large (max 8MB)")
                photo_bytes = data
                photo_filename = getattr(file, "filename", None) or "dm.jpg"
    else:
        try:
            body = await request.json()
        except Exception:
            body = {}
        try:
            telegram_id = int((body or {}).get("telegram_id") or 0)
        except (TypeError, ValueError):
            telegram_id = 0
        message = str((body or {}).get("message") or "").strip()

    if not telegram_id:
        raise HTTPException(400, "telegram_id required")
    if not message and not photo_bytes:
        raise HTTPException(400, "message or photo required")
    if not message:
        message = " "

    try:
        if photo_bytes:
            await bot.send_photo(
                chat_id=telegram_id,
                photo=BufferedInputFile(photo_bytes, filename=photo_filename),
                caption=message[:1024],
                parse_mode="HTML",
            )
        else:
            await bot.send_message(
                chat_id=telegram_id,
                text=message,
                parse_mode="HTML",
            )
    except Exception as e:
        raise HTTPException(400, f"Failed to send DM: {type(e).__name__}: {e}")
    await write_audit(
        session,
        action=AuditAction.BROADCAST_SENT,
        actor_admin_id=admin.id,
        target_type="user",
        target_id=str(telegram_id),
        metadata={"kind": "dm", "has_photo": bool(photo_bytes)},
    )
    return {"ok": True, "message": "DM sent", "has_photo": bool(photo_bytes)}


@router.post("/admin/qrs/{qr_id}/notify-user")
async def notify_one_user(
    qr_id: int,
    body: NotifyUserRequest,
    admin: Admin = Depends(require_perm("qr.post")),
    session: AsyncSession = Depends(get_db),
):
    from app.bot import claim_button, get_bot

    qr = await qr_service.get_qr_by_id(session, qr_id)
    if not qr:
        raise HTTPException(404, "QR not found")
    if qr.status in (QRStatus.DISABLED, QRStatus.EXPIRED, QRStatus.CLAIMED):
        raise HTTPException(400, f"QR not available ({qr.status.value})")
    bot = get_bot()
    if not bot:
        raise HTTPException(503, "Bot is offline")
    try:
        if qr.status in (QRStatus.AVAILABLE, QRStatus.RECLAIMED):
            await qr_service.mark_qr_posted(session, qr, admin.id)
        app_label = (qr.app_name or "QR task").strip()
        exp_line = "No expiry"
        if qr.expires_at:
            exp_line = qr.expires_at.strftime("%H:%M:%S UTC")
        reward_str = f"{qr.reward_cents / 100:.2f}"
        msg = (
            f"<b>New task #{qr.id} · {app_label}</b>\n"
            f"Your reward: <b>${reward_str}</b>\n"
            f"Expires: {exp_line}\n"
            f"Claim before opening the payment link."
        )
        await bot.send_message(
            chat_id=body.telegram_id,
            text=msg,
            reply_markup=claim_button(qr.code),
            parse_mode="HTML",
        )
    except Exception as e:
        raise HTTPException(400, f"Failed to notify user: {type(e).__name__}: {e}")
    return {"ok": True, "message": f"QR sent to Telegram ID {body.telegram_id}"}


# ---- Admin role management (OWNER) ----


class AdminCreateRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=64)
    password: str = Field(..., min_length=8)
    role: str = Field(..., pattern="^(SUPER_ADMIN|MANAGER|MODERATOR)$")
    display_name: Optional[str] = None
    telegram_id: Optional[int] = None


class AdminUpdateRequest(BaseModel):
    role: Optional[str] = None
    is_active: Optional[bool] = None
    display_name: Optional[str] = None
    password: Optional[str] = Field(None, min_length=8)
    permissions: Optional[dict] = None


@router.get("/admin/admins")
async def list_admins(
    admin: Admin = Depends(require_perm("admins.manage")),
    session: AsyncSession = Depends(get_db),
):
    rows = (await session.execute(select(Admin).order_by(Admin.id))).scalars().all()
    return {
        "items": [
            {
                "id": a.id,
                "username": a.username,
                "role": a.role.value,
                "display_name": a.display_name,
                "telegram_id": a.telegram_id,
                "is_active": a.is_active,
                "permissions": a.permissions or {},
                "last_login_at": a.last_login_at.isoformat() if a.last_login_at else None,
            }
            for a in rows
        ]
    }


@router.post("/admin/admins")
async def create_admin(
    body: AdminCreateRequest,
    admin: Admin = Depends(require_perm("admins.manage")),
    session: AsyncSession = Depends(get_db),
):
    from app.models import AdminRole

    existing = (
        await session.execute(select(Admin).where(Admin.username == body.username))
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(400, "Username already exists")
    role = AdminRole(body.role)
    new_admin = Admin(
        username=body.username,
        password_hash=hash_password(body.password),
        role=role,
        display_name=body.display_name or body.username,
        telegram_id=body.telegram_id,
        is_active=True,
    )
    session.add(new_admin)
    await session.commit()
    await write_audit(
        session,
        action=AuditAction.ROLE_CHANGED,
        actor_admin_id=admin.id,
        target_type="admin",
        target_id=str(new_admin.id),
        metadata={"role": body.role, "username": body.username},
    )
    return {"ok": True, "id": new_admin.id}


@router.patch("/admin/admins/{admin_id}")
async def update_admin(
    admin_id: int,
    body: AdminUpdateRequest,
    admin: Admin = Depends(require_perm("admins.manage")),
    session: AsyncSession = Depends(get_db),
):
    from app.models import AdminRole

    target = await admin_service.get_admin_by_id(session, admin_id)
    if not target:
        raise HTTPException(404, "Admin not found")
    if target.role == AdminRole.OWNER and target.id != admin.id:
        raise HTTPException(400, "Cannot modify another OWNER")
    if body.role is not None:
        if body.role == "OWNER":
            raise HTTPException(400, "Cannot assign OWNER via API")
        target.role = AdminRole(body.role)
    if body.is_active is not None:
        if target.role == AdminRole.OWNER and not body.is_active:
            raise HTTPException(400, "Cannot deactivate OWNER")
        target.is_active = body.is_active
    if body.display_name is not None:
        target.display_name = body.display_name
    if body.password:
        target.password_hash = hash_password(body.password)
    if body.permissions is not None:
        target.permissions = body.permissions
    session.add(target)
    await session.commit()
    await write_audit(
        session,
        action=AuditAction.ROLE_CHANGED,
        actor_admin_id=admin.id,
        target_type="admin",
        target_id=str(admin_id),
        metadata=body.model_dump(exclude_none=True),
    )
    return {"ok": True}


# ---- Browser notification poll (Chrome push via Notification API) ----


@router.get("/admin/events/poll")
async def poll_events(
    since_claim_id: int = 0,
    since_withdrawal_id: int = 0,
    admin: Admin = Depends(get_current_admin),
    session: AsyncSession = Depends(get_db),
):
    """Lightweight poll for new claims/withdrawals for browser notifications."""
    from app.models import QRClaim, Withdrawal

    new_claims = (
        await session.execute(
            select(QRClaim)
            .options(selectinload(QRClaim.user), selectinload(QRClaim.qr_code))
            .where(QRClaim.id > since_claim_id, QRClaim.status == ClaimStatus.PENDING)
            .order_by(QRClaim.id.asc())
            .limit(20)
        )
    ).scalars().all()
    new_wds = (
        await session.execute(
            select(Withdrawal)
            .options(selectinload(Withdrawal.user))
            .where(
                Withdrawal.id > since_withdrawal_id,
                Withdrawal.status == WithdrawalStatus.PENDING,
            )
            .order_by(Withdrawal.id.asc())
            .limit(20)
        )
    ).scalars().all()
    return {
        "claims": [
            {
                "id": c.id,
                "reward_cents": c.reward_cents,
                "qr_code": c.qr_code.code if c.qr_code else None,
                "username": c.user.username if c.user else None,
                "telegram_id": c.user.telegram_id if c.user else None,
            }
            for c in new_claims
        ],
        "withdrawals": [
            {
                "id": w.id,
                "amount_cents": w.amount_cents,
                "payout_method": getattr(w, "payout_method", None),
                "payout_details": getattr(w, "payout_details", None),
                "username": w.user.username if w.user else None,
                "telegram_id": w.user.telegram_id if w.user else None,
            }
            for w in new_wds
        ],
        "max_claim_id": max([c.id for c in new_claims] + [since_claim_id]),
        "max_withdrawal_id": max([w.id for w in new_wds] + [since_withdrawal_id]),
    }
