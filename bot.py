import os, sqlite3, logging, threading, asyncio
from datetime import datetime, timezone
from urllib.parse import parse_qsl
import hashlib, hmac, json

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
import uvicorn

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, WebAppInfo
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters
)

logging.basicConfig(level=logging.INFO)
TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}
DB_PATH = os.getenv("DB_PATH", "qr_claim_bot.db")
GROUP_ID = os.getenv("GROUP_ID", "").strip()
SUPPORT_URL = os.getenv("SUPPORT_URL", "https://t.me/itsvanshu").strip()
WEBAPP_URL = os.getenv("WEBAPP_URL", "").strip().rstrip("/")
MIN_WITHDRAWAL = float(os.getenv("MIN_WITHDRAWAL", "50"))
ENABLE_UPI = os.getenv("ENABLE_UPI", "1") == "1"
ENABLE_USDT = os.getenv("ENABLE_USDT", "1") == "1"

app_web = FastAPI(title="QR Claim Admin")

def conn():
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA busy_timeout=30000")
    return c

def init_db():
    c = conn()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS users(
      id INTEGER PRIMARY KEY, name TEXT, username TEXT,
      balance REAL DEFAULT 0, total_earned REAL DEFAULT 0,
      total_withdrawn REAL DEFAULT 0, created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS qrs(
      id INTEGER PRIMARY KEY AUTOINCREMENT, app TEXT, reward REAL,
      file_id TEXT, active INTEGER DEFAULT 1, group_chat_id TEXT,
      group_message_id INTEGER, claimed_by INTEGER, claimed_at TEXT,
      created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS claims(
      id INTEGER PRIMARY KEY AUTOINCREMENT, qr_id INTEGER, user_id INTEGER,
      status TEXT DEFAULT 'claimed', proof_file_id TEXT,
      created_at TEXT DEFAULT CURRENT_TIMESTAMP, reviewed_at TEXT
    );
    CREATE TABLE IF NOT EXISTS withdrawals(
      id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, amount REAL,
      method TEXT, account TEXT DEFAULT '', status TEXT DEFAULT 'pending',
      created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT
    );
    CREATE TABLE IF NOT EXISTS transactions(
      id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, amount REAL,
      kind TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS settings(
      key TEXT PRIMARY KEY, value TEXT
    );
    """)
    migrations = {
        "users": [("created_at","TEXT")],
        "claims": [("reviewed_at","TEXT")],
        "withdrawals": [("account","TEXT DEFAULT ''"),("updated_at","TEXT")],
        "qrs": [("group_chat_id","TEXT"),("group_message_id","INTEGER"),("claimed_by","INTEGER"),("claimed_at","TEXT")]
    }
    for table, cols in migrations.items():
        existing={r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
        for name, definition in cols:
            if name not in existing:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
    defaults = {
        "force_join_channel": os.getenv("FORCE_JOIN_CHANNEL","").strip(),
        "force_join_url": os.getenv("FORCE_JOIN_URL","").strip(),
    }
    for k,v in defaults.items():
        c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)",(k,v))
    c.commit(); c.close()

def get_setting(key, default=""):
    c=conn(); r=c.execute("SELECT value FROM settings WHERE key=?",(key,)).fetchone(); c.close()
    return r["value"] if r else default

def set_setting(key,value):
    c=conn(); c.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(key,value)); c.commit(); c.close()

def main_kb():
    return ReplyKeyboardMarkup([
        ["💰 WALLET","🎁 MY REWARDS"],
        ["💸 WITHDRAW","📜 TRANSACTIONS"],
        ["👤 PROFILE","ℹ️ HELP"]
    ], resize_keyboard=True)

def back_kb():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 BACK TO MENU", callback_data="menu")]])

async def ensure_user(u):
    c=conn()
    c.execute("""INSERT INTO users(id,name,username) VALUES(?,?,?)
                ON CONFLICT(id) DO UPDATE SET name=excluded.name, username=excluded.username""",
              (u.id,u.full_name,u.username or ""))
    c.commit(); c.close()

async def is_joined(bot, user_id):
    if user_id in ADMIN_IDS:
        return True
    channel = get_setting("force_join_channel")
    if not channel:
        return True
    try:
        m = await bot.get_chat_member(channel, user_id)
        if m.status in ("creator","administrator","member"):
            return True
        if hasattr(m, "is_member") and m.is_member:
            return True
    except Exception:
        logging.exception("Force-join membership check failed")
    return False

def join_markup():
    url=get_setting("force_join_url")
    if not url:
        channel=get_setting("force_join_channel")
        if channel.startswith("@"):
            url="https://t.me/"+channel[1:]
    rows=[]
    if url:
        rows.append([InlineKeyboardButton("📢 JOIN CHANNEL", url=url)])
    rows.append([InlineKeyboardButton("✅ I HAVE JOINED", callback_data="checkjoin")])
    return InlineKeyboardMarkup(rows)

async def require_join(update, context):
    uid=update.effective_user.id
    if uid in ADMIN_IDS or await is_joined(context.bot, uid):
        return True
    msg = update.effective_message
    if msg:
        await msg.reply_text("🔒 Please join our required channel first.\n\nAfter joining, tap ✅ I HAVE JOINED.", reply_markup=join_markup())
    return False

async def start(update,context):
    await ensure_user(update.effective_user)
    if not await require_join(update,context): return
    c=conn(); bal=c.execute("SELECT balance FROM users WHERE id=?",(update.effective_user.id,)).fetchone()["balance"]; c.close()
    await update.message.reply_text(
        f"👋 Welcome to QR Claim Bot 🎉\n\n🎯 Claim QR orders, complete tasks, earn rewards and withdraw your balance.\n\n💰 Balance: ₹{bal:.2f}",
        reply_markup=main_kb())

async def wallet(update,context):
    if not await require_join(update,context): return
    await ensure_user(update.effective_user)
    c=conn(); u=c.execute("SELECT * FROM users WHERE id=?",(update.effective_user.id,)).fetchone()
    p=c.execute("SELECT COALESCE(SUM(amount),0) x FROM withdrawals WHERE user_id=? AND status IN ('pending','processing')",(u["id"],)).fetchone()["x"]; c.close()
    kb=InlineKeyboardMarkup([
      [InlineKeyboardButton("💸 WITHDRAW",callback_data="withdraw"),InlineKeyboardButton("📜 TRANSACTIONS",callback_data="tx")],
      [InlineKeyboardButton("🔙 BACK TO MENU",callback_data="menu")]])
    await update.message.reply_text(f"💰 MY WALLET\n\n💵 Available Balance: ₹{u['balance']:.2f}\n🏆 Total Earned: ₹{u['total_earned']:.2f}\n💸 Total Withdrawn: ₹{u['total_withdrawn']:.2f}\n⏳ Pending Withdrawal: ₹{p:.2f}",reply_markup=kb)

async def rewards(update,context):
    if not await require_join(update,context): return
    c=conn(); rows=c.execute("SELECT amount,kind,created_at FROM transactions WHERE user_id=? AND amount>0 ORDER BY id DESC LIMIT 20",(update.effective_user.id,)).fetchall(); c.close()
    text="🎁 MY REWARDS\n\n"+("\n".join(f"✅ ₹{r['amount']:.2f} — {r['kind']}" for r in rows) or "No approved rewards yet.")
    await update.message.reply_text(text,reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📜 HISTORY",callback_data="tx")],[InlineKeyboardButton("🔙 BACK TO MENU",callback_data="menu")]]))

async def transactions(update,context):
    if not await require_join(update,context): return
    c=conn(); rows=c.execute("SELECT amount,kind FROM transactions WHERE user_id=? ORDER BY id DESC LIMIT 30",(update.effective_user.id,)).fetchall(); c.close()
    text="📜 TRANSACTION HISTORY\n\n"+("\n".join(("➕" if r["amount"]>=0 else "➖")+f" ₹{abs(r['amount']):.2f} — {r['kind']}" for r in rows) or "No transactions yet.")
    await update.message.reply_text(text,reply_markup=back_kb())

async def profile(update,context):
    if not await require_join(update,context): return
    await ensure_user(update.effective_user)
    c=conn(); u=c.execute("SELECT * FROM users WHERE id=?",(update.effective_user.id,)).fetchone(); c.close()
    await update.message.reply_text(
      f"👤 MY PROFILE\n\nName: {u['name']}\nUsername: @{u['username'] or '—'}\nTelegram ID: {u['id']}\n\n💰 Balance: ₹{u['balance']:.2f}\n🎁 Total Rewards: ₹{u['total_earned']:.2f}\n💸 Total Withdrawn: ₹{u['total_withdrawn']:.2f}",
      reply_markup=back_kb())

async def help_cmd(update,context):
    if not await require_join(update,context): return
    await update.message.reply_text(
      "ℹ️ HOW IT WORKS\n\n1️⃣ Claim an available QR.\n2️⃣ Receive the QR in your private chat.\n3️⃣ Complete the required action.\n4️⃣ Upload your proof.\n5️⃣ Submit it for review.\n6️⃣ After approval, the reward is added to your wallet.\n7️⃣ Withdraw your available balance.",
      reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📞 SUPPORT",url=SUPPORT_URL)],[InlineKeyboardButton("🔙 BACK TO MENU",callback_data="menu")]]))

async def withdraw(update,context):
    if not await require_join(update,context): return
    c=conn(); row=c.execute("SELECT balance FROM users WHERE id=?",(update.effective_user.id,)).fetchone(); c.close()
    bal=row["balance"] if row else 0
    context.user_data["withdraw_step"]="amount"
    await update.message.reply_text(f"💸 WITHDRAW\n\n💰 Available Balance: ₹{bal:.2f}\n\nMinimum withdrawal: ₹{MIN_WITHDRAWAL:.0f}\n\nEnter the amount you want to withdraw.",reply_markup=back_kb())

async def admin(update,context):
    if update.effective_user.id not in ADMIN_IDS: return
    init_db()
    c=conn()
    users=c.execute("SELECT COUNT(*) x FROM users").fetchone()["x"]
    qrs=c.execute("SELECT COUNT(*) x FROM qrs WHERE active=1").fetchone()["x"]
    pending=c.execute("SELECT COUNT(*) x FROM withdrawals WHERE status='pending'").fetchone()["x"]
    proofs=c.execute("SELECT COUNT(*) x FROM claims WHERE status='under_review'").fetchone()["x"]
    total=c.execute("SELECT COALESCE(SUM(amount),0) x FROM withdrawals").fetchone()["x"]
    c.close()
    rows=[
      [InlineKeyboardButton("📊 OPEN ADMIN DASHBOARD",web_app=WebAppInfo(url=WEBAPP_URL))] if WEBAPP_URL else [],
      [InlineKeyboardButton("➕ ADD QR",callback_data="addqr"),InlineKeyboardButton("📢 POST TO GROUP",callback_data="posthelp")],
      [InlineKeyboardButton("🧾 PROOF REVIEW",callback_data="admin_proofs"),InlineKeyboardButton("💸 WITHDRAWALS",callback_data="admin_wd")],
      [InlineKeyboardButton("👥 USERS",callback_data="users"),InlineKeyboardButton("📊 STATS",callback_data="stats")],
      [InlineKeyboardButton("💰 ADD BALANCE",callback_data="addbalance"),InlineKeyboardButton("📢 FORCE JOIN",callback_data="forcejoin")]
    ]
    rows=[r for r in rows if r]
    msg="🛠 ADMIN PANEL\n\n👥 Users: %s\n🎯 Active QR: %s\n🧾 Pending proofs: %s\n⏳ Pending withdrawals: %s\n💸 Total withdrawal requests: ₹%.2f"%(users,qrs,proofs,pending,total)
    if not WEBAPP_URL: msg+="\n\n⚠️ Set WEBAPP_URL in Railway Variables to enable the Mini App."
    await update.message.reply_text(msg,reply_markup=InlineKeyboardMarkup(rows))

async def addqr(update,context):
    if update.effective_user.id not in ADMIN_IDS: return
    context.user_data["addqr_step"]="photo"
    await update.message.reply_text("➕ ADD QR\n\n📷 Send the QR image.\n\nThe QR will NOT be posted in the group.")

async def photo(update,context):
    if update.effective_user.id not in ADMIN_IDS or context.user_data.get("addqr_step")!="photo": return
    context.user_data["qr_file_id"]=update.message.photo[-1].file_id
    context.user_data["addqr_step"]="app"
    await update.message.reply_text("📱 Enter the QR app name.")

async def proof_photo(update,context):
    if context.user_data.get("proof_step") != "photo": return False
    qid=context.user_data.get("proof_qid")
    if not qid: return False
    c=conn(); claim=c.execute("SELECT status FROM claims WHERE qr_id=? AND user_id=? ORDER BY id DESC LIMIT 1",(qid,update.effective_user.id)).fetchone()
    if not claim or claim["status"]!="claimed":
        c.close(); context.user_data.clear()
        await update.message.reply_text("❌ This claim cannot accept a proof now.",reply_markup=main_kb()); return True
    file_id=update.message.photo[-1].file_id
    context.user_data["proof_file_id"]=file_id; context.user_data["proof_step"]="submit"; c.close()
    await update.message.reply_text("📷 PROOF RECEIVED\n\nYour proof image is ready to submit for admin review.",
      reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🟢 SUBMIT FOR REVIEW",callback_data=f"submitproof:{qid}")],[InlineKeyboardButton("🔙 CANCEL",callback_data="menu")]]))
    return True

async def submit_proof(update,context):
    q=update.callback_query; await q.answer()
    qid=int(q.data.split(":")[1]); file_id=context.user_data.get("proof_file_id")
    if context.user_data.get("proof_qid")!=qid or not file_id:
        return await q.message.reply_text("❌ Please upload your proof first.")
    c=conn()
    cur=c.execute("UPDATE claims SET proof_file_id=?,status='under_review' WHERE qr_id=? AND user_id=? AND status='claimed'",(file_id,qid,q.from_user.id))
    r=c.execute("SELECT app,reward FROM qrs WHERE id=?",(qid,)).fetchone()
    c.commit(); c.close()
    if cur.rowcount != 1:
        context.user_data.clear(); return await q.message.edit_text("⏳ This claim is already under review or has been processed.")
    context.user_data.clear()
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_photo(admin_id,file_id,
                caption=f"📥 PENDING PROOF REVIEW\n\n👤 User: {q.from_user.full_name} (@{q.from_user.username or '—'})\n🆔 User ID: {q.from_user.id}\n🎯 QR ID: {qid}\n📱 App: {r['app'] if r else '—'}\n💰 Reward: ₹{float(r['reward']):.2f}",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🟢 APPROVE",callback_data=f"approveproof:{qid}:{q.from_user.id}"),InlineKeyboardButton("🔴 REJECT",callback_data=f"rejectproof:{qid}:{q.from_user.id}")]
                ]))
        except Exception: logging.exception("Admin proof notification failed")
    await q.message.edit_text("⏳ UNDER REVIEW\n\nYour proof was submitted successfully. Further submission is disabled until review.",reply_markup=back_kb())

async def postqr(update,context,qid=None):
    if update.effective_user.id not in ADMIN_IDS: return
    if qid is None:
        if not context.args: return await update.message.reply_text("Usage: /postqr QR_ID")
        try: qid=int(context.args[0])
        except: return await update.message.reply_text("❌ Invalid QR ID.")
    if not GROUP_ID: return await update.message.reply_text("❌ Add GROUP_ID in Railway Variables first.")
    c=conn(); r=c.execute("SELECT * FROM qrs WHERE id=? AND active=1",(qid,)).fetchone(); c.close()
    if not r: return await update.message.reply_text("❌ QR not found or already claimed.")
    kb=InlineKeyboardMarkup([[InlineKeyboardButton("💳 CLAIM QR",callback_data=f"claim:{qid}")]])
    msg=await context.bot.send_message(GROUP_ID,f"🎯 QR CLAIM AVAILABLE\n\n📱 App: {r['app']}\n💰 Reward: ₹{r['reward']:.2f}\n\n🔒 Only ONE user can claim this QR.\nClaim the QR to receive it in your private chat.",reply_markup=kb)
    c=conn(); c.execute("UPDATE qrs SET group_chat_id=?,group_message_id=? WHERE id=?",(str(GROUP_ID),msg.message_id,qid)); c.commit(); c.close()
    await update.message.reply_text("✅ Posted to group.\n\n🔒 QR image was NOT posted in the group.")

async def claim(update,context):
    q=update.callback_query; uid=q.from_user.id; qid=int(q.data.split(":")[1])
    if not await is_joined(context.bot,uid):
        return await q.answer("🔒 Join the required channel first.",show_alert=True)
    c=conn(); started=c.execute("SELECT 1 FROM users WHERE id=?",(uid,)).fetchone(); c.close()
    if not started: return await q.answer("❌ Start the bot in private chat first.",show_alert=True)
    c=conn()
    try:
        c.execute("BEGIN IMMEDIATE")
        r=c.execute("SELECT * FROM qrs WHERE id=? AND active=1",(qid,)).fetchone()
        if not r: c.rollback(); c.close(); return await q.answer("❌ This QR has already been claimed.",show_alert=True)
        existing=c.execute("SELECT id FROM claims WHERE qr_id=?",(qid,)).fetchone()
        if existing:
            c.execute("UPDATE qrs SET active=0 WHERE id=?",(qid,)); c.commit(); c.close()
            return await q.answer("❌ This QR has already been claimed.",show_alert=True)
        c.execute("UPDATE qrs SET active=0,claimed_by=?,claimed_at=CURRENT_TIMESTAMP WHERE id=? AND active=1",(uid,qid))
        if c.execute("SELECT changes()").fetchone()[0]!=1:
            c.rollback(); c.close(); return await q.answer("❌ This QR has already been claimed.",show_alert=True)
        c.execute("INSERT INTO claims(qr_id,user_id) VALUES(?,?)",(qid,uid)); c.commit()
    except Exception:
        c.rollback(); c.close(); logging.exception("Claim failed")
        return await q.answer("❌ Could not claim this QR.",show_alert=True)
    c.close()
    display_name=("@"+q.from_user.username) if q.from_user.username else q.from_user.full_name
    if r["group_chat_id"] and r["group_message_id"]:
        try:
            await context.bot.edit_message_text(chat_id=r["group_chat_id"],message_id=r["group_message_id"],
                text=f"🎯 QR CLAIMED\n\n📱 App: {r['app']}\n💰 Reward: ₹{r['reward']:.2f}\n\n👤 Claimed by: {display_name}\n🔒 This QR is no longer available.",reply_markup=None)
        except Exception: logging.exception("Could not update group message")
    try:
        await context.bot.send_photo(uid,r["file_id"],caption=f"🎯 QR CLAIM\n\n📱 App: {r['app']}\n💰 Reward: ₹{r['reward']:.2f}\n\nComplete the required action and upload your proof.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📤 UPLOAD PROOF",callback_data=f"proof:{qid}")]]))
        await q.answer("✅ QR claimed! Check your private chat.")
    except Exception:
        await q.answer("✅ Claimed, but DM delivery failed. Open the bot and contact support.",show_alert=True)

async def admin_proof_action(update,context,action,qid,uid):
    if update.effective_user.id not in ADMIN_IDS: return
    c=conn()
    claim=c.execute("""SELECT c.id,c.status,q.reward,q.app,c.proof_file_id
                       FROM claims c JOIN qrs q ON q.id=c.qr_id
                       WHERE c.qr_id=? AND c.user_id=? ORDER BY c.id DESC LIMIT 1""",(qid,uid)).fetchone()
    if not claim:
        c.close(); return await update.callback_query.answer("Proof not found.",show_alert=True)
    if claim["status"]!="under_review":
        c.close(); return await update.callback_query.answer("Already processed.",show_alert=True)
    now=datetime.now(timezone.utc).isoformat()
    if action=="approve":
        c.execute("UPDATE claims SET status='approved',reviewed_at=? WHERE id=? AND status='under_review'",(now,claim["id"]))
        c.execute("UPDATE users SET balance=balance+?,total_earned=total_earned+? WHERE id=?",(claim["reward"],claim["reward"],uid))
        c.execute("INSERT INTO transactions(user_id,amount,kind) VALUES(?,?,?)",(uid,claim["reward"],f"QR #{qid} approved"))
        text=f"🟢 APPROVED\n\n🎯 QR #{qid}\n💰 Reward ₹{claim['reward']:.2f} added to your balance."
    else:
        c.execute("UPDATE claims SET status='rejected',reviewed_at=? WHERE id=? AND status='under_review'",(now,claim["id"]))
        text=f"🔴 PROOF REJECTED\n\n🎯 QR #{qid}\n\nYour proof was rejected by admin. Contact support if you believe this was a mistake."
    c.commit(); c.close()
    try:
        await context.bot.send_message(uid,text,reply_markup=main_kb())
    except Exception: logging.exception("User review notification failed")
    q=update.callback_query; await q.answer("Done.")
    try: await q.edit_message_reply_markup(reply_markup=None)
    except: pass

async def callbacks(update,context):
    q=update.callback_query; d=q.data
    if d.startswith("wd:"):
        _,action,wid=d.split(":")
        return await withdrawal_admin_action(update,context,action,int(wid))
    if d=="checkjoin":
        await q.answer()
        if await is_joined(context.bot,q.from_user.id):
            await q.message.reply_text("✅ Membership verified! You can use the bot now.",reply_markup=main_kb())
        else:
            await q.answer("❌ You have not joined the required channel yet.",show_alert=True)
        return
    if d.startswith("approveproof:"):
        _,qid,uid=d.split(":"); return await admin_proof_action(update,context,"approve",int(qid),int(uid))
    if d.startswith("rejectproof:"):
        _,qid,uid=d.split(":"); return await admin_proof_action(update,context,"reject",int(qid),int(uid))
    if d=="menu":
        await q.answer()
        try: await q.message.delete()
        except: pass
        return await q.message.chat.send_message("🏠 Main Menu",reply_markup=main_kb())
    if d=="wallet":
        if not await is_joined(context.bot,q.from_user.id): return await q.answer("🔒 Join the required channel first.",show_alert=True)
        c=conn(); r=c.execute("SELECT * FROM users WHERE id=?",(q.from_user.id,)).fetchone(); c.close()
        return await q.edit_message_text(f"💰 MY WALLET\n\n💵 Available Balance: ₹{r['balance']:.2f}\n🏆 Total Earned: ₹{r['total_earned']:.2f}\n💸 Total Withdrawn: ₹{r['total_withdrawn']:.2f}",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💸 WITHDRAW",callback_data="withdraw")],[InlineKeyboardButton("📜 TRANSACTIONS",callback_data="tx")],[InlineKeyboardButton("🔙 BACK TO MENU",callback_data="menu")]]))
    if d=="tx":
        c=conn(); rows=c.execute("SELECT amount,kind FROM transactions WHERE user_id=? ORDER BY id DESC LIMIT 30",(q.from_user.id,)).fetchall(); c.close()
        return await q.edit_message_text("📜 TRANSACTION HISTORY\n\n"+("\n".join(("➕" if r["amount"]>=0 else "➖")+f" ₹{abs(r['amount']):.2f} — {r['kind']}" for r in rows) or "No transactions yet."),reply_markup=back_kb())
    if d=="withdraw":
        if not await is_joined(context.bot,q.from_user.id): return await q.answer("🔒 Join the required channel first.",show_alert=True)
        c=conn(); bal=c.execute("SELECT balance FROM users WHERE id=?",(q.from_user.id,)).fetchone()["balance"]; c.close()
        context.user_data["withdraw_step"]="amount"
        return await q.edit_message_text(f"💸 WITHDRAW\n\n💰 Available Balance: ₹{bal:.2f}\n\nMinimum: ₹{MIN_WITHDRAWAL:.0f}\n\nEnter amount.",reply_markup=back_kb())
    if d.startswith("w:"):
        method=d.split(":")[1]
        if method=="UPI" and not ENABLE_UPI: return await q.answer("UPI is disabled.",show_alert=True)
        if method=="USDT" and not ENABLE_USDT: return await q.answer("USDT is disabled.",show_alert=True)
        context.user_data["withdraw_method"]=method; context.user_data["withdraw_step"]="details"
        return await q.edit_message_text(f"🏦 {method} SELECTED\n\nSend your {method} payment details now.",reply_markup=back_kb())
    if d.startswith("submitproof:"): return await submit_proof(update,context)
    if d.startswith("proof:"): return await proof_button(update,context)
    if d=="addqr":
        return await q.message.chat.send_message("Use /addqr to start QR creation.")
    if d.startswith("post:"): return await postqr(update,context,int(d.split(":")[1]))
    if d=="posthelp":
        return await q.edit_message_text("📢 POST TO GROUP\n\nThe group receives only App + Reward + CLAIM button. The QR image is sent privately after a claim.",reply_markup=back_kb())
    if d=="admin":
        return await admin(update,context)
    if d=="admin_proofs":
        if q.from_user.id not in ADMIN_IDS: return
        c=conn(); rows=c.execute("""SELECT c.id,c.qr_id,c.user_id,u.username,u.name,q.app,q.reward
                                    FROM claims c JOIN users u ON u.id=c.user_id JOIN qrs q ON q.id=c.qr_id
                                    WHERE c.status='under_review' ORDER BY c.id DESC LIMIT 30""").fetchall(); c.close()
        if not rows: return await q.edit_message_text("🧾 PROOF REVIEW\n\nNo pending proofs.",reply_markup=back_kb())
        text="🧾 PENDING PROOFS\n\n"+"\n".join(f"#{r['id']} • QR #{r['qr_id']} • {('@'+r['username']) if r['username'] else r['name']} • ₹{r['reward']:.2f}" for r in rows)
        buttons=[]
        for r in rows:
            buttons.append([InlineKeyboardButton(f"🟢 #{r['id']} APPROVE",callback_data=f"approveproof:{r['qr_id']}:{r['user_id']}"),
                            InlineKeyboardButton("🔴 REJECT",callback_data=f"rejectproof:{r['qr_id']}:{r['user_id']}")])
        buttons.append([InlineKeyboardButton("🔙 ADMIN PANEL",callback_data="admin")])
        return await q.edit_message_text(text,reply_markup=InlineKeyboardMarkup(buttons))
    if d=="admin_wd":
        if q.from_user.id not in ADMIN_IDS: return
        c=conn(); rows=c.execute("""SELECT w.*,u.username,u.name FROM withdrawals w LEFT JOIN users u ON u.id=w.user_id ORDER BY w.id DESC LIMIT 30""").fetchall(); c.close()
        text="💸 WITHDRAWALS\n\n"+("\n".join(f"#{r['id']} • {r['name'] or r['user_id']} • ₹{r['amount']:.2f} • {r['method']} • {r['status']}" for r in rows) or "No withdrawals.")
        return await q.edit_message_text(text,reply_markup=back_kb())
    if d=="stats":
        if q.from_user.id not in ADMIN_IDS: return
        c=conn(); users=c.execute("SELECT COUNT(*) x FROM users").fetchone()["x"]; claims=c.execute("SELECT COUNT(*) x FROM claims").fetchone()["x"]; proofs=c.execute("SELECT COUNT(*) x FROM claims WHERE status='under_review'").fetchone()["x"]; paid=c.execute("SELECT COALESCE(SUM(amount),0) x FROM withdrawals WHERE status='paid'").fetchone()["x"]; earned=c.execute("SELECT COALESCE(SUM(total_earned),0) x FROM users").fetchone()["x"]; c.close()
        return await q.edit_message_text(f"📊 STATS\n\n👥 Users: {users}\n🎯 Claims: {claims}\n🧾 Pending proofs: {proofs}\n💰 Approved rewards: ₹{earned:.2f}\n💸 Paid withdrawals: ₹{paid:.2f}",reply_markup=back_kb())
    if d=="users":
        if q.from_user.id not in ADMIN_IDS: return
        c=conn(); rows=c.execute("SELECT id,name,username,balance FROM users ORDER BY id DESC LIMIT 20").fetchall(); c.close()
        text="👥 USERS\n\n"+("\n".join(f"{r['id']} • {r['name']} • {('@'+r['username']) if r['username'] else '—'} • ₹{r['balance']:.2f}" for r in rows) or "No users.")
        return await q.edit_message_text(text,reply_markup=back_kb())
    if d=="addbalance":
        if q.from_user.id not in ADMIN_IDS: return
        context.user_data["admin_step"]="balance_user"
        return await q.edit_message_text("💰 ADD BALANCE\n\nSend the Telegram User ID.",reply_markup=back_kb())
    if d=="forcejoin":
        if q.from_user.id not in ADMIN_IDS: return
        ch=get_setting("force_join_channel"); url=get_setting("force_join_url")
        return await q.edit_message_text(f"📢 FORCE JOIN\n\nStatus: {'ON' if ch else 'OFF'}\nChannel: {ch or '—'}\nURL: {url or '—'}\n\nUse the Mini App to change it.",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📊 OPEN DASHBOARD",web_app=WebAppInfo(url=WEBAPP_URL))] if WEBAPP_URL else [],[InlineKeyboardButton("🔙 ADMIN PANEL",callback_data="admin")]]))
    if d=="noop": return await q.answer()

async def text(update,context):
    t=update.message.text or ""
    uid=update.effective_user.id
    if uid not in ADMIN_IDS and not await require_join(update,context): return
    if t=="💰 WALLET": return await wallet(update,context)
    if t=="🎁 MY REWARDS": return await rewards(update,context)
    if t=="💸 WITHDRAW": return await withdraw(update,context)
    if t=="📜 TRANSACTIONS": return await transactions(update,context)
    if t=="👤 PROFILE": return await profile(update,context)
    if t=="ℹ️ HELP": return await help_cmd(update,context)
    if uid in ADMIN_IDS and context.user_data.get("admin_step")=="balance_user":
        if not t.isdigit(): return await update.message.reply_text("❌ Enter a numeric Telegram User ID.")
        context.user_data["admin_balance_user"]=int(t); context.user_data["admin_step"]="balance_amount"
        return await update.message.reply_text("💰 Enter amount to ADD.")
    if uid in ADMIN_IDS and context.user_data.get("admin_step")=="balance_amount":
        try: amount=float(t)
        except: return await update.message.reply_text("❌ Invalid amount.")
        if amount<=0: return await update.message.reply_text("❌ Amount must be positive.")
        target=context.user_data["admin_balance_user"]; c=conn(); user=c.execute("SELECT balance FROM users WHERE id=?",(target,)).fetchone()
        if not user: c.close(); return await update.message.reply_text("❌ User not found. They must start the bot first.")
        c.execute("UPDATE users SET balance=balance+? WHERE id=?",(amount,target)); c.execute("INSERT INTO transactions(user_id,amount,kind) VALUES(?,?,?)",(target,amount,"Admin balance added")); c.commit(); bal=c.execute("SELECT balance FROM users WHERE id=?",(target,)).fetchone()["balance"]; c.close(); context.user_data.pop("admin_step",None); context.user_data.pop("admin_balance_user",None)
        try: await context.bot.send_message(target,f"💰 Balance Added\n\n➕ ₹{amount:.2f}\n💵 New balance: ₹{bal:.2f}")
        except: pass
        return await update.message.reply_text(f"✅ Added ₹{amount:.2f} to user {target}.\nNew balance: ₹{bal:.2f}")
    if context.user_data.get("addqr_step")=="app" and uid in ADMIN_IDS:
        context.user_data["qr_app"]=t; context.user_data["addqr_step"]="reward"
        return await update.message.reply_text("💰 Enter reward amount in ₹, e.g. 15")
    if context.user_data.get("addqr_step")=="reward" and uid in ADMIN_IDS:
        try: reward=float(t)
        except: return await update.message.reply_text("❌ Enter a valid number.")
        if reward<=0: return await update.message.reply_text("❌ Reward must be positive.")
        c=conn(); cur=c.execute("INSERT INTO qrs(app,reward,file_id) VALUES(?,?,?)",(context.user_data["qr_app"],reward,context.user_data["qr_file_id"])); c.commit(); qid=cur.lastrowid; c.close()
        appname=context.user_data.pop("qr_app"); context.user_data.pop("addqr_step",None)
        return await update.message.reply_text(f"✅ QR CREATED\n\n🆔 QR ID: {qid}\n📱 App: {appname}\n💰 Reward: ₹{reward:.2f}\n\nUse /postqr {qid}",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📢 POST TO GROUP",callback_data=f"post:{qid}")],[InlineKeyboardButton("🔙 ADMIN PANEL",callback_data="admin")]]))
    if context.user_data.get("withdraw_step")=="amount":
        try: amount=float(t)
        except: return await update.message.reply_text("❌ Enter a valid amount.")
        c=conn(); row=c.execute("SELECT balance FROM users WHERE id=?",(uid,)).fetchone(); c.close(); bal=row["balance"] if row else 0
        if amount<MIN_WITHDRAWAL: return await update.message.reply_text(f"❌ Minimum withdrawal is ₹{MIN_WITHDRAWAL:.0f}.")
        if amount>bal: return await update.message.reply_text("❌ Insufficient balance.")
        context.user_data["withdraw_amount"]=amount; context.user_data["withdraw_step"]="method"
        methods=[]
        if ENABLE_UPI: methods.append(InlineKeyboardButton("🏦 UPI",callback_data="w:UPI"))
        if ENABLE_USDT: methods.append(InlineKeyboardButton("🪙 USDT",callback_data="w:USDT"))
        return await update.message.reply_text("Select withdrawal method:",reply_markup=InlineKeyboardMarkup([methods,[InlineKeyboardButton("🔙 CANCEL",callback_data="menu")]]))
    if context.user_data.get("withdraw_step")=="details":
        amount=context.user_data["withdraw_amount"]; method=context.user_data["withdraw_method"]
        c=conn(); row=c.execute("SELECT balance FROM users WHERE id=?",(uid,)).fetchone()
        if not row or amount>row["balance"]: c.close(); context.user_data.clear(); return await update.message.reply_text("❌ Insufficient balance.",reply_markup=main_kb())
        c.execute("UPDATE users SET balance=balance-? WHERE id=?",(amount,uid))
        c.execute("INSERT INTO withdrawals(user_id,amount,method,account,status) VALUES(?,?,?,?,'pending')",(uid,amount,method,t))
        c.execute("INSERT INTO transactions(user_id,amount,kind) VALUES(?,?,?)",(uid,-amount,"Withdrawal hold"))
        c.commit(); c.close(); context.user_data.clear()
        await update.message.reply_text(f"⏳ WITHDRAWAL PENDING\n\nAmount: ₹{amount:.2f}\nMethod: {method}\n\nYour request has been sent for admin review.",reply_markup=main_kb())
        return
    await update.message.reply_text("Use the menu buttons below.",reply_markup=main_kb())

async def withdrawal_admin_action(update,context,action,wid):
    if update.effective_user.id not in ADMIN_IDS: return
    c=conn(); w=c.execute("SELECT * FROM withdrawals WHERE id=?",(wid,)).fetchone()
    if not w: c.close(); return await update.callback_query.answer("Not found.",show_alert=True)
    if w["status"] in ("paid","rejected"): c.close(); return await update.callback_query.answer("Already completed.",show_alert=True)
    now=datetime.now(timezone.utc).isoformat()
    if action=="processing":
        c.execute("UPDATE withdrawals SET status='processing',updated_at=? WHERE id=? AND status='pending'",(now,wid))
        text=f"🔄 WITHDRAWAL PROCESSING\n\n₹{w['amount']:.2f} • {w['method']}"
    elif action=="paid":
        if w["status"]!="processing": c.close(); return await update.callback_query.answer("Set Processing first.",show_alert=True)
        c.execute("UPDATE withdrawals SET status='paid',updated_at=? WHERE id=? AND status='processing'",(now,wid))
        c.execute("UPDATE users SET total_withdrawn=total_withdrawn+? WHERE id=?",(w["amount"],w["user_id"]))
        text=f"✅ WITHDRAWAL PAID\n\n₹{w['amount']:.2f} • {w['method']}"
    else:
        c.execute("UPDATE withdrawals SET status='rejected',updated_at=? WHERE id=? AND status IN ('pending','processing')",(now,wid))
        c.execute("UPDATE users SET balance=balance+? WHERE id=?",(w["amount"],w["user_id"]))
        c.execute("INSERT INTO transactions(user_id,amount,kind) VALUES(?,?,?)",(w["user_id"],w["amount"],"Withdrawal refund"))
        text=f"🔴 WITHDRAWAL REJECTED\n\n₹{w['amount']:.2f} refunded to your balance."
    c.commit(); c.close()
    try: await context.bot.send_message(w["user_id"],text,reply_markup=main_kb())
    except: pass
    await update.callback_query.answer("Done.")

async def withdrawal_callbacks(update,context):
    d=update.callback_query.data
    if d.startswith("wd:"):
        _,action,wid=d.split(":"); return await withdrawal_admin_action(update,context,action,int(wid))

async def proof_button(update,context):
    q=update.callback_query; await q.answer()
    qid=int(q.data.split(":")[1])
    c=conn(); claim=c.execute("SELECT status FROM claims WHERE qr_id=? AND user_id=? ORDER BY id DESC LIMIT 1",(qid,q.from_user.id)).fetchone(); c.close()
    if not claim: return await q.message.reply_text("❌ Claim not found.")
    if claim["status"]=="under_review": return await q.message.reply_text("⏳ Already under review.")
    if claim["status"]!="claimed": return await q.message.reply_text("❌ This claim is no longer available.")
    context.user_data["proof_qid"]=qid; context.user_data["proof_step"]="photo"
    await q.message.reply_text("📤 UPLOAD PROOF\n\nSend your proof image here.")

# ----- Dashboard authentication/API -----
def validate_init_data(init_data):
    if not init_data or not TOKEN: return None
    try:
        data=dict(parse_qsl(init_data,keep_blank_values=True))
        received=data.pop("hash",None)
        if not received: return None
        check="\n".join(f"{k}={data[k]}" for k in sorted(data))
        secret=hmac.new(b"WebAppData",TOKEN.encode(),hashlib.sha256).digest()
        calc=hmac.new(secret,check.encode(),hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc,received): return None
        auth_date=int(data.get("auth_date","0"))
        if abs(datetime.now(timezone.utc).timestamp()-auth_date)>86400: return None
        user=json.loads(data.get("user","{}"))
        return user
    except Exception:
        return None

async def admin_user(request: Request):
    init=request.headers.get("X-Telegram-Init-Data","")
    user=validate_init_data(init)
    if not user or int(user.get("id",0)) not in ADMIN_IDS:
        raise HTTPException(status_code=403,detail="Telegram admin authentication failed. Open from the bot's Admin Dashboard button.")
    return user

@app_web.get("/",response_class=HTMLResponse)
async def home():
    return HTMLResponse(open("dashboard/index.html",encoding="utf-8").read())

@app_web.get("/health")
async def health(): return {"ok":True}

@app_web.get("/api/admin/overview")
async def overview(request:Request):
    await admin_user(request); c=conn()
    out={
      "users":c.execute("SELECT COUNT(*) x FROM users").fetchone()["x"],
      "qrs":c.execute("SELECT COUNT(*) x FROM qrs").fetchone()["x"],
      "available":c.execute("SELECT COUNT(*) x FROM qrs WHERE active=1").fetchone()["x"],
      "claims":c.execute("SELECT COUNT(*) x FROM claims").fetchone()["x"],
      "proofs":c.execute("SELECT COUNT(*) x FROM claims WHERE status='under_review'").fetchone()["x"],
      "withdrawals":c.execute("SELECT COUNT(*) x FROM withdrawals").fetchone()["x"],
      "pending_withdrawals":c.execute("SELECT COUNT(*) x FROM withdrawals WHERE status='pending'").fetchone()["x"],
      "paid":c.execute("SELECT COALESCE(SUM(amount),0) x FROM withdrawals WHERE status='paid'").fetchone()["x"]
    }; c.close(); return out

@app_web.get("/api/admin/proofs")
async def proofs(request:Request):
    await admin_user(request); c=conn()
    rows=c.execute("""SELECT c.id,c.qr_id,c.user_id,c.status,u.username,u.name,q.app app_name,q.reward,c.proof_file_id
                      FROM claims c JOIN users u ON u.id=c.user_id JOIN qrs q ON q.id=c.qr_id
                      WHERE c.status='under_review' ORDER BY c.id DESC LIMIT 50""").fetchall(); c.close()
    return {"proofs":[dict(r) for r in rows]}

@app_web.post("/api/admin/proof")
async def proof_action(request:Request):
    await admin_user(request); body=await request.json(); pid=int(body.get("id",0)); action=body.get("action")
    if action not in ("approve","reject"): raise HTTPException(400,"Invalid action")
    c=conn(); r=c.execute("""SELECT c.id,c.qr_id,c.user_id,c.status,q.reward FROM claims c JOIN qrs q ON q.id=c.qr_id WHERE c.id=?""",(pid,)).fetchone()
    if not r: c.close(); raise HTTPException(404,"Proof not found")
    if r["status"]!="under_review": c.close(); raise HTTPException(409,"Proof already processed")
    now=datetime.now(timezone.utc).isoformat()
    if action=="approve":
        c.execute("UPDATE claims SET status='approved',reviewed_at=? WHERE id=? AND status='under_review'",(now,pid))
        c.execute("UPDATE users SET balance=balance+?,total_earned=total_earned+? WHERE id=?",(r["reward"],r["reward"],r["user_id"]))
        c.execute("INSERT INTO transactions(user_id,amount,kind) VALUES(?,?,?)",(r["user_id"],r["reward"],f"QR #{r['qr_id']} approved"))
    else:
        c.execute("UPDATE claims SET status='rejected',reviewed_at=? WHERE id=? AND status='under_review'",(now,pid))
    c.commit(); c.close()
    return {"ok":True}

@app_web.get("/api/admin/withdrawals")
async def withdrawals_api(request:Request):
    await admin_user(request); c=conn()
    rows=c.execute("""SELECT w.*,u.username,u.name FROM withdrawals w LEFT JOIN users u ON u.id=w.user_id ORDER BY w.id DESC LIMIT 50""").fetchall(); c.close()
    return {"withdrawals":[dict(r) for r in rows]}

@app_web.post("/api/admin/withdrawal")
async def withdrawal_api(request:Request):
    user=await admin_user(request); body=await request.json(); wid=int(body.get("id",0)); action=body.get("action")
    if action not in ("processing","paid","reject"): raise HTTPException(400,"Invalid action")
    c=conn(); w=c.execute("SELECT * FROM withdrawals WHERE id=?",(wid,)).fetchone()
    if not w: c.close(); raise HTTPException(404,"Withdrawal not found")
    if w["status"] in ("paid","rejected"): c.close(); raise HTTPException(409,"Already completed")
    now=datetime.now(timezone.utc).isoformat()
    if action=="processing":
        if w["status"]!="pending": c.close(); raise HTTPException(409,"Not pending")
        c.execute("UPDATE withdrawals SET status='processing',updated_at=? WHERE id=?",(now,wid))
    elif action=="paid":
        if w["status"]!="processing": c.close(); raise HTTPException(409,"Set Processing first")
        c.execute("UPDATE withdrawals SET status='paid',updated_at=? WHERE id=?",(now,wid))
        c.execute("UPDATE users SET total_withdrawn=total_withdrawn+? WHERE id=?",(w["amount"],w["user_id"]))
    else:
        c.execute("UPDATE withdrawals SET status='rejected',updated_at=? WHERE id=?",(now,wid))
        c.execute("UPDATE users SET balance=balance+? WHERE id=?",(w["amount"],w["user_id"]))
        c.execute("INSERT INTO transactions(user_id,amount,kind) VALUES(?,?,?)",(w["user_id"],w["amount"],"Withdrawal refund"))
    c.commit(); c.close(); return {"ok":True}

async def balance_change(request:Request, sign):
    await admin_user(request); body=await request.json()
    try: uid=int(body.get("user_id")); amount=float(body.get("amount"))
    except: raise HTTPException(400,"Invalid user_id or amount")
    if amount<=0: raise HTTPException(400,"Amount must be positive")
    c=conn(); u=c.execute("SELECT balance FROM users WHERE id=?",(uid,)).fetchone()
    if not u: c.close(); raise HTTPException(404,"User not found")
    if sign<0 and amount>u["balance"]: c.close(); raise HTTPException(400,"Insufficient user balance")
    delta=amount*sign; reason=(body.get("reason") or "Admin adjustment").strip()
    c.execute("UPDATE users SET balance=balance+? WHERE id=?",(delta,uid))
    c.execute("INSERT INTO transactions(user_id,amount,kind) VALUES(?,?,?)",(uid,delta,f"Admin: {reason}"))
    c.commit(); bal=c.execute("SELECT balance FROM users WHERE id=?",(uid,)).fetchone()["balance"]; c.close()
    return {"ok":True, "added":amount if sign>0 else 0, "deducted":amount if sign<0 else 0, "balance":bal}

@app_web.post("/api/admin/add-balance")
async def add_balance(request:Request): return await balance_change(request,1)

@app_web.post("/api/admin/deduct-balance")
async def deduct_balance(request:Request): return await balance_change(request,-1)

@app_web.get("/api/admin/user")
async def user_lookup(request:Request, user_id:int):
    await admin_user(request); c=conn(); u=c.execute("SELECT * FROM users WHERE id=?",(user_id,)).fetchone()
    if not u: c.close(); raise HTTPException(404,"User not found")
    tx=c.execute("SELECT * FROM transactions WHERE user_id=? ORDER BY id DESC LIMIT 20",(user_id,)).fetchall()
    wd=c.execute("SELECT * FROM withdrawals WHERE user_id=? ORDER BY id DESC LIMIT 20",(user_id,)).fetchall(); c.close()
    return {"user":dict(u),"transactions":[dict(x) for x in tx],"withdrawals":[dict(x) for x in wd]}

@app_web.get("/api/admin/settings")
async def settings_api(request:Request):
    await admin_user(request)
    return {"force_join_channel":get_setting("force_join_channel"),"force_join_url":get_setting("force_join_url"),
            "upi":ENABLE_UPI,"usdt":ENABLE_USDT,"min_withdrawal":MIN_WITHDRAWAL}

@app_web.post("/api/admin/force-join")
async def force_join(request:Request):
    await admin_user(request); body=await request.json()
    channel=(body.get("channel") or "").strip(); url=(body.get("url") or "").strip()
    if channel and not url and channel.startswith("@"): url="https://t.me/"+channel[1:]
    if channel and url and not url.startswith("https://t.me/"): raise HTTPException(400,"Channel URL must start with https://t.me/")
    set_setting("force_join_channel",channel); set_setting("force_join_url",url)
    return {"ok":True,"enabled":bool(channel)}

def run_dashboard():
    port=int(os.getenv("PORT","8080"))
    uvicorn.run(app_web,host="0.0.0.0",port=port,log_level="info")

def run():
    if not TOKEN: raise RuntimeError("BOT_TOKEN is missing")
    if not WEBAPP_URL:
        logging.warning("WEBAPP_URL is empty; Mini App button will be disabled.")
    init_db()
    threading.Thread(target=run_dashboard,daemon=True).start()
    app=Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start",start))
    app.add_handler(CommandHandler("admin",admin))
    app.add_handler(CommandHandler("addqr",addqr))
    app.add_handler(CommandHandler("postqr",postqr))
    app.add_handler(CallbackQueryHandler(claim,pattern=r"^claim:\d+$"))
    app.add_handler(CallbackQueryHandler(proof_button,pattern=r"^proof:\d+$"))
    app.add_handler(CallbackQueryHandler(callbacks))
    app.add_handler(CallbackQueryHandler(withdrawal_callbacks,pattern=r"^wd:(processing|paid|reject):\d+$"))
    async def photo_router(update,context):
        if context.user_data.get("proof_step")=="photo" and update.effective_user.id not in ADMIN_IDS:
            if await proof_photo(update,context): return
        await photo(update,context)
    app.add_handler(MessageHandler(filters.PHOTO,photo_router))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,text))
    app.run_polling(drop_pending_updates=True)

if __name__=="__main__": run()
