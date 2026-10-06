import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .db import init_db
from .api import router

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("qr-claim-bot")

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    logger.info("DATABASE READY")

    yield

app = FastAPI(title="QR Claim Bot")
app.include_router(router)
