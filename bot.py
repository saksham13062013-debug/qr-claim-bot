import os
import sqlite3
import logging
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from dotenv import load_dotenv
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove
)
from telegram.constants import ChatType
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, ConversationHandler, filters
)

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN", "")
DB_PATH = os.getenv("DB_PATH", "qr_claim_bot.db")
ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}
SUPPORT_URL = os.getenv("SUPPORT_URL", "https://t.me/your_support")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("qr_claim_bot")

QR_IMAGE, QR_APP, QR_REWARD = range(3)
WITHDRAW_AMOUNT, WITHDRAW_METHOD = range(10, 12)

def now():
    return datetime.now(timezone.utc).isoformat()

def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    con = db()
    con.executescript("""
    CREATE TABLE IF NOT EXISTS users(
      id INTEGER PRIMARY KEY, name TEXT NOT NULL, username TEXT,
      balance REAL NOT NULL DEFAULT 0, total_earned REAL NOT NULL DEFAULT 0,
      total_withdrawn REAL NOT NULL DEFAULT 0, created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS settings(
      key TEXT PRIMARY KEY, value TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS qrs(
      id INTEGER PRIMARY KEY AUTOINCREMENT, image_file_id TEXT NOT NULL,
      app_name TEXT NOT NULL, reward REAL NOT NULL, active INTEGER NOT NULL DEFAULT 1,
      created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS claims(
      id INTEGER PRIMARY KEY AUTOINCREMENT, qr_id INTEGER NOT NULL,
      user_id INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'claimed',
      proof_file_id TEXT, claimed_at TEXT NOT NULL, submitted_at TEXT,
      reviewed_at TEXT, reward REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS withdrawals(
      id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
      amount REAL NOT NULL, method TEXT NOT NULL,
      destination TEXT, status TEXT NOT NULL DEFAULT 'pending',
      created_at TEXT NOT NULL, reviewed_at TEXT
    );
    CREATE TABLE IF NOT EXISTS transactions(
      id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
      amount REAL NOT NULL, kind TEXT NOT NULL, note TEXT NOT NULL,
      created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS admins(
      user_id INTEGER PRIMARY KEY,
      role TEXT NOT NULL DEFAULT 'admin',
      added_at TEXT NOT NULL
    );
    """)
    con.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('min_withdraw','50')")
    con.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('method_upi','1')")
    con.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('force_join_enabled','0')")
    con.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('force_join_channels','')")
    con.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('method_usdt','1')")
    for aid in ADMIN_IDS:
        con.execute("INSERT OR IGNORE INTO admins(user_id,role,added_at) VALUES(?,?,?)",(aid,"owner",now()))
    con.commit(); con.close()

def upsert_user(u):
    con=db()
    con.execute("""INSERT INTO users(id,name,username,created_at) VALUES(?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET name=excluded.name, username=excluded.username""",
                (u.id, u.full_name, u.username, now()))
    con.commit(); con.close()

def get_user(uid):
    con=db(); row=con.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone(); con.close()
    return row

def setting(key, default=None):
    con=db(); row=con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone(); con.close()
    return row["value"] if row else default

def set_setting(key,value):
    con=db(); con.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(key,value)); con.commit(); con.close()

def money(v): return f"₹{float(v):,.2f}"

async def force_join_ok(bot, user_id):
    if setting("force_join_enabled","0") != "1":
        return True
    raw=setting("force_join_channels","")
    channels=[x.strip() for x in raw.split(",") if x.strip()]
    if not channels:
        return True
    for ch in channels:
        try:
            member=await bot.get_chat_member(ch, user_id)
            if member.status in ("left","kicked"):
                return False
        except Exception as e:
            log.warning("Force-join check failed for %s: %s", ch, e)
            # If the bot cannot verify a configured channel, fail closed.
            return False
    return True

def force_join_kb():
    raw=setting("force_join_channels","")
    channels=[x.strip() for x in raw.split(",") if x.strip()]
    rows=[]
    for ch in channels:
        label=ch if ch.startswith("@") else f"Join {ch}"
        url=f"https://t.me/{ch.lstrip('@')}" if ch.startswith("@") else None
        if url:
            rows.append([InlineKeyboardButton(f"📢 {label}",url=url)])
    rows.append([InlineKeyboardButton("✅ CHECK JOIN",callback_data="check_join")])
    return InlineKeyboardMarkup(rows)

async def require_join(update, context):
    if await force_join_ok(context.bot, update.effective_user.id):
        return True
    await update.message.reply_text(
        "🔒 JOIN REQUIRED\n\nTo use QR Claim Bot, please join all required channel(s) below.\n\n"
        "After joining, press ✅ CHECK JOIN.",
        reply_markup=force_join_kb()
    )
    return False

async def require_join_callback(update, context):
    if await force_join_ok(context.bot, update.effective_user.id):
        await update.callback_query.answer("✅ Verification successful!")
        await update.callback_query.message.reply_text("✅ You have joined all required channels.", reply_markup=main_keyboard())
        return True
    await update.callback_query.answer("❌ You haven't joined all required channels yet.", show_alert=True)
    return False

