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
from html import escape

# =========================
# ENVIRONMENT
# =========================
TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = {
    int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}
GROUP_ID = os.getenv("GROUP_ID", "")
GROUP_IDS = os.getenv("GROUP_IDS", "").strip()
# Multiple groups are supported. Set GROUP_IDS=-1001,-1002 or @group1,@group2.
GROUP_IDS_RAW = os.getenv("GROUP_IDS", "").strip()
GROUP_IDS = [x.strip() for x in GROUP_IDS_RAW.split(",") if x.strip()]
if not GROUP_IDS and GROUP_ID:
    GROUP_IDS = [GROUP_ID]
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
# Telegram Web Apps require an absolute HTTPS URL. Railway may be configured
# with only the hostname, so normalize it automatically.
if WEBAPP_URL and not WEBAPP_URL.startswith(("https://", "http://")):
    WEBAPP_URL = "https://" + WEBAPP_URL
if WEBAPP_URL.startswith("http://"):
    WEBAPP_URL = "https://" + WEBAPP_URL[len("http://"):]
RAILWAY_DOMAIN = os.getenv("RAILWAY_PUBLIC_DOMAIN", "").strip().rstrip("/")
if not WEBAPP_URL and RAILWAY_DOMAIN:
    WEBAPP_URL = RAILWAY_DOMAIN if RAILWAY_DOMAIN.startswith("https://") else "https://" + RAILWAY_DOMAIN

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DASHBOARD_FILE = os.path.join(BASE_DIR, "dashboard", "index.html")

SUPPORT_ID = os.getenv("SUPPORT_ID", "@itsvanshu").strip()
FORCE_JOIN_CHANNEL = os.getenv("FORCE_JOIN_CHANNEL", "").strip()
FORCE_JOIN_URL = os.getenv("FORCE_JOIN_URL", "").strip()
LOG_CHANNEL_ID = os.getenv("LOG_CHANNEL_ID", "").strip()

ENABLE_UPI = os.getenv("ENABLE_UPI", "1").lower() not in ("0", "false", "no")
ENABLE_USDT = os.getenv("ENABLE_USDT", "1").lower() not in ("0", "false", "no")
MIN_WITHDRAWAL = float(os.getenv("MIN_WITHDRAWAL", "50"))
USD_TO_INR = float(os.getenv("USD_TO_INR", "90"))
DAILY_BONUS_USD = float(os.getenv("DAILY_BONUS_USD", "0.10"))
REFERRAL_REWARD_USD = float(os.getenv("REFERRAL_REWARD_USD", "0.10"))

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO
)
log = logging.getLogger("qr-claim-bot")

async def send_log(bot, text):
    """Send an operational/admin log to the configured Telegram log channel."""
    if not LOG_CHANNEL_ID:
        return
    try:
        await bot.send_message(LOG_CHANNEL_ID, text, parse_mode="HTML", disable_web_page_preview=True)
    except Exception:
        log.exception("Log channel send failed")

def display_name(user):
    return escape(user.full_name or "User")

def claim_name_button(user_id, name):
    return InlineKeyboardButton(f"👤 {name[:50]}", callback_data=f"claimant_{user_id}")


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
    return FileResponse(DASHBOARD_FILE)


@dashboard_app.get("/admin")
async def dashboard_admin():
    return FileResponse(DASHBOARD_FILE)


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
        paid = con.execute("SELECT COALESCE(SUM(inr_amount),0) x FROM withdrawals WHERE status='paid'").fetchone()["x"]
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
            user_text = f"🎉 <b>REWARD APPROVED</b>\n\n📱 App: {proof['app_name']}\n💰 Reward added: ${proof['reward']:.2f}\n\nYour reward has been added to your wallet."
        else:
            con.execute("UPDATE proofs SET status='rejected', reviewed_by=?, reviewed_at=CURRENT_TIMESTAMP WHERE id=?", (admin_id, proof_id))
            con.execute("UPDATE qrs SET status='rejected' WHERE id=?", (proof["qr_id"],))
            user_text = f"❌ <b>PROOF REJECTED</b>\n\n📱 App: {proof['app_name']}\nYour submitted proof was rejected by an admin."
        con.commit()
    if telegram_bot is not None:
        try:
            await telegram_bot.send_message(proof["user_id"], user_text, parse_mode="HTML")
        except Exception:
            pass
        result_text = (
            "🟢 <b>QR SUCCESS</b>\n\n"
            f"📱 App: {escape(proof['app_name'])}\n"
            f"💵 Reward: ${proof['reward']:.2f}"
            if action == "approve" else
            "🔴 <b>QR FAILED</b>\n\n"
            f"📱 App: {escape(proof['app_name'])}\n❌ Proof was rejected by admin."
        )
        await notify_qr_groups_for_bot(proof["qr_id"], result_text)
        await send_log(telegram_bot, f"{'🟢' if action == 'approve' else '🔴'} <b>QR RESULT</b>\nQR: #{proof['qr_id']}\nUser: <code>{proof['user_id']}</code>\nStatus: {'SUCCESS' if action == 'approve' else 'FAILED'}")
    return {"ok":True,"status":action,"proof_id":proof_id}


