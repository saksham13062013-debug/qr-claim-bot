"""Unit tests for security helpers."""

from app.security import (
    generate_qr_code,
    generate_reference,
    hash_password,
    verify_password,
    create_access_token,
    decode_access_token,
    has_permission,
)
from app.models import AdminRole


def test_password_hash_and_verify():
    h = hash_password("securepassword123")
    assert h != "securepassword123"
    assert verify_password("securepassword123", h)
    assert not verify_password("wrong", h)


def test_jwt_roundtrip():
    token = create_access_token({"sub": "1", "role": "OWNER"})
    payload = decode_access_token(token)
    assert payload is not None
    assert payload["sub"] == "1"
    assert payload["role"] == "OWNER"


def test_jwt_invalid():
    assert decode_access_token("garbage.token.here") is None


def test_qr_code_format():
    code = generate_qr_code()
    assert code.startswith("QR-")
    assert len(code) == 15  # QR- + 12 hex
    # Uniqueness over many generations
    codes = {generate_qr_code() for _ in range(100)}
    assert len(codes) == 100


def test_reference_unique():
    refs = {generate_reference() for _ in range(50)}
    assert len(refs) == 50


def test_rbac_owner():
    assert has_permission(AdminRole.OWNER, "reset.activity")
    assert has_permission(AdminRole.OWNER, "qr.create")
    assert has_permission(AdminRole.OWNER, "claims.approve")


def test_rbac_moderator():
    assert has_permission(AdminRole.MODERATOR, "dashboard.view")
    assert has_permission(AdminRole.MODERATOR, "users.view")
    assert not has_permission(AdminRole.MODERATOR, "qr.create")
    assert not has_permission(AdminRole.MODERATOR, "reset.activity")
    assert not has_permission(AdminRole.MODERATOR, "claims.approve")


def test_rbac_manager_extra():
    assert has_permission(AdminRole.MANAGER, "qr.create")
    assert not has_permission(AdminRole.MANAGER, "qr.reclaim")
    # With extra permission
    assert has_permission(
        AdminRole.MANAGER, "qr.reclaim", extra_permissions={"qr.reclaim": True}
    )
