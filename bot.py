
import os, sqlite3, logging, asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, ContextTypes, filters

logging.basicConfig(level=logging.INFO)
TOKEN=os.getenv("BOT_TOKEN")
ADMIN_IDS={int(x.strip()) for x in os.getenv("ADMIN_IDS","").split(",") if x.strip().isdigit()}
DB_PATH=os.getenv("DB_PATH","qr_claim_bot.db")
GROUP_ID=os.getenv("GROUP_ID","")
SUPPORT_URL=os.getenv("SUPPORT_URL","https://t.me/itsvanshu")
MIN_WITHDRAWAL=float(os.getenv("MIN_WITHDRAWAL","50"))

def conn():
    c=sqlite3.connect(DB_PATH)
    c.row_factory=sqlite3.Row
    return c

def init_db():
    c=conn()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS users(
      id INTEGER PRIMARY KEY,name TEXT,username TEXT,balance REAL DEFAULT 0,
      total_earned REAL DEFAULT 0,total_withdrawn REAL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS qrs(
      id INTEGER PRIMARY KEY AUTOINCREMENT,app TEXT,reward REAL,file_id TEXT,
      active INTEGER DEFAULT 1,group_chat_id TEXT,group_message_id INTEGER,claimed_by INTEGER,claimed_at TEXT,created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS claims(
      id INTEGER PRIMARY KEY AUTOINCREMENT,qr_id INTEGER,user_id INTEGER,
      status TEXT DEFAULT 'claimed',proof_file_id TEXT,created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS withdrawals(
      id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER,amount REAL,method TEXT,
      status TEXT DEFAULT 'pending',created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS transactions(
      id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER,amount REAL,kind TEXT,
      created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    """)
    # Migrate older databases created before single-claim tracking was added.
    cols={row[1] for row in c.execute("PRAGMA table_info(qrs)").fetchall()}
    for col, definition in [("group_chat_id","TEXT"),("group_message_id","INTEGER"),("claimed_by","INTEGER"),("claimed_at","TEXT")]:
        if col not in cols:
            c.execute(f"ALTER TABLE qrs ADD COLUMN {col} {definition}")
    c.commit(); c.close()

def main_kb():
    return ReplyKeyboardMarkup([
        ["💰 WALLET","🎁 MY REWARDS"],
        ["💸 WITHDRAW","📜 TRANSACTIONS"],
        ["👤 PROFILE","ℹ️ HELP"]
    ],resize_keyboard=True)

def back_kb():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 BACK TO MENU",callback_data="menu")]])

async def ensure_user(u):
    c=conn()
    c.execute("""INSERT INTO users(id,name,username) VALUES(?,?,?)
                ON CONFLICT(id) DO UPDATE SET name=excluded.name,username=excluded.username""",
              (u.id,u.full_name,u.username or ""))
    c.commit(); c.close()

async def start(update,context):
    await ensure_user(update.effective_user)
    c=conn(); bal=c.execute("SELECT balance FROM users WHERE id=?",(update.effective_user.id,)).fetchone()["balance"]; c.close()
    await update.message.reply_text(
        f"👋 Welcome to QR Claim Bot\n\n🎯 Claim QR orders, complete tasks, earn rewards and withdraw your balance.\n\n💰 Balance: ₹{bal:.2f}",
        reply_markup=main_kb())

async def wallet(update,context):
    await ensure_user(update.effective_user)
    c=conn(); u=c.execute("SELECT * FROM users WHERE id=?",(update.effective_user.id,)).fetchone()
    p=c.execute("SELECT COALESCE(SUM(amount),0) x FROM withdrawals WHERE user_id=? AND status IN ('pending','processing')",(u["id"],)).fetchone()["x"]; c.close()
    kb=InlineKeyboardMarkup([
      [InlineKeyboardButton("💸 WITHDRAW",callback_data="withdraw"),InlineKeyboardButton("📜 TRANSACTIONS",callback_data="tx")],
      [InlineKeyboardButton("🔙 BACK TO MENU",callback_data="menu")]
    ])
    await update.message.reply_text(f"💰 MY WALLET\n\n💵 Available Balance: ₹{u['balance']:.2f}\n🏆 Total Earned: ₹{u['total_earned']:.2f}\n💸 Total Withdrawn: ₹{u['total_withdrawn']:.2f}\n⏳ Pending Withdrawal: ₹{p:.2f}",reply_markup=kb)

async def rewards(update,context):
    c=conn(); rows=c.execute("SELECT amount,kind,created_at FROM transactions WHERE user_id=? AND amount>0 ORDER BY id DESC LIMIT 10",(update.effective_user.id,)).fetchall(); c.close()
    text="🎁 MY REWARDS\n\n"
    text+="\n".join(f"✅ ₹{r['amount']:.2f} — {r['kind']}" for r in rows) or "No approved rewards yet."
    await update.message.reply_text(text,reply_markup=InlineKeyboardMarkup([
      [InlineKeyboardButton("📜 REWARD HISTORY",callback_data="tx"),InlineKeyboardButton("💰 WALLET",callback_data="wallet")],
      [InlineKeyboardButton("🔙 BACK TO MENU",callback_data="menu")]
    ]))

async def transactions(update,context):
    c=conn(); rows=c.execute("SELECT amount,kind FROM transactions WHERE user_id=? ORDER BY id DESC LIMIT 20",(update.effective_user.id,)).fetchall(); c.close()
    text="📜 TRANSACTION HISTORY\n\n"
    text+="\n".join(("➕" if r["amount"]>=0 else "➖")+f" ₹{abs(r['amount']):.2f} — {r['kind']}" for r in rows) or "No transactions yet."
    await update.message.reply_text(text,reply_markup=InlineKeyboardMarkup([
      [InlineKeyboardButton("◀️ PREVIOUS",callback_data="noop"),InlineKeyboardButton("NEXT ▶️",callback_data="noop")],
      [InlineKeyboardButton("🔙 BACK TO MENU",callback_data="menu")]
    ]))

async def profile(update,context):
    await ensure_user(update.effective_user)
    c=conn(); u=c.execute("SELECT * FROM users WHERE id=?",(update.effective_user.id,)).fetchone(); c.close()
    await update.message.reply_text(
      f"👤 MY PROFILE\n\nName: {u['name']}\nUsername: @{u['username'] or '—'}\nTelegram ID: {u['id']}\n\n💰 Balance: ₹{u['balance']:.2f}\n🎁 Total Rewards: ₹{u['total_earned']:.2f}\n💸 Total Withdrawn: ₹{u['total_withdrawn']:.2f}",
      reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📜 TRANSACTIONS",callback_data="tx")],[InlineKeyboardButton("🔙 BACK TO MENU",callback_data="menu")]]))

async def help_cmd(update,context):
    await update.message.reply_text(
      "ℹ️ HOW IT WORKS\n\n1️⃣ Claim an available QR.\n2️⃣ Receive the QR in your private chat.\n3️⃣ Complete the required payment/action.\n4️⃣ Upload your proof.\n5️⃣ Submit it for review.\n6️⃣ After approval, your reward is added to your wallet.\n7️⃣ Withdraw your available balance.",
      reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📞 SUPPORT",url=SUPPORT_URL)],[InlineKeyboardButton("🔙 BACK TO MENU",callback_data="menu")]]))

async def withdraw(update,context):
    c=conn(); bal=c.execute("SELECT balance FROM users WHERE id=?",(update.effective_user.id,)).fetchone()["balance"]; c.close()
    context.user_data["withdraw_step"]="amount"
    await update.message.reply_text(f"💸 WITHDRAW\n\n💰 Available Balance: ₹{bal:.2f}\n\nMinimum withdrawal: ₹{MIN_WITHDRAWAL:.0f}\n\nEnter the amount you want to withdraw.",
      reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 CANCEL",callback_data="menu")]]))

async def admin(update,context):
    if update.effective_user.id not in ADMIN_IDS: return
    c=conn()
    users=c.execute("SELECT COUNT(*) x FROM users").fetchone()["x"]
    qrs=c.execute("SELECT COUNT(*) x FROM qrs WHERE active=1").fetchone()["x"]
    pending=c.execute("SELECT COUNT(*) x FROM withdrawals WHERE status='pending'").fetchone()["x"]
    total=c.execute("SELECT COALESCE(SUM(amount),0) x FROM withdrawals").fetchone()["x"]
    c.close()
    kb=InlineKeyboardMarkup([
      [InlineKeyboardButton("➕ ADD QR",callback_data="addqr"),InlineKeyboardButton("📢 POST TO GROUP",callback_data="posthelp")],
      [InlineKeyboardButton("💸 WITHDRAWALS",callback_data="admin_wd"),InlineKeyboardButton("📊 STATS",callback_data="stats")],
      [InlineKeyboardButton("👥 USERS",callback_data="users"),InlineKeyboardButton("📢 FORCE JOIN",callback_data="forcejoin")]
    ])
    await update.message.reply_text(f"🛠 ADMIN PANEL\n\n👥 Users: {users}\n🎯 Active QR: {qrs}\n⏳ Pending withdrawals: {pending}\n💸 Total withdrawal requests: ₹{total:.2f}",reply_markup=kb)

async def addqr(update,context):
    if update.effective_user.id not in ADMIN_IDS: return
    context.user_data["addqr_step"]="photo"
    await update.message.reply_text("➕ ADD QR\n\n📷 Send the QR image.\n\nThe QR will NOT be posted in the group.")

async def photo(update,context):
    if update.effective_user.id not in ADMIN_IDS or context.user_data.get("addqr_step")!="photo": return
    context.user_data["qr_file_id"]=update.message.photo[-1].file_id
    context.user_data["addqr_step"]="app"
    await update.message.reply_text("📱 Enter the QR app name.")

async def text(update,context):
    t=update.message.text
    if t=="💰 WALLET": return await wallet(update,context)
    if t=="🎁 MY REWARDS": return await rewards(update,context)
    if t=="💸 WITHDRAW": return await withdraw(update,context)
    if t=="📜 TRANSACTIONS": return await transactions(update,context)
    if t=="👤 PROFILE": return await profile(update,context)
    if t=="ℹ️ HELP": return await help_cmd(update,context)

    if context.user_data.get("addqr_step")=="app" and update.effective_user.id in ADMIN_IDS:
        context.user_data["qr_app"]=t; context.user_data["addqr_step"]="reward"
        return await update.message.reply_text("💰 Enter reward amount in ₹, e.g. 15")
    if context.user_data.get("addqr_step")=="reward" and update.effective_user.id in ADMIN_IDS:
        try: reward=float(t)
        except: return await update.message.reply_text("❌ Enter a valid number.")
        c=conn(); cur=c.execute("INSERT INTO qrs(app,reward,file_id) VALUES(?,?,?)",(context.user_data["qr_app"],reward,context.user_data["qr_file_id"])); c.commit(); qid=cur.lastrowid; c.close()
        context.user_data.pop("addqr_step",None)
        await update.message.reply_text(f"✅ QR CREATED\n\n🆔 QR ID: {qid}\n📱 App: {context.user_data.pop('qr_app')}\n💰 Reward: ₹{reward:.2f}\n\nNow press POST TO GROUP or use:\n/postqr {qid}",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📢 POST TO GROUP",callback_data=f"post:{qid}")],[InlineKeyboardButton("🔙 ADMIN PANEL",callback_data="admin")]]))
        return
    if context.user_data.get("withdraw_step")=="amount":
        try: amount=float(t)
        except: return await update.message.reply_text("❌ Enter a valid amount.")
        c=conn(); bal=c.execute("SELECT balance FROM users WHERE id=?",(update.effective_user.id,)).fetchone()["balance"]; c.close()
        if amount<MIN_WITHDRAWAL: return await update.message.reply_text(f"Minimum withdrawal is ₹{MIN_WITHDRAWAL:.0f}.")
        if amount>bal: return await update.message.reply_text("❌ Insufficient balance.")
        context.user_data["withdraw_amount"]=amount; context.user_data["withdraw_step"]="method"
        return await update.message.reply_text(f"💸 WITHDRAWAL REQUEST\n\nAmount: ₹{amount:.2f}\nAvailable Balance: ₹{bal:.2f}\n\nSelect withdrawal method:",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏦 UPI",callback_data="w:UPI"),InlineKeyboardButton("🪙 USDT",callback_data="w:USDT")],[InlineKeyboardButton("🔙 CANCEL",callback_data="menu")]]))
    if context.user_data.get("withdraw_step")=="details":
        context.user_data["withdraw_details"]=t
        amount=context.user_data["withdraw_amount"]; method=context.user_data["withdraw_method"]
        c=conn()
        c.execute("UPDATE users SET balance=balance-? WHERE id=?",(amount,update.effective_user.id))
        c.execute("INSERT INTO withdrawals(user_id,amount,method,status) VALUES(?,?,?,'pending')",(update.effective_user.id,amount,method))
        c.execute("INSERT INTO transactions(user_id,amount,kind) VALUES(?,?,?)",(update.effective_user.id,-amount,"Withdrawal"))
        c.commit(); c.close()
        context.user_data.clear()
        await update.message.reply_text(f"⏳ WITHDRAWAL PENDING\n\nAmount: ₹{amount:.2f}\nMethod: {method}\n\nYour request has been sent for admin review.",reply_markup=main_kb())
        return
    await update.message.reply_text("Use the menu buttons below.",reply_markup=main_kb())

async def postqr(update,context,qid=None):
    if update.effective_user.id not in ADMIN_IDS: return
    if qid is None:
        if not context.args: return await update.message.reply_text("Usage: /postqr QR_ID")
        qid=int(context.args[0])
    if not GROUP_ID: return await update.message.reply_text("❌ Add GROUP_ID in Railway Variables first.")
    c=conn(); r=c.execute("SELECT * FROM qrs WHERE id=? AND active=1",(qid,)).fetchone(); c.close()
    if not r: return await update.message.reply_text("❌ QR not found.")
    kb=InlineKeyboardMarkup([[InlineKeyboardButton("💳 CLAIM QR",callback_data=f"claim:{qid}")]])
    msg=await context.bot.send_message(GROUP_ID,f"🎯 QR CLAIM AVAILABLE\n\n📱 App: {r['app']}\n💰 Reward: ₹{r['reward']:.2f}\n\n🔒 Only ONE user can claim this QR.\nClaim the QR to receive it in your private chat.",reply_markup=kb)
    c=conn(); c.execute("UPDATE qrs SET group_chat_id=?,group_message_id=? WHERE id=?",(str(GROUP_ID),msg.message_id,qid)); c.commit(); c.close()
    await update.message.reply_text("✅ Posted to group.\n\n🔒 QR image was NOT posted in the group.\n👤 Only the first successful claimant can receive it.")

async def claim(update,context):
    q=update.callback_query
    qid=int(q.data.split(":")[1])
    uid=q.from_user.id

    # The user must have opened the bot first so the bot can DM the QR.
    c=conn(); started=c.execute("SELECT 1 FROM users WHERE id=?",(uid,)).fetchone(); c.close()
    if not started:
        return await q.answer("❌ Start the bot in private chat first, then claim again.",show_alert=True)

    # Atomic reservation: only one user can move an active QR to claimed.
    c=conn()
    try:
        c.execute("BEGIN IMMEDIATE")
        r=c.execute("SELECT * FROM qrs WHERE id=? AND active=1",(qid,)).fetchone()
        if not r:
            c.rollback(); c.close()
            return await q.answer("❌ This QR has already been claimed.",show_alert=True)

        existing=c.execute("SELECT id,status FROM claims WHERE qr_id=?",(qid,)).fetchone()
        if existing:
            c.execute("UPDATE qrs SET active=0 WHERE id=?",(qid,)); c.commit(); c.close()
            return await q.answer("❌ This QR has already been claimed.",show_alert=True)

        c.execute("UPDATE qrs SET active=0,claimed_by=?,claimed_at=CURRENT_TIMESTAMP WHERE id=? AND active=1",(uid,qid))
        if c.execute("SELECT changes()").fetchone()[0] != 1:
            c.rollback(); c.close()
            return await q.answer("❌ This QR has already been claimed.",show_alert=True)
        claim_cur=c.execute("INSERT INTO claims(qr_id,user_id) VALUES(?,?)",(qid,uid))
        claim_id=claim_cur.lastrowid
        c.commit()
    except Exception:
        c.rollback(); c.close()
        logging.exception("Claim reservation failed")
        return await q.answer("❌ Could not claim this QR. Please try again.",show_alert=True)
    c.close()

    # Show the claimant in the group and remove the claim button.
    display_name=(q.from_user.username and "@"+q.from_user.username) or q.from_user.full_name
    if r["group_chat_id"] and r["group_message_id"]:
        try:
            await context.bot.edit_message_text(
                chat_id=r["group_chat_id"],message_id=r["group_message_id"],
                text=f"🎯 QR CLAIMED\n\n📱 App: {r['app']}\n💰 Reward: ₹{r['reward']:.2f}\n\n👤 Claimed by: {display_name}\n🔒 This QR is no longer available.",
                reply_markup=None)
        except Exception:
            logging.exception("Could not update group claim message")

    try:
        await context.bot.send_photo(
            uid,r["file_id"],
            caption=f"🎯 QR CLAIM\n\n📱 App: {r['app']}\n💰 Reward: ₹{r['reward']:.2f}\n\n👤 Claimed by: {display_name}\n\nComplete the required action and upload your proof.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📤 UPLOAD PROOF",callback_data=f"proof:{qid}")]])
        )
        await q.answer("✅ QR claimed! It has been sent to your private chat.")
    except Exception:
        # The claim remains reserved so another user cannot take it. The user can start the bot and contact support if delivery failed.
        await q.answer("✅ QR claimed, but I couldn't send the DM. Please open the bot and contact support.",show_alert=True)

async def proof_button(update,context):
    q=update.callback_query
    await q.answer()
    qid=int(q.data.split(":")[1])
    c=conn()
    claim=c.execute("SELECT status,proof_file_id FROM claims WHERE qr_id=? AND user_id=? ORDER BY id DESC LIMIT 1",(qid,q.from_user.id)).fetchone()
    c.close()
    if not claim:
        return await q.message.reply_text("❌ Claim not found.")
    if claim["status"] == "under_review":
        return await q.message.reply_text("⏳ Your proof is already under review. Further submission is disabled.")
    if claim["status"] != "claimed":
        return await q.message.reply_text("❌ This claim is no longer available for proof submission.")
    context.user_data["proof_qid"]=qid
    context.user_data["proof_step"]="photo"
    await q.message.reply_text("📤 UPLOAD PROOF\n\nSend your payment/action proof image here.\n\nAfter uploading, you will get a SUBMIT FOR REVIEW button.")

async def proof_photo(update,context):
    if context.user_data.get("proof_step") != "photo":
        return False
    qid=context.user_data.get("proof_qid")
    if not qid:
        return False
    c=conn()
    claim=c.execute("SELECT status FROM claims WHERE qr_id=? AND user_id=? ORDER BY id DESC LIMIT 1",(qid,update.effective_user.id)).fetchone()
    if not claim or claim["status"] != "claimed":
        c.close()
        context.user_data.clear()
        await update.message.reply_text("❌ This claim cannot accept a proof now.",reply_markup=main_kb())
        return True
    file_id=update.message.photo[-1].file_id
    context.user_data["proof_file_id"]=file_id
    context.user_data["proof_step"]="submit"
    c.close()
    await update.message.reply_text(
        "📷 PROOF RECEIVED\n\nYour proof image is ready to submit for admin review.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🟢 SUBMIT FOR REVIEW",callback_data=f"submitproof:{qid}")],
            [InlineKeyboardButton("🔙 CANCEL",callback_data="menu")]
        ]))
    return True

async def submit_proof(update,context):
    q=update.callback_query
    await q.answer()
    qid=int(q.data.split(":")[1])
    file_id=context.user_data.get("proof_file_id")
    if context.user_data.get("proof_qid") != qid or not file_id:
        return await q.message.reply_text("❌ Please upload your proof first.")
    c=conn()
    cur=c.execute("UPDATE claims SET proof_file_id=?,status='under_review' WHERE qr_id=? AND user_id=? AND status='claimed'",(file_id,qid,q.from_user.id))
    changed=cur.rowcount
    r=c.execute("SELECT app,reward FROM qrs WHERE id=?",(qid,)).fetchone()
    c.commit(); c.close()
    if changed != 1:
        context.user_data.clear()
        return await q.message.edit_text("⏳ This claim is already under review or has been processed.")
    context.user_data.clear()
    # Notify all configured admins with the submitted proof.
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_photo(
                admin_id,file_id,
                caption=f"📥 NEW PROOF SUBMISSION\n\n👤 User: {q.from_user.full_name} (@{q.from_user.username or '—'})\n🆔 User ID: {q.from_user.id}\n🎯 QR ID: {qid}\n📱 App: {r['app'] if r else '—'}\n💰 Reward: ₹{r['reward']:.2f} if r else 0",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🛠 ADMIN PANEL",callback_data="admin")]])
            )
        except Exception:
            logging.exception("Could not notify admin about proof")
    await q.message.edit_text("⏳ UNDER REVIEW\n\nYour proof has been submitted successfully. Further submission is disabled until review.",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 BACK TO MENU",callback_data="menu")]]))

async def callbacks(update,context):
    q=update.callback_query; await q.answer()
    d=q.data
    if d=="menu":
        await q.message.delete(); return await q.message.chat.send_message("🏠 Main Menu",reply_markup=main_kb())
    if d=="wallet":
        c=conn(); r=c.execute("SELECT * FROM users WHERE id=?",(q.from_user.id,)).fetchone(); c.close()
        return await q.edit_message_text(f"💰 MY WALLET\n\n💵 Available Balance: ₹{r['balance']:.2f}\n🏆 Total Earned: ₹{r['total_earned']:.2f}\n💸 Total Withdrawn: ₹{r['total_withdrawn']:.2f}",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💸 WITHDRAW",callback_data="withdraw")],[InlineKeyboardButton("📜 TRANSACTIONS",callback_data="tx")],[InlineKeyboardButton("🔙 BACK TO MENU",callback_data="menu")]]))
    if d=="tx":
        c=conn(); rows=c.execute("SELECT amount,kind FROM transactions WHERE user_id=? ORDER BY id DESC LIMIT 20",(q.from_user.id,)).fetchall(); c.close()
        text="📜 TRANSACTION HISTORY\n\n"+("\n".join(("➕" if r["amount"]>=0 else "➖")+f" ₹{abs(r['amount']):.2f} — {r['kind']}" for r in rows) or "No transactions yet.")
        return await q.edit_message_text(text,reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ PREVIOUS",callback_data="noop"),InlineKeyboardButton("NEXT ▶️",callback_data="noop")],[InlineKeyboardButton("🔙 BACK TO MENU",callback_data="menu")]]))
    if d=="withdraw":
        c=conn(); bal=c.execute("SELECT balance FROM users WHERE id=?",(q.from_user.id,)).fetchone()["balance"]; c.close()
        context.user_data["withdraw_step"]="amount"
        return await q.edit_message_text(f"💸 WITHDRAW\n\n💰 Available Balance: ₹{bal:.2f}\n\nMinimum withdrawal: ₹{MIN_WITHDRAWAL:.0f}\n\nEnter the amount you want to withdraw.",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 CANCEL",callback_data="menu")]]))
    if d.startswith("w:"):
        method=d.split(":")[1]; context.user_data["withdraw_method"]=method; context.user_data["withdraw_step"]="details"
        return await q.edit_message_text(f"🏦 {method} SELECTED\n\nSend your {method} payment details now.")
    if d.startswith("submitproof:"): return await submit_proof(update,context)
    if d.startswith("proof:"): return await proof_button(update,context)
    if d=="addqr":
        await q.message.chat.send_message("Use /addqr to start QR creation."); return
    if d.startswith("post:"):
        return await postqr(update,context,int(d.split(":")[1]))
    if d=="posthelp":
        return await q.edit_message_text("📢 POST TO GROUP\n\nAfter creating a QR, press the POST TO GROUP button. The group receives only App + Reward + green CLAIM QR. The QR image is sent privately after a user claims it.",reply_markup=back_kb())
    if d=="admin":
        return await admin(update,context)
    if d=="admin_wd":
        if q.from_user.id not in ADMIN_IDS: return
        c=conn(); rows=c.execute("SELECT * FROM withdrawals ORDER BY id DESC LIMIT 20").fetchall(); c.close()
        text="💸 WITHDRAWALS\n\n"+("\n".join(f"#{r['id']} • User {r['user_id']} • ₹{r['amount']:.2f} • {r['method']} • {r['status']}" for r in rows) or "No withdrawals.")
        return await q.edit_message_text(text,reply_markup=back_kb())
    if d=="stats":
        c=conn(); users=c.execute("SELECT COUNT(*) x FROM users").fetchone()["x"]; w=c.execute("SELECT COUNT(*) x FROM withdrawals").fetchone()["x"]; paid=c.execute("SELECT COALESCE(SUM(amount),0) x FROM withdrawals WHERE status='paid'").fetchone()["x"]; c.close()
        return await q.edit_message_text(f"📊 STATS\n\n👥 Users: {users}\n💸 Total withdrawals: {w}\n💰 Paid amount: ₹{paid:.2f}",reply_markup=back_kb())
    if d=="users":
        return await q.edit_message_text("👥 USERS\n\nUser management can be expanded from the admin dashboard.",reply_markup=back_kb())
    if d=="forcejoin":
        return await q.edit_message_text("📢 FORCE JOIN\n\nConfigure your required channel in the admin settings/database before enabling enforcement.",reply_markup=back_kb())
    if d=="noop": return

async def post_command(update,context):
    await postqr(update,context)

def run():
    if not TOKEN: raise RuntimeError("BOT_TOKEN is missing")
    init_db()
    app=Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start",start))
    app.add_handler(CommandHandler("admin",admin))
    app.add_handler(CommandHandler("addqr",addqr))
    app.add_handler(CommandHandler("postqr",post_command))
    app.add_handler(CallbackQueryHandler(claim,pattern=r"^claim:\d+$"))
    app.add_handler(CallbackQueryHandler(proof_button,pattern=r"^proof:\d+$"))
    app.add_handler(CallbackQueryHandler(callbacks))
    async def photo_router(update, context):
        if context.user_data.get("proof_step") in ("photo", "submit") and update.effective_user.id not in ADMIN_IDS:
            handled = await proof_photo(update, context)
            if handled:
                return
        await photo(update, context)

    app.add_handler(MessageHandler(filters.PHOTO,photo_router))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,text))
    app.run_polling(drop_pending_updates=True)

if __name__=="__main__": run()
