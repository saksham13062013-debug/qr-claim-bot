"""
Central configuration loaded from environment variables (.env locally, Railway Variables in prod).
"""
import os
from dotenv import load_dotenv

load_dotenv()


def _split_csv(value: str):
    return [v.strip() for v in value.split(",") if v.strip()]


def _split_csv_int(value: str):
    return [int(v.strip()) for v in value.split(",") if v.strip()]


# --- Core ---
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
PORT = int(os.getenv("PORT", "8000"))
WEBAPP_URL = os.getenv("WEBAPP_URL", "")
DB_PATH = os.getenv("DB_PATH", "qr_reward_bot.db")

# --- Admins ---
ADMIN_IDS = set(_split_csv_int(os.getenv("ADMIN_IDS", "")))

# --- Groups where QR/task posts are broadcast ---
GROUP_IDS = _split_csv_int(os.getenv("GROUP_IDS", "")) if os.getenv("GROUP_IDS") else []

# --- Currency ---
USD_TO_INR = float(os.getenv("USD_TO_INR", "85"))
MIN_WITHDRAWAL_USD = float(os.getenv("MIN_WITHDRAWAL_USD", "1"))

# --- Force join ---
FORCE_JOIN_CHANNELS = _split_csv(os.getenv("FORCE_JOIN_CHANNEL", ""))
FORCE_JOIN_URLS = _split_csv(os.getenv("FORCE_JOIN_URL", ""))
FORCE_JOIN_STRICT = int(os.getenv("FORCE_JOIN_STRICT", "0"))  # min number of channels required to join
FORCE_JOIN_ENABLED = len(FORCE_JOIN_CHANNELS) > 0

# --- Support ---
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is required")
if not ADMIN_IDS:
    raise RuntimeError("ADMIN_IDS environment variable is required (comma-separated Telegram IDs)")
