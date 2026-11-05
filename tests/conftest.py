"""Pytest configuration – set required env vars before app imports."""

import os

os.environ.setdefault("SECRET_KEY", "test-secret-key-at-least-32-characters-long")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-at-least-32-characters-long")
os.environ.setdefault("CSRF_SECRET", "test-csrf-secret-at-least-32-characters-long")
os.environ.setdefault("BOT_TOKEN", "123456:TESTTOKEN")
os.environ.setdefault("OWNER_TELEGRAM_ID", "999999")
os.environ.setdefault("ADMIN_USERNAME", "testowner")
os.environ.setdefault("ADMIN_PASSWORD", "testpassword123")
os.environ.setdefault(
    "DATABASE_URL",
    os.environ.get(
        "TEST_DATABASE_URL",
        "postgresql+asyncpg://postgres:postgres@localhost:5432/qrclaim_test",
    ),
)