def main_keyboard():
    return ReplyKeyboardMarkup([
        [KeyboardButton("💰 WALLET"), KeyboardButton("🎁 MY REWARDS")],
        [KeyboardButton("💸 WITHDRAW"), KeyboardButton("📜 TRANSACTIONS")],
        [KeyboardButton("👤 PROFILE"), KeyboardButton("ℹ️ HELP")]
    ], resize_keyboard=True)

def back_kb():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 BACK TO MENU", callback_data="menu")]])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    upsert_user(update.effective_user)
    if not await force_join_ok(context.bot, update.effective_user.id):
        await update.message.reply_text(
            "🔒 JOIN REQUIRED\n\nTo use QR Claim Bot, please join all required channel(s) below.\n\n"
            "After joining, press ✅ CHECK JOIN.",
            reply_markup=force_join_kb()
        )
        return
    u=get_user(update.effective_user.id)
    text=(f"👋 Welcome to QR Claim Bot\n\n"
          f"🎯 Claim QR orders, complete tasks, earn rewards and withdraw your balance.\n\n"
          f"💰 Balance: {money(u['balance'])}")
    await update.message.reply_text(text, reply_markup=main_keyboard())

async def menu_cb(update, context):
    q=update.callback_query; await q.answer()
    await q.message.reply_text("🏠 Main Menu", reply_markup=main_keyboard())

async def wallet(update, context):
    u=get_user(update.effective_user.id)
    con=db(); p=con.execute("SELECT COALESCE(SUM(amount),0) x FROM withdrawals WHERE user_id=? AND status='pending'",(u["id"],)).fetchone()["x"]; con.close()
    text=(f"💰 MY WALLET\n\n💵 Available Balance: {money(u['balance'])}\n"
          f"🏆 Total Earned: {money(u['total_earned'])}\n💸 Total Withdrawn: {money(u['total_withdrawn'])}\n"
          f"⏳ Pending Withdrawal: {money(p)}")
    kb=InlineKeyboardMarkup([[InlineKeyboardButton("💸 WITHDRAW",callback_data="withdraw")],
                             [InlineKeyboardButton("📜 TRANSACTIONS",callback_data="transactions")],
                             [InlineKeyboardButton("🔙 BACK TO MENU",callback_data="menu")]])
    await update.message.reply_text(text, reply_markup=kb)

async def rewards(update, context):
    u=get_user(update.effective_user.id)
    con=db(); rows=con.execute("SELECT status,reward FROM claims WHERE user_id=? ORDER BY id DESC LIMIT 10",(u["id"],)).fetchall(); con.close()
    approved=sum(r["reward"] for r in rows if r["status"]=="approved")
    pending=sum(r["reward"] for r in rows if r["status"] in ("claimed","under_review"))
    rejected=sum(r["reward"] for r in rows if r["status"]=="rejected")
    lines=["🎁 MY REWARDS","",f"✅ Approved: {money(approved)}",f"⏳ Pending: {money(pending)}",f"❌ Rejected: {money(rejected)}",""]
    for r in rows:
        if r["status"]=="approved": lines.append(f"✅ +{money(r['reward'])} — QR Reward")
    kb=InlineKeyboardMarkup([[InlineKeyboardButton("📜 REWARD HISTORY",callback_data="transactions")],
                             [InlineKeyboardButton("💰 WALLET",callback_data="wallet")],
                             [InlineKeyboardButton("🔙 BACK TO MENU",callback_data="menu")]])
    await update.message.reply_text("\n".join(lines),reply_markup=kb)

async def transactions(update, context):
    uid=update.effective_user.id
    con=db(); rows=con.execute("SELECT amount,kind,note FROM transactions WHERE user_id=? ORDER BY id DESC LIMIT 10",(uid,)).fetchall(); con.close()
    lines=["📜 TRANSACTION HISTORY",""]
    for r in rows:
        sign="➕" if r["amount"]>=0 else "➖"
        lines.append(f"{sign} {money(abs(r['amount']))} — {r['note']}")
    if len(lines)==2: lines.append("No transactions yet.")
    kb=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ PREVIOUS",callback_data="noop"),InlineKeyboardButton("NEXT ▶️",callback_data="noop")],
                             [InlineKeyboardButton("🔙 BACK TO MENU",callback_data="menu")]])
    await update.message.reply_text("\n".join(lines),reply_markup=kb)

async def profile(update, context):
    u=get_user(update.effective_user.id)
    await update.message.reply_text(
        f"👤 MY PROFILE\n\nName: {u['name']}\nUsername: @{u['username'] if u['username'] else '—'}\n"
        f"Telegram ID: {u['id']}\n\n💰 Balance: {money(u['balance'])}\n"
        f"🎁 Total Rewards: {money(u['total_earned'])}\n💸 Total Withdrawn: {money(u['total_withdrawn'])}",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📜 TRANSACTIONS",callback_data="transactions")],
                                            [InlineKeyboardButton("🔙 BACK TO MENU",callback_data="menu")]]))