async def notify_qr_groups_for_bot(qr_id, text):
    if telegram_bot is None:
        return
    with closing(db()) as con:
        posts=con.execute("SELECT group_id,message_id FROM qr_posts WHERE qr_id=?",(qr_id,)).fetchall()
    for post in posts:
        try:
            await telegram_bot.edit_message_text(chat_id=post["group_id"], message_id=post["message_id"], text=text, parse_mode="HTML")
        except Exception:
            log.exception("Could not update QR result in group %s", post["group_id"])

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
        raise HTTPException(400, "Amount must be greater than $0 and at most $10,000.")

    with closing(db()) as con:
        con.execute("BEGIN IMMEDIATE")
        u = con.execute("SELECT id,balance FROM users WHERE id=?", (user_id,)).fetchone()
        if not u:
            con.rollback()
            raise HTTPException(404, "User not found. Ask the user to press /start first.")
        con.execute("UPDATE users SET balance=balance+? WHERE id=?", (amount, user_id))
        con.execute("INSERT INTO transactions(user_id,amount,kind) VALUES(?,?,?)", (user_id, amount, f"Admin Credit: {reason[:100]}"))
        new_balance = con.execute("SELECT balance FROM users WHERE id=?", (user_id,)).fetchone()["balance"]
        con.execute("INSERT INTO admin_actions(admin_id,action,target_user_id,details) VALUES(?,?,?,?)", (admin_id,"add_balance",user_id,reason))
        con.commit()

    if telegram_bot is not None:
        try:
            await telegram_bot.send_message(
                user_id,
                f"💰 <b>BALANCE ADDED</b>\n\n"
                f"➕ Added: ${amount:.2f}\n"
                f"📝 Reason: {reason}\n"
                f"💵 New balance: ${new_balance:.2f}",
                parse_mode="HTML"
            )
        except Exception:
            pass

    return {"ok": True, "user_id": user_id, "added": amount, "balance": new_balance, "admin_id": admin_id}


@dashboard_app.get("/api/admin/withdrawals")
async def dashboard_withdrawals(x_telegram_init_data: str | None = Header(default=None)):
    require_web_admin(x_telegram_init_data)
    with closing(db()) as con:
        rows = con.execute("""
            SELECT w.id,w.user_id,w.amount,w.inr_amount,w.conversion_rate,w.method,w.account,w.status,w.created_at,
                   COALESCE(NULLIF(u.username,''),NULLIF(u.name,''),CAST(w.user_id AS TEXT)) AS username
            FROM withdrawals w LEFT JOIN users u ON u.id=w.user_id
            ORDER BY w.id DESC LIMIT 100
        """).fetchall()
    return {"withdrawals":[dict(r) for r in rows]}


@dashboard_app.post("/api/admin/withdrawal")
async def dashboard_withdrawal_action(payload: dict, x_telegram_init_data: str | None = Header(default=None)):
    admin_id=require_web_admin(x_telegram_init_data)
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
    try:
        inr=w["inr_amount"] or round(w["amount"]*USD_TO_INR,2)
        if telegram_bot:
            if new_status == "processing": msg=f"🟡 <b>WITHDRAWAL PROCESSING</b>\n\n🆔 #{wid}\n🇮🇳 ₹{inr:.2f}\n🏦 Binance ID: <code>{escape(w['account'])}</code>"
            elif new_status == "paid": msg=f"🟢 <b>WITHDRAWAL PAID</b>\n\n🆔 #{wid}\n🇮🇳 ₹{inr:.2f}\n🏦 Binance ID: <code>{escape(w['account'])}</code>"
            else: msg=f"🔴 <b>WITHDRAWAL REJECTED</b>\n\n🆔 #{wid}\n🇮🇳 ₹{inr:.2f}\n💵 ${w['amount']:.2f} refunded to your wallet."
            await telegram_bot.send_message(w["user_id"],msg,parse_mode="HTML",reply_markup=None)
    except Exception: pass
    await send_log(telegram_bot,f"💸 <b>WITHDRAWAL ACTION</b>\nAdmin: <code>{admin_id}</code>\n#{wid}\nAction: {new_status}\nUser: <code>{w['user_id']}</code>\nINR: ₹{(w['inr_amount'] or w['amount']*USD_TO_INR):.2f}")
    return {"ok":True,"status":new_status}


