"""Security utilities: password hashing, JWT, CSRF, RBAC."""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from jose import JWTError, jwt
import bcrypt

from app.config import get_settings
from app.models import AdminRole

settings = get_settings()

# ---------------------------------------------------------------------------
# Password (bcrypt directly – avoid passlib backend quirks)
# ---------------------------------------------------------------------------

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# JWT
# ---------------------------------------------------------------------------

def create_access_token(
    data: dict[str, Any],
    expires_delta: Optional[timedelta] = None,
) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (
        expires_delta or timedelta(minutes=settings.jwt_expire_minutes)
    )
    to_encode.update({"exp": expire, "iat": datetime.now(timezone.utc)})
    return jwt.encode(to_encode, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> Optional[dict[str, Any]]:
    try:
        payload = jwt.decode(
            token, settings.jwt_secret, algorithms=[settings.jwt_algorithm]
        )
        return payload
    except JWTError:
        return None


# ---------------------------------------------------------------------------
# CSRF
# ---------------------------------------------------------------------------

def generate_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def verify_csrf_token(token: str, expected: str) -> bool:
    if not token or not expected:
        return False
    return secrets.compare_digest(token, expected)


# ---------------------------------------------------------------------------
# Secure QR code generation
# ---------------------------------------------------------------------------

def generate_qr_code() -> str:
    """Cryptographically secure unpredictable QR code."""
    raw = secrets.token_hex(6).upper()  # 12 hex chars
    return f"QR-{raw}"


def generate_reference(prefix: str = "TX") -> str:
    """Unique transaction / approval reference."""
    return f"{prefix}-{secrets.token_hex(8).upper()}"


# ---------------------------------------------------------------------------
# RBAC Permission Matrix
# ---------------------------------------------------------------------------

# Permission keys
PERMISSIONS = {
    # Dashboard
    "dashboard.view": {AdminRole.OWNER, AdminRole.SUPER_ADMIN, AdminRole.MANAGER, AdminRole.MODERATOR},
    # Users
    "users.view": {AdminRole.OWNER, AdminRole.SUPER_ADMIN, AdminRole.MANAGER, AdminRole.MODERATOR},
    "users.edit": {AdminRole.OWNER, AdminRole.SUPER_ADMIN},
    "users.disable": {AdminRole.OWNER, AdminRole.SUPER_ADMIN},
    "users.adjust_balance": {AdminRole.OWNER, AdminRole.SUPER_ADMIN},
    # QR
    "qr.view": {AdminRole.OWNER, AdminRole.SUPER_ADMIN, AdminRole.MANAGER, AdminRole.MODERATOR},
    "qr.create": {AdminRole.OWNER, AdminRole.SUPER_ADMIN, AdminRole.MANAGER},
    "qr.post": {AdminRole.OWNER, AdminRole.SUPER_ADMIN, AdminRole.MANAGER},
    "qr.reclaim": {AdminRole.OWNER, AdminRole.SUPER_ADMIN},  # Manager needs explicit flag
    "qr.disable": {AdminRole.OWNER, AdminRole.SUPER_ADMIN, AdminRole.MANAGER},
    # Claims
    "claims.view": {AdminRole.OWNER, AdminRole.SUPER_ADMIN, AdminRole.MANAGER, AdminRole.MODERATOR},
    "claims.approve": {AdminRole.OWNER, AdminRole.SUPER_ADMIN, AdminRole.MANAGER},
    "claims.reject": {AdminRole.OWNER, AdminRole.SUPER_ADMIN, AdminRole.MANAGER},
    "claims.hold": {AdminRole.OWNER, AdminRole.SUPER_ADMIN, AdminRole.MANAGER},
    # Withdrawals
    "withdrawals.view": {AdminRole.OWNER, AdminRole.SUPER_ADMIN, AdminRole.MANAGER},
    "withdrawals.pay": {AdminRole.OWNER, AdminRole.SUPER_ADMIN, AdminRole.MANAGER},
    "withdrawals.reject": {AdminRole.OWNER, AdminRole.SUPER_ADMIN, AdminRole.MANAGER},
    "withdrawals.hold": {AdminRole.OWNER, AdminRole.SUPER_ADMIN, AdminRole.MANAGER},
    # Channels
    "channels.view": {AdminRole.OWNER, AdminRole.SUPER_ADMIN, AdminRole.MANAGER},
    "channels.manage": {AdminRole.OWNER, AdminRole.SUPER_ADMIN},
    # Settings
    "settings.view": {AdminRole.OWNER, AdminRole.SUPER_ADMIN},
    "settings.edit": {AdminRole.OWNER, AdminRole.SUPER_ADMIN},
    # Audit
    "audit.view": {AdminRole.OWNER, AdminRole.SUPER_ADMIN},
    # Analytics
    "analytics.view": {AdminRole.OWNER, AdminRole.SUPER_ADMIN, AdminRole.MANAGER},
    # Roles
    "roles.manage": {AdminRole.OWNER},
    # Reset
    "reset.activity": {AdminRole.OWNER},
    "reset.qr_data": {AdminRole.OWNER},
    # Admins
    "admins.manage": {AdminRole.OWNER},
}


def has_permission(
    role: AdminRole,
    permission: str,
    extra_permissions: Optional[dict] = None,
) -> bool:
    """Check if role has the given permission.

    Managers can be granted extra flags via the permissions JSON column.
    """
    allowed = PERMISSIONS.get(permission)
    if not allowed:
        return False
    if role in allowed:
        return True
    # Manager special: check extra permissions dict
    if role == AdminRole.MANAGER and extra_permissions:
        return bool(extra_permissions.get(permission, False))
    return False


def require_roles(*roles: AdminRole):
    """Decorator helper – used in API layer."""
    return set(roles)
