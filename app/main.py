import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .db import init_db
from .api import router
from .bot import run_bot

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("qr-claim-bot")

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    logger.info("DATABASE READY")

    task = asyncio.create_task(run_bot())

    try:
        yield
    finally:
        logger.info("Stopping Telegram bot...")
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

app = FastAPI(title="QR Claim Bot")
app.include_router(router)