async def help_menu(update, context):
    await update.message.reply_text(
        "ℹ️ HOW IT WORKS\n\n1️⃣ Claim an available QR.\n2️⃣ Receive the QR in your private chat.\n"
        "3️⃣ Complete the required payment/action.\n4️⃣ Upload your proof.\n5️⃣ Submit it for review.\n"
        "6️⃣ After approval, your reward is added to your wallet.\n7️⃣ Withdraw your available balance.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📞 SUPPORT",url=SUPPORT_URL)],
                                            [InlineKeyboardButton("🔙 BACK TO MENU",callback_data="menu")]]))

async def claim_qr(update, context):
    if update.effective_chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    if not await force_join_ok(context.bot, update.effective_user.id):
        await update.callback_query.answer("Join the required channel(s) first.", show_alert=True)
        try:
            await context.bot.send_message(update.effective_user.id,
                "🔒 JOIN REQUIRED\n\nPlease join the required channel(s), then press CHECK JOIN.",
                reply_markup=force_join_kb())
        except Exception:
            pass
        return
    con=db(); qr=con.execute("SELECT * FROM qrs WHERE active=1 ORDER BY id LIMIT 1").fetchone(); con.close()
    if not qr:
        await update.callback_query.answer("No QR is currently available.",show_alert=True); return
    uid=update.effective_user.id
    con=db()
    exists=con.execute("SELECT 1 FROM claims WHERE qr_id=? AND user_id=? AND status IN ('claimed','under_review')",(qr["id"],uid)).fetchone()
    if exists:
        con.close(); await update.callback_query.answer("You already have an active claim.",show_alert=True); return
    con.execute("INSERT INTO claims(qr_id,user_id,reward,claimed_at) VALUES(?,?,?,?)",(qr["id"],uid,qr["reward"],now())); con.commit(); con.close()
    try:
        await context.bot.send_photo(uid, qr["image_file_id"], caption=f"📱 QR CLAIM\n\nQR App: {qr['app_name']}\n💰 Reward: {money(qr['reward'])}\n\nComplete the required action and upload your proof.",
                                      reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📤 UPLOAD PROOF",callback_data="upload_proof")]]))
        await update.callback_query.answer("QR sent to your private chat.")
    except Exception:
        await update.callback_query.answer("Open a private chat with the bot first and press /start.",show_alert=True)

async def group_claim_button(update, context):
    # Makes it easy to post the green primary action.
    if update.effective_chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        await update.message.reply_text("💳 CLAIM QR", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💳 CLAIM QR",callback_data="claim")]]))

async def upload_proof(update, context):
    await update.callback_query.answer()
    await update.callback_query.message.reply_text("📤 Send your proof image/document now.", reply_markup=back_kb())
    context.user_data["awaiting_proof"]=True

async def proof_received(update, context):
    if not context.user_data.get("awaiting_proof"): return
    uid=update.effective_user.id
    con=db(); claim=con.execute("SELECT * FROM claims WHERE user_id=? AND status='claimed' ORDER BY id DESC LIMIT 1",(uid,)).fetchone()
    if not claim:
        con.close(); context.user_data["awaiting_proof"]=False
        await update.message.reply_text("No active claim awaiting proof.",reply_markup=main_keyboard()); return
    file_id = update.message.photo[-1].file_id if update.message.photo else (update.message.document.file_id if update.message.document else None)
    if not file_id: await update.message.reply_text("Please upload an image or document."); return
    con.execute("UPDATE claims SET proof_file_id=? WHERE id=?",(file_id,claim["id"])); con.commit(); con.close()
    context.user_data["awaiting_proof"]=False
    await update.message.reply_text("Proof uploaded.",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🟢 SUBMIT FOR REVIEW",callback_data=f"submit:{claim['id']}")]]))

async def submit_review(update, context):
    q=update.callback_query; await q.answer()
    cid=int(q.data.split(":")[1]); uid=update.effective_user.id
    con=db(); claim=con.execute("SELECT * FROM claims WHERE id=? AND user_id=?",(cid,uid)).fetchone()
    if not claim or not claim["proof_file_id"] or claim["status"]!="claimed":
        con.close(); await q.message.reply_text("This proof cannot be submitted.",reply_markup=main_keyboard()); return
    con.execute("UPDATE claims SET status='under_review',submitted_at=? WHERE id=?",(now(),cid)); con.commit(); con.close()
    await q.message.reply_text("⏳ UNDER REVIEW\n\nYour proof has been submitted. Please wait for admin review.",reply_markup=main_keyboard())

async def withdraw_start(update, context):
    u=get_user(update.effective_user.id); minimum=float(setting("min_withdraw","50"))
    if u["balance"] < minimum:
        await update.message.reply_text(f"💸 WITHDRAW\n\n💰 Available Balance: {money(u['balance'])}\n\nMinimum withdrawal: {money(minimum)}\n\nYour balance is below the minimum.",reply_markup=main_keyboard()); return ConversationHandler.END
    await update.message.reply_text(f"💸 WITHDRAW\n\n💰 Available Balance: {money(u['balance'])}\n\nMinimum withdrawal: {money(minimum)}\n\nEnter the amount you want to withdraw.")
    return WITHDRAW_AMOUNT

async def withdraw_amount(update, context):
    try: amount=float(Decimal(update.message.text.strip()))
    except (InvalidOperation,ValueError):
        await update.message.reply_text("Enter a valid amount, e.g. 100"); return WITHDRAW_AMOUNT
    u=get_user(update.effective_user.id); minimum=float(setting("min_withdraw","50"))
    if amount < minimum or amount > u["balance"]:
        await update.message.reply_text(f"Amount must be between {money(minimum)} and {money(u['balance'])}."); return WITHDRAW_AMOUNT
    context.user_data["withdraw_amount"]=amount
    methods=[]
    if setting("method_upi","1")=="1": methods.append(InlineKeyboardButton("🏦 UPI",callback_data="wm:upi"))
    if setting("method_usdt","1")=="1": methods.append(InlineKeyboardButton("🪙 USDT",callback_data="wm:usdt"))
    methods.append(InlineKeyboardButton("🔙 CANCEL",callback_data="withdraw_cancel"))
    await update.message.reply_text(f"💸 WITHDRAWAL REQUEST\n\nAmount: {money(amount)}\nAvailable Balance: {money(u['balance'])}\n\nSelect withdrawal method:",
                                    reply_markup=InlineKeyboardMarkup([[*methods[:2]],[methods[-1]]]))
    return WITHDRAW_METHOD

async def withdraw_method(update, context):
    q=update.callback_query; await q.answer()
    if q.data=="withdraw_cancel":
        await q.message.reply_text("Withdrawal cancelled.",reply_markup=main_keyboard()); return ConversationHandler.END
    method=q.data.split(":")[1].upper()
    context.user_data["withdraw_method"]=method
    await q.message.reply_text(f"Enter your {method} destination (e.g. UPI ID or USDT address).")
    return WITHDRAW_METHOD

async def withdraw_destination(update, context):
    dest=update.message.text.strip(); uid=update.effective_user.id; amount=float(context.user_data["withdraw_amount"]); method=context.user_data["withdraw_method"]
    con=db(); u=con.execute("SELECT * FROM users WHERE id=?",(uid,)).fetchone()
    if not u or u["balance"]<amount: con.close(); await update.message.reply_text("Insufficient balance.",reply_markup=main_keyboard()); return ConversationHandler.END
    con.execute("UPDATE users SET balance=balance-? WHERE id=?",(amount,uid))
    con.execute("INSERT INTO withdrawals(user_id,amount,method,destination,created_at) VALUES(?,?,?,?,?)",(uid,amount,method,dest,now()))
    con.execute("INSERT INTO transactions(user_id,amount,kind,note,created_at) VALUES(?,?,?,?,?)",(uid,-amount,"withdrawal","Withdrawal",now()))
    con.commit(); con.close()
    await update.message.reply_text("✅ Withdrawal request submitted.\n\nStatus: ⏳ Pending",reply_markup=main_keyboard())
    return ConversationHandler.END

def admin_role(uid):
    con=db()
    row=con.execute("SELECT role FROM admins WHERE user_id=?",(uid,)).fetchone()
    con.close()
    return row["role"] if row else None

def admin_only(uid): return admin_role(uid) in ("admin","owner")
def owner_only(uid): return admin_role(uid) == "owner"

def admin_panel_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ ADD QR",callback_data="admin_addqr")],
        [InlineKeyboardButton("📋 PENDING CLAIMS",callback_data="admin_claims")],
        [InlineKeyboardButton("💸 WITHDRAWALS",callback_data="admin_withdrawals")],
        [InlineKeyboardButton("⚙️ WITHDRAWAL METHODS",callback_data="admin_methods")],
        [InlineKeyboardButton("👥 MANAGE ADMINS",callback_data="admin_manage")],
        [InlineKeyboardButton("📢 FORCE CHANNEL JOIN",callback_data="force_join")],
        [InlineKeyboardButton("📊 BOT STATISTICS",callback_data="admin_stats")]
    ])

async def admin(update, context):
    if not admin_only(update.effective_user.id): return
    await update.message.reply_text("🛠 ADMIN PANEL",reply_markup=admin_panel_kb())

async def manage_admins(update, context):
    q=update.callback_query; await q.answer()
    if not owner_only(q.from_user.id):
        await q.message.reply_text("🔒 Owner access required."); return
    con=db(); rows=con.execute("SELECT user_id,role FROM admins ORDER BY role DESC,user_id").fetchall(); con.close()
    lines=["👥 MANAGE ADMINS","", "👑 Current Admins"]
    for i,r in enumerate(rows,1):
        lines.append(f"{i}. ID: {r['user_id']} • {r['role'].upper()}")
    kb=InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ ADD ADMIN",callback_data="admin_add")],
        [InlineKeyboardButton("➖ REMOVE ADMIN",callback_data="admin_remove")],
        [InlineKeyboardButton("📋 ADMIN LIST",callback_data="admin_manage")],
        [InlineKeyboardButton("🔙 BACK TO ADMIN PANEL",callback_data="admin_back")]
    ])
    await q.message.reply_text("\n".join(lines),reply_markup=kb)

