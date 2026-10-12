"""FastAPI application entrypoint – serves Admin Panel + API + starts bot."""

from __future__ import annotations

import asyncio
import logging
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from app.api import router as api_router
from app.bot import start_bot, stop_bot
from app.config import get_settings
from app.db import close_db, get_db_context, init_db
from app.services.admin_service import bootstrap_owner
from app.services.settings_service import seed_default_settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)
settings = get_settings()
limiter = Limiter(key_func=get_remote_address)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Application starting (env=%s)", settings.app_env)
    await init_db()
    async with get_db_context() as session:
        await bootstrap_owner(session)
        await seed_default_settings(session)
    try:
        await start_bot()
    except Exception as exc:
        logger.exception("Failed to start Telegram bot: %s", exc)

    async def process_scheduled_posts():
        while True:
            try:
                from sqlalchemy import select

                from app.bot import claim_button, get_bot
                from app.models import QRCode, QRStatus
                from app.services import qr_service

                bot = get_bot()
                if bot:
                    async with get_db_context() as session:
                        now = datetime.now(timezone.utc)
                        rows = (
                            await session.execute(
                                select(QRCode)
                                .where(
                                    QRCode.scheduled_post_at.is_not(None),
                                    QRCode.scheduled_post_at <= now,
                                    QRCode.status.in_(
                                        [QRStatus.AVAILABLE, QRStatus.RECLAIMED]
                                    ),
                                )
                                .limit(20)
                            )
                        ).scalars().all()
                        from app.models import User, UserStatus
                        for qr in rows:
                            qr.scheduled_post_at = None
                            qr.scheduled_chat_id = None
                            try:
                                await qr_service.mark_qr_posted(
                                    session, qr, qr.created_by_admin_id or 0
                                )
                            except Exception:
                                pass
                            msg = (
                                f"<b>NEW QR REWARD</b>\n\n"
                                f"Reward: ${qr.reward_cents / 100:.2f}\n"
                                f"One user only!\n\n"
                                f"Tap <b>CLAIM REWARD</b> below."
                            )
                            users = (
                                await session.execute(
                                    select(User).where(User.status == UserStatus.ACTIVE)
                                )
                            ).scalars().all()
                            for u in users:
                                try:
                                    await bot.send_message(
                                        chat_id=u.telegram_id,
                                        text=msg,
                                        reply_markup=claim_button(qr.code),
                                        parse_mode="HTML",
                                    )
                                except Exception as e:
                                    logger.warning("Scheduled DM failed: %s", e)
                            session.add(qr)
                        await session.commit()
                # Auto-expire QR codes past expires_at
                async with get_db_context() as session:
                    now = datetime.now(timezone.utc)
                    from sqlalchemy import update
                    from app.models import QRCode, QRStatus

                    await session.execute(
                        update(QRCode)
                        .where(
                            QRCode.expires_at.is_not(None),
                            QRCode.expires_at <= now,
                            QRCode.status.in_(
                                [
                                    QRStatus.AVAILABLE,
                                    QRStatus.POSTED,
                                    QRStatus.RECLAIMED,
                                ]
                            ),
                        )
                        .values(status=QRStatus.EXPIRED)
                    )
                    await session.commit()
            except Exception as e:
                logger.warning("schedule loop: %s", e)
            await asyncio.sleep(30)

    sched_task = asyncio.create_task(process_scheduled_posts())
    yield
    sched_task.cancel()
    await stop_bot()
    await close_db()
    logger.info("Application shut down")


app = FastAPI(
    title=settings.app_name,
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/api/docs" if not settings.is_production else None,
    redoc_url=None,
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

BASE_DIR = Path(__file__).resolve().parent
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
app.include_router(api_router, prefix="/api")


@app.get("/health")
async def health():
    return {"status": "ok", "version": "1.0.0"}


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})


@app.get("/admin", response_class=HTMLResponse)
@app.get("/admin/{path:path}", response_class=HTMLResponse)
async def admin_panel(request: Request, path: str = ""):
    return templates.TemplateResponse(
        "admin.html",
        {"request": request, "app_name": settings.app_name},
    )


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled error on %s", request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})
