"""Web QR scanner IDs and success-rate tracking."""

from __future__ import annotations

import secrets
import string
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import QRCode, QRStatus, ScanEvent, User


def _gen_scanner_id() -> str:
    alphabet = string.ascii_uppercase + string.digits
    body = "".join(secrets.choice(alphabet) for _ in range(8))
    return f"SCR-{body}"


async def ensure_scanner_id(session: AsyncSession, user: User) -> str:
    if user.scanner_id:
        return user.scanner_id
    for _ in range(8):
        sid = _gen_scanner_id()
        exists = (
            await session.execute(select(User.id).where(User.scanner_id == sid))
        ).scalar_one_or_none()
        if exists is None:
            user.scanner_id = sid
            await session.flush()
            return sid
    raise RuntimeError("Could not allocate scanner id")


async def get_user_by_scanner_id(
    session: AsyncSession, scanner_id: str
) -> Optional[User]:
    sid = (scanner_id or "").strip().upper()
    if not sid:
        return None
    result = await session.execute(select(User).where(User.scanner_id == sid))
    return result.scalar_one_or_none()


def success_rate(attempts: int, successes: int) -> float:
    if attempts <= 0:
        return 0.0
    return round(100.0 * successes / attempts, 1)


def extract_code_from_payload(raw: str) -> str:
    """Accept plain code or URL containing the code."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    # If payload is our internal code (no spaces, short)
    if "://" not in raw and " " not in raw and len(raw) <= 64:
        return raw.upper()
    # Try last path segment
    for sep in ("/", "=", "code=", "qr="):
        if sep in raw:
            part = raw.rstrip("/").split(sep)[-1]
            part = part.split("&")[0].split("?")[0].strip()
            if part and len(part) <= 64:
                return part.upper()
    return raw[:64].upper()


async def record_scan(
    session: AsyncSession,
    *,
    scanner_id: Optional[str],
    raw_payload: str,
) -> dict[str, Any]:
    """
    Record a scan attempt. Success = QR exists and is claimable (AVAILABLE/POSTED/RECLAIMED).
    """
    user = None
    if scanner_id:
        user = await get_user_by_scanner_id(session, scanner_id)

    code = extract_code_from_payload(raw_payload)
    qr = None
    if code:
        qr = (
            await session.execute(select(QRCode).where(QRCode.code == code))
        ).scalar_one_or_none()

    success = False
    result_status = "not_found"
    if qr:
        st = qr.status.value if hasattr(qr.status, "value") else str(qr.status)
        result_status = st
        if qr.status in (
            QRStatus.AVAILABLE,
            QRStatus.POSTED,
            QRStatus.RECLAIMED,
        ):
            success = True
            result_status = "claimable"
        elif qr.status == QRStatus.CLAIMED:
            result_status = "already_claimed"
        elif qr.status == QRStatus.PENDING:
            result_status = "pending"
        elif qr.status == QRStatus.EXPIRED:
            result_status = "expired"
        elif qr.status == QRStatus.DISABLED:
            result_status = "disabled"

    event = ScanEvent(
        user_id=user.id if user else None,
        scanner_id=(user.scanner_id if user else (scanner_id or None)),
        raw_payload=(raw_payload or "")[:512],
        qr_code_id=qr.id if qr else None,
        success=success,
        result_status=result_status[:64],
    )
    session.add(event)

    if user:
        user.scan_attempts = int(user.scan_attempts or 0) + 1
        if success:
            user.scan_successes = int(user.scan_successes or 0) + 1

    await session.flush()

    rate = success_rate(
        int(user.scan_attempts or 0) if user else 1,
        int(user.scan_successes or 0) if user else (1 if success else 0),
    ) if user else (100.0 if success else 0.0)

    return {
        "ok": True,
        "success": success,
        "result_status": result_status,
        "code": code or None,
        "qr_id": qr.id if qr else None,
        "reward_usd": (qr.reward_cents / 100.0) if qr else None,
        "app_name": getattr(qr, "app_name", None) if qr else None,
        "payment_link": getattr(qr, "payment_link", None) if qr else None,
        "scanner_id": user.scanner_id if user else scanner_id,
        "scan_attempts": int(user.scan_attempts or 0) if user else None,
        "scan_successes": int(user.scan_successes or 0) if user else None,
        "success_rate": rate if user else None,
        "message": _message_for(result_status, success),
    }


def _message_for(status: str, success: bool) -> str:
    if success:
        return "QR is valid and claimable. Open the bot to claim."
    mapping = {
        "not_found": "QR not recognized in this system.",
        "already_claimed": "This QR was already claimed.",
        "pending": "This QR has a claim pending review.",
        "expired": "This QR has expired.",
        "disabled": "This QR is disabled.",
        "APPROVED": "This QR is already approved / closed.",
        "REJECTED": "This QR was rejected.",
    }
    return mapping.get(status, f"Status: {status}")
