import os
import sqlite3
import logging
import json
import hmac
import hashlib
import threading
import shutil
from urllib.parse import parse_qsl
from contextlib import closing
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardRemove, WebAppInfo
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters
)
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
import uvicorn

# =========================
# ENVIRONMENT
# =========================
TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = {
    int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}
GROUP_ID = os.getenv("GROUP_ID", "").strip()
# Persistent database path. On Railway, set DB_PATH=/data/qr_claim_bot.db
# and attach a Railway Volume mounted at /data. If an older local DB exists and
# the persistent DB does not exist yet, copy it once so existing data is preserved.
_requested_db = os.getenv("DB_PATH", "").strip()
if _requested_db:
    DB_PATH = _requested_db
elif os.path.isdir("/data"):
    DB_PATH = "/data/qr_claim_bot.db"
else:
    DB_PATH = "qr_claim_bot.db"

_db_dir = os.path.dirname(os.path.abspath(DB_PATH))
try:
    os.makedirs(_db_dir, exist_ok=True)
except Exception:
    pass

# One-time migration from the old working-directory database to /data.
if DB_PATH != os.path.abspath("qr_claim_bot.db") and not os.path.exists(DB_PATH) and os.path.exists("qr_claim_bot.db"):
    try:
        shutil.copy2("qr_claim_bot.db", DB_PATH)
        log_msg = f"Migrated existing database to {DB_PATH}"
        print(log_msg)
    except Exception as exc:
        print(f"Database migration skipped: {exc}")
WEBAPP_URL = os.getenv("WEBAPP_URL", "").strip().rstrip("/")

SUPPORT_ID = os.getenv("SUPPORT_ID", "@itsvanshu").strip()
FORCE_JOIN_CHANNEL = os.getenv("FORCE_JOIN_CHANNEL", "").strip()
FORCE_JOIN_URL = os.getenv("FORCE_JOIN_URL", "").strip()

ENABLE_UPI = os.getenv("ENABLE_UPI", "1").lower() not in ("0", "false", "no")
ENABLE_USDT = os.getenv("ENABLE_USDT", "1").lower() not in ("0", "false", "no")
MIN_WITHDRAWAL = float(os.getenv("MIN_WITHDRAWAL", "50"))

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO
)
log = logging.getLogger("qr-claim-bot")


# =========================
# MINI APP / DASHBOARD SERVER
# =========================
dashboard_app = FastAPI(title="QR Claim Bot Admin")
telegram_bot = None


def verify_webapp(init_data: str | None):
    if not init_data:
        raise HTTPException(401, "Open this dashboard from Telegram.")
    data = dict(parse_qsl(init_data, keep_blank_values=True))
    received = data.pop("hash", None)
    if not received:
        raise HTTPException(401, "Invalid Telegram initData.")
    check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, received):
        raise HTTPException(401, "Invalid Telegram signature.")
    try:
        user = json.loads(data.get("user", "{}"))
        uid = int(user["id"])
    except Exception:
        raise HTTPException(401, "Telegram user missing.")
    return uid


def require_web_admin(init_data: str | None):
    uid = verify_webapp(init_data)
    if not is_admin(uid):
        raise HTTPException(403, "Admin access required.")
    return uid


@dashboard_app.get("/")
async def dashboard_home():
    return FileResponse("dashboard/index.html")


@dashboard_app.get("/admin")
async def dashboard_admin():
    return FileResponse("dashboard/index.html")


@dashboard_app.get("/api/admin/overview")
async def dashboard_overview(x_telegram_init_data: str | None = Header(default=None)):
    require_web_admin(x_telegram_init_data)
    with closing(db()) as con:
        users = con.execute("SELECT COUNT(*) x FROM users").fetchone()["x"]
        qrs = con.execute("SELECT COUNT(*) x FROM qrs").fetchone()["x"]
        available = con.execute("SELECT COUNT(*) x FROM qrs WHERE status='available'").fetchone()["x"]
        claims = con.execute("SELECT COUNT(*) x FROM qrs WHERE claimed_by IS NOT NULL").fetchone()["x"]
        proofs = con.execute("SELECT COUNT(*) x FROM proofs WHERE status='pending'").fetchone()["x"]
        wd = con.execute("SELECT COUNT(*) x FROM withdrawals").fetchone()["x"]
        pending_wd = con.execute("SELECT COUNT(*) x FROM withdrawals WHERE status='pending'").fetchone()["x"]
        processing_wd = con.execute("SELECT COUNT(*) x FROM withdrawals WHERE status='processing'").fetchone()["x"]
        paid = con.execute("SELECT COALESCE(SUM(amount),0) x FROM withdrawals WHERE status='paid'").fetchone()["x"]
    return {"users":users,"qrs":qrs,"available":available,"claims":claims,"proofs":proofs,"withdrawals":wd,"pending_withdrawals":pending_wd,"processing":processing_wd,"paid":paid}


@dashboard_app.get("/api/admin/proofs")
async def dashboard_proofs(x_telegram_init_data: str | None = Header(default=None)):
    require_web_admin(x_telegram_init_data)
    with closing(db()) as con:
        rows = con.execute("""
            SELECT p.id,p.qr_id,p.user_id,p.file_id,p.status,q.app_name,q.reward,
                   COALESCE(NULLIF(u.username,''),NULLIF(u.name,''),CAST(p.user_id AS TEXT)) AS username
            FROM proofs p JOIN qrs q ON q.id=p.qr_id
            LEFT JOIN users u ON u.id=p.user_id
            WHERE p.status='pending' ORDER BY p.id DESC LIMIT 50
        """).fetchall()
    return {"proofs":[dict(r) for r in rows]}


@dashboard_app.post("/api/admin/proof")
async def dashboard_proof_action(payload: dict, x_telegram_init_data: str | None = Header(default=None)):
    admin_id = require_web_admin(x_telegram_init_data)
    try:
        proof_id = int(payload.get("id", 0))
        action = str(payload.get("action", "")).lower()
    except Exception:
        raise HTTPException(400, "Invalid proof details.")
    if action not in ("approve", "reject"):
        raise HTTPException(400, "Action must be approve or reject.")
    with closing(db()) as con:
        proof = con.execute("""
            SELECT p.*, q.reward, q.app_name, q.claimed_by
            FROM proofs p JOIN qrs q ON q.id=p.qr_id
            WHERE p.id=? AND p.status='pending'
        """, (proof_id,)).fetchone()
        if not proof:
            raise HTTPException(404, "Pending proof not found or already reviewed.")
        if action == "approve":
            con.execute("UPDATE proofs SET status='approved', reviewed_by=?, reviewed_at=CURRENT_TIMESTAMP WHERE id=?", (admin_id, proof_id))
            con.execute("UPDATE qrs SET status='completed' WHERE id=?", (proof["qr_id"],))
            con.execute("UPDATE users SET balance=balance+?, total_earned=total_earned+? WHERE id=?", (proof["reward"], proof["reward"], proof["user_id"]))
            con.execute("INSERT INTO transactions(user_id,amount,kind) VALUES(?,?,?)", (proof["user_id"], proof["reward"], "QR Reward"))
            user_text = f"🎉 <b>REWARD APPROVED</b>\n\n📱 App: {proof['app_name']}\n💰 Reward added: ₹{proof['reward']:.2f}\n\nYour reward has been added to your wallet."
        else:
            con.execute("UPDATE proofs SET status='rejected', reviewed_by=?, reviewed_at=CURRENT_TIMESTAMP WHERE id=?", (admin_id, proof_id))
            con.execute("UPDATE qrs SET status='rejected' WHERE id=?", (proof["qr_id"],))
            user_text = f"❌ <b>PROOF REJECTED</b>\n\n📱 App: {proof['app_name']}\nYour submitted proof was rejected by an admin."
        con.commit()
    if telegram_bot is not None:
        try:
            await telegram_bot.send_message(proof["user_id"], user_text, parse_mode="HTML", reply_markup=user_menu())
        except Exception:
            pass
    return {"ok":True,"status":action,"proof_id":proof_id}


