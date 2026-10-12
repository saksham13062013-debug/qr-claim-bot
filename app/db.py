"""Database engine, session and initialization."""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.models import Base

logger = logging.getLogger(__name__)

settings = get_settings()

engine = create_async_engine(
    settings.database_url,
    echo=settings.debug and not settings.is_production,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
    pool_recycle=300,
)

# For tests we may override with NullPool
async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that yields a database session."""
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


@asynccontextmanager
async def get_db_context() -> AsyncGenerator[AsyncSession, None]:
    """Context manager for database sessions outside of FastAPI."""
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def init_db() -> None:
    """Create all tables if they do not exist. Safe for production startup."""
    from sqlalchemy import text

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Safe additive migrations for existing deployments
        await conn.execute(text(
            "ALTER TABLE qr_codes ADD COLUMN IF NOT EXISTS image_png BYTEA"
        ))
        await conn.execute(text(
            "ALTER TABLE qr_codes ADD COLUMN IF NOT EXISTS scheduled_post_at TIMESTAMPTZ"
        ))
        await conn.execute(text(
            "ALTER TABLE qr_codes ADD COLUMN IF NOT EXISTS scheduled_chat_id VARCHAR(64)"
        ))
        await conn.execute(text(
            "ALTER TABLE withdrawals ADD COLUMN IF NOT EXISTS payout_method VARCHAR(32)"
        ))
        await conn.execute(text(
            "ALTER TABLE withdrawals ADD COLUMN IF NOT EXISTS payout_details VARCHAR(255)"
        ))
    logger.info("Database tables initialized")


async def close_db() -> None:
    """Dispose of the engine."""
    await engine.dispose()
    logger.info("Database engine disposed")
