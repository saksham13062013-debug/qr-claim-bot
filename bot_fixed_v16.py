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
async def dashboard_broadcast(payload: dict, x_telegram_init_data: str | None = Header(default=None)):
    admin_id=require_web_admin(x_telegram_init_data); text=str(payload.get("text","")).strip()
    if not text or len(text)>4096: raise HTTPException(400,"Broadcast must be 1-4096 characters")
    if telegram_bot is None: raise HTTPException(503,"Bot not ready")
    with closing(db()) as con: ids=[r[0] for r in con.execute("SELECT id FROM users WHERE banned=0").fetchall()]
    sent=failed=0
    for uid in ids:
        try: await telegram_bot.send_message(uid,text); sent+=1
        except Exception: failed+=1
    await send_log(telegram_bot,f"📢 <b>BROADCAST</b>\nAdmin: <code>{admin_id}</code>\nSent: {sent}\nFailed: {failed}")
    return {"ok":True,"sent":sent,"failed":failed}

@dashboard_app.get("/api/admin/settings")
async def dashboard_settings(x_telegram_init_data: str | None = Header(default=None)):
    require_web_admin(x_telegram_init_data)
    with closing(db()) as con:
        r=con.execute("SELECT value FROM settings WHERE key='force_join'").fetchone()
    fj=json.loads(r[0]) if r else {"channels":[],"urls":[]}
    return {"force_join":fj,"log_channel_configured":bool(LOG_CHANNEL_ID)}

@dashboard_app.post("/api/admin/force-join")
async def dashboard_force_join(payload: dict, x_telegram_init_data: str | None = Header(default=None)):
    admin_id=require_web_admin(x_telegram_init_data)
    channels=payload.get("channels") or ([payload.get("channel")] if payload.get("channel") else [])
    urls=payload.get("urls") or ([payload.get("url")] if payload.get("url") else [])
    channels=[str(x).strip() for x in channels if str(x).strip()]; urls=[str(x).strip() for x in urls if str(x).strip()]
    with closing(db()) as con:
        con.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('force_join',?)",(json.dumps({"channels":channels,"urls":urls}),))
        con.execute("INSERT INTO admin_actions(admin_id,action,details) VALUES(?,?,?)",(admin_id,"force_join_update",json.dumps({"channels":channels,"urls":urls}))); con.commit()
    await send_log(telegram_bot,f"🔒 <b>FORCE JOIN UPDATED</b>\nAdmin: <code>{admin_id}</code>\nChannels: {escape(', '.join(channels) or 'OFF')}")
    return {"ok":True,"force_join":{"channels":channels,"urls":urls}}

@dashboard_app.get("/api/admin/logs")
async def dashboard_logs(x_telegram_init_data: str | None = Header(default=None)):
    require_web_admin(x_telegram_init_data)
    with closing(db()) as con:
        rows=con.execute("SELECT * FROM admin_actions ORDER BY id DESC LIMIT 100").fetchall()
    return {"logs":[dict(r) for r in rows]}



# =========================
# EXTENDED ADMIN DASHBOARD API
# =========================
@dashboard_app.get("/api/admin/qrs")
async def dashboard_qrs(x_telegram_init_data: str | None = Header(default=None)):
    require_web_admin(x_telegram_init_data)
    with closing(db()) as con:
        rows=con.execute("""
            SELECT q.*, COALESCE(u.name,'') AS claimant_name
            FROM qrs q LEFT JOIN users u ON u.id=q.claimed_by
            ORDER BY q.id DESC LIMIT 100
        """).fetchall()
    return {"qrs":[dict(r) for r in rows]}

@dashboard_app.post("/api/admin/qr")
async def dashboard_qr_action(payload: dict, x_telegram_init_data: str | None = Header(default=None)):
    admin_id=require_web_admin(x_telegram_init_data)
    action=str(payload.get("action","")).lower()
    qid=int(payload.get("id",0)) if str(payload.get("id","")).strip() else 0
    if action=="create":
        file_id=str(payload.get("file_id","")).strip(); app_name=str(payload.get("app_name","")).strip(); reward=float(payload.get("reward",0))
        if not file_id or not app_name or reward<=0: raise HTTPException(400,"file_id, app name and positive USD reward are required")
        with closing(db()) as con:
            cur=con.execute("INSERT INTO qrs(file_id,app_name,reward,status,created_by) VALUES(?,?,?,'available',?)",(file_id,app_name[:200],reward,admin_id)); qid=cur.lastrowid; con.commit()
        await send_log(telegram_bot,f"📱 <b>QR CREATED FROM DASHBOARD</b>\nAdmin: <code>{admin_id}</code>\nQR: #{qid}\nApp: {escape(app_name)}\nReward: ${reward:.2f}")
        return {"ok":True,"id":qid}
    if qid<=0 or action not in ("enable","disable","delete"):
        raise HTTPException(400,"Invalid QR action")
    with closing(db()) as con:
        q=con.execute("SELECT * FROM qrs WHERE id=?",(qid,)).fetchone()
        if not q: raise HTTPException(404,"QR not found")
        if action=="delete":
            if q["claimed_by"] is not None or q["status"] in ("completed","rejected"): raise HTTPException(400,"Claimed/completed QR cannot be deleted")
            con.execute("DELETE FROM qrs WHERE id=?",(qid,))
        else:
            con.execute("UPDATE qrs SET status=? WHERE id=? AND claimed_by IS NULL",("available" if action=="enable" else "disabled",qid))
        con.execute("INSERT INTO admin_actions(admin_id,action,details) VALUES(?,?,?)",(admin_id,f"qr_{action}",f"QR #{qid}")); con.commit()
    await send_log(telegram_bot,f"📱 <b>QR {action.upper()}</b>\nAdmin: <code>{admin_id}</code>\nQR: #{qid}")
    return {"ok":True}

@dashboard_app.get("/api/admin/claims")
async def dashboard_claims(x_telegram_init_data: str | None = Header(default=None)):
    require_web_admin(x_telegram_init_data)
    with closing(db()) as con:
        rows=con.execute("""
          SELECT q.id,q.app_name,q.reward,q.status,q.claimed_by,q.claimed_at,
                 COALESCE(u.name,'') claimant_name,u.username
          FROM qrs q LEFT JOIN users u ON u.id=q.claimed_by
          WHERE q.claimed_by IS NOT NULL ORDER BY q.id DESC LIMIT 100
        """).fetchall()
    return {"claims":[dict(r) for r in rows]}

@dashboard_app.post("/api/admin/post-qr")
async def dashboard_post_qr(payload: dict, x_telegram_init_data: str | None = Header(default=None)):
    admin_id=require_web_admin(x_telegram_init_data)
    qid=int(payload.get("id",0))
    if not GROUP_IDS: raise HTTPException(400,"GROUP_IDS is not configured")
    if telegram_bot is None: raise HTTPException(503,"Bot is not ready")
    with closing(db()) as con:
        qr=con.execute("SELECT * FROM qrs WHERE id=? AND status='available' AND claimed_by IS NULL",(qid,)).fetchone()
    if not qr: raise HTTPException(404,"Available QR not found")
    posted=[]
    errors=[]
    markup=InlineKeyboardMarkup([[InlineKeyboardButton("💳 CLAIM QR",callback_data=f"claim_{qr['id']}")]])
    text=f"🎯 <b>QR CLAIM AVAILABLE</b>\n\n📱 App: {escape(qr['app_name'])}\n💵 Reward: ${qr['reward']:.2f}\n\nFirst eligible user to claim gets this QR in DM."
    for gid in GROUP_IDS:
        try:
            msg=await telegram_bot.send_message(gid,text,parse_mode="HTML",reply_markup=markup)
            posted.append((gid,msg.message_id))
        except Exception as exc:
            errors.append(f"{gid}: {exc}")
    if not posted:
        raise HTTPException(400,"Could not post QR to any configured group")
    with closing(db()) as con:
        con.execute("DELETE FROM qr_posts WHERE qr_id=?",(qid,))
        for gid,mid in posted:
            con.execute("INSERT OR REPLACE INTO qr_posts(qr_id,group_id,message_id) VALUES(?,?,?)",(qid,str(gid),mid))
        con.execute("UPDATE qrs SET posted_message_id=? WHERE id=?",(posted[0][1],qid))
        con.execute("INSERT INTO admin_actions(admin_id,action,details) VALUES(?,?,?)",(admin_id,"post_qr",f"QR #{qid}, groups={len(posted)}"))
        con.commit()
    await send_log(telegram_bot,f"📢 <b>QR POSTED TO GROUPS</b>\nAdmin: <code>{admin_id}</code>\nQR: #{qid}\nGroups: {len(posted)}")
    return {"ok":True,"posted":len(posted),"errors":errors}