async def admin_add_prompt(update, context):
    q=update.callback_query; await q.answer()
    if not owner_only(q.from_user.id): return
    context.user_data["admin_action"]="add"
    await q.message.reply_text("➕ ADD ADMIN\n\nEnter the Telegram ID of the new admin:")

async def admin_remove_prompt(update, context):
    q=update.callback_query; await q.answer()
    if not owner_only(q.from_user.id): return
    context.user_data["admin_action"]="remove"
    await q.message.reply_text("➖ REMOVE ADMIN\n\nEnter the Telegram ID of the admin to remove:")

async def admin_id_input(update, context):
    action=context.user_data.get("admin_action")
    if not action or not owner_only(update.effective_user.id): return False
    try: target=int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("Please enter a valid numeric Telegram ID."); return True
    if action=="add":
        con=db(); con.execute("INSERT OR IGNORE INTO admins(user_id,role,added_at) VALUES(?,?,?)",(target,"admin",now())); con.commit(); con.close()
        await update.message.reply_text(f"✅ Admin added.\nTelegram ID: {target}",reply_markup=admin_panel_kb())
    else:
        if target==update.effective_user.id:
            await update.message.reply_text("You cannot remove yourself."); return True
        con=db(); row=con.execute("SELECT role FROM admins WHERE user_id=?",(target,)).fetchone()
        if row and row["role"]=="owner":
            con.close(); await update.message.reply_text("Owner accounts cannot be removed from this panel."); return True
        con.execute("DELETE FROM admins WHERE user_id=? AND role='admin'",(target,)); con.commit(); con.close()
        await update.message.reply_text(f"✅ Admin removed if the ID was an admin.\nTelegram ID: {target}",reply_markup=admin_panel_kb())
    context.user_data.pop("admin_action",None)
    return True

