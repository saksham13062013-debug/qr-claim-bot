import os
import sqlite3
import logging
from contextlib import closing

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    ContextTypes, filters
)

TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}
GROUP_ID = os.getenv("GROUP_ID", "")
DB_PATH = os.getenv("DB_PATH", "qr_claim_bot.db")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("qr-claim-bot")

def db():
    con = sqlite3.connect(DB_PATH)
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
            total_withdrawn REAL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS qrs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_id TEXT NOT NULL,
            app_name TEXT NOT NULL,
            reward REAL NOT NULL,
            claimed_by INTEGER,
            claimed_at TEXT,
            status TEXT DEFAULT 'available'
        );
        CREATE TABLE IF NOT EXISTS proofs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            qr_id INTEGER,
            user_id INTEGER,
            file_id TEXT,
            status TEXT DEFAULT 'pending'
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
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        """)
        con.commit()

def ensure_user(u):
    with closing(db()) as con:
        con.execute("""INSERT INTO users(id,username,name) VALUES(?,?,?)
                       ON CONFLICT(id) DO UPDATE SET username=excluded.username,name=excluded.name""",
                    (u.id, u.username or "", u.full_name))
        con.commit()

def menu():
    # Telegram does not expose arbitrary button colors through the Bot API.
    # These blue-themed labels are rendered as inline buttons; clients may use their own theme.
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💰 WALLET", callback_data="wallet"),
         InlineKeyboardButton("🎁 MY REWARDS", callback_data="rewards")],
        [InlineKeyboardButton("💸 WITHDRAW", callback_data="withdraw"),
         InlineKeyboardButton("📜 TRANSACTIONS", callback_data="transactions")],
        [InlineKeyboardButton("👤 PROFILE", callback_data="profile"),
         InlineKeyboardButton("ℹ️ HELP", callback_data="help")],
    ])

def back():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 BACK TO MENU", callback_data="home")]])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Main menu is DM-only. Never show the user keyboard/menu in groups.
    if update.effective_chat.type != "private":
        return
    u = update.effective_user
    ensure_user(u)
    text = (
        "✅ <b>Verification Successful!</b>\n\n"
        "👋 <b>Welcome to QR Claim Bot</b> 🎉\n\n"
        "🎯 Claim QR orders, complete tasks, earn rewards and withdraw your balance.\n\n"
        "💰 <b>Balance:</b> ₹0.00"
    )
    # If your force-join logic is already configured, insert it before this screen.
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=menu())

async def home(update, context):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    ensure_user(q.from_user)
    with closing(db()) as con:
        row = con.execute("SELECT balance FROM users WHERE id=?", (uid,)).fetchone()
    bal = row["balance"] if row else 0
    text = (
        "👋 <b>Welcome to QR Claim Bot</b> 🎉\n\n"
        "🎯 Claim QR orders, complete tasks, earn rewards and withdraw your balance.\n\n"
        f"💰 <b>Balance:</b> ₹{bal:.2f}"
    )
    await q.edit_message_text(text, parse_mode="HTML", reply_markup=menu())

async def wallet(update, context):
    q = update.callback_query; await q.answer()
    with closing(db()) as con:
        r = con.execute("SELECT balance,total_earned,total_withdrawn FROM users WHERE id=?", (q.from_user.id,)).fetchone()
        p = con.execute("SELECT COALESCE(SUM(amount),0) x FROM withdrawals WHERE user_id=? AND status IN ('pending','processing')", (q.from_user.id,)).fetchone()
    text = (
        "💰 <b>MY WALLET</b>\n\n"
        f"💵 Available Balance: ₹{r['balance']:.2f}\n"
        f"🏆 Total Earned: ₹{r['total_earned']:.2f}\n"
        f"💸 Total Withdrawn: ₹{r['total_withdrawn']:.2f}\n"
        f"⏳ Pending Withdrawal: ₹{p['x']:.2f}"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("💸 WITHDRAW", callback_data="withdraw"),
         InlineKeyboardButton("📜 TRANSACTIONS", callback_data="transactions")],
        [InlineKeyboardButton("🔙 BACK TO MENU", callback_data="home")]
    ])
    await q.edit_message_text(text, parse_mode="HTML", reply_markup=kb)

async def rewards(update, context):
    q=update.callback_query; await q.answer()
    with closing(db()) as con:
        rows=con.execute("SELECT kind,amount FROM transactions WHERE user_id=? ORDER BY id DESC LIMIT 8",(q.from_user.id,)).fetchall()
    text="🎁 <b>MY REWARDS</b>\n\n"
    text+="\n".join(f"➕ ₹{r['amount']:.2f} — {r['kind']}" for r in rows) if rows else "No rewards yet."
    kb=InlineKeyboardMarkup([
        [InlineKeyboardButton("📜 REWARD HISTORY", callback_data="transactions")],
        [InlineKeyboardButton("💰 WALLET", callback_data="wallet")],
        [InlineKeyboardButton("🔙 BACK TO MENU", callback_data="home")]
    ])
    await q.edit_message_text(text,parse_mode="HTML",reply_markup=kb)

async def transactions(update, context):
    q=update.callback_query; await q.answer()
    with closing(db()) as con:
        rows=con.execute("SELECT amount,kind FROM transactions WHERE user_id=? ORDER BY id DESC LIMIT 10",(q.from_user.id,)).fetchall()
    text="📜 <b>TRANSACTION HISTORY</b>\n\n"
    text+="\n".join(("➕ " if r["amount"]>=0 else "➖ ")+f"₹{abs(r['amount']):.2f} — {r['kind']}" for r in rows) if rows else "No transactions yet."
    kb=InlineKeyboardMarkup([
        [InlineKeyboardButton("◀️ PREVIOUS", callback_data="noop"),
         InlineKeyboardButton("NEXT ▶️", callback_data="noop")],
        [InlineKeyboardButton("🔙 BACK TO MENU", callback_data="home")]
    ])
    await q.edit_message_text(text,parse_mode="HTML",reply_markup=kb)

async def profile(update, context):
    q=update.callback_query; await q.answer()
    with closing(db()) as con:
        r=con.execute("SELECT * FROM users WHERE id=?",(q.from_user.id,)).fetchone()
    username=f"@{r['username']}" if r['username'] else "Not set"
    text=(f"👤 <b>MY PROFILE</b>\n\nName: {r['name']}\nUsername: {username}\n"
          f"Telegram ID: {r['id']}\n\n💰 Balance: ₹{r['balance']:.2f}\n"
          f"🎁 Total Rewards: ₹{r['total_earned']:.2f}\n💸 Total Withdrawn: ₹{r['total_withdrawn']:.2f}")
    kb=InlineKeyboardMarkup([
        [InlineKeyboardButton("📜 TRANSACTIONS",callback_data="transactions")],
        [InlineKeyboardButton("🔙 BACK TO MENU",callback_data="home")]
    ])
    await q.edit_message_text(text,parse_mode="HTML",reply_markup=kb)

async def help_page(update, context):
    q=update.callback_query; await q.answer()
    text=("ℹ️ <b>HOW IT WORKS</b>\n\n1️⃣ Claim an available QR.\n"
          "2️⃣ Receive the QR in your private chat.\n3️⃣ Complete the required payment/action.\n"
          "4️⃣ Upload your proof.\n5️⃣ Submit it for review.\n6️⃣ After approval, your reward is added to your wallet.\n7️⃣ Withdraw your available balance.")
    kb=InlineKeyboardMarkup([
        [InlineKeyboardButton("📞 SUPPORT", url="https://t.me/itsvanshu")],
        [InlineKeyboardButton("🔙 BACK TO MENU",callback_data="home")]
    ])
    await q.edit_message_text(text,parse_mode="HTML",reply_markup=kb)

async def withdraw(update, context):
    q=update.callback_query; await q.answer()
    with closing(db()) as con:
        r=con.execute("SELECT balance FROM users WHERE id=?",(q.from_user.id,)).fetchone()
    context.user_data["withdraw_stage"]="amount"
    await q.edit_message_text(f"💸 <b>WITHDRAW</b>\n\n💰 Available Balance: ₹{r['balance']:.2f}\n\nMinimum withdrawal: ₹50\n\nEnter the amount you want to withdraw.",
                               parse_mode="HTML",reply_markup=back())

async def text_handler(update, context):
    # Ignore ordinary group messages completely.
    if update.effective_chat.type != "private":
        return
    stage=context.user_data.get("withdraw_stage")
    if stage=="amount":
        try: amount=float(update.message.text.strip())
        except ValueError:
            await update.message.reply_text("Please enter a valid amount."); return
        with closing(db()) as con:
            r=con.execute("SELECT balance FROM users WHERE id=?",(update.effective_user.id,)).fetchone()
        if amount<50 or amount>r["balance"]:
            await update.message.reply_text(f"Invalid amount. Minimum ₹50 and available balance is ₹{r['balance']:.2f}."); return
        context.user_data["withdraw_amount"]=amount
        context.user_data["withdraw_stage"]="method"
        await update.message.reply_text(
            f"💸 <b>WITHDRAWAL REQUEST</b>\n\nAmount: ₹{amount:.2f}\nAvailable Balance: ₹{r['balance']:.2f}\n\nSelect withdrawal method:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🏦 UPI",callback_data="wmethod_upi"),
                 InlineKeyboardButton("🪙 USDT",callback_data="wmethod_usdt")],
                [InlineKeyboardButton("🔙 CANCEL",callback_data="home")]
            ]))
        return
    await update.message.reply_text("Use the buttons below to navigate.", reply_markup=menu())

async def method(update, context):
    q=update.callback_query; await q.answer()
    method=q.data.replace("wmethod_","").upper()
    context.user_data["withdraw_method"]=method
    context.user_data["withdraw_stage"]="account"
    await q.edit_message_text(f"🏦 <b>{method}</b>\n\nSend your {method} details.",parse_mode="HTML",reply_markup=back())

async def claim(update, context):
    q=update.callback_query; await q.answer()
    # Atomic single-claim transaction: only one user can win the available QR.
    with closing(db()) as con:
        con.execute("BEGIN IMMEDIATE")
        qr=con.execute("SELECT * FROM qrs WHERE status='available' ORDER BY id LIMIT 1").fetchone()
        if not qr:
            con.commit()
            await q.answer("No QR is currently available.",show_alert=True); return
        con.execute("UPDATE qrs SET status='claimed',claimed_by=?,claimed_at=CURRENT_TIMESTAMP WHERE id=? AND status='available'",
                     (q.from_user.id,qr["id"]))
        con.commit()
    try:
        await context.bot.send_photo(q.from_user.id, qr["file_id"],
            caption=f"📷 <b>{qr['app_name']}</b>\n💰 Reward: ₹{qr['reward']:.2f}\n\nComplete the required action and upload your proof.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📤 UPLOAD PROOF",callback_data=f"upload_{qr['id']}")]]))
        # The group only gets the claim result. Never attach the private user menu here.
        group_text = (
            f"🔒 <b>QR CLAIMED</b>\n\n"
            f"👤 Claimed by: @{q.from_user.username}" if q.from_user.username
            else "🔒 <b>QR CLAIMED</b>\n\n👤 Claimed by a user."
        )
        await q.edit_message_text(group_text, parse_mode="HTML")
    except Exception as e:
        log.exception(e)
        with closing(db()) as con:
            con.execute("UPDATE qrs SET status='available',claimed_by=NULL,claimed_at=NULL WHERE id=?",(qr["id"],)); con.commit()
        await q.answer("Please start the bot in private chat first.",show_alert=True)

async def upload(update, context):
    q=update.callback_query; await q.answer()
    qr_id=int(q.data.split("_")[1])
    context.user_data["proof_qr_id"]=qr_id
    context.user_data["proof_stage"]="photo"
    await q.edit_message_text("📤 <b>UPLOAD PROOF</b>\n\nSend your proof image/photo here.",parse_mode="HTML",reply_markup=back())

async def photo_handler(update, context):
    # Proof uploads are DM-only. Admin QR creation is handled separately below.
    if update.effective_chat.type != "private" or context.user_data.get("proof_stage")!="photo":
        return
    qr_id=context.user_data.get("proof_qr_id")
    file_id=update.message.photo[-1].file_id
    context.user_data["proof_file_id"]=file_id
    context.user_data["proof_stage"]="confirm"
    await update.message.reply_photo(file_id,caption="📷 Proof received.\n\nPress submit to send it for review.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🟢 SUBMIT FOR REVIEW",callback_data=f"submit_{qr_id}")]]))

async def submit(update, context):
    q=update.callback_query; await q.answer()
    qr_id=int(q.data.split("_")[1]); file_id=context.user_data.get("proof_file_id")
    if not file_id:
        await q.answer("Upload proof first.",show_alert=True); return
    with closing(db()) as con:
        existing=con.execute("SELECT id FROM proofs WHERE qr_id=? AND user_id=? AND status IN ('pending','approved')",(qr_id,q.from_user.id)).fetchone()
        if existing:
            await q.edit_message_text("⏳ <b>UNDER REVIEW</b>\n\nYour proof has already been submitted.",parse_mode="HTML",reply_markup=menu()); return
        con.execute("INSERT INTO proofs(qr_id,user_id,file_id,status) VALUES(?,?,?,'pending')",(qr_id,q.from_user.id,file_id)); con.commit()
    context.user_data["proof_stage"]="done"
    await q.edit_message_text("⏳ <b>UNDER REVIEW</b>\n\nYour proof has been submitted. Please wait for admin review.",parse_mode="HTML",reply_markup=menu())
    for aid in ADMIN_IDS:
        try:
            await context.bot.send_photo(aid,file_id,caption=f"📥 New proof\nUser: {q.from_user.id}\nQR: {qr_id}",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🟢 APPROVE",callback_data=f"approve_{qr_id}_{q.from_user.id}"),
                     InlineKeyboardButton("🔴 REJECT",callback_data=f"reject_{qr_id}_{q.from_user.id}")]
                ]))
        except Exception: pass

async def admin_qr(update, context):
    if update.effective_user.id not in ADMIN_IDS: return
    await update.message.reply_text("Send a QR image/photo now. After the image, send: App name, then reward amount.",reply_markup=ReplyKeyboardRemove())
    context.user_data["admin_qr_stage"]="photo"

async def admin_photo(update, context):
    if update.effective_chat.type != "private": return
    if update.effective_user.id not in ADMIN_IDS or context.user_data.get("admin_qr_stage")!="photo": return
    context.user_data["new_qr_file"]=update.message.photo[-1].file_id
    context.user_data["admin_qr_stage"]="app"
    await update.message.reply_text("Enter the QR app name.")

async def admin_text(update, context):
    if update.effective_chat.type != "private": return
    stage=context.user_data.get("admin_qr_stage")
    if update.effective_user.id not in ADMIN_IDS or not stage: return
    if stage=="app":
        context.user_data["new_qr_app"]=update.message.text.strip(); context.user_data["admin_qr_stage"]="reward"; await update.message.reply_text("Enter reward amount in ₹.")
    elif stage=="reward":
        try: reward=float(update.message.text.strip())
        except: await update.message.reply_text("Enter a valid number."); return
        app_name=context.user_data["new_qr_app"]
        with closing(db()) as con:
            con.execute("INSERT INTO qrs(file_id,app_name,reward) VALUES(?,?,?)",(context.user_data["new_qr_file"],app_name,reward)); con.commit()
        context.user_data.clear()
        await update.message.reply_text(f"✅ QR created\nApp: {app_name}\nReward: ₹{reward:.2f}\n\nUse /postqr to post the available claim button.")

async def postqr(update, context):
    if update.effective_user.id not in ADMIN_IDS: return
    if not GROUP_ID:
        await update.message.reply_text("GROUP_ID is not configured."); return
    with closing(db()) as con:
        qr=con.execute("SELECT * FROM qrs WHERE status='available' ORDER BY id LIMIT 1").fetchone()
    if not qr:
        await update.message.reply_text("No available QR. Create one with /addqr."); return
    kb=InlineKeyboardMarkup([[InlineKeyboardButton("💳 CLAIM QR",callback_data="claim")]])
    await context.bot.send_message(int(GROUP_ID),
        f"🎯 <b>QR CLAIM AVAILABLE</b>\n\n📱 App: {qr['app_name']}\n💰 Reward: ₹{qr['reward']:.2f}\n\nFirst eligible user to claim gets this QR in DM.",
        parse_mode="HTML",reply_markup=kb)
    await update.message.reply_text("✅ Posted to group. QR image was not posted.")

async def status(update, context):
    if update.effective_user.id not in ADMIN_IDS: return
    parts=update.message.text.split()
    if len(parts)!=3: await update.message.reply_text("Usage: /status WITHDRAWAL_ID paid"); return
    wid, st=parts[1],parts[2].lower()
    if st not in ("pending","processing","paid","rejected"): return
    with closing(db()) as con:
        w=con.execute("SELECT * FROM withdrawals WHERE id=?",(wid,)).fetchone()
        if not w: await update.message.reply_text("Withdrawal not found."); return
        con.execute("UPDATE withdrawals SET status=? WHERE id=?",(st,wid)); con.commit()
    await context.bot.send_message(w["user_id"],f"💸 Withdrawal #{wid} status: <b>{st.upper()}</b>",parse_mode="HTML")

async def callbacks(update, context):
    d=update.callback_query.data
    if d=="home": return await home(update,context)
    if d=="wallet": return await wallet(update,context)
    if d=="rewards": return await rewards(update,context)
    if d=="transactions": return await transactions(update,context)
    if d=="profile": return await profile(update,context)
    if d=="help": return await help_page(update,context)
    if d=="withdraw": return await withdraw(update,context)
    if d=="claim": return await claim(update,context)
    if d.startswith("upload_"): return await upload(update,context)
    if d.startswith("submit_"): return await submit(update,context)
    if d.startswith("wmethod_"): return await method(update,context)
    if d=="noop": return await update.callback_query.answer("No more pages.")

def main():
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is missing")
    init_db()
    app=Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start",start))
    app.add_handler(CommandHandler("addqr",admin_qr))
    app.add_handler(CommandHandler("postqr",postqr))
    app.add_handler(CommandHandler("status",status))
    app.add_handler(CallbackQueryHandler(callbacks))
    # Admin handlers must be registered before generic user handlers.
    app.add_handler(MessageHandler(filters.PHOTO, admin_photo))
    app.add_handler(MessageHandler(filters.PHOTO, photo_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, admin_text))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__=="__main__":
    main()