@dashboard_app.post("/api/admin/add-balance")
async def dashboard_add_balance(payload: dict, x_telegram_init_data: str | None = Header(default=None)):
    admin_id = require_web_admin(x_telegram_init_data)
    try:
        user_id = int(payload.get("user_id", 0))
        amount = float(payload.get("amount", 0))
    except (TypeError, ValueError):
        raise HTTPException(400, "Invalid user ID or amount.")
    reason = str(payload.get("reason", "Admin balance credit")).strip() or "Admin balance credit"
    if user_id <= 0:
        raise HTTPException(400, "Enter a valid Telegram user ID.")
    if amount <= 0 or amount > 1000000:
        raise HTTPException(400, "Amount must be greater than ₹0 and at most ₹10,00,000.")

    with closing(db()) as con:
        con.execute("BEGIN IMMEDIATE")
        u = con.execute("SELECT id,balance FROM users WHERE id=?", (user_id,)).fetchone()
        if not u:
            con.rollback()
            raise HTTPException(404, "User not found. Ask the user to press /start first.")
        con.execute("UPDATE users SET balance=balance+? WHERE id=?", (amount, user_id))
        con.execute("INSERT INTO transactions(user_id,amount,kind) VALUES(?,?,?)", (user_id, amount, f"Admin Credit: {reason[:100]}"))
        new_balance = con.execute("SELECT balance FROM users WHERE id=?", (user_id,)).fetchone()["balance"]
        con.commit()

    if telegram_bot is not None:
        try:
            await telegram_bot.send_message(
                user_id,
                f"💰 <b>BALANCE ADDED</b>\n\n"
                f"➕ Added: ₹{amount:.2f}\n"
                f"📝 Reason: {reason}\n"
                f"💵 New balance: ₹{new_balance:.2f}",
                parse_mode="HTML", reply_markup=user_menu()
            )
        except Exception:
            pass

    return {"ok": True, "user_id": user_id, "added": amount, "balance": new_balance, "admin_id": admin_id}


@dashboard_app.get("/api/admin/withdrawals")
async def dashboard_withdrawals(x_telegram_init_data: str | None = Header(default=None)):
    require_web_admin(x_telegram_init_data)
    with closing(db()) as con:
        rows = con.execute("""
            SELECT w.id,w.user_id,w.amount,w.method,w.account,w.status,w.created_at,
                   COALESCE(NULLIF(u.username,''),NULLIF(u.name,''),CAST(w.user_id AS TEXT)) AS username
            FROM withdrawals w LEFT JOIN users u ON u.id=w.user_id
            ORDER BY w.id DESC LIMIT 100
        """).fetchall()
    return {"withdrawals":[dict(r) for r in rows]}