async def admin_stats(update, context):
    q=update.callback_query; await q.answer()
    if not admin_only(q.from_user.id): return
    con=db()
    users=con.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
    qrs=con.execute("SELECT COUNT(*) c FROM qrs WHERE active=1").fetchone()["c"]
    claims=con.execute("SELECT COUNT(*) c FROM claims").fetchone()["c"]
    pending=con.execute("SELECT COUNT(*) c FROM claims WHERE status='under_review'").fetchone()["c"]
    withdrawals=con.execute("SELECT COUNT(*) c FROM withdrawals WHERE status='pending'").fetchone()["c"]
    con.close()
    await q.message.reply_text(f"📊 BOT STATISTICS\n\n👤 Users: {users}\n📱 Active QRs: {qrs}\n🎯 Total Claims: {claims}\n⏳ Pending Claims: {pending}\n💸 Pending Withdrawals: {withdrawals}",reply_markup=admin_panel_kb())

async def admin_back(update, context):
    q=update.callback_query; await q.answer()
    await q.message.reply_text("🛠 ADMIN PANEL",reply_markup=admin_panel_kb())

async def force_join_admin(update, context):
    q=update.callback_query; await q.answer()
    if not admin_only(q.from_user.id): return
    enabled=setting("force_join_enabled","0")=="1"
    channels=setting("force_join_channels","")
    await q.message.reply_text(
        f"📢 FORCE CHANNEL JOIN\n\nStatus: {'🟢 ENABLED' if enabled else '🔴 DISABLED'}\n"
        f"Required Channels:\n{channels or 'None configured'}",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ ADD CHANNEL",callback_data="fj_add")],
            [InlineKeyboardButton("➖ REMOVE CHANNEL",callback_data="fj_remove")],
            [InlineKeyboardButton("🔄 ENABLE / DISABLE",callback_data="fj_toggle")],
            [InlineKeyboardButton("📋 CHANNEL LIST",callback_data="fj_list")],
            [InlineKeyboardButton("🔙 BACK TO ADMIN PANEL",callback_data="admin_back")]
        ]))

async def fj_add_prompt(update, context):
    q=update.callback_query; await q.answer()
    if not admin_only(q.from_user.id): return
    context.user_data["fj_action"]="add"
    await q.message.reply_text("➕ ADD CHANNEL\n\nSend the public channel username, e.g. @MyChannel")

async def fj_remove_prompt(update, context):
    q=update.callback_query; await q.answer()
    if not admin_only(q.from_user.id): return
    context.user_data["fj_action"]="remove"
    await q.message.reply_text("➖ REMOVE CHANNEL\n\nSend the channel username to remove, e.g. @MyChannel")