@dashboard_app.get("/api/admin/admins")
async def dashboard_admins(x_telegram_init_data: str | None = Header(default=None)):
    require_web_admin(x_telegram_init_data)
    with closing(db()) as con: rows=con.execute("SELECT user_id,added_at FROM admins ORDER BY user_id").fetchall()
    return {"admins":[dict(r) for r in rows],"env_admins":sorted(ADMIN_IDS)}

@dashboard_app.post("/api/admin/admin")
async def dashboard_admin_action(payload: dict, x_telegram_init_data: str | None = Header(default=None)):
    admin_id=require_web_admin(x_telegram_init_data); action=str(payload.get("action","")); uid=int(payload.get("user_id",0))
    if uid<=0 or action not in ("add","remove"): raise HTTPException(400,"Invalid admin action")
    if action=="remove" and uid in ADMIN_IDS: raise HTTPException(400,"ADMIN_IDS entry cannot be removed here")
    with closing(db()) as con:
        if action=="add": con.execute("INSERT OR IGNORE INTO admins(user_id) VALUES(?)",(uid,))
        else: con.execute("DELETE FROM admins WHERE user_id=?",(uid,))
        con.execute("INSERT INTO admin_actions(admin_id,action,target_user_id) VALUES(?,?,?)",(admin_id,f"admin_{action}",uid)); con.commit()
    await send_log(telegram_bot,f"👮 <b>ADMIN {action.upper()}</b>\nBy: <code>{admin_id}</code>\nUser: <code>{uid}</code>")
    return {"ok":True}

@dashboard_app.get("/api/admin/giftcodes")
async def dashboard_giftcodes(x_telegram_init_data: str | None = Header(default=None)):
    require_web_admin(x_telegram_init_data)
    with closing(db()) as con: rows=con.execute("SELECT * FROM gift_codes ORDER BY rowid DESC LIMIT 100").fetchall()
    return {"giftcodes":[dict(r) for r in rows]}

@dashboard_app.post("/api/admin/giftcode")
async def dashboard_giftcode(payload: dict, x_telegram_init_data: str | None = Header(default=None)):
    admin_id=require_web_admin(x_telegram_init_data); action=str(payload.get("action","create")); code=str(payload.get("code","")).strip().upper()
    if action=="delete":
        if not code: raise HTTPException(400,"Code required")
        with closing(db()) as con: con.execute("DELETE FROM gift_codes WHERE code=?",(code,)); con.execute("INSERT INTO admin_actions(admin_id,action,details) VALUES(?,?,?)",(admin_id,"gift_delete",code)); con.commit()
        return {"ok":True}
    amount=float(payload.get("amount",0)); uses=int(payload.get("uses",1)); expires=str(payload.get("expires_at","")).strip() or None
    if not code or amount<=0 or uses<1: raise HTTPException(400,"Invalid gift code")
    with closing(db()) as con:
        con.execute("INSERT OR REPLACE INTO gift_codes(code,amount,max_uses,uses,expires_at) VALUES(?,?,?,0,?)",(code,amount,uses,expires)); con.execute("INSERT INTO admin_actions(admin_id,action,details) VALUES(?,?,?)",(admin_id,"gift_create",f"{code} ${amount:.2f} uses={uses}")); con.commit()
    await send_log(telegram_bot,f"🎟️ <b>GIFT CODE CREATED</b>\nAdmin: <code>{admin_id}</code>\nCode: <code>{escape(code)}</code>\nAmount: ${amount:.2f}\nUses: {uses}")
    return {"ok":True}