@dashboard_app.post("/api/admin/withdrawal")
async def dashboard_withdrawal_action(payload: dict, x_telegram_init_data: str | None = Header(default=None)):
    require_web_admin(x_telegram_init_data)
    wid = int(payload.get("id", 0)); action = str(payload.get("action", "")).lower()
    if action not in ("processing", "paid", "reject"):
        raise HTTPException(400, "Invalid withdrawal action.")
    new_status = {"processing":"processing","paid":"paid","reject":"rejected"}[action]
    with closing(db()) as con:
        w = con.execute("SELECT * FROM withdrawals WHERE id=?", (wid,)).fetchone()
        if not w: raise HTTPException(404, "Withdrawal not found.")
        if w["status"] in ("paid","rejected"): raise HTTPException(400, "Withdrawal already finalized.")
        if new_status == "rejected" and w["status"] != "rejected":
            con.execute("UPDATE users SET balance=balance+? WHERE id=?", (w["amount"], w["user_id"]))
            con.execute("INSERT INTO transactions(user_id,amount,kind) VALUES(?,?,?)", (w["user_id"], w["amount"], "Withdrawal Refund"))
        if new_status == "paid":
            con.execute("UPDATE users SET total_withdrawn=total_withdrawn+? WHERE id=?", (w["amount"], w["user_id"]))
        con.execute("UPDATE withdrawals SET status=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (new_status, wid))
        con.commit()
    return {"ok":True,"status":new_status}


def start_dashboard_server():
    try:
        uvicorn.run(dashboard_app, host="0.0.0.0", port=int(os.getenv("PORT", "8080")), log_level="info")
    except Exception:
        log.exception("Dashboard server stopped")


# =========================
# DATABASE
# =========================
def db():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with closing(db()) as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY,
            username TEXT,
            name TEXT,
            balance REAL DEFAULT 0,
            total_earned REAL DEFAULT 0,
            total_withdrawn REAL DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS admins(
            user_id INTEGER PRIMARY KEY,
            added_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS qrs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_id TEXT NOT NULL,
            app_name TEXT NOT NULL,
            reward REAL NOT NULL,
            claimed_by INTEGER,
            claimed_at TEXT,
            status TEXT DEFAULT 'available',
            posted_message_id INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS proofs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            qr_id INTEGER,
            user_id INTEGER,
            file_id TEXT,
            status TEXT DEFAULT 'pending',
            reviewed_by INTEGER,
            reviewed_at TEXT
        );

        CREATE TABLE IF NOT EXISTS transactions(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            amount REAL,
            kind TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS withdrawals(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            amount REAL,
            method TEXT,
            account TEXT,
            status TEXT DEFAULT 'pending',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS settings(
            key TEXT PRIMARY KEY,
            value TEXT
        );
        """)
        for aid in ADMIN_IDS:
            con.execute(
                "INSERT OR IGNORE INTO admins(user_id) VALUES(?)", (aid,)
            )
        con.commit()


def is_admin(user_id: int) -> bool:
    if user_id in ADMIN_IDS:
        return True
    with closing(db()) as con:
        return con.execute(
            "SELECT 1 FROM admins WHERE user_id=?", (user_id,)
        ).fetchone() is not None


def ensure_user(u):
    with closing(db()) as con:
        con.execute("""
            INSERT INTO users(id, username, name)
            VALUES(?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                username=excluded.username,
                name=excluded.name
        """, (u.id, u.username or "", u.full_name))
        con.commit()


# =========================
# KEYBOARDS
# =========================
def user_menu():
    # Telegram Bot API does not allow arbitrary button background colors.
    # These labels are designed for Telegram's standard blue inline-button UI.
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("💰 WALLET", callback_data="wallet"),
            InlineKeyboardButton("🎁 MY REWARDS", callback_data="rewards")
        ],
        [
            InlineKeyboardButton("💸 WITHDRAW", callback_data="withdraw"),
            InlineKeyboardButton("📜 TRANSACTIONS", callback_data="transactions")
        ],
        [
            InlineKeyboardButton("👤 PROFILE", callback_data="profile"),
            InlineKeyboardButton("ℹ️ HELP", callback_data="help")
        ]
    ])


def back_button():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 BACK TO MENU", callback_data="home")]
    ])


def admin_menu():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📊 DASHBOARD", callback_data="admin_dashboard"),
            InlineKeyboardButton("💸 WITHDRAWALS", callback_data="admin_withdrawals")
        ],
        [
            InlineKeyboardButton("📱 QR MANAGEMENT", callback_data="admin_qrs"),
            InlineKeyboardButton("📋 CLAIMS", callback_data="admin_claims")
        ],
        [
            InlineKeyboardButton("🧾 PROOF REVIEW", callback_data="admin_proofs"),
            InlineKeyboardButton("👥 USERS", callback_data="admin_users")
        ],
        [
            InlineKeyboardButton("➕ ADD QR", callback_data="admin_addqr"),
            InlineKeyboardButton("📢 POST QR", callback_data="admin_postqr")
        ],
        [
            InlineKeyboardButton("👮 ADMINS", callback_data="admin_admins"),
            InlineKeyboardButton("⚙️ SETTINGS", callback_data="admin_settings")
        ],
        [
            InlineKeyboardButton("🏠 USER MENU", callback_data="home")
        ]
    ])


# =========================
# FORCE JOIN
# =========================
async def force_join_ok(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not FORCE_JOIN_CHANNEL:
        return True

    user = update.effective_user
    chat_id = update.effective_chat.id

    try:
        member = await context.bot.get_chat_member(FORCE_JOIN_CHANNEL, user.id)
        if member.status in ("member", "administrator", "creator"):
            return True
    except Exception:
        # If force-join is misconfigured or the bot cannot inspect the channel,
        # do not block users silently.
        log.warning("Force join check failed for %s", FORCE_JOIN_CHANNEL)

    url = FORCE_JOIN_URL or f"https://t.me/{FORCE_JOIN_CHANNEL.lstrip('@')}"
    if update.message:
        await update.message.reply_text(
            "🔒 <b>Channel Join Required</b>\n\n"
            "Please join our required channel first, then press "
            "<b>✅ CHECK JOIN</b>.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📢 JOIN CHANNEL", url=url)],
                [InlineKeyboardButton("✅ CHECK JOIN", callback_data="check_join")]
            ])
        )
    return False


# =========================
# USER PAGES
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return

    ensure_user(update.effective_user)

    if not await force_join_ok(update, context):
        return

    with closing(db()) as con:
        row = con.execute(
            "SELECT balance FROM users WHERE id=?",
            (update.effective_user.id,)
        ).fetchone()

    balance = row["balance"] if row else 0
    text = (
        "✅ <b>Verification Successful!</b>\n\n"
        "👋 <b>Welcome to QR Claim Bot</b> 🎉\n\n"
        "🎯 Claim QR orders, complete tasks, earn rewards and withdraw your balance.\n\n"
        f"💰 <b>Balance:</b> ₹{balance:.2f}"
    )
    await update.message.reply_text(
        text, parse_mode="HTML", reply_markup=user_menu()
    )


async def home(update, context):
    q = update.callback_query
    await q.answer()
    ensure_user(q.from_user)

    with closing(db()) as con:
        row = con.execute(
            "SELECT balance FROM users WHERE id=?", (q.from_user.id,)
        ).fetchone()

    balance = row["balance"] if row else 0
    text = (
        "👋 <b>Welcome to QR Claim Bot</b> 🎉\n\n"
        "🎯 Claim QR orders, complete tasks, earn rewards and withdraw your balance.\n\n"
        f"💰 <b>Balance:</b> ₹{balance:.2f}"
    )
    await q.edit_message_text(
        text, parse_mode="HTML", reply_markup=user_menu()
    )


async def wallet(update, context):
    q = update.callback_query
    await q.answer()
    ensure_user(q.from_user)

    with closing(db()) as con:
        r = con.execute(
            "SELECT balance,total_earned,total_withdrawn FROM users WHERE id=?",
            (q.from_user.id,)
        ).fetchone()
        p = con.execute("""
            SELECT COALESCE(SUM(amount),0) x
            FROM withdrawals
            WHERE user_id=? AND status IN ('pending','processing')
        """, (q.from_user.id,)).fetchone()

    text = (
        "💰 <b>MY WALLET</b>\n\n"
        f"💵 Available Balance: ₹{r['balance']:.2f}\n"
        f"🏆 Total Earned: ₹{r['total_earned']:.2f}\n"
        f"💸 Total Withdrawn: ₹{r['total_withdrawn']:.2f}\n"
        f"⏳ Pending Withdrawal: ₹{p['x']:.2f}"
    )
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("💸 WITHDRAW", callback_data="withdraw"),
            InlineKeyboardButton("📜 TRANSACTIONS", callback_data="transactions")
        ],
        [InlineKeyboardButton("🔙 BACK TO MENU", callback_data="home")]
    ])
    await q.edit_message_text(text, parse_mode="HTML", reply_markup=kb)


async def rewards(update, context):
    q = update.callback_query
    await q.answer()
    ensure_user(q.from_user)

    with closing(db()) as con:
        approved = con.execute("""
            SELECT COALESCE(SUM(amount),0) x
            FROM transactions
            WHERE user_id=? AND kind='QR Reward'
        """, (q.from_user.id,)).fetchone()["x"]

        pending = con.execute("""
            SELECT COALESCE(SUM(q.reward),0) x
            FROM proofs p
            JOIN qrs q ON q.id=p.qr_id
            WHERE p.user_id=? AND p.status='pending'
        """, (q.from_user.id,)).fetchone()["x"]

        rejected = con.execute("""
            SELECT COALESCE(SUM(q.reward),0) x
            FROM proofs p
            JOIN qrs q ON q.id=p.qr_id
            WHERE p.user_id=? AND p.status='rejected'
        """, (q.from_user.id,)).fetchone()["x"]

        rows = con.execute("""
            SELECT amount, created_at
            FROM transactions
            WHERE user_id=? AND kind='QR Reward'
            ORDER BY id DESC LIMIT 8
        """, (q.from_user.id,)).fetchall()

    text = (
        "🎁 <b>MY REWARDS</b>\n\n"
        f"✅ Approved: ₹{approved:.2f}\n"
        f"⏳ Pending: ₹{pending:.2f}\n"
        f"❌ Rejected: ₹{rejected:.2f}\n\n"
        "<b>Recent approved QR rewards:</b>\n"
    )
    text += (
        "\n".join(f"➕ ₹{r['amount']:.2f} — {r['created_at']}" for r in rows)
        if rows else "No approved rewards yet."
    )

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📜 REWARD HISTORY", callback_data="transactions")],
        [InlineKeyboardButton("💰 WALLET", callback_data="wallet")],
        [InlineKeyboardButton("🔙 BACK TO MENU", callback_data="home")]
    ])
    await q.edit_message_text(text, parse_mode="HTML", reply_markup=kb)


async def transactions(update, context):
    q = update.callback_query
    await q.answer()
    page = int(context.user_data.get("tx_page", 0))
    per_page = 10
    offset = page * per_page

    with closing(db()) as con:
        rows = con.execute("""
            SELECT amount,kind,created_at
            FROM transactions
            WHERE user_id=?
            ORDER BY id DESC
            LIMIT ? OFFSET ?
        """, (q.from_user.id, per_page, offset)).fetchall()

        total = con.execute(
            "SELECT COUNT(*) x FROM transactions WHERE user_id=?",
            (q.from_user.id,)
        ).fetchone()["x"]

    text = "📜 <b>TRANSACTION HISTORY</b>\n\n"
    if rows:
        text += "\n".join(
            ("➕ " if r["amount"] >= 0 else "➖ ")
            + f"₹{abs(r['amount']):.2f} — {r['kind']}"
            for r in rows
        )
    else:
        text += "No transactions yet."

    buttons = []
    if page > 0:
        buttons.append(InlineKeyboardButton("◀️ PREVIOUS", callback_data="tx_prev"))
    if offset + per_page < total:
        buttons.append(InlineKeyboardButton("NEXT ▶️", callback_data="tx_next"))
    if buttons:
        kb_rows = [buttons]
    else:
        kb_rows = []
    kb_rows.append([InlineKeyboardButton("🔙 BACK TO MENU", callback_data="home")])

    await q.edit_message_text(
        text, parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(kb_rows)
    )


async def profile(update, context):
    q = update.callback_query
    await q.answer()
    ensure_user(q.from_user)

    with closing(db()) as con:
        r = con.execute(
            "SELECT * FROM users WHERE id=?", (q.from_user.id,)
        ).fetchone()

    username = f"@{r['username']}" if r["username"] else "Not set"
    text = (
        "👤 <b>MY PROFILE</b>\n\n"
        f"Name: {r['name']}\n"
        f"Username: {username}\n"
        f"Telegram ID: {r['id']}\n\n"
        f"💰 Balance: ₹{r['balance']:.2f}\n"
        f"🎁 Total Rewards: ₹{r['total_earned']:.2f}\n"
        f"💸 Total Withdrawn: ₹{r['total_withdrawn']:.2f}"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📜 TRANSACTIONS", callback_data="transactions")],
        [InlineKeyboardButton("🔙 BACK TO MENU", callback_data="home")]
    ])
    await q.edit_message_text(text, parse_mode="HTML", reply_markup=kb)


async def help_page(update, context):
    q = update.callback_query
    await q.answer()

    text = (
        "ℹ️ <b>HOW IT WORKS</b>\n\n"
        "1️⃣ Claim an available QR.\n"
        "2️⃣ Receive the QR in your private chat.\n"
        "3️⃣ Complete the required payment/action.\n"
        "4️⃣ Upload your proof.\n"
        "5️⃣ Submit it for review.\n"
        "6️⃣ After approval, your reward is added to your wallet.\n"
        "7️⃣ Withdraw your available balance."
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "📞 SUPPORT",
            url=f"https://t.me/{SUPPORT_ID.lstrip('@')}"
        )],
        [InlineKeyboardButton("🔙 BACK TO MENU", callback_data="home")]
    ])
    await q.edit_message_text(text, parse_mode="HTML", reply_markup=kb)


# =========================
# WITHDRAWAL FLOW
# =========================
async def withdraw(update, context):
    q = update.callback_query
    await q.answer()
    ensure_user(q.from_user)

    with closing(db()) as con:
        r = con.execute(
            "SELECT balance FROM users WHERE id=?", (q.from_user.id,)
        ).fetchone()

    context.user_data["withdraw_stage"] = "amount"
    await q.edit_message_text(
        f"💸 <b>WITHDRAW</b>\n\n"
        f"💰 Available Balance: ₹{r['balance']:.2f}\n\n"
        f"Minimum withdrawal: ₹{MIN_WITHDRAWAL:.0f}\n\n"
        "Enter the amount you want to withdraw.",
        parse_mode="HTML",
        reply_markup=back_button()
    )


async def choose_withdraw_method(update, context):
    q = update.callback_query
    await q.answer()

    method = q.data.replace("wmethod_", "").upper()
    if method == "UPI" and not ENABLE_UPI:
        await q.answer("UPI is disabled.", show_alert=True)
        return
    if method == "USDT" and not ENABLE_USDT:
        await q.answer("USDT is disabled.", show_alert=True)
        return

    context.user_data["withdraw_method"] = method
    context.user_data["withdraw_stage"] = "account"

    prompt = (
        "Send your UPI ID."
        if method == "UPI"
        else "Send your USDT wallet address."
    )
    await q.edit_message_text(
        f"💸 <b>{method} WITHDRAWAL</b>\n\n{prompt}",
        parse_mode="HTML",
        reply_markup=back_button()
    )


async def create_withdrawal(update, context, account):
    uid = update.effective_user.id
    amount = float(context.user_data["withdraw_amount"])
    method = context.user_data["withdraw_method"]

    with closing(db()) as con:
        con.execute("BEGIN IMMEDIATE")
        user = con.execute(
            "SELECT balance FROM users WHERE id=?", (uid,)
        ).fetchone()

        if not user or user["balance"] < amount:
            con.rollback()
            await update.message.reply_text(
                "❌ Insufficient balance. Please try again."
            )
            context.user_data.clear()
            return

        con.execute(
            "UPDATE users SET balance=balance-? WHERE id=?",
            (amount, uid)
        )
        cur = con.execute("""
            INSERT INTO withdrawals(user_id,amount,method,account,status)
            VALUES(?,?,?,?, 'pending')
        """, (uid, amount, method, account))
        wid = cur.lastrowid

        con.execute("""
            INSERT INTO transactions(user_id,amount,kind)
            VALUES(?,?,?)
        """, (uid, -amount, "Withdrawal Hold"))

        con.commit()

    context.user_data.clear()

    await update.message.reply_text(
        f"✅ <b>WITHDRAWAL REQUEST CREATED</b>\n\n"
        f"🆔 Withdrawal ID: #{wid}\n"
        f"💰 Amount: ₹{amount:.2f}\n"
        f"🏦 Method: {method}\n"
        f"📌 Status: <b>PENDING</b>\n\n"
        "You will receive notifications when the status changes.",
        parse_mode="HTML",
        reply_markup=user_menu()
    )

    for aid in get_admin_ids():
        try:
            await context.bot.send_message(
                aid,
                f"💸 <b>NEW WITHDRAWAL</b>\n\n"
                f"🆔 #{wid}\n"
                f"👤 User: {uid}\n"
                f"💰 Amount: ₹{amount:.2f}\n"
                f"🏦 Method: {method}\n"
                f"📋 Account: <code>{account}</code>\n"
                f"📌 Status: PENDING",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(
                            "⏳ PROCESSING",
                            callback_data=f"wprocessing_{wid}"
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "❌ REJECT",
                            callback_data=f"wreject_{wid}"
                        ),
                        InlineKeyboardButton(
                            "💰 PAID",
                            callback_data=f"wpaid_{wid}"
                        )
                    ]
                ])
            )
        except Exception:
            log.exception("Could not notify admin %s", aid)


def get_admin_ids():
    with closing(db()) as con:
        rows = con.execute("SELECT user_id FROM admins").fetchall()
    ids = {r["user_id"] for r in rows}
    ids.update(ADMIN_IDS)
    return ids


# =========================
# CLAIM + PROOF
# =========================
async def claim(update, context):
    q = update.callback_query
    await q.answer()

    if q.message.chat.type != "private":
        # Claim button is intentionally posted in a group. Continue.
        pass

    uid = q.from_user.id
    ensure_user(q.from_user)

    # Atomically reserve exactly one QR.
    with closing(db()) as con:
        con.execute("BEGIN IMMEDIATE")
        qr = con.execute("""
            SELECT * FROM qrs
            WHERE status='available'
            ORDER BY id
            LIMIT 1
        """).fetchone()

        if not qr:
            con.rollback()
            await q.answer(
                "No QR is currently available.",
                show_alert=True
            )
            return

        cur = con.execute("""
            UPDATE qrs
            SET status='claimed',
                claimed_by=?,
                claimed_at=CURRENT_TIMESTAMP
            WHERE id=? AND status='available'
        """, (uid, qr["id"]))

        if cur.rowcount != 1:
            con.rollback()
            await q.answer(
                "This QR was just claimed by another user.",
                show_alert=True
            )
            return

        con.commit()

    try:
        await context.bot.send_photo(
            uid,
            qr["file_id"],
            caption=(
                f"📷 <b>{qr['app_name']}</b>\n"
                f"💰 Reward: ₹{qr['reward']:.2f}\n\n"
                "Complete the required action and upload your proof."
            ),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    "📤 UPLOAD PROOF",
                    callback_data=f"upload_{qr['id']}"
                )]
            ])
        )

        # Only the group claim post is edited. Never send the user menu to the group.
        group_text = (
            "🔒 <b>QR CLAIMED</b>\n\n"
            f"👤 Claimed by: "
            f"@{q.from_user.username}"
            if q.from_user.username
            else "🔒 <b>QR CLAIMED</b>\n\n👤 Claimed by a user."
        )
        await q.edit_message_text(group_text, parse_mode="HTML")

    except Exception as e:
        log.exception("Claim DM failed: %s", e)
        with closing(db()) as con:
            con.execute("""
                UPDATE qrs
                SET status='available',claimed_by=NULL,claimed_at=NULL
                WHERE id=?
            """, (qr["id"],))
            con.commit()

        await q.answer(
            "Please open the bot in private chat and press /start first.",
            show_alert=True
        )


async def upload(update, context):
    q = update.callback_query
    await q.answer()

    qr_id = int(q.data.split("_", 1)[1])

    with closing(db()) as con:
        qr = con.execute(
            "SELECT claimed_by,status FROM qrs WHERE id=?", (qr_id,)
        ).fetchone()

    if not qr or qr["claimed_by"] != q.from_user.id:
        await q.answer(
            "This QR is not assigned to you.",
            show_alert=True
        )
        return

    if qr["status"] != "claimed":
        await q.answer("This QR is no longer active.", show_alert=True)
        return

    context.user_data["proof_qr_id"] = qr_id
    context.user_data["proof_stage"] = "photo"

    await q.edit_message_text(
        "📤 <b>UPLOAD PROOF</b>\n\n"
        "Send your proof image/photo here.",
        parse_mode="HTML",
        reply_markup=back_button()
    )


async def photo_handler(update, context):
    if update.effective_chat.type != "private":
        return

    # Admin QR creation has priority.
    if is_admin(update.effective_user.id) and \
            context.user_data.get("admin_qr_stage") == "photo":
        context.user_data["new_qr_file"] = update.message.photo[-1].file_id
        context.user_data["admin_qr_stage"] = "app"
        await update.message.reply_text("Enter the QR app name.")
        return

    if context.user_data.get("proof_stage") != "photo":
        return

    qr_id = context.user_data.get("proof_qr_id")
    if not qr_id:
        return

    file_id = update.message.photo[-1].file_id
    context.user_data["proof_file_id"] = file_id
    context.user_data["proof_stage"] = "confirm"

    await update.message.reply_photo(
        file_id,
        caption="📷 Proof received.\n\nPress submit to send it for review.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(
                "🟢 SUBMIT FOR REVIEW",
                callback_data=f"submit_{qr_id}"
            )]
        ])
    )


async def submit(update, context):
    q = update.callback_query
    await q.answer()

    qr_id = int(q.data.split("_", 1)[1])
    file_id = context.user_data.get("proof_file_id")

    if not file_id:
        await q.answer("Upload proof first.", show_alert=True)
        return

    with closing(db()) as con:
        qr = con.execute(
            "SELECT * FROM qrs WHERE id=?", (qr_id,)
        ).fetchone()

        if not qr or qr["claimed_by"] != q.from_user.id:
            await q.answer("This QR is not assigned to you.", show_alert=True)
            return

        existing = con.execute("""
            SELECT id FROM proofs
            WHERE qr_id=? AND user_id=?
            AND status IN ('pending','approved')
        """, (qr_id, q.from_user.id)).fetchone()

        if existing:
            await q.edit_message_text(
                "⏳ <b>UNDER REVIEW</b>\n\n"
                "Your proof has already been submitted.",
                parse_mode="HTML",
                reply_markup=user_menu()
            )
            return

        con.execute("""
            INSERT INTO proofs(qr_id,user_id,file_id,status)
            VALUES(?,?,?,'pending')
        """, (qr_id, q.from_user.id, file_id))

        con.commit()

    context.user_data["proof_stage"] = "done"

    await q.edit_message_text(
        "⏳ <b>UNDER REVIEW</b>\n\n"
        "Your proof has been submitted. Please wait for admin review.",
        parse_mode="HTML",
        reply_markup=user_menu()
    )

    for aid in get_admin_ids():
        try:
            await context.bot.send_photo(
                aid,
                file_id,
                caption=(
                    f"📥 <b>NEW PROOF</b>\n\n"
                    f"👤 User: {q.from_user.id}\n"
                    f"📱 QR: #{qr_id}\n"
                    f"💰 Reward: ₹{qr['reward']:.2f}"
                ),
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(
                            "🟢 APPROVE",
                            callback_data=f"approve_{qr_id}_{q.from_user.id}"
                        ),
                        InlineKeyboardButton(
                            "🔴 REJECT",
                            callback_data=f"reject_{qr_id}_{q.from_user.id}"
                        )
                    ]
                ])
            )
        except Exception:
            log.exception("Could not send proof to admin %s", aid)


async def review_proof(update, context, approved: bool):
    q = update.callback_query
    await q.answer()

    parts = q.data.split("_")
    qr_id = int(parts[1])
    uid = int(parts[2])

    if not is_admin(q.from_user.id):
        return

    with closing(db()) as con:
        proof = con.execute("""
            SELECT p.*, q.reward, q.app_name
            FROM proofs p JOIN qrs q ON q.id=p.qr_id
            WHERE p.qr_id=? AND p.user_id=? AND p.status='pending'
            ORDER BY p.id DESC LIMIT 1
        """, (qr_id, uid)).fetchone()

        if not proof:
            await q.answer("Proof already reviewed.", show_alert=True)
            return

        if approved:
            con.execute(
                "UPDATE proofs SET status='approved',reviewed_by=?,reviewed_at=CURRENT_TIMESTAMP WHERE id=?",
                (q.from_user.id, proof["id"])
            )
            con.execute(
                "UPDATE qrs SET status='completed' WHERE id=?",
                (qr_id,)
            )
            con.execute("""
                UPDATE users
                SET balance=balance+?, total_earned=total_earned+?
                WHERE id=?
            """, (proof["reward"], proof["reward"], uid))
            con.execute("""
                INSERT INTO transactions(user_id,amount,kind)
                VALUES(?,?, 'QR Reward')
            """, (uid, proof["reward"]))
            con.commit()

            user_text = (
                "🎉 <b>REWARD APPROVED</b>\n\n"
                f"📱 App: {proof['app_name']}\n"
                f"💰 Reward added: ₹{proof['reward']:.2f}\n\n"
                "Your reward has been added to your wallet."
            )
        else:
            con.execute(
                "UPDATE proofs SET status='rejected',reviewed_by=?,reviewed_at=CURRENT_TIMESTAMP WHERE id=?",
                (q.from_user.id, proof["id"])
            )
            con.execute(
                "UPDATE qrs SET status='rejected' WHERE id=?",
                (qr_id,)
            )
            con.commit()

            user_text = (
                "❌ <b>PROOF REJECTED</b>\n\n"
                f"📱 App: {proof['app_name']}\n"
                "Your submitted proof was rejected by an admin."
            )

    try:
        await context.bot.send_message(
            uid, user_text, parse_mode="HTML", reply_markup=user_menu()
        )
    except Exception:
        pass

    try:
        await q.edit_message_caption(
            caption=q.message.caption + (
                "\n\n🟢 APPROVED" if approved else "\n\n🔴 REJECTED"
            )
        )
    except Exception:
        pass


# =========================
# ADMIN QR CREATION
# =========================
async def admin_qr(update, context):
    if update.effective_chat.type != "private":
        return
    if not is_admin(update.effective_user.id):
        return

    context.user_data.clear()
    context.user_data["admin_qr_stage"] = "photo"

    await update.message.reply_text(
        "📱 <b>ADD QR</b>\n\n"
        "Send the QR image/photo now.",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardRemove()
    )


async def admin_text(update, context):
    if update.effective_chat.type != "private":
        return

    uid = update.effective_user.id
    stage = context.user_data.get("admin_qr_stage")

    if not is_admin(uid) or not stage:
        return

    if stage == "app":
        context.user_data["new_qr_app"] = update.message.text.strip()
        context.user_data["admin_qr_stage"] = "reward"
        await update.message.reply_text(
            "Enter reward amount in ₹, e.g. 15"
        )
        return

    if stage == "reward":
        try:
            reward = float(update.message.text.strip())
            if reward <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Enter a valid positive number.")
            return

        file_id = context.user_data.get("new_qr_file")
        app_name = context.user_data.get("new_qr_app")

        with closing(db()) as con:
            cur = con.execute("""
                INSERT INTO qrs(file_id,app_name,reward,status)
                VALUES(?,?,?,'available')
            """, (file_id, app_name, reward))
            qr_id = cur.lastrowid
            con.commit()

        context.user_data.clear()

        await update.message.reply_text(
            f"✅ <b>QR CREATED</b>\n\n"
            f"🆔 QR ID: #{qr_id}\n"
            f"📱 App: {app_name}\n"
            f"💰 Reward: ₹{reward:.2f}\n\n"
            "Use <b>/postqr</b> or the admin panel's POST QR button.",
            parse_mode="HTML",
            reply_markup=admin_menu()
        )
        return

    if stage == "admin_add":
        # handled below for adding an admin
        return


# =========================
# ADMIN DASHBOARD
# =========================
async def admin_command(update, context):
    if update.effective_chat.type != "private":
        return
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ You are not an admin.")
        return
    if WEBAPP_URL:
        await update.message.reply_text(
            "🛠️ <b>ADMIN PANEL</b>\n\nOpen the full admin dashboard below.\nYou can review proofs with APPROVE / REJECT buttons.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📊 OPEN ADMIN DASHBOARD", web_app=WebAppInfo(url=WEBAPP_URL + "/admin"))
            ],[
                InlineKeyboardButton("🧾 PROOF REVIEW", callback_data="admin_proofs"),
                InlineKeyboardButton("📱 QR MANAGEMENT", callback_data="admin_qrs")
            ],[
                InlineKeyboardButton("💸 WITHDRAWALS", callback_data="admin_withdrawals")
            ]])
        )
    else:
        await update.message.reply_text("⚠️ WEBAPP_URL is not configured in Railway. Set it to your Railway public URL, then redeploy.")
        await send_admin_dashboard(update, context, message_mode=True)


async def send_admin_dashboard(update, context, message_mode=False):
    with closing(db()) as con:
        users = con.execute("SELECT COUNT(*) x FROM users").fetchone()["x"]
        qr_total = con.execute("SELECT COUNT(*) x FROM qrs").fetchone()["x"]
        qr_available = con.execute(
            "SELECT COUNT(*) x FROM qrs WHERE status='available'"
        ).fetchone()["x"]
        claims = con.execute(
            "SELECT COUNT(*) x FROM qrs WHERE claimed_by IS NOT NULL"
        ).fetchone()["x"]
        pending_proofs = con.execute(
            "SELECT COUNT(*) x FROM proofs WHERE status='pending'"
        ).fetchone()["x"]
        wd_total = con.execute(
            "SELECT COUNT(*) x FROM withdrawals"
        ).fetchone()["x"]
        wd_pending = con.execute(
            "SELECT COUNT(*) x FROM withdrawals WHERE status='pending'"
        ).fetchone()["x"]
        wd_processing = con.execute(
            "SELECT COUNT(*) x FROM withdrawals WHERE status='processing'"
        ).fetchone()["x"]
        paid_amount = con.execute(
            "SELECT COALESCE(SUM(amount),0) x FROM withdrawals WHERE status='paid'"
        ).fetchone()["x"]
        rejected_wd = con.execute(
            "SELECT COUNT(*) x FROM withdrawals WHERE status='rejected'"
        ).fetchone()["x"]

    text = (
        "🛠️ <b>ADMIN PANEL</b>\n\n"
        "📊 <b>DASHBOARD</b>\n"
        f"👥 Total Users: {users}\n"
        f"📱 Total QRs: {qr_total}\n"
        f"🟢 Available QRs: {qr_available}\n"
        f"🎯 Total Claims: {claims}\n"
        f"🧾 Pending Proofs: {pending_proofs}\n\n"
        "💸 <b>WITHDRAWALS</b>\n"
        f"📦 Total Withdrawals: {wd_total}\n"
        f"⏳ Pending: {wd_pending}\n"
        f"🔄 Processing: {wd_processing}\n"
        f"💰 Paid Amount: ₹{paid_amount:.2f}\n"
        f"❌ Rejected: {rejected_wd}"
    )

    if message_mode:
        await update.message.reply_text(
            text, parse_mode="HTML", reply_markup=admin_menu()
        )
    else:
        await update.callback_query.edit_message_text(
            text, parse_mode="HTML", reply_markup=admin_menu()
        )


async def admin_withdrawals(update, context):
    q = update.callback_query
    await q.answer()

    if not is_admin(q.from_user.id):
        return

    with closing(db()) as con:
        rows = con.execute("""
            SELECT w.*, u.username, u.name
            FROM withdrawals w
            LEFT JOIN users u ON u.id=w.user_id
            ORDER BY w.id DESC LIMIT 15
        """).fetchall()

    text = "💸 <b>WITHDRAWAL MANAGEMENT</b>\n\n"
    if not rows:
        text += "No withdrawals yet."
        kb = [[InlineKeyboardButton("🔙 ADMIN PANEL", callback_data="admin_dashboard")]]
    else:
        for w in rows:
            uname = f"@{w['username']}" if w["username"] else str(w["user_id"])
            text += (
                f"🆔 #{w['id']} | {uname}\n"
                f"💰 ₹{w['amount']:.2f} | {w['method']}\n"
                f"📌 <b>{w['status'].upper()}</b>\n\n"
            )

        kb = []
        for w in rows[:8]:
            if w["status"] == "pending":
                kb.append([
                    InlineKeyboardButton(
                        f"#{w['id']} ⏳ PROCESSING",
                        callback_data=f"wprocessing_{w['id']}"
                    ),
                    InlineKeyboardButton(
                        "❌ REJECT",
                        callback_data=f"wreject_{w['id']}"
                    )
                ])
            elif w["status"] == "processing":
                kb.append([
                    InlineKeyboardButton(
                        f"#{w['id']} 💰 PAID",
                        callback_data=f"wpaid_{w['id']}"
                    ),
                    InlineKeyboardButton(
                        "❌ REJECT",
                        callback_data=f"wreject_{w['id']}"
                    )
                ])
        kb.append([InlineKeyboardButton(
            "🔙 ADMIN PANEL", callback_data="admin_dashboard"
        )])

    await q.edit_message_text(
        text, parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(kb)
    )


async def change_withdrawal_status(update, context):
    q = update.callback_query
    await q.answer()

    if not is_admin(q.from_user.id):
        return

    action, wid_text = q.data.split("_", 1)
    wid = int(wid_text)

    if action == "wprocessing":
        new_status = "processing"
    elif action == "wpaid":
        new_status = "paid"
    elif action == "wreject":
        new_status = "rejected"
    else:
        return

    with closing(db()) as con:
        con.execute("BEGIN IMMEDIATE")
        w = con.execute(
            "SELECT * FROM withdrawals WHERE id=?", (wid,)
        ).fetchone()

        if not w:
            con.rollback()
            await q.answer("Withdrawal not found.", show_alert=True)
            return

        old = w["status"]
        if old in ("paid", "rejected"):
            con.rollback()
            await q.answer("This withdrawal is already finalized.", show_alert=True)
            return

        if new_status == "rejected" and old != "rejected":
            # Refund the reserved amount.
            con.execute(
                "UPDATE users SET balance=balance+? WHERE id=?",
                (w["amount"], w["user_id"])
            )
            con.execute("""
                INSERT INTO transactions(user_id,amount,kind)
                VALUES(?,?, 'Withdrawal Refund')
            """, (w["user_id"], w["amount"]))

        if new_status == "paid":
            con.execute("""
                UPDATE users
                SET total_withdrawn=total_withdrawn+?
                WHERE id=?
            """, (w["amount"], w["user_id"]))

        con.execute("""
            UPDATE withdrawals
            SET status=?, updated_at=CURRENT_TIMESTAMP
            WHERE id=?
        """, (new_status, wid))

        con.commit()

    labels = {
        "processing": "🔄 PROCESSING",
        "paid": "✅ PAID",
        "rejected": "❌ REJECTED"
    }
    try:
        await context.bot.send_message(
            w["user_id"],
            f"💸 <b>WITHDRAWAL UPDATE</b>\n\n"
            f"🆔 #{wid}\n"
            f"💰 Amount: ₹{w['amount']:.2f}\n"
            f"📌 Status: <b>{labels[new_status]}</b>",
            parse_mode="HTML",
            reply_markup=user_menu()
        )
    except Exception:
        pass

    await admin_withdrawals(update, context)


async def admin_qrs(update, context):
    q = update.callback_query
    await q.answer()

    if not is_admin(q.from_user.id):
        return

    with closing(db()) as con:
        rows = con.execute("""
            SELECT id,app_name,reward,status,claimed_by
            FROM qrs ORDER BY id DESC LIMIT 15
        """).fetchall()

    text = "📱 <b>QR MANAGEMENT</b>\n\n"
    if rows:
        for r in rows:
            claimed = f" | 👤 {r['claimed_by']}" if r["claimed_by"] else ""
            text += (
                f"#{r['id']} — {r['app_name']}\n"
                f"💰 ₹{r['reward']:.2f} | 📌 {r['status']}{claimed}\n\n"
            )
    else:
        text += "No QRs yet."

    kb = [
        [
            InlineKeyboardButton("➕ ADD QR", callback_data="admin_addqr"),
            InlineKeyboardButton("📢 POST QR", callback_data="admin_postqr")
        ],
        [InlineKeyboardButton("🔙 ADMIN PANEL", callback_data="admin_dashboard")]
    ]
    await q.edit_message_text(
        text, parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(kb)
    )


async def admin_claims(update, context):
    q = update.callback_query
    await q.answer()

    if not is_admin(q.from_user.id):
        return

    with closing(db()) as con:
        rows = con.execute("""
            SELECT q.id,q.app_name,q.reward,q.claimed_by,q.claimed_at,q.status,
                   u.username
            FROM qrs q
            LEFT JOIN users u ON u.id=q.claimed_by
            WHERE q.claimed_by IS NOT NULL
            ORDER BY q.id DESC LIMIT 20
        """).fetchall()

    text = "📋 <b>CLAIMS</b>\n\n"
    if not rows:
        text += "No claims yet."
    else:
        for r in rows:
            uname = f"@{r['username']}" if r["username"] else str(r["claimed_by"])
            text += (
                f"📱 #{r['id']} — {r['app_name']}\n"
                f"👤 {uname}\n"
                f"💰 ₹{r['reward']:.2f}\n"
                f"📌 {r['status']}\n\n"
            )

    await q.edit_message_text(
        text, parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(
                "🔙 ADMIN PANEL", callback_data="admin_dashboard"
            )]
        ])
    )


async def admin_proofs(update, context):
    q = update.callback_query
    await q.answer()

    if not is_admin(q.from_user.id):
        return

    with closing(db()) as con:
        rows = con.execute("""
            SELECT p.id,p.qr_id,p.user_id,p.status,q.app_name,q.reward,
                   u.username,p.file_id
            FROM proofs p
            JOIN qrs q ON q.id=p.qr_id
            LEFT JOIN users u ON u.id=p.user_id
            WHERE p.status='pending'
            ORDER BY p.id DESC LIMIT 20
        """).fetchall()

    if not rows:
        await q.edit_message_text(
            "🧾 <b>PENDING PROOF REVIEW</b>\n\nNo pending proofs.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 ADMIN PANEL", callback_data="admin_dashboard")]
            ])
        )
        return

    # Show each pending proof with its own Approve/Reject buttons.
    # This makes the Admin Panel review screen actionable even if the
    # original NEW PROOF notification was missed.
    first = True
    for r in rows:
        uname = f"@{r['username']}" if r["username"] else str(r["user_id"])
        caption = (
            f"🧾 <b>PENDING PROOF REVIEW</b>\n\n"
            f"Proof #{r['id']} | QR #{r['qr_id']}\n"
            f"👤 {uname} | 📱 {r['app_name']}\n"
            f"💰 ₹{r['reward']:.2f}"
        )
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "🟢 APPROVE",
                    callback_data=f"approve_{r['qr_id']}_{r['user_id']}"
                ),
                InlineKeyboardButton(
                    "🔴 REJECT",
                    callback_data=f"reject_{r['qr_id']}_{r['user_id']}"
                )
            ],
            [InlineKeyboardButton("🔙 ADMIN PANEL", callback_data="admin_dashboard")]
        ])

        try:
            await context.bot.send_photo(
                chat_id=q.from_user.id,
                photo=r["file_id"],
                caption=caption,
                parse_mode="HTML",
                reply_markup=keyboard
            )
        except Exception:
            # If Telegram cannot resend the photo, still show an actionable text card.
            await context.bot.send_message(
                chat_id=q.from_user.id,
                text=caption,
                parse_mode="HTML",
                reply_markup=keyboard
            )
        first = False

    # Remove the old panel message after sending the actionable review cards.
    try:
        await q.edit_message_text(
            "✅ Pending proof(s) shown above. Use APPROVE or REJECT on each proof.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 ADMIN PANEL", callback_data="admin_dashboard")]
            ])
        )
    except Exception:
        pass


async def admin_users(update, context):
    q = update.callback_query
    await q.answer()

    if not is_admin(q.from_user.id):
        return

    with closing(db()) as con:
        total = con.execute("SELECT COUNT(*) x FROM users").fetchone()["x"]
        rows = con.execute("""
            SELECT id,username,name,balance,total_earned,total_withdrawn
            FROM users ORDER BY id DESC LIMIT 15
        """).fetchall()

    text = f"👥 <b>USERS</b>\n\nTotal users: {total}\n\n"
    for r in rows:
        uname = f"@{r['username']}" if r["username"] else "no username"
        text += (
            f"👤 {r['name']} ({uname})\n"
            f"🆔 {r['id']} | 💰 ₹{r['balance']:.2f}\n\n"
        )

    await q.edit_message_text(
        text, parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(
                "🔙 ADMIN PANEL", callback_data="admin_dashboard"
            )]
        ])
    )


async def admin_admins(update, context):
    q = update.callback_query
    await q.answer()

    if not is_admin(q.from_user.id):
        return

    with closing(db()) as con:
        rows = con.execute(
            "SELECT user_id,added_at FROM admins ORDER BY user_id"
        ).fetchall()

    text = "👮 <b>ADMIN MANAGEMENT</b>\n\n"
    for r in rows:
        text += f"🆔 {r['user_id']}\n"
    text += "\nUse /addadmin USER_ID to add an admin.\n"
    text += "Use /deladmin USER_ID to remove an admin."

    await q.edit_message_text(
        text, parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(
                "🔙 ADMIN PANEL", callback_data="admin_dashboard"
            )]
        ])
    )


async def add_admin(update, context):
    if update.effective_chat.type != "private" or not is_admin(update.effective_user.id):
        return

    parts = update.message.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        await update.message.reply_text("Usage: /addadmin USER_ID")
        return

    uid = int(parts[1])
    with closing(db()) as con:
        con.execute(
            "INSERT OR IGNORE INTO admins(user_id) VALUES(?)", (uid,)
        )
        con.commit()

    await update.message.reply_text(
        f"✅ Admin added: <code>{uid}</code>",
        parse_mode="HTML",
        reply_markup=admin_menu()
    )


async def del_admin(update, context):
    if update.effective_chat.type != "private" or not is_admin(update.effective_user.id):
        return

    parts = update.message.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        await update.message.reply_text("Usage: /deladmin USER_ID")
        return

    uid = int(parts[1])
    if uid in ADMIN_IDS:
        await update.message.reply_text(
            "⚠️ This ID is configured in ADMIN_IDS and cannot be removed from the bot database."
        )
        return

    with closing(db()) as con:
        con.execute("DELETE FROM admins WHERE user_id=?", (uid,))
        con.commit()

    await update.message.reply_text(
        f"✅ Admin removed: <code>{uid}</code>",
        parse_mode="HTML",
        reply_markup=admin_menu()
    )


async def add_balance(update, context):
    if update.effective_chat.type != "private" or not is_admin(update.effective_user.id):
        return

    parts = update.message.text.split(maxsplit=3)
    if len(parts) < 3 or not parts[1].isdigit():
        await update.message.reply_text(
            "Usage: /addbalance USER_ID AMOUNT [REASON]\n\n"
            "Example: /addbalance 123456789 50 Bonus"
        )
        return

    uid = int(parts[1])
    try:
        amount = float(parts[2])
    except ValueError:
        await update.message.reply_text("❌ Enter a valid amount.")
        return
    if uid <= 0 or amount <= 0 or amount > 1000000:
        await update.message.reply_text("❌ User ID must be valid and amount must be between ₹0.01 and ₹10,00,000.")
        return

    reason = parts[3].strip() if len(parts) == 4 else "Admin balance credit"
    with closing(db()) as con:
        con.execute("BEGIN IMMEDIATE")
        u = con.execute("SELECT id,username,name FROM users WHERE id=?", (uid,)).fetchone()
        if not u:
            con.rollback()
            await update.message.reply_text("❌ User not found. Ask the user to press /start first.")
            return
        con.execute("UPDATE users SET balance=balance+? WHERE id=?", (amount, uid))
        con.execute("INSERT INTO transactions(user_id,amount,kind) VALUES(?,?,?)", (uid, amount, f"Admin Credit: {reason[:100]}"))
        new_balance = con.execute("SELECT balance FROM users WHERE id=?", (uid,)).fetchone()["balance"]
        con.commit()

    try:
        await context.bot.send_message(
            uid,
            f"💰 <b>BALANCE ADDED</b>\n\n➕ Added: ₹{amount:.2f}\n📝 Reason: {reason}\n💵 New balance: ₹{new_balance:.2f}",
            parse_mode="HTML", reply_markup=user_menu()
        )
    except Exception:
        pass

    await update.message.reply_text(
        f"✅ <b>BALANCE ADDED</b>\n\n👤 User ID: <code>{uid}</code>\n➕ Added: ₹{amount:.2f}\n💵 New balance: ₹{new_balance:.2f}\n📝 Reason: {reason}",
        parse_mode="HTML", reply_markup=admin_menu()
    )


async def admin_settings(update, context):
    q = update.callback_query
    await q.answer()

    if not is_admin(q.from_user.id):
        return

    text = (
        "⚙️ <b>SETTINGS</b>\n\n"
        f"🏦 UPI: {'ON' if ENABLE_UPI else 'OFF'}\n"
        f"🪙 USDT: {'ON' if ENABLE_USDT else 'OFF'}\n"
        f"💰 Minimum withdrawal: ₹{MIN_WITHDRAWAL:.0f}\n"
        f"📢 Force join: {'ON' if FORCE_JOIN_CHANNEL else 'OFF'}\n"
        f"📞 Support: {SUPPORT_ID}\n\n"
        "Payment method settings are controlled by Railway environment variables."
    )
    await q.edit_message_text(
        text, parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(
                "🔙 ADMIN PANEL", callback_data="admin_dashboard"
            )]
        ])
    )


# =========================
# POST QR
# =========================
async def postqr(update, context):
    if update.effective_chat.type != "private":
        return
    if not is_admin(update.effective_user.id):
        return

    if not GROUP_ID:
        await update.message.reply_text("❌ GROUP_ID is not configured.")
        return

    with closing(db()) as con:
        qr = con.execute("""
            SELECT * FROM qrs
            WHERE status='available'
            ORDER BY id LIMIT 1
        """).fetchone()

    if not qr:
        await update.message.reply_text(
            "No available QR. Create one with /addqr."
        )
        return

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("💳 CLAIM QR", callback_data="claim")]
    ])

    msg = await context.bot.send_message(
        int(GROUP_ID),
        "🎯 <b>QR CLAIM AVAILABLE</b>\n\n"
        f"📱 App: {qr['app_name']}\n"
        f"💰 Reward: ₹{qr['reward']:.2f}\n\n"
        "First eligible user to claim gets this QR in DM.",
        parse_mode="HTML",
        reply_markup=kb
    )

    with closing(db()) as con:
        con.execute(
            "UPDATE qrs SET posted_message_id=? WHERE id=?",
            (msg.message_id, qr["id"])
        )
        con.commit()

    await update.message.reply_text(
        "✅ Posted to group.\n\n"
        "🔒 QR image was NOT posted in the group.",
        reply_markup=admin_menu()
    )


# =========================
# TEXT HANDLER
# =========================
async def text_handler(update, context):
    if update.effective_chat.type != "private":
        return

    # Admin QR wizard first.
    if is_admin(update.effective_user.id) and \
            context.user_data.get("admin_qr_stage"):
        await admin_text(update, context)
        return

    stage = context.user_data.get("withdraw_stage")

    if stage == "amount":
        try:
            amount = float(update.message.text.strip())
        except ValueError:
            await update.message.reply_text("Please enter a valid amount.")
            return

        with closing(db()) as con:
            r = con.execute(
                "SELECT balance FROM users WHERE id=?",
                (update.effective_user.id,)
            ).fetchone()

        if amount < MIN_WITHDRAWAL or amount > r["balance"]:
            # Stop the amount-entry state after an invalid amount.
            # This prevents repeated "minimum withdrawal" replies when
            # the user keeps sending the same invalid number.
            context.user_data.pop("withdraw_stage", None)
            context.user_data.pop("withdraw_amount", None)
            context.user_data.pop("withdraw_method", None)
            await update.message.reply_text(
                f"❌ Withdrawal not started.\n\n"
                f"Minimum withdrawal: ₹{MIN_WITHDRAWAL:.0f}\n"
                f"Available balance: ₹{r['balance']:.2f}\n\n"
                "Please tap 💸 WITHDRAW again when you have enough balance.",
                reply_markup=user_menu()
            )
            return

        methods = []
        if ENABLE_UPI:
            methods.append(
                InlineKeyboardButton("🏦 UPI", callback_data="wmethod_UPI")
            )
        if ENABLE_USDT:
            methods.append(
                InlineKeyboardButton("🪙 USDT", callback_data="wmethod_USDT")
            )

        if not methods:
            await update.message.reply_text(
                "❌ No withdrawal method is enabled."
            )
            context.user_data.clear()
            return

        context.user_data["withdraw_amount"] = amount
        context.user_data["withdraw_stage"] = "method"

        await update.message.reply_text(
            f"💸 <b>WITHDRAWAL REQUEST</b>\n\n"
            f"Amount: ₹{amount:.2f}\n"
            f"Available Balance: ₹{r['balance']:.2f}\n\n"
            "Select withdrawal method:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                methods,
                [InlineKeyboardButton(
                    "🔙 CANCEL", callback_data="home"
                )]
            ])
        )
        return

    if stage == "account":
        account = update.message.text.strip()
        if len(account) < 3:
            await update.message.reply_text("Please enter valid withdrawal details.")
            return

        await create_withdrawal(update, context, account)
        return

    await update.message.reply_text(
        "Use the buttons below to navigate.",
        reply_markup=user_menu()
    )


# =========================
# CALLBACK ROUTER
# =========================
async def callbacks(update, context):
    d = update.callback_query.data

    if d == "home":
        return await home(update, context)
    if d == "check_join":
        q = update.callback_query
        await q.answer()
        if await force_join_ok(update, context):
            await q.edit_message_text(
                "✅ <b>Verification Successful!</b>\n\n"
                "You can now use the bot.",
                parse_mode="HTML",
                reply_markup=user_menu()
            )
        return

    if d == "wallet":
        return await wallet(update, context)
    if d == "rewards":
        return await rewards(update, context)
    if d == "transactions":
        return await transactions(update, context)
    if d == "tx_prev":
        context.user_data["tx_page"] = max(
            0, int(context.user_data.get("tx_page", 0)) - 1
        )
        return await transactions(update, context)
    if d == "tx_next":
        context.user_data["tx_page"] = int(
            context.user_data.get("tx_page", 0)
        ) + 1
        return await transactions(update, context)
    if d == "profile":
        return await profile(update, context)
    if d == "help":
        return await help_page(update, context)
    if d == "withdraw":
        return await withdraw(update, context)

    if d == "claim":
        return await claim(update, context)
    if d.startswith("upload_"):
        return await upload(update, context)
    if d.startswith("submit_"):
        return await submit(update, context)
    if d.startswith("wmethod_"):
        return await choose_withdraw_method(update, context)

    # Proof review
    if d.startswith("approve_"):
        return await review_proof(update, context, True)
    if d.startswith("reject_"):
        return await review_proof(update, context, False)

    # Withdrawal status
    if d.startswith("wprocessing_") or d.startswith("wpaid_") or d.startswith("wreject_"):
        return await change_withdrawal_status(update, context)

    # Admin panel
    if d == "admin_dashboard":
        if not is_admin(update.effective_user.id):
            return
        return await send_admin_dashboard(update, context, message_mode=False)
    if d == "admin_withdrawals":
        return await admin_withdrawals(update, context)
    if d == "admin_qrs":
        return await admin_qrs(update, context)
    if d == "admin_claims":
        return await admin_claims(update, context)
    if d == "admin_proofs":
        return await admin_proofs(update, context)
    if d == "admin_users":
        return await admin_users(update, context)
    if d == "admin_admins":
        return await admin_admins(update, context)
    if d == "admin_settings":
        return await admin_settings(update, context)

    if d == "admin_addqr":
        if not is_admin(update.effective_user.id):
            return
        q = update.callback_query
        await q.answer()
        context.user_data.clear()
        context.user_data["admin_qr_stage"] = "photo"
        await q.edit_message_text(
            "📱 <b>ADD QR</b>\n\n"
            "Send the QR image/photo now.",
            parse_mode="HTML"
        )
        return

    if d == "admin_postqr":
        q = update.callback_query
        await q.answer()
        if not is_admin(q.from_user.id):
            return
        if not GROUP_ID:
            await q.message.reply_text("❌ GROUP_ID is not configured.")
            return

        with closing(db()) as con:
            qr = con.execute("""
                SELECT * FROM qrs WHERE status='available'
                ORDER BY id LIMIT 1
            """).fetchone()

        if not qr:
            await q.message.reply_text(
                "No available QR. Create one first."
            )
            return

        msg = await context.bot.send_message(
            int(GROUP_ID),
            "🎯 <b>QR CLAIM AVAILABLE</b>\n\n"
            f"📱 App: {qr['app_name']}\n"
            f"💰 Reward: ₹{qr['reward']:.2f}\n\n"
            "First eligible user to claim gets this QR in DM.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💳 CLAIM QR", callback_data="claim")]
            ])
        )
        with closing(db()) as con:
            con.execute(
                "UPDATE qrs SET posted_message_id=? WHERE id=?",
                (msg.message_id, qr["id"])
            )
            con.commit()

        await q.edit_message_text(
            "✅ <b>QR POSTED TO GROUP</b>\n\n"
            "The QR image is hidden from the group. "
            "Users receive it only in DM after claiming.",
            parse_mode="HTML",
            reply_markup=admin_menu()
        )
        return


# =========================
# MAIN
# =========================
def main():
    global telegram_bot
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is missing")

    init_db()

    app = Application.builder().token(TOKEN).build()
    telegram_bot = app.bot

    # Run FastAPI dashboard beside the Telegram polling bot.
    threading.Thread(target=start_dashboard_server, daemon=True).start()

    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_command))
    app.add_handler(CommandHandler("addqr", admin_qr))
    app.add_handler(CommandHandler("postqr", postqr))
    app.add_handler(CommandHandler("addadmin", add_admin))
    app.add_handler(CommandHandler("deladmin", del_admin))
    app.add_handler(CommandHandler("addbalance", add_balance))

    # Callback buttons
    app.add_handler(CallbackQueryHandler(callbacks))

    # Photos: admin QR wizard and user proof wizard
    app.add_handler(MessageHandler(filters.PHOTO, photo_handler))

    # Text: withdrawal/admin wizard/user fallback
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        text_handler
    ))

    log.info("Bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