async def fj_text_input(update, context):
    action=context.user_data.get("fj_action")
    if not action or not admin_only(update.effective_user.id): return False
    ch=update.message.text.strip()
    if not ch.startswith("@") or len(ch)<2:
        await update.message.reply_text("Please enter a public channel username like @MyChannel."); return True
    channels=[x.strip() for x in setting("force_join_channels","").split(",") if x.strip()]
    if action=="add":
        if ch not in channels: channels.append(ch)
        set_setting("force_join_channels",",".join(channels))
        await update.message.reply_text(f"✅ Channel added: {ch}",reply_markup=admin_panel_kb())
    else:
        channels=[x for x in channels if x.lower()!=ch.lower()]
        set_setting("force_join_channels",",".join(channels))
        await update.message.reply_text(f"✅ Channel removed if configured: {ch}",reply_markup=admin_panel_kb())
    context.user_data.pop("fj_action",None)
    return True

async def fj_toggle(update, context):
    q=update.callback_query; await q.answer()
    if not admin_only(q.from_user.id): return
    current=setting("force_join_enabled","0")
    set_setting("force_join_enabled","0" if current=="1" else "1")
    await force_join_admin(update,context)

async def fj_list(update, context):
    q=update.callback_query; await q.answer()
    if not admin_only(q.from_user.id): return
    channels=[x for x in setting("force_join_channels","").split(",") if x]
    await q.message.reply_text("📋 CHANNEL LIST\n\n"+("\n".join(f"{i}. {x}" for i,x in enumerate(channels,1)) or "No channels configured."))

async def addqr_start(update, context):
    if not admin_only(update.effective_user.id): return ConversationHandler.END
    await update.message.reply_text("Send the QR image.")
    return QR_IMAGE

async def addqr_image(update, context):
    if not update.message.photo: await update.message.reply_text("Please send an image."); return QR_IMAGE
    context.user_data["qr_image"]=update.message.photo[-1].file_id
    await update.message.reply_text("Enter the QR app name.")
    return QR_APP

async def addqr_app(update, context):
    context.user_data["qr_app"]=update.message.text.strip()
    await update.message.reply_text("Enter reward amount in ₹, e.g. 100")
    return QR_REWARD

async def addqr_reward(update, context):
    try: reward=float(Decimal(update.message.text.strip()))
    except (InvalidOperation,ValueError): await update.message.reply_text("Enter a valid number."); return QR_REWARD
    con=db(); con.execute("INSERT INTO qrs(image_file_id,app_name,reward,created_at) VALUES(?,?,?,?)",(context.user_data["qr_image"],context.user_data["qr_app"],reward,now())); con.commit(); con.close()
    await update.message.reply_text(f"✅ QR created\nApp: {context.user_data['qr_app']}\nReward: {money(reward)}")
    return ConversationHandler.END

async def admin_claims(update, context):
    q=update.callback_query; await q.answer()
    con=db(); rows=con.execute("""SELECT c.id,c.user_id,c.reward,c.proof_file_id,q.app_name
        FROM claims c JOIN qrs q ON q.id=c.qr_id WHERE c.status='under_review' ORDER BY c.id""").fetchall(); con.close()
    if not rows: await q.message.reply_text("No pending claims."); return
    for r in rows:
        kb=InlineKeyboardMarkup([[InlineKeyboardButton("🟢 APPROVE",callback_data=f"approve:{r['id']}"),
                                  InlineKeyboardButton("🔴 REJECT",callback_data=f"reject:{r['id']}")]])
        if r["proof_file_id"]:
            try: await context.bot.send_document(update.effective_chat.id,r["proof_file_id"],caption=f"Claim #{r['id']} • User {r['user_id']} • {r['app_name']} • {money(r['reward'])}",reply_markup=kb)
            except: await q.message.reply_text(f"Claim #{r['id']} • User {r['user_id']} • {money(r['reward'])}",reply_markup=kb)

async def review(update, context):
    q=update.callback_query
    if not admin_only(q.from_user.id): await q.answer("Admin only",show_alert=True); return
    action,cid=q.data.split(":"); cid=int(cid)
    con=db(); claim=con.execute("SELECT * FROM claims WHERE id=?",(cid,)).fetchone()
    if not claim or claim["status"]!="under_review": con.close(); await q.answer("Already processed.",show_alert=True); return
    if action=="approve":
        con.execute("UPDATE claims SET status='approved',reviewed_at=? WHERE id=?",(now(),cid))
        con.execute("UPDATE users SET balance=balance+?, total_earned=total_earned+? WHERE id=?",(claim["reward"],claim["reward"],claim["user_id"]))
        con.execute("INSERT INTO transactions(user_id,amount,kind,note,created_at) VALUES(?,?,?,?,?)",(claim["user_id"],claim["reward"],"reward","QR Reward",now()))
        msg=f"✅ Claim #{cid} approved."
    else:
        con.execute("UPDATE claims SET status='rejected',reviewed_at=? WHERE id=?",(now(),cid))
        msg=f"❌ Claim #{cid} rejected."
    con.commit(); con.close(); await q.edit_message_reply_markup(reply_markup=None); await q.message.reply_text(msg)
    try: await context.bot.send_message(claim["user_id"],msg)
    except: pass