@dashboard_app.get("/api/admin/analytics")
async def dashboard_analytics(x_telegram_init_data: str | None = Header(default=None)):
    require_web_admin(x_telegram_init_data)
    with closing(db()) as con:
        total_balance=con.execute("SELECT COALESCE(SUM(balance),0) x FROM users").fetchone()["x"]
        total_earned=con.execute("SELECT COALESCE(SUM(total_earned),0) x FROM users").fetchone()["x"]
        total_withdrawn=con.execute("SELECT COALESCE(SUM(total_withdrawn),0) x FROM users").fetchone()["x"]
        qr_rewards=con.execute("SELECT COALESCE(SUM(reward),0) x FROM qrs WHERE claimed_by IS NOT NULL").fetchone()["x"]
        paid_inr=con.execute("SELECT COALESCE(SUM(inr_amount),0) x FROM withdrawals WHERE status='paid'").fetchone()["x"]
        daily=con.execute("SELECT substr(created_at,1,10) day, COUNT(*) count FROM users GROUP BY day ORDER BY day DESC LIMIT 14").fetchall()
    return {"total_balance":total_balance,"total_earned":total_earned,"total_withdrawn":total_withdrawn,"qr_rewards":qr_rewards,"paid_inr":paid_inr,"daily_users":[dict(x) for x in daily]}

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

        CREATE TABLE IF NOT EXISTS qr_posts(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            qr_id INTEGER NOT NULL,
            group_id TEXT NOT NULL,
            message_id INTEGER NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(qr_id, group_id)
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

        CREATE TABLE IF NOT EXISTS admin_actions(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            admin_id INTEGER,
            action TEXT,
            target_user_id INTEGER,
            details TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS referrals(
            user_id INTEGER PRIMARY KEY,
            referrer_id INTEGER,
            reward_paid REAL DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS daily_bonuses(
            user_id INTEGER PRIMARY KEY,
            claimed_at TEXT
        );
        CREATE TABLE IF NOT EXISTS gift_codes(
            code TEXT PRIMARY KEY, amount REAL NOT NULL, max_uses INTEGER DEFAULT 1, uses INTEGER DEFAULT 0, expires_at TEXT
        );
        CREATE TABLE IF NOT EXISTS gift_redemptions(
            code TEXT, user_id INTEGER, redeemed_at TEXT DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(code,user_id)
        );
        CREATE TABLE IF NOT EXISTS missions(
            id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, description TEXT, reward REAL, target INTEGER DEFAULT 1, active INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS mission_progress(
            mission_id INTEGER, user_id INTEGER, progress INTEGER DEFAULT 0, completed INTEGER DEFAULT 0, PRIMARY KEY(mission_id,user_id)
        );
        """)
        # Backward-compatible migrations for older databases.
        cols = {r[1] for r in con.execute("PRAGMA table_info(users)").fetchall()}
        if "banned" not in cols: con.execute("ALTER TABLE users ADD COLUMN banned INTEGER DEFAULT 0")
        wcols = {r[1] for r in con.execute("PRAGMA table_info(withdrawals)").fetchall()}
        if "inr_amount" not in wcols: con.execute("ALTER TABLE withdrawals ADD COLUMN inr_amount REAL")
        if "conversion_rate" not in wcols: con.execute("ALTER TABLE withdrawals ADD COLUMN conversion_rate REAL")
        if "admin_note" not in wcols: con.execute("ALTER TABLE withdrawals ADD COLUMN admin_note TEXT")
        qcols = {r[1] for r in con.execute("PRAGMA table_info(qrs)").fetchall()}
        if "created_by" not in qcols: con.execute("ALTER TABLE qrs ADD COLUMN created_by INTEGER")
        if "group_id" not in qcols: con.execute("ALTER TABLE qrs ADD COLUMN group_id TEXT")
        pcols = {r[1] for r in con.execute("PRAGMA table_info(proofs)").fetchall()}
        if "created_at" not in pcols: con.execute("ALTER TABLE proofs ADD COLUMN created_at TEXT DEFAULT CURRENT_TIMESTAMP")
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


def is_banned(user_id: int) -> bool:
    with closing(db()) as con:
        row = con.execute("SELECT banned FROM users WHERE id=?", (user_id,)).fetchone()
    return bool(row and row["banned"])

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
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("WALLET", callback_data="wallet"), InlineKeyboardButton("REWARDS", callback_data="rewards")],
        [InlineKeyboardButton("WITHDRAW", callback_data="withdraw"), InlineKeyboardButton("ORDER HISTORY", callback_data="order_history")],
        [InlineKeyboardButton("PROFILE", callback_data="profile"), InlineKeyboardButton("HELP", callback_data="help")]
    ])


def back_button():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 BACK TO MENU", callback_data="home")]
    ])


def admin_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 DASHBOARD", callback_data="admin_dashboard"), InlineKeyboardButton("💸 WITHDRAWALS", callback_data="admin_withdrawals")],
        [InlineKeyboardButton("📱 QR MANAGEMENT", callback_data="admin_qrs"), InlineKeyboardButton("📋 CLAIMS", callback_data="admin_claims")],
        [InlineKeyboardButton("🧾 PROOF REVIEW", callback_data="admin_proofs"), InlineKeyboardButton("👥 USERS", callback_data="admin_users")],
        [InlineKeyboardButton("➕ ADD QR", callback_data="admin_addqr"), InlineKeyboardButton("📢 POST QR", callback_data="admin_postqr")],
        [InlineKeyboardButton("👮 ADMINS", callback_data="admin_admins"), InlineKeyboardButton("🎟️ GIFT CODES", callback_data="admin_gifts")],
        [InlineKeyboardButton("📢 BROADCAST", callback_data="admin_broadcast"), InlineKeyboardButton("🔒 FORCE JOIN", callback_data="admin_forcejoin")],
        [InlineKeyboardButton("📝 LOGS", callback_data="admin_logs"), InlineKeyboardButton("📈 ANALYTICS", callback_data="admin_analytics")],
        [InlineKeyboardButton("⚙️ SETTINGS", callback_data="admin_settings")],
        [InlineKeyboardButton("🏠 USER MENU", callback_data="home")]
    ])


# =========================
# FORCE JOIN
# =========================
async def force_join_ok(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    with closing(db()) as con:
        setting = con.execute("SELECT value FROM settings WHERE key='force_join'").fetchone()
    fj = json.loads(setting["value"]) if setting and setting["value"] else {}
    channels = fj.get("channels") or ([] if not FORCE_JOIN_CHANNEL else [FORCE_JOIN_CHANNEL])
    urls = fj.get("urls") or ([] if not FORCE_JOIN_URL else [FORCE_JOIN_URL])
    if not channels:
        return True

    user = update.effective_user
    chat_id = update.effective_chat.id

    missing = []
    for i, channel in enumerate(channels):
        try:
            member = await context.bot.get_chat_member(channel, user.id)
            if member.status not in ("member", "administrator", "creator"):
                missing.append(i)
        except Exception:
            log.warning("Force join check failed for %s", channel)
            missing.append(i)
    if not missing:
        return True

    rows=[]
    for i in missing:
        channel=channels[i]
        url=(urls[i] if i < len(urls) else "") or (f"https://t.me/{channel.lstrip('@')}" if str(channel).startswith("@") else "")
        if url:
            rows.append([InlineKeyboardButton(f"📢 JOIN CHANNEL {i+1}", url=url)])
    rows.append([InlineKeyboardButton("✅ CHECK JOIN", callback_data="check_join")])
    if update.message:
        await update.message.reply_text(
            "🔒 <b>Channel Join Required</b>\n\nJoin all required channels, then press <b>CHECK JOIN</b>.",
            parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows)
        )
    return False


# =========================
# USER PAGES
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return

    ensure_user(update.effective_user)
    # Process a referral deep-link once.
    if context.args and context.args[0].startswith("ref_"):
        try:
            ref=int(context.args[0].split("_",1)[1])
            if ref != update.effective_user.id:
                with closing(db()) as con:
                    if not con.execute("SELECT 1 FROM referrals WHERE user_id=?",(update.effective_user.id,)).fetchone() and con.execute("SELECT 1 FROM users WHERE id=?",(ref,)).fetchone():
                        con.execute("INSERT INTO referrals(user_id,referrer_id,reward_paid) VALUES(?,?,?)",(update.effective_user.id,ref,REFERRAL_REWARD_USD))
                        con.execute("UPDATE users SET balance=balance+?,total_earned=total_earned+? WHERE id=?",(REFERRAL_REWARD_USD,REFERRAL_REWARD_USD,ref))
                        con.execute("INSERT INTO transactions(user_id,amount,kind) VALUES(?,?,?)",(ref,REFERRAL_REWARD_USD,"Referral Reward")); con.commit()
                        await context.bot.send_message(ref,f"🟢 New referral joined! ${REFERRAL_REWARD_USD:.2f} referral reward added.")
        except Exception: pass
    if is_banned(update.effective_user.id):
        await update.message.reply_text("🚫 Your account is banned from using this bot.")
        return

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
        f"💰 <b>Balance:</b> ${balance:.2f}"
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
        f"💰 <b>Balance:</b> ${balance:.2f}"
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
        f"💵 Available Balance: ${r['balance']:.2f}\n"
        f"🏆 Total Earned: ${r['total_earned']:.2f}\n"
        f"💸 Total Withdrawn: ${r['total_withdrawn']:.2f}\n"
        f"⏳ Pending Withdrawal: ${p['x']:.2f}"
    )
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("💸 WITHDRAW", callback_data="withdraw"),
            InlineKeyboardButton("ORDER HISTORY", callback_data="order_history")
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
        f"🟢 Approved: ${approved:.2f}\n"
        f"🟡 Pending: ${pending:.2f}\n"
        f"❌ Rejected: ${rejected:.2f}\n\n"
        "<b>Recent approved QR rewards:</b>\n"
    )
    text += (
        "\n".join(f"➕ ${r['amount']:.2f} — {r['created_at']}" for r in rows)
        if rows else "No approved rewards yet."
    )

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("ORDER HISTORY", callback_data="order_history")],
        [InlineKeyboardButton("💰 WALLET", callback_data="wallet")],
        [InlineKeyboardButton("🔙 BACK TO MENU", callback_data="home")]
    ])
    await q.edit_message_text(text, parse_mode="HTML", reply_markup=kb)


async def order_history(update, context):
    q = update.callback_query
    await q.answer("Loading order history…")
    ensure_user(q.from_user)
    page = int(context.user_data.get("order_page", 0))
    per_page = 8
    offset = page * per_page
    with closing(db()) as con:
        rows = con.execute("""
            SELECT q.id, q.app_name, q.reward, q.status, q.claimed_at,
                   COALESCE((SELECT p.status FROM proofs p WHERE p.qr_id=q.id AND p.user_id=? ORDER BY p.id DESC LIMIT 1),'') AS confirmation_status
            FROM qrs q
            WHERE q.claimed_by=?
            ORDER BY q.id DESC LIMIT ? OFFSET ?
        """, (q.from_user.id, q.from_user.id, per_page, offset)).fetchall()
        total = con.execute("SELECT COUNT(*) x FROM qrs WHERE claimed_by=?", (q.from_user.id,)).fetchone()["x"]
    if not rows:
        text = "<b>ORDER HISTORY</b>\n\nNo QR orders yet."
    else:
        parts=[]
        for r in rows:
            st = (r["status"] or "unknown").upper()
            if r["confirmation_status"] == "pending" and st == "CLAIMED": st = "CONFIRMATION PENDING"
            elif r["confirmation_status"] == "approved": st = "SUCCESS"
            elif r["confirmation_status"] == "rejected": st = "FAILED"
            parts.append(f"<b>Order #{r['id']}</b>\nApp: {escape(r['app_name'])}\nReward: ${r['reward']:.2f}\nStatus: <b>{escape(st)}</b>\nDate: {escape(str(r['claimed_at'] or '-'))}")
        text = "<b>ORDER HISTORY</b>\n\n" + "\n\n".join(parts)
    buttons=[]
    if page>0: buttons.append(InlineKeyboardButton("PREVIOUS", callback_data="order_prev"))
    if offset+per_page<total: buttons.append(InlineKeyboardButton("NEXT", callback_data="order_next"))
    rows_kb=[buttons] if buttons else []
    rows_kb.append([InlineKeyboardButton("BACK TO MENU", callback_data="home")])
    await q.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows_kb))


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
            + f"${abs(r['amount']):.2f} — {r['kind']}"
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
        f"💰 Balance: ${r['balance']:.2f}\n"
        f"🎁 Total Rewards: ${r['total_earned']:.2f}\n"
        f"💸 Total Withdrawn: ${r['total_withdrawn']:.2f}"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("ORDER HISTORY", callback_data="order_history")],
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
        r = con.execute("SELECT balance FROM users WHERE id=?", (q.from_user.id,)).fetchone()
    context.user_data["withdraw_stage"] = "amount"
    await q.edit_message_text(
        f"💸 <b>INR WITHDRAWAL</b>\n\n💵 Available USD Balance: ${r['balance']:.2f}\n💱 Rate: $1 = ₹{USD_TO_INR:.2f}\n📌 Minimum: ₹{MIN_WITHDRAWAL:.0f}\n\nEnter the INR amount you want to withdraw.",
        parse_mode="HTML", reply_markup=back_button())

async def choose_withdraw_method(update, context):
    q = update.callback_query
    await q.answer()
    context.user_data["withdraw_method"] = "BINANCE"
    context.user_data["withdraw_stage"] = "account"
    await q.edit_message_text(
        "🏦 <b>BINANCE WITHDRAWAL</b>\n\nSend your Binance ID / UID.",
        parse_mode="HTML", reply_markup=back_button())

async def create_withdrawal(update, context, account):
    uid = update.effective_user.id
    inr_amount = float(context.user_data["withdraw_inr"])
    usd_amount = round(inr_amount / USD_TO_INR, 8)
    method = "BINANCE"
    with closing(db()) as con:
        con.execute("BEGIN IMMEDIATE")
        user = con.execute("SELECT balance FROM users WHERE id=?", (uid,)).fetchone()
        if not user or user["balance"] < usd_amount:
            con.rollback(); await update.message.reply_text("🔴 Insufficient USD balance."); context.user_data.clear(); return
        con.execute("UPDATE users SET balance=balance-? WHERE id=?", (usd_amount, uid))
        cur=con.execute("INSERT INTO withdrawals(user_id,amount,inr_amount,conversion_rate,method,account,status) VALUES(?,?,?,?,?,?, 'pending')", (uid,usd_amount,inr_amount,USD_TO_INR,method,account))
        wid=cur.lastrowid
        con.execute("INSERT INTO transactions(user_id,amount,kind) VALUES(?,?,?)", (uid,-usd_amount,"Withdrawal Hold"))
        con.commit()
    context.user_data.clear()
    await update.message.reply_text(f"🟢 <b>WITHDRAWAL CREATED</b>\n\n🆔 #{wid}\n💵 USD deducted: ${usd_amount:.2f}\n🇮🇳 INR amount: ₹{inr_amount:.2f}\n🏦 Binance ID: <code>{escape(account)}</code>\n📌 Status: <b>PENDING</b>", parse_mode="HTML")
    await send_log(context.bot, f"💸 <b>NEW WITHDRAWAL</b>\n#{wid} | User <code>{uid}</code> | ₹{inr_amount:.2f} / ${usd_amount:.2f} | Binance <code>{escape(account)}</code>")
    for aid in get_admin_ids():
        try:
            await context.bot.send_message(aid, f"💸 <b>NEW WITHDRAWAL</b>\n\n🆔 #{wid}\n👤 User: <code>{uid}</code>\n🇮🇳 INR: ₹{inr_amount:.2f}\n💵 USD: ${usd_amount:.2f}\n🏦 Binance ID: <code>{escape(account)}</code>\n🟡 Status: PENDING", parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🟡 PROCESSING", callback_data=f"wprocessing_{wid}")],[InlineKeyboardButton("🔴 REJECT", callback_data=f"wreject_{wid}"),InlineKeyboardButton("🟢 PAID", callback_data=f"wpaid_{wid}")]]))
        except Exception: pass


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
    try:
        qr_id = int(q.data.split("_", 1)[1]) if "_" in q.data else None
    except Exception:
        qr_id = None

    if q.message.chat.type != "private":
        # Claim button is intentionally posted in a group. Continue.
        pass

    uid = q.from_user.id
    ensure_user(q.from_user)
    if is_banned(uid):
        await q.answer("🚫 You are banned.", show_alert=True)
        return

    # Atomically reserve exactly one QR.
    with closing(db()) as con:
        con.execute("BEGIN IMMEDIATE")
        qr = con.execute("""
            SELECT * FROM qrs
            WHERE status='available'
            AND (? IS NULL OR id=?)
            ORDER BY id
            LIMIT 1
        """, (qr_id, qr_id)).fetchone()

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
                f"💵 Reward: ${qr['reward']:.2f}\n\n"
                "Complete the required payment/action, then press the button below."
            ),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    "🟢 CONFIRM PAYMENT",
                    callback_data=f"confirm_{qr['id']}"
                )]
            ])
        )

        # Only the group claim post is edited. Never send the user menu to the group.
        name = q.from_user.full_name or "User"
        group_text = (
            "🔒 <b>QR CLAIMED</b>\n\n"
            f"👤 Claimed by: <b>{escape(name)}</b>"
        )
        claimant_markup=InlineKeyboardMarkup([[claim_name_button(uid, name)]])
        # Update every group where this QR was posted.
        with closing(db()) as con:
            posts=con.execute("SELECT group_id,message_id FROM qr_posts WHERE qr_id=?",(qr["id"],)).fetchall()
        if posts:
            for post in posts:
                try:
                    await context.bot.edit_message_text(
                        chat_id=post["group_id"], message_id=post["message_id"],
                        text=group_text, parse_mode="HTML", reply_markup=claimant_markup
                    )
                except Exception:
                    log.exception("Could not update claimed QR post in %s", post["group_id"])
        else:
            try:
                await q.edit_message_text(group_text, parse_mode="HTML", reply_markup=claimant_markup)
            except Exception:
                pass
        await send_log(context.bot, f"🟢 <b>QR CLAIM SUCCESS</b>\nQR: #{qr['id']}\nUser: {escape(name)}\nID: <code>{uid}</code>")

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


async def confirm_payment(update, context):
    q = update.callback_query
    await q.answer("Submitting for admin review…", show_alert=False)
    try:
        qr_id = int(q.data.split("_", 1)[1])
    except Exception:
        await q.answer("🔴 Invalid QR.", show_alert=True); return
    uid = q.from_user.id
    ensure_user(q.from_user)
    with closing(db()) as con:
        qr = con.execute("SELECT * FROM qrs WHERE id=? AND claimed_by=? AND status='claimed'", (qr_id, uid)).fetchone()
        if not qr:
            await q.answer("🔴 This QR is not assigned to you or is already reviewed.", show_alert=True); return
        existing = con.execute("SELECT id,status FROM proofs WHERE qr_id=? AND user_id=? AND status IN ('pending','approved') ORDER BY id DESC LIMIT 1", (qr_id, uid)).fetchone()
        if existing:
            await q.edit_message_reply_markup(reply_markup=None)
            await q.message.reply_text("🟡 <b>UNDER REVIEW</b>\n\nYour confirmation is already with the admin.", parse_mode="HTML")
            return
        con.execute("INSERT INTO proofs(qr_id,user_id,file_id,status) VALUES(?,?,NULL,'pending')", (qr_id, uid))
        con.commit()
    name = q.from_user.full_name or "User"
    await send_log(context.bot, f"🧾 <b>PAYMENT CONFIRMATION SUBMITTED</b>\nQR: #{qr_id}\nUser: {escape(name)}\nID: <code>{uid}</code>")
    await q.edit_message_reply_markup(reply_markup=None)
    await q.message.reply_text("🟡 <b>UNDER REVIEW</b>\n\nYour payment confirmation has been sent to the admin for review.", parse_mode="HTML")
    for aid in get_admin_ids():
        try:
            await context.bot.send_message(aid, f"📥 <b>NEW PAYMENT CONFIRMATION</b>\n\n👤 User: {escape(name)}\n🆔 <code>{uid}</code>\n📱 QR: #{qr_id}\n💵 Reward: ${qr['reward']:.2f}\n📱 App: {escape(qr['app_name'])}\n\n🟡 Waiting for review.", parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🟢 APPROVE", callback_data=f"approve_{qr_id}_{uid}"), InlineKeyboardButton("🔴 REJECT", callback_data=f"reject_{qr_id}_{uid}")]]))
        except Exception:
            log.exception("Could not notify admin %s", aid)


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
            )
            return

        con.execute("""
            INSERT INTO proofs(qr_id,user_id,file_id,status)
            VALUES(?,?,?,'pending')
        """, (qr_id, q.from_user.id, file_id))

        con.commit()

    await send_log(context.bot, f"🧾 <b>PROOF SUBMITTED</b>\nUser: <code>{q.from_user.id}</code>\nQR: #{qr_id}")
    context.user_data["proof_stage"] = "done"

    await q.edit_message_text(
        "⏳ <b>UNDER REVIEW</b>\n\n"
        "Your proof has been submitted. Please wait for admin review.",
        parse_mode="HTML",
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
                    f"💰 Reward: ${qr['reward']:.2f}"
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


async def notify_qr_groups(context, qr_id, text):
    """Update all group posts for a QR with a final success/fail status."""
    with closing(db()) as con:
        posts=con.execute("SELECT group_id,message_id FROM qr_posts WHERE qr_id=?",(qr_id,)).fetchall()
    for post in posts:
        try:
            await context.bot.edit_message_text(
                chat_id=post["group_id"], message_id=post["message_id"],
                text=text, parse_mode="HTML"
            )
        except Exception:
            log.exception("Could not update QR result in group %s", post["group_id"])


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
                f"💰 Reward added: ${proof['reward']:.2f}\n\n"
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

    # Update every group with the final QR result.
    if approved:
        group_result = (
            "🟢 <b>QR SUCCESS</b>\n\n"
            f"📱 App: {escape(proof['app_name'])}\n"
            f"💰 Reward: ${proof['reward']:.2f}\n"
            f"👤 User ID: <code>{uid}</code>\n\n"
            "✅ Proof approved successfully."
        )
    else:
        group_result = (
            "🔴 <b>QR FAILED</b>\n\n"
            f"📱 App: {escape(proof['app_name'])}\n"
            f"👤 User ID: <code>{uid}</code>\n\n"
            "❌ Proof was rejected."
        )
    await notify_qr_groups(context, qr_id, group_result)

    await send_log(
        context.bot,
        f"{'🟢' if approved else '🔴'} <b>QR {'SUCCESS' if approved else 'FAILED'}</b>\n"
        f"QR: #{qr_id} | User: <code>{uid}</code> | Admin: <code>{q.from_user.id}</code>"
    )

    try:
        await context.bot.send_message(
            uid, user_text, parse_mode="HTML"
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

    with closing(db()) as con:
        user_row = con.execute("SELECT name FROM users WHERE id=?", (uid,)).fetchone()
    claimant_name = (user_row["name"] if user_row and user_row["name"] else "User")
    result_text = (
        "🟢 <b>QR SUCCESS</b>\n\n"
        f"📱 App: {escape(proof['app_name'])}\n"
        f"💵 Reward: ${proof['reward']:.2f}\n\n"
        f"👤 Claimed by: <b>{escape(claimant_name)}</b>"
        if approved else
        "🔴 <b>QR FAILED</b>\n\n"
        f"📱 App: {escape(proof['app_name'])}\n"
        "❌ Proof was rejected by admin."
    )
    await notify_qr_groups(context, qr_id, result_text)
    await send_log(context.bot, f"{'🟢' if approved else '🔴'} <b>QR RESULT</b>\nQR: #{qr_id}\nUser: <code>{uid}</code>\nStatus: {'SUCCESS' if approved else 'FAILED'}")


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
            "Enter reward amount in USD, e.g. 0.50"
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
                INSERT INTO qrs(file_id,app_name,reward,status,created_by)
                VALUES(?,?,?,'available',?)
            """, (file_id, app_name, reward, uid))
            qr_id = cur.lastrowid
            con.commit()

        context.user_data.clear()
        await send_log(context.bot, f"📱 <b>QR CREATED</b>\nAdmin: <code>{uid}</code>\nQR: #{qr_id}\nApp: {escape(app_name)}\nReward: ${reward:.2f}")

        await update.message.reply_text(
            f"✅ <b>QR CREATED</b>\n\n"
            f"🆔 QR ID: #{qr_id}\n"
            f"📱 App: {app_name}\n"
            f"💰 Reward: ${reward:.2f}\n\n"
            "Use <b>/postqr</b> or <b>/postqr QR_ID</b> to post a specific QR.",
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

    # Keep the normal Telegram admin controls available even if the Web App
    # URL is misconfigured. A malformed WebAppInfo used to crash the handler.
    if WEBAPP_URL:
        try:
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
            return
        except Exception:
            log.exception("Failed to create Telegram WebApp button for WEBAPP_URL=%r", WEBAPP_URL)

    await update.message.reply_text(
        "⚠️ <b>Web dashboard URL is not usable.</b>\n\n"
        "Use the buttons below for admin controls.\n"
        "Set Railway <code>WEBAPP_URL</code> to the full HTTPS Railway URL, e.g. "
        "<code>https://your-service.up.railway.app</code>.",
        parse_mode="HTML",
        reply_markup=admin_menu()
    )


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
        f"💰 Paid Amount: ${paid_amount:.2f}\n"
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
                f"💰 ${w['amount']:.2f} | {w['method']}\n"
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

    await send_log(context.bot, f"💸 <b>WITHDRAWAL ACTION</b>\nAdmin: <code>{q.from_user.id}</code>\nWithdrawal: #{wid}\nAction: {new_status}\nUser: <code>{w['user_id']}</code>\nINR: ₹{(w['inr_amount'] or w['amount']*USD_TO_INR):.2f}")

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
            f"🇮🇳 INR: ₹{(w['inr_amount'] or w['amount']*USD_TO_INR):.2f}\n💵 USD: ${w['amount']:.2f}\n"
            f"📌 Status: <b>{labels[new_status]}</b>",
            parse_mode="HTML"
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
            SELECT id,app_name,reward,status,claimed_by,created_by
            FROM qrs ORDER BY id DESC LIMIT 15
        """).fetchall()

    text = "📱 <b>QR MANAGEMENT</b>\n\n"
    if rows:
        for r in rows:
            claimed = f" | 👤 {r['claimed_by']}" if r["claimed_by"] else ""
            text += (
                f"#{r['id']} — {r['app_name']}\n"
                f"💰 ${r['reward']:.2f} | 📌 {r['status']}{claimed}\n"
                f"👮 Created by admin: <code>{r['created_by'] or 'unknown'}</code>\n\n"
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
                f"💰 ${r['reward']:.2f}\n"
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
            f"💰 ${r['reward']:.2f}"
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
            f"🆔 {r['id']} | 💰 ${r['balance']:.2f}\n\n"
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

    await send_log(context.bot, f"👮 <b>ADMIN ADDED</b>\nBy: <code>{update.effective_user.id}</code>\nAdmin: <code>{uid}</code>")
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

    await send_log(context.bot, f"👮 <b>ADMIN REMOVED</b>\nBy: <code>{update.effective_user.id}</code>\nAdmin: <code>{uid}</code>")
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
        await update.message.reply_text("❌ User ID must be valid and amount must be between $0.01 and $10,000.")
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
            f"💰 <b>BALANCE ADDED</b>\n\n➕ Added: ${amount:.2f}\n📝 Reason: {reason}\n💵 New balance: ${new_balance:.2f}",
            parse_mode="HTML"
        )
    except Exception:
        pass

    await send_log(context.bot, f"💰 <b>BALANCE ADDED</b>\nAdmin: <code>{update.effective_user.id}</code>\nUser: <code>{uid}</code>\nAmount: ${amount:.2f}\nReason: {escape(reason)}")

    await update.message.reply_text(
        f"✅ <b>BALANCE ADDED</b>\n\n👤 User ID: <code>{uid}</code>\n➕ Added: ${amount:.2f}\n💵 New balance: ${new_balance:.2f}\n📝 Reason: {reason}",
        parse_mode="HTML", reply_markup=admin_menu()
    )


async def message_user_cmd(update, context):
    if update.effective_chat.type != "private" or not is_admin(update.effective_user.id): return
    parts=update.message.text.split(maxsplit=2)
    if len(parts)<3 or not parts[1].isdigit():
        await update.message.reply_text("Usage: /message USER_ID MESSAGE", reply_markup=admin_menu()); return
    uid=int(parts[1]); text=parts[2]
    try: await context.bot.send_message(uid,text)
    except Exception as e:
        await update.message.reply_text(f"❌ Failed: {e}", reply_markup=admin_menu()); return
    await send_log(context.bot,f"✉️ <b>MESSAGE SENT</b>\nAdmin: <code>{update.effective_user.id}</code>\nUser: <code>{uid}</code>")
    await update.message.reply_text("✅ Message sent.", reply_markup=admin_menu())

async def ban_cmd(update, context):
    if update.effective_chat.type != "private" or not is_admin(update.effective_user.id): return
    parts=update.message.text.split()
    if len(parts)!=2 or not parts[1].isdigit(): await update.message.reply_text("Usage: /ban USER_ID", reply_markup=admin_menu()); return
    uid=int(parts[1])
    with closing(db()) as con:
        con.execute("UPDATE users SET banned=1 WHERE id=?",(uid,)); con.execute("INSERT INTO admin_actions(admin_id,action,target_user_id) VALUES(?,?,?)",(update.effective_user.id,"ban",uid)); con.commit()
    await send_log(context.bot,f"🚫 <b>USER BANNED</b>\nAdmin: <code>{update.effective_user.id}</code>\nUser: <code>{uid}</code>")
    await update.message.reply_text(f"🚫 Banned <code>{uid}</code>",parse_mode="HTML",reply_markup=admin_menu())

async def unban_cmd(update, context):
    if update.effective_chat.type != "private" or not is_admin(update.effective_user.id): return
    parts=update.message.text.split()
    if len(parts)!=2 or not parts[1].isdigit(): await update.message.reply_text("Usage: /unban USER_ID", reply_markup=admin_menu()); return
    uid=int(parts[1])
    with closing(db()) as con:
        con.execute("UPDATE users SET banned=0 WHERE id=?",(uid,)); con.execute("INSERT INTO admin_actions(admin_id,action,target_user_id) VALUES(?,?,?)",(update.effective_user.id,"unban",uid)); con.commit()
    await send_log(context.bot,f"✅ <b>USER UNBANNED</b>\nAdmin: <code>{update.effective_user.id}</code>\nUser: <code>{uid}</code>")
    await update.message.reply_text(f"✅ Unbanned <code>{uid}</code>",parse_mode="HTML",reply_markup=admin_menu())

async def broadcast_cmd(update, context):
    if update.effective_chat.type != "private" or not is_admin(update.effective_user.id): return
    parts=update.message.text.split(maxsplit=1)
    if len(parts)<2: await update.message.reply_text("Usage: /broadcast MESSAGE", reply_markup=admin_menu()); return
    text=parts[1]
    with closing(db()) as con: ids=[r[0] for r in con.execute("SELECT id FROM users WHERE banned=0").fetchall()]
    sent=failed=0
    for uid in ids:
        try: await context.bot.send_message(uid,text); sent+=1
        except Exception: failed+=1
    await send_log(context.bot,f"📢 <b>BROADCAST</b>\nAdmin: <code>{update.effective_user.id}</code>\nSent: {sent}\nFailed: {failed}")
    await update.message.reply_text(f"📢 Broadcast complete.\n\n✅ Sent: {sent}\n❌ Failed: {failed}",reply_markup=admin_menu())

async def admin_settings(update, context):
    q = update.callback_query
    await q.answer()

    if not is_admin(q.from_user.id):
        return

    text = (
        "⚙️ <b>SETTINGS</b>\n\n"
        f"🏦 UPI: {'ON' if ENABLE_UPI else 'OFF'}\n"
        f"🪙 USDT: {'ON' if ENABLE_USDT else 'OFF'}\n"
        f"🇮🇳 Minimum withdrawal: ₹{MIN_WITHDRAWAL:.0f}\n💱 USD→INR: {USD_TO_INR:.2f}\n"
        f"📢 Force join: {'ON' if FORCE_JOIN_CHANNEL else 'OFF'} (dashboard configurable)\n"
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
# GROUP CONFIGURATION
# =========================
def get_group_ids():
    """Return all configured group IDs. GROUP_IDS supports comma/newline separated IDs."""
    raw = os.getenv("GROUP_IDS", "").strip()
    if not raw:
        raw = os.getenv("GROUP_ID", "").strip()
    ids = []
    for part in re.split(r"[,;]+", raw.replace("\n", ",")):
        part = part.strip()
        if not part:
            continue
        try:
            ids.append(int(part))
        except ValueError:
            log.warning("Invalid group ID in GROUP_IDS: %s", part)
    # Preserve order and remove duplicates.
    return list(dict.fromkeys(ids))


# =========================
# POST QR
# =========================
async def postqr(update, context):
    """Post a selected/next available QR to every configured group and notify known users."""
    # Never fail silently: tell the admin exactly why /postqr was not handled.
    if not update.effective_user:
        return
    if not is_admin(update.effective_user.id):
        if update.effective_message:
            await update.effective_message.reply_text("🔴 You are not authorized to use /postqr.")
        return
    if update.effective_chat and update.effective_chat.type != "private":
        if update.effective_message:
            await update.effective_message.reply_text(
                "🔴 Please use /postqr in the bot's private chat (Admin only)."
            )
        return

    group_ids = get_group_ids()
    if not group_ids:
        await update.message.reply_text("🔴 No groups configured.\n\nSet GROUP_IDS=-100123,-100456 in Railway variables.")
        return

    requested_id = None
    if len(context.args) >= 1:
        try: requested_id = int(context.args[0])
        except ValueError: requested_id = None
    with closing(db()) as con:
        if requested_id:
            qr = con.execute("SELECT * FROM qrs WHERE id=? AND status='available'", (requested_id,)).fetchone()
        else:
            qr = con.execute("SELECT * FROM qrs WHERE status='available' ORDER BY id LIMIT 1").fetchone()

    if not qr:
        await update.message.reply_text("🔴 No available QR. Create one first with /addqr.")
        return

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("CLAIM QR", callback_data=f"claim_{qr['id']}")]
    ])

    posted = []
    failed_groups = []
    for gid in group_ids:
        try:
            msg = await context.bot.send_message(
                chat_id=gid,
                text=(
                    "🎯 <b>QR CLAIM AVAILABLE</b>\n\n"
                    f"📱 App: {escape(qr['app_name'])}\n"
                    f"💵 Reward: ${qr['reward']:.2f}\n\n"
                    "First eligible user to claim gets this QR in DM."
                ),
                parse_mode="HTML",
                reply_markup=kb
            )
            posted.append((gid, msg.message_id))
        except Exception as e:
            failed_groups.append((gid, str(e)))
            log.exception("Could not post QR #%s to group %s", qr["id"], gid)

    if not posted:
        details = "\n".join(f"{gid}: {err}" for gid, err in failed_groups)
        await update.message.reply_text(
            "🔴 <b>QR POST FAILED</b>\n\n"
            "Check these items:\n"
            "1. GROUP_IDS contains the correct -100... group IDs.\n"
            "2. The bot is added to each group.\n"
            "3. The bot is an admin with permission to send messages.\n\n"
            "Telegram errors:\n" + details,
            parse_mode="HTML",
            reply_markup=admin_menu()
        )
        return

    with closing(db()) as con:
        # Keep the legacy single-post fields updated too.
        first_gid, first_mid = posted[0]
        con.execute(
            "UPDATE qrs SET posted_message_id=?, group_id=? WHERE id=?",
            (first_mid, str(first_gid), qr["id"])
        )
        for gid, mid in posted:
            con.execute(
                "INSERT OR REPLACE INTO qr_posts(qr_id,group_id,message_id) VALUES(?,?,?)",
                (qr["id"], str(gid), mid)
            )
        users = [
            r["id"] for r in
            con.execute("SELECT id FROM users WHERE banned=0").fetchall()
        ]
        con.commit()

    sent = failed = 0
    blocked_or_not_started = 0
    dm_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("CLAIM QR", callback_data=f"claim_{qr['id']}")]
    ])
    for target in users:
        try:
            await context.bot.send_message(
                target,
                (
                    "🎯 <b>QR CLAIM AVAILABLE</b>\n\n"
                    f"📱 App: {escape(qr['app_name'])}\n"
                    f"💰 Reward: ${qr['reward']:.2f}\n\n"
                    "First eligible user gets this QR in DM."
                ),
                parse_mode="HTML",
                reply_markup=dm_kb
            )
            sent += 1
        except Exception as e:
            failed += 1
            if "Forbidden" in str(e) or "bot was blocked" in str(e).lower() or "chat not found" in str(e).lower():
                blocked_or_not_started += 1
            log.warning("Could not DM user %s: %s", target, e)

    await send_log(
        context.bot,
        f"📢 <b>QR POSTED</b>\n"
        f"Admin: <code>{update.effective_user.id}</code>\n"
        f"QR: #{qr['id']}\n"
        f"Groups: {len(posted)}/{len(group_ids)}\n"
        f"DM sent: {sent}, failed: {failed}, blocked/not-started: {blocked_or_not_started}"
    )

    group_line = "\n".join(
        f"🟢 {gid}" for gid, _ in posted
    )
    fail_line = "\n".join(
        f"🔴 {gid}" for gid, _ in failed_groups
    )
    result = (
        f"🟢 <b>QR POSTED SUCCESSFULLY</b>\n\n"
        f"📱 QR: #{qr['id']}\n"
        f"👥 Groups posted: {len(posted)}/{len(group_ids)}\n"
        f"DM claim messages sent: {sent}\n"
        f"DM failed: {failed}\n"
        f"Blocked/not-started: {blocked_or_not_started}\n\n"
        f"{group_line}"
    )
    if fail_line:
        result += f"\n\n<b>Failed groups:</b>\n{fail_line}"

    await update.message.reply_text(
        result,
        parse_mode="HTML",
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
        try: inr=float(update.message.text.strip())
        except ValueError:
            await update.message.reply_text("🔴 Enter a valid INR amount."); return
        with closing(db()) as con: r=con.execute("SELECT balance FROM users WHERE id=?",(update.effective_user.id,)).fetchone()
        if inr < MIN_WITHDRAWAL or inr > r["balance"]*USD_TO_INR:
            context.user_data.clear()
            await update.message.reply_text(f"🔴 Withdrawal not started.\n\nMinimum: ₹{MIN_WITHDRAWAL:.0f}\nMaximum available: ₹{r['balance']*USD_TO_INR:.2f}",reply_markup=None); return
        context.user_data["withdraw_inr"]=inr
        context.user_data["withdraw_stage"]="method"
        await update.message.reply_text(f"💸 <b>WITHDRAWAL</b>\n\n🇮🇳 Amount: ₹{inr:.2f}\n💵 USD required: ${inr/USD_TO_INR:.2f}\n💱 Rate: $1 = ₹{USD_TO_INR:.2f}\n\nSelect method:",parse_mode="HTML",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏦 BINANCE ID / UID",callback_data="wmethod_BINANCE")],[InlineKeyboardButton("🔙 CANCEL",callback_data="home")]])); return

    if context.user_data.get("gift_stage"):
        code=update.message.text.strip().upper()
        with closing(db()) as con:
            g=con.execute("SELECT * FROM gift_codes WHERE code=?",(code,)).fetchone()
            expired = bool(g and g["expires_at"] and str(g["expires_at"]) < __import__('datetime').datetime.utcnow().isoformat(sep=' '))
            if not g or expired or g["uses"]>=g["max_uses"] or con.execute("SELECT 1 FROM gift_redemptions WHERE code=? AND user_id=?",(code,update.effective_user.id)).fetchone():
                context.user_data.clear(); await update.message.reply_text("🔴 Invalid, exhausted or already-used gift code.",reply_markup=None); return
            con.execute("INSERT INTO gift_redemptions(code,user_id) VALUES(?,?)",(code,update.effective_user.id)); con.execute("UPDATE gift_codes SET uses=uses+1 WHERE code=?",(code,)); con.execute("UPDATE users SET balance=balance+?,total_earned=total_earned+? WHERE id=?",(g["amount"],g["amount"],update.effective_user.id)); con.execute("INSERT INTO transactions(user_id,amount,kind) VALUES(?,?,?)",(update.effective_user.id,g["amount"],"Gift Code")); con.commit()
        context.user_data.clear(); await update.message.reply_text(f"🟢 Gift code redeemed! ${g['amount']:.2f} added.",reply_markup=None); return

    if stage == "account":
        account = update.message.text.strip()
        if len(account) < 3 or len(account) > 100:
            await update.message.reply_text("Please enter valid withdrawal details.")
            return

        await create_withdrawal(update, context, account)
        return

    return


# =========================
# EXTRA USER FEATURES
# =========================
async def referral_page(update, context):
    q=update.callback_query; await q.answer(); ensure_user(q.from_user)
    me=await context.bot.get_me()
    link=f"https://t.me/{me.username}?start=ref_{q.from_user.id}"
    with closing(db()) as con:
        count=con.execute("SELECT COUNT(*) x FROM referrals WHERE referrer_id=?",(q.from_user.id,)).fetchone()["x"]
        earned=con.execute("SELECT COALESCE(SUM(reward_paid),0) x FROM referrals WHERE referrer_id=?",(q.from_user.id,)).fetchone()["x"]
    await q.edit_message_text(f"👥 <b>REFERRALS</b>\n\n🔗 Your link:\n<code>{link}</code>\n\n👤 Referrals: {count}\n💵 Referral earnings: ${earned:.2f}\n🎁 Reward per valid referral: ${REFERRAL_REWARD_USD:.2f}",parse_mode="HTML",reply_markup=back_button())

async def leaderboard(update, context):
    q=update.callback_query; await q.answer()
    with closing(db()) as con: rows=con.execute("SELECT name,total_earned FROM users WHERE banned=0 ORDER BY total_earned DESC LIMIT 10").fetchall()
    text="🏆 <b>LEADERBOARD</b>\n\n"+"\n".join(f"{i}. {escape(r['name'])} — ${r['total_earned']:.2f}" for i,r in enumerate(rows,1)) if rows else "🏆 No data yet."
    await q.edit_message_text(text,parse_mode="HTML",reply_markup=back_button())

async def daily_bonus(update, context):
    q=update.callback_query; await q.answer(); uid=q.from_user.id; ensure_user(q.from_user)
    with closing(db()) as con:
        row=con.execute("SELECT claimed_at FROM daily_bonuses WHERE user_id=?",(uid,)).fetchone()
        today=__import__('datetime').datetime.utcnow().date().isoformat()
        if row and row["claimed_at"]==today:
            await q.answer("🔴 Daily bonus already claimed today.",show_alert=True); return
        con.execute("INSERT INTO daily_bonuses(user_id,claimed_at) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET claimed_at=excluded.claimed_at",(uid,today))
        con.execute("UPDATE users SET balance=balance+?, total_earned=total_earned+? WHERE id=?",(DAILY_BONUS_USD,DAILY_BONUS_USD,uid))
        con.execute("INSERT INTO transactions(user_id,amount,kind) VALUES(?,?,?)",(uid,DAILY_BONUS_USD,"Daily Bonus")); con.commit()
    await q.edit_message_text(f"🟢 <b>DAILY BONUS CLAIMED</b>\n\n🎁 Added: ${DAILY_BONUS_USD:.2f}",parse_mode="HTML",reply_markup=back_button())

async def gift_page(update, context):
    q=update.callback_query; await q.answer(); context.user_data["gift_stage"]=True
    await q.edit_message_text("🎟️ <b>GIFT CODE</b>\n\nSend your gift code:",parse_mode="HTML",reply_markup=back_button())

# =========================
# CALLBACK ROUTER
# =========================
async def _callbacks(update, context):
    d = update.callback_query.data

    if d.startswith("claimant_"):
        q = update.callback_query
        uid = d.split("_",1)[1]
        await q.answer(f"Telegram ID: {uid}", show_alert=True)
        return

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
            )
        return

    if d == "wallet":
        return await wallet(update, context)
    if d == "rewards":
        return await rewards(update, context)
    if d == "order_history":
        return await order_history(update, context)
    if d == "order_prev":
        context.user_data["order_page"] = max(0, int(context.user_data.get("order_page", 0))-1)
        return await order_history(update, context)
    if d == "order_next":
        context.user_data["order_page"] = int(context.user_data.get("order_page", 0))+1
        return await order_history(update, context)
    if d == "transactions":
        return await order_history(update, context)
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
    if d == "referral": return await referral_page(update, context)
    if d == "leaderboard": return await leaderboard(update, context)
    if d == "daily_bonus": return await daily_bonus(update, context)
    if d == "gift": return await gift_page(update, context)
    if d == "withdraw":
        return await withdraw(update, context)

    if d == "claim" or d.startswith("claim_"):
        return await claim(update, context)
    if d.startswith("confirm_"):
        return await confirm_payment(update, context)
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
    if d in ("admin_broadcast","admin_forcejoin","admin_logs","admin_analytics","admin_gifts"):
        if not is_admin(update.effective_user.id): return
        q=update.callback_query; await q.answer()
        labels={"admin_broadcast":"📢 BROADCAST","admin_forcejoin":"🔒 FORCE JOIN","admin_logs":"📝 LOGS","admin_analytics":"📈 ANALYTICS","admin_gifts":"🎟️ GIFT CODES"}
        await q.edit_message_text(f"{labels[d]}\n\nOpen the full Admin Dashboard to manage this section.",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📊 OPEN ADMIN DASHBOARD",web_app=WebAppInfo(url=WEBAPP_URL+"/admin"))],[InlineKeyboardButton("🔙 ADMIN PANEL",callback_data="admin_dashboard")]]))
        return

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
        if not is_admin(update.effective_user.id):
            return
        q = update.callback_query
        await q.answer()
        # Telegram callback has no message object suitable for the /postqr command,
        # so post the QR here using the same multi-group logic.
        group_ids = get_group_ids()
        if not group_ids:
            await q.edit_message_text(
                "🔴 No groups configured. Set GROUP_IDS in Railway.",
                reply_markup=admin_menu()
            )
            return
        with closing(db()) as con:
            qr = con.execute(
                "SELECT * FROM qrs WHERE status='available' ORDER BY id LIMIT 1"
            ).fetchone()
        if not qr:
            await q.edit_message_text(
                "🔴 No available QR. Create one first.",
                reply_markup=admin_menu()
            )
            return

        kb = InlineKeyboardMarkup([[InlineKeyboardButton("CLAIM QR", callback_data=f"claim_{qr['id']}")]])
        posted = []
        for gid in group_ids:
            try:
                msg = await context.bot.send_message(
                    gid,
                    f"🎯 <b>QR CLAIM AVAILABLE</b>\n\n📱 App: {escape(qr['app_name'])}\n💵 Reward: ${qr['reward']:.2f}\n\nFirst eligible user to claim gets this QR in DM.",
                    parse_mode="HTML",
                    reply_markup=kb
                )
                posted.append((gid, msg.message_id))
            except Exception:
                log.exception("Could not post QR #%s to group %s", qr["id"], gid)

        if not posted:
            await q.edit_message_text(
                "🔴 Failed to post QR to all groups. Check bot admin permissions.",
                reply_markup=admin_menu()
            )
            return

        with closing(db()) as con:
            con.execute(
                "UPDATE qrs SET posted_message_id=?, group_id=? WHERE id=?",
                (posted[0][1], str(posted[0][0]), qr["id"])
            )
            for gid, mid in posted:
                con.execute(
                    "INSERT OR REPLACE INTO qr_posts(qr_id,group_id,message_id) VALUES(?,?,?)",
                    (qr["id"], str(gid), mid)
                )
            con.commit()

        await send_log(
            context.bot,
            f"📢 <b>QR POSTED</b>\nQR: #{qr['id']}\nAdmin: <code>{q.from_user.id}</code>\nGroups: {len(posted)}"
        )
        await q.edit_message_text(
            f"🟢 <b>QR POSTED</b>\n\n📱 QR: #{qr['id']}\n👥 Groups: {len(posted)}",
            parse_mode="HTML",
            reply_markup=admin_menu()
        )
        return

    # Every button must get a response, even if a stale/unknown callback is pressed.
    try:
        await update.callback_query.answer("Button received. Please try again.", show_alert=True)
    except Exception:
        pass


async def giftcode_cmd(update, context):
    if not is_admin(update.effective_user.id): return
    parts=update.message.text.split()
    if len(parts) not in (3,4): await update.message.reply_text("Usage: /giftcode CODE USD_AMOUNT [USES]"); return
    code=parts[1].upper()
    try: amount=float(parts[2]); uses=int(parts[3]) if len(parts)==4 else 1
    except ValueError: await update.message.reply_text("🔴 Invalid amount/uses."); return
    if amount<=0 or uses<1: await update.message.reply_text("🔴 Invalid values."); return
    with closing(db()) as con:
        con.execute("INSERT OR REPLACE INTO gift_codes(code,amount,max_uses,uses) VALUES(?,?,?,0)",(code,amount,uses)); con.commit()
    await update.message.reply_text(f"🟢 Gift code created\n\n🎟️ <code>{escape(code)}</code>\n💵 ${amount:.2f}\n👥 Uses: {uses}",parse_mode="HTML")

async def system_stats_cmd(update, context):
    if not is_admin(update.effective_user.id): return
    with closing(db()) as con:
        u=con.execute("SELECT COUNT(*) x FROM users").fetchone()["x"]; q=con.execute("SELECT COUNT(*) x FROM qrs").fetchone()["x"]; c=con.execute("SELECT COUNT(*) x FROM qrs WHERE claimed_by IS NOT NULL").fetchone()["x"]; p=con.execute("SELECT COUNT(*) x FROM proofs WHERE status='pending'").fetchone()["x"]; w=con.execute("SELECT COUNT(*) x FROM withdrawals WHERE status IN ('pending','processing')").fetchone()["x"]
    await update.message.reply_text(f"📊 <b>SYSTEM STATS</b>\n\n👥 Users: {u}\n📱 QRs: {q}\n🎯 Claims: {c}\n🧾 Pending proofs: {p}\n💸 Pending withdrawals: {w}\n💱 Rate: $1 = ₹{USD_TO_INR:.2f}",parse_mode="HTML",reply_markup=admin_menu())


async def callbacks(update, context):
    """Safe callback entry point so one broken button never leaves Telegram spinning."""
    q = update.callback_query
    try:
        await _callbacks(update, context)
    except Exception as e:
        log.exception("Callback failed: %s", e)
        try:
            await q.answer("🔴 This button encountered an error. Please try again.", show_alert=True)
        except Exception:
            pass
        try:
            if is_admin(q.from_user.id):
                await send_log(
                    context.bot,
                    f"🔴 <b>BUTTON ERROR</b>\n"
                    f"User: <code>{q.from_user.id}</code>\n"
                    f"Button: <code>{escape(q.data or '')}</code>"
                )
        except Exception:
            pass


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
    app.add_handler(CommandHandler("message", message_user_cmd))
    app.add_handler(CommandHandler("ban", ban_cmd))
    app.add_handler(CommandHandler("unban", unban_cmd))
    app.add_handler(CommandHandler("broadcast", broadcast_cmd))
    app.add_handler(CommandHandler("giftcode", giftcode_cmd))
    app.add_handler(CommandHandler("stats", system_stats_cmd))

    # Callback buttons
    app.add_handler(CallbackQueryHandler(callbacks))

    # Photos: admin QR wizard and user proof wizard
    app.add_handler(MessageHandler(filters.PHOTO, photo_handler))

    # Text: withdrawal/admin wizard/user fallback
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        text_handler
    ))

    log.info("Dashboard URL: %s", WEBAPP_URL or "NOT SET")
    log.info("Dashboard file: %s (exists=%s)", DASHBOARD_FILE, os.path.exists(DASHBOARD_FILE))
    log.info("Bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