@dashboard_app.get("/api/admin/users")
async def dashboard_users(q: str = "", x_telegram_init_data: str | None = Header(default=None)):
    require_web_admin(x_telegram_init_data)
    q=q.strip()
    with closing(db()) as con:
        if q:
            like=f"%{q}%"
            rows=con.execute("SELECT id,username,name,balance,total_earned,total_withdrawn,banned FROM users WHERE CAST(id AS TEXT)=? OR username LIKE ? OR name LIKE ? ORDER BY id DESC LIMIT 100",(q,like,like)).fetchall()
        else:
            rows=con.execute("SELECT id,username,name,balance,total_earned,total_withdrawn,banned FROM users ORDER BY id DESC LIMIT 100").fetchall()
    return {"users":[dict(r) for r in rows]}

@dashboard_app.post("/api/admin/deduct-balance")
async def dashboard_deduct_balance(payload: dict, x_telegram_init_data: str | None = Header(default=None)):
    admin_id=require_web_admin(x_telegram_init_data)
    uid=int(payload.get("user_id",0)); amount=float(payload.get("amount",0)); reason=str(payload.get("reason","Admin balance deduction")).strip() or "Admin balance deduction"
    if uid<=0 or amount<=0: raise HTTPException(400,"Invalid user or amount")
    with closing(db()) as con:
        con.execute("BEGIN IMMEDIATE")
        u=con.execute("SELECT balance FROM users WHERE id=?",(uid,)).fetchone()
        if not u: con.rollback(); raise HTTPException(404,"User not found")
        if u["balance"] < amount: con.rollback(); raise HTTPException(400,"Insufficient user balance")
        con.execute("UPDATE users SET balance=balance-? WHERE id=?",(amount,uid))
        con.execute("INSERT INTO transactions(user_id,amount,kind) VALUES(?,?,?)",(uid,-amount,f"Admin Deduction: {reason[:100]}"))
        con.execute("INSERT INTO admin_actions(admin_id,action,target_user_id,details) VALUES(?,?,?,?)",(admin_id,"deduct_balance",uid,reason))
        bal=con.execute("SELECT balance FROM users WHERE id=?",(uid,)).fetchone()["balance"]; con.commit()
    if telegram_bot:
        try: await telegram_bot.send_message(uid,f"💸 <b>BALANCE DEDUCTED</b>\n\n➖ Deducted: ${amount:.2f}\n📝 Reason: {escape(reason)}\n💵 New balance: ${bal:.2f}",parse_mode="HTML",reply_markup=None)
        except Exception: pass
    await send_log(telegram_bot,f"💸 <b>BALANCE DEDUCTED</b>\nAdmin: <code>{admin_id}</code>\nUser: <code>{uid}</code>\nAmount: ${amount:.2f}\nReason: {escape(reason)}")
    return {"ok":True,"balance":bal}

@dashboard_app.post("/api/admin/user-action")
async def dashboard_user_action(payload: dict, x_telegram_init_data: str | None = Header(default=None)):
    admin_id=require_web_admin(x_telegram_init_data)
    uid=int(payload.get("user_id",0)); action=str(payload.get("action",""))
    if action not in ("ban","unban"): raise HTTPException(400,"Invalid action")
    with closing(db()) as con:
        if not con.execute("SELECT 1 FROM users WHERE id=?",(uid,)).fetchone(): raise HTTPException(404,"User not found")
        banned=1 if action=="ban" else 0
        con.execute("UPDATE users SET banned=? WHERE id=?",(banned,uid))
        con.execute("INSERT INTO admin_actions(admin_id,action,target_user_id) VALUES(?,?,?)",(admin_id,action,uid)); con.commit()
    await send_log(telegram_bot,f"🚫 <b>USER {action.upper()}</b>\nAdmin: <code>{admin_id}</code>\nUser: <code>{uid}</code>")
    return {"ok":True,"banned":bool(banned)}

@dashboard_app.post("/api/admin/message-user")
async def dashboard_message_user(payload: dict, x_telegram_init_data: str | None = Header(default=None)):
    admin_id=require_web_admin(x_telegram_init_data); uid=int(payload.get("user_id",0)); text=str(payload.get("text","")).strip()
    if not text or len(text)>4096: raise HTTPException(400,"Invalid message")
    if telegram_bot is None: raise HTTPException(503,"Bot not ready")
    try: await telegram_bot.send_message(uid,text)
    except Exception as e: raise HTTPException(400,f"Could not message user: {e}")
    await send_log(telegram_bot,f"✉️ <b>USER MESSAGE SENT</b>\nAdmin: <code>{admin_id}</code>\nUser: <code>{uid}</code>")
    return {"ok":True}

@dashboard_app.post("/api/admin/broadcast")
async def dashboard_broadcast(payload: dict, x_telegram_init_data: str |