async def admin_withdrawals(update, context):
    q=update.callback_query; await q.answer()
    con=db(); rows=con.execute("SELECT * FROM withdrawals WHERE status='pending' ORDER BY id").fetchall(); con.close()
    if not rows: await q.message.reply_text("No pending withdrawals."); return
    for r in rows:
        kb=InlineKeyboardMarkup([[InlineKeyboardButton("🟢 PAY / APPROVE",callback_data=f"wp:{r['id']}"),
                                  InlineKeyboardButton("🔴 REJECT / REFUND",callback_data=f"wr:{r['id']}")]])
        await q.message.reply_text(f"Withdrawal #{r['id']}\nUser: {r['user_id']}\nAmount: {money(r['amount'])}\nMethod: {r['method']}\nDestination: {r['destination']}",reply_markup=kb)

async def notify_withdrawal_status(bot, withdrawal, status):
    """Send a user notification for withdrawal status changes."""
    wid=withdrawal["id"]; amount=withdrawal["amount"]; method=withdrawal["method"]; uid=withdrawal["user_id"]
    messages={
        "processing": f"⚙️ WITHDRAWAL PROCESSING\\n\\nWithdrawal ID: #{wid}\\nAmount: {money(amount)}\\nMethod: {method}\\n\\nYour withdrawal is being processed. You will receive another notification once it is marked as paid.",
        "paid": f"✅ WITHDRAWAL PAID\\n\\nWithdrawal ID: #{wid}\\nAmount: {money(amount)}\\nMethod: {method}\\n\\nYour withdrawal has been marked as paid successfully. 🎉",
        "rejected": f"❌ WITHDRAWAL REJECTED\\n\\nWithdrawal ID: #{wid}\\nAmount: {money(amount)}\\nMethod: {method}\\n\\nYour withdrawal was rejected and the amount has been refunded to your wallet."
    }
    if status not in messages: return
    try: await bot.send_message(uid,messages[status])
    except Exception as e: log.warning("Could not send withdrawal notification to %s: %s",uid,e)

async def withdrawal_review(update, context):
    q=update.callback_query
    if not admin_only(q.from_user.id): await q.answer("Admin only",show_alert=True); return
    action,wid=q.data.split(":"); wid=int(wid)
    con=db(); w=con.execute("SELECT * FROM withdrawals WHERE id=?",(wid,)).fetchone()
    if not w or w["status"]!="pending": con.close(); await q.answer("Already processed.",show_alert=True); return
    if action=="wp":
        con.execute("UPDATE withdrawals SET status='paid',reviewed_at=? WHERE id=?",(now(),wid))
        con.execute("UPDATE users SET total_withdrawn=total_withdrawn+? WHERE id=?",(w["amount"],w["user_id"]))
        msg=f"✅ Withdrawal #{wid} marked paid."
    else:
        con.execute("UPDATE withdrawals SET status='rejected',reviewed_at=? WHERE id=?",(now(),wid))
        con.execute("UPDATE users SET balance=balance+? WHERE id=?",(w["amount"],w["user_id"]))
        con.execute("INSERT INTO transactions(user_id,amount,kind,note,created_at) VALUES(?,?,?,?,?)",(w["user_id"],w["amount"],"refund","Withdrawal Refund",now()))
        msg=f"↩️ Withdrawal #{wid} rejected and refunded."
    con.commit(); con.close(); await q.edit_message_reply_markup(reply_markup=None); await q.message.reply_text(msg)
    try: await context.bot.send_message(w["user_id"],msg)
    except: pass

async def admin_methods(update, context):
    q=update.callback_query; await q.answer()
    await q.message.reply_text(f"🏦 UPI: {'ON' if setting('method_upi')=='1' else 'OFF'}\n🪙 USDT: {'ON' if setting('method_usdt')=='1' else 'OFF'}\nMinimum withdrawal: {money(setting('min_withdraw','50'))}\n\nUse /enablemethod upi|usdt, /disablemethod upi|usdt and /setminwithdraw 50.")

async def enable_method(update, context):
    if not admin_only(update.effective_user.id) or not context.args or context.args[0].lower() not in ("upi","usdt"): return
    set_setting("method_"+context.args[0].lower(),"1"); await update.message.reply_text("Enabled.")

async def disable_method(update, context):
    if not admin_only(update.effective_user.id) or not context.args or context.args[0].lower() not in ("upi","usdt"): return
    set_setting("method_"+context.args[0].lower(),"0"); await update.message.reply_text("Disabled.")

