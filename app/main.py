"""FastAPI application entrypoint – serves Admin Panel + API + starts bot."""

from __future__ import annotations

import logging
import os
import sys
from contextlib import asynccontextmanager
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
from app.db import close_db, init_db
from app.services.admin_service import bootstrap_owner
from app.services.settings_service import seed_default_settings
from app.db import get_db_context

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)
settings = get_settings()

# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------

limiter = Limiter(key_func=get_remote_address)

# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

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
        # Continue serving admin panel even if bot fails
    yield
    await stop_bot()
    await close_db()
    logger.info("Application shut down")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title=settings.app_name,
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/api/docs" if not settings.is_production else None,
    redoc_url=None,
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# Static & templates
BASE_DIR = Path(__file__).resolve().parent
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# API
app.include_router(api_router, prefix="/api")


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok", "version": "1.0.0"}


# ---------------------------------------------------------------------------
# Admin Panel pages (server-rendered shell + SPA-like JS)
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Global error handler – never expose stack traces to clients
# ---------------------------------------------------------------------------

@app.exception_handler(Exception)
async def unhandled_exception(request: Request, exc: Exception):
    logger.exception("Unhandled error on %s: %s", request.url.path, exc)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"},
    )


# ---------------------------------------------------------------------------
# Entrypoint for uvicorn
# ---------------------------------------------------------------------------

def run():
    import uvicorn

    port = int(os.environ.get("PORT", settings.port))
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=port,
        proxy_headers=True,
        forwarded_allow_ips="*",
        log_level="info",
    )


if __name__ == "__main__":
    run()