async def set_min(update, context):
    if not admin_only(update.effective_user.id) or not context.args: return
    try: v=float(Decimal(context.args[0]))
    except: await update.message.reply_text("Invalid amount."); return
    set_setting("min_withdraw",str(v)); await update.message.reply_text(f"Minimum withdrawal set to {money(v)}.")

async def button_router(update, context):
    q=update.callback_query; data=q.data
    if data=="wallet": await q.answer(); await q.message.reply_text("Use the WALLET button below.",reply_markup=main_keyboard())
    elif data=="transactions": await q.answer(); uid=q.from_user.id; con=db(); rows=con.execute("SELECT amount,note FROM transactions WHERE user_id=? ORDER BY id DESC LIMIT 10",(uid,)).fetchall(); con.close(); txt="📜 TRANSACTION HISTORY\n\n" + ("\n".join(("➕" if r["amount"]>=0 else "➖")+f" {money(abs(r['amount']))} — {r['note']}" for r in rows) or "No transactions yet."); await q.message.reply_text(txt,reply_markup=back_kb())
    elif data=="menu": await menu_cb(update,context)
    elif data=="claim": await claim_qr(update,context)
    elif data=="upload_proof": await upload_proof(update,context)
    elif data.startswith("submit:"): await submit_review(update,context)
    elif data=="withdraw": await q.answer(); await q.message.reply_text("Use the 💸 WITHDRAW button in the menu.",reply_markup=main_keyboard())
    elif data=="noop": await q.answer("No more pages.")
    elif data=="admin_claims": await admin_claims(update,context)
    elif data=="admin_withdrawals": await admin_withdrawals(update,context)
    elif data=="admin_methods": await admin_methods(update,context)
    elif data=="admin_addqr": await q.answer(); await q.message.reply_text("Use /addqr to create a QR.")
    elif data=="admin_manage": await manage_admins(update,context)
    elif data=="force_join": await force_join_admin(update,context)
    elif data=="fj_add": await fj_add_prompt(update,context)
    elif data=="fj_remove": await fj_remove_prompt(update,context)
    elif data=="fj_toggle": await fj_toggle(update,context)
    elif data=="fj_list": await fj_list(update,context)
    elif data=="check_join": await require_join_callback(update,context)
    elif data=="admin_add": await admin_add_prompt(update,context)
    elif data=="admin_remove": await admin_remove_prompt(update,context)
    elif data=="admin_stats": await admin_stats(update,context)
    elif data=="admin_back": await admin_back(update,context)
    elif data.startswith("approve:") or data.startswith("reject:"): await review(update,context)
    elif data.startswith("wp:") or data.startswith("wr:"): await withdrawal_review(update,context)
    else: await q.answer()

def build_app():
    init_db()
    app=Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start",start))
    app.add_handler(CommandHandler("admin",admin))
    app.add_handler(CommandHandler("setminwithdraw",set_min))
    app.add_handler(CommandHandler("enablemethod",enable_method))
    app.add_handler(CommandHandler("disablemethod",disable_method))
    app.add_handler(ConversationHandler(entry_points=[CommandHandler("addqr",addqr_start)],
        states={QR_IMAGE:[MessageHandler(filters.PHOTO,addqr_image)],QR_APP:[MessageHandler(filters.TEXT & ~filters.COMMAND,addqr_app)],QR_REWARD:[MessageHandler(filters.TEXT & ~filters.COMMAND,addqr_reward)]},
        fallbacks=[]))
    app.add_handler(ConversationHandler(entry_points=[MessageHandler(filters.Regex("^💸 WITHDRAW$"),withdraw_start)],
        states={WITHDRAW_AMOUNT:[MessageHandler(filters.TEXT & ~filters.COMMAND,withdraw_amount)],
                WITHDRAW_METHOD:[CallbackQueryHandler(withdraw_method,pattern=r"^(wm:|withdraw_cancel)"),
                                 MessageHandler(filters.TEXT & ~filters.COMMAND,withdraw_destination)]},
        fallbacks=[CommandHandler("start",start)]))
    app.add_handler(MessageHandler(filters.Regex("^💰 WALLET$"),wallet))
    app.add_handler(MessageHandler(filters.Regex("^🎁 MY REWARDS$"),rewards))
    app.add_handler(MessageHandler(filters.Regex("^📜 TRANSACTIONS$"),transactions))
    app.add_handler(MessageHandler(filters.Regex("^👤 PROFILE$"),profile))
    app.add_handler(MessageHandler(filters.Regex("^ℹ️ HELP$"),help_menu))
    app.add_handler(CallbackQueryHandler(button_router))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,fj_text_input),group=0)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,admin_id_input),group=1)
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL,proof_received))
    app.add_handler(CommandHandler("claim",group_claim_button))
    return app

if __name__=="__main__":
    if not TOKEN:
        raise SystemExit("BOT_TOKEN is missing. Put it in .env")
    build_app().run_polling(allowed_updates=Update.ALL_TYPES)
