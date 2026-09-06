import os, sqlite3, logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, ContextTypes, filters, ConversationHandler

logging.basicConfig(level=logging.INFO)
TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}
DB_PATH = os.getenv("DB_PATH", "qr_claim_bot.db")
SUPPORT_URL = os.getenv("SUPPORT_URL", "https://t.me/itsvanshu")
GROUP_ID = os.getenv("GROUP_ID", "")

def db():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    c=db()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, name TEXT, username TEXT, balance REAL DEFAULT 0, total_earned REAL DEFAULT 0, total_withdrawn REAL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS qrs(id INTEGER PRIMARY KEY AUTOINCREMENT, app TEXT, reward REAL, file_id TEXT, active INTEGER DEFAULT 1);
    CREATE TABLE IF NOT EXISTS claims(id INTEGER PRIMARY KEY AUTOINCREMENT, qr_id INTEGER, user_id INTEGER, status TEXT DEFAULT 'claimed', proof_file_id TEXT);
    CREATE TABLE IF NOT EXISTS withdrawals(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, amount REAL, method TEXT, status TEXT DEFAULT 'pending');
    CREATE TABLE IF NOT EXISTS transactions(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, amount REAL, kind TEXT);
    """)
    c.commit(); c.close()

def menu():
    return ReplyKeyboardMarkup([
        ["💰 WALLET","🎁 MY REWARDS"],
        ["💸 WITHDRAW","📜 TRANSACTIONS"],
        ["👤 PROFILE","ℹ️ HELP"]
    ], resize_keyboard=True)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u=update.effective_user
    c=db()
    c.execute("INSERT INTO users(id,name,username) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,username=excluded.username",
              (u.id,u.full_name,u.username or ""))
    c.commit()
    bal=c.execute("SELECT balance FROM users WHERE id=?",(u.id,)).fetchone()["balance"]; c.close()
    await update.message.reply_text(
        f"👋 Welcome to QR Claim Bot\n\n🎯 Claim QR orders, complete tasks, earn rewards and withdraw your balance.\n\n💰 Balance: ₹{bal:.2f}",
        reply_markup=menu())

async def wallet(update, context):
    c=db(); r=c.execute("SELECT balance,total_earned,total_withdrawn FROM users WHERE id=?",(update.effective_user.id,)).fetchone()
    p=c.execute("SELECT COALESCE(SUM(amount),0) x FROM withdrawals WHERE user_id=? AND status='pending'",(update.effective_user.id,)).fetchone()["x"]; c.close()
    kb=InlineKeyboardMarkup([[InlineKeyboardButton("💸 WITHDRAW",callback_data="withdraw"),InlineKeyboardButton("📜 TRANSACTIONS",callback_data="tx")],
                             [InlineKeyboardButton("🔙 BACK TO MENU",callback_data="menu")]])
    await update.message.reply_text(f"💰 MY WALLET\n\n💵 Available Balance: ₹{r['balance']:.2f}\n🏆 Total Earned: ₹{r['total_earned']:.2f}\n💸 Total Withdrawn: ₹{r['total_withdrawn']:.2f}\n⏳ Pending Withdrawal: ₹{p:.2f}",reply_markup=kb)

async def rewards(update, context):
    c=db(); rows=c.execute("SELECT amount,kind FROM transactions WHERE user_id=? AND amount>0 ORDER BY id DESC LIMIT 5",(update.effective_user.id,)).fetchall(); c.close()
    text="🎁 MY REWARDS\n\n✅ Approved rewards are shown below.\n"
    text += "\n".join(f"➕ ₹{r['amount']:.2f} — {r['kind']}" for r in rows) or "\nNo rewards yet."
    kb=InlineKeyboardMarkup([[InlineKeyboardButton("📜 REWARD HISTORY",callback_data="tx"),InlineKeyboardButton("💰 WALLET",callback_data="wallet")],[InlineKeyboardButton("🔙 BACK TO MENU",callback_data="menu")]])
    await update.message.reply_text(text,reply_markup=kb)

async def transactions(update, context):
    c=db(); rows=c.execute("SELECT amount,kind FROM transactions WHERE user_id=? ORDER BY id DESC LIMIT 10",(update.effective_user.id,)).fetchall(); c.close()
    text="📜 TRANSACTION HISTORY\n\n"+("\n".join(("➕" if r["amount"]>=0 else "➖")+f" ₹{abs(r['amount']):.2f} — {r['kind']}" for r in rows) or "No transactions yet.")
    await update.message.reply_text(text,reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ PREVIOUS",callback_data="noop"),InlineKeyboardButton("NEXT ▶️",callback_data="noop")],[InlineKeyboardButton("🔙 BACK TO MENU",callback_data="menu")]]))

async def profile(update, context):
    u=update.effective_user; c=db(); r=c.execute("SELECT * FROM users WHERE id=?",(u.id,)).fetchone(); c.close()
    await update.message.reply_text(f"👤 MY PROFILE\n\nName: {u.full_name}\nUsername: @{u.username or '—'}\nTelegram ID: {u.id}\n\n💰 Balance: ₹{r['balance']:.2f}\n🎁 Total Rewards: ₹{r['total_earned']:.2f}\n💸 Total Withdrawn: ₹{r['total_withdrawn']:.2f}",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📜 TRANSACTIONS",callback_data="tx")],[InlineKeyboardButton("🔙 BACK TO MENU",callback_data="menu")]]))

async def help_cmd(update, context):
    await update.message.reply_text("ℹ️ HOW IT WORKS\n\n1️⃣ Claim an available QR.\n2️⃣ Receive the QR in your private chat.\n3️⃣ Complete the required payment/action.\n4️⃣ Upload your proof.\n5️⃣ Submit it for review.\n6️⃣ After approval, your reward is added to your wallet.\n7️⃣ Withdraw your available balance.",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📞 SUPPORT",url=SUPPORT_URL)],[InlineKeyboardButton("🔙 BACK TO MENU",callback_data="menu")]]))

async def withdraw(update, context):
    c=db(); r=c.execute("SELECT balance FROM users WHERE id=?",(update.effective_user.id,)).fetchone(); c.close()
    await update.message.reply_text(f"💸 WITHDRAW\n\n💰 Available Balance: ₹{r['balance']:.2f}\n\nMinimum withdrawal: ₹50\n\nEnter the amount you want to withdraw.",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 CANCEL",callback_data="menu")]]))

async def admin(update, context):
    if update.effective_user.id not in ADMIN_IDS: return
    c=db()
    users=c.execute("SELECT COUNT(*) x FROM users").fetchone()["x"]; qrs=c.execute("SELECT COUNT(*) x FROM qrs WHERE active=1").fetchone()["x"]
    pending=c.execute("SELECT COUNT(*) x FROM withdrawals WHERE status='pending'").fetchone()["x"]; total=c.execute("SELECT COALESCE(SUM(amount),0) x FROM withdrawals").fetchone()["x"]; c.close()
    kb=InlineKeyboardMarkup([[InlineKeyboardButton("➕ ADD QR",callback_data="addqr"),InlineKeyboardButton("📢 POST TO GROUP",callback_data="postgroup")],
                             [InlineKeyboardButton("💸 WITHDRAWALS",callback_data="admin_withdraw"),InlineKeyboardButton("📊 STATS",callback_data="admin_stats")],
                             [InlineKeyboardButton("📢 FORCE JOIN",callback_data="forcejoin")]])
    await update.message.reply_text(f"🛠 ADMIN PANEL\n\n👥 Users: {users}\n🎯 Active QR: {qrs}\n⏳ Pending withdrawals: {pending}\n💸 Total withdrawal requests: ₹{total:.2f}",reply_markup=kb)

async def callbacks(update, context):
    q=update.callback_query; await q.answer()
    if q.data=="menu":
        await q.message.delete()
        await q.message.chat.send_message("🏠 Main Menu",reply_markup=menu())
    elif q.data=="wallet":
        c=db(); r=c.execute("SELECT balance,total_earned,total_withdrawn FROM users WHERE id=?",(q.from_user.id,)).fetchone(); c.close()
        await q.edit_message_text(f"💰 MY WALLET\n\n💵 Available Balance: ₹{r['balance']:.2f}\n🏆 Total Earned: ₹{r['total_earned']:.2f}\n💸 Total Withdrawn: ₹{r['total_withdrawn']:.2f}",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💸 WITHDRAW",callback_data="withdraw")],[InlineKeyboardButton("📜 TRANSACTIONS",callback_data="tx")],[InlineKeyboardButton("🔙 BACK TO MENU",callback_data="menu")]]))
    elif q.data=="tx":
        c=db(); rows=c.execute("SELECT amount,kind FROM transactions WHERE user_id=? ORDER BY id DESC LIMIT 10",(q.from_user.id,)).fetchall(); c.close()
        text="📜 TRANSACTION HISTORY\n\n"+("\n".join(("➕" if r["amount"]>=0 else "➖")+f" ₹{abs(r['amount']):.2f} — {r['kind']}" for r in rows) or "No transactions yet.")
        await q.edit_message_text(text,reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 BACK TO MENU",callback_data="menu")]]))
    elif q.data=="withdraw":
        c=db(); r=c.execute("SELECT balance FROM users WHERE id=?",(q.from_user.id,)).fetchone(); c.close()
        await q.edit_message_text(f"💸 WITHDRAWAL\n\nAvailable Balance: ₹{r['balance']:.2f}\nMinimum withdrawal: ₹50\n\nEnter the amount you want to withdraw.",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏦 UPI",callback_data="w_upi"),InlineKeyboardButton("🪙 USDT",callback_data="w_usdt")],[InlineKeyboardButton("🔙 CANCEL",callback_data="menu")]]))
    elif q.data=="postgroup":
        await q.edit_message_text("📢 POST TO GROUP\n\nUse /postqr after selecting/creating an active QR. The group post contains only the app/reward and the green CLAIM QR button; the QR image is never posted.")
    elif q.data=="addqr":
        await q.edit_message_text("➕ ADD QR\n\nUse /addqr. Send the QR image, then app name and reward. After creation, use POST TO GROUP.")
    elif q.data=="admin_withdraw":
        if q.from_user.id not in ADMIN_IDS: return
        c=db(); rows=c.execute("SELECT id,user_id,amount,method,status FROM withdrawals ORDER BY id DESC LIMIT 10").fetchall(); c.close()
        text="💸 WITHDRAWALS\n\n"+("\n".join(f"#{r['id']} • User {r['user_id']} • ₹{r['amount']:.2f} • {r['method']} • {r['status']}" for r in rows) or "No withdrawals.")
        await q.edit_message_text(text,reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 ADMIN PANEL",callback_data="admin")]]))
    elif q.data=="admin":
        await q.message.delete(); await admin(update,context)
    elif q.data in ("forcejoin","admin_stats"):
        await q.edit_message_text("This admin module is ready for configuration in Railway variables/database. Add your channel/group settings before enabling it.",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 ADMIN PANEL",callback_data="admin")]]))
    elif q.data=="noop": pass

async def addqr(update, context):
    if update.effective_user.id not in ADMIN_IDS: return
    context.user_data["addqr"]={"step":"image"}
    await update.message.reply_text("📷 Send the QR image.")

async def text_handler(update, context):
    t=update.message.text
    if t=="💰 WALLET": await wallet(update,context)
    elif t=="🎁 MY REWARDS": await rewards(update,context)
    elif t=="💸 WITHDRAW": await withdraw(update,context)
    elif t=="📜 TRANSACTIONS": await transactions(update,context)
    elif t=="👤 PROFILE": await profile(update,context)
    elif t=="ℹ️ HELP": await help_cmd(update,context)
    elif context.user_data.get("addqr",{}).get("step")=="app":
        context.user_data["addqr"]["app"]=t; context.user_data["addqr"]["step"]="reward"; await update.message.reply_text("💰 Enter reward amount (e.g. 15)")
    elif context.user_data.get("addqr",{}).get("step")=="reward":
        try: reward=float(t)
        except: await update.message.reply_text("Enter a valid amount."); return
        a=context.user_data["addqr"]; c=db(); cur=c.execute("INSERT INTO qrs(app,reward,file_id) VALUES(?,?,?)",(a["app"],reward,a["file_id"])); c.commit(); qid=cur.lastrowid; c.close()
        context.user_data.pop("addqr",None)
        await update.message.reply_text(f"✅ QR Created\n\nID: {qid}\nApp: {a['app']}\nReward: ₹{reward:.2f}\n\nUse /postqr {qid} to post the claim button in the group.")
    else:
        await update.message.reply_text("Use the menu buttons below.",reply_markup=menu())

async def photo_handler(update, context):
    if update.effective_user.id not in ADMIN_IDS or context.user_data.get("addqr",{}).get("step")!="image": return
    context.user_data["addqr"]["file_id"]=update.message.photo[-1].file_id
    context.user_data["addqr"]["step"]="app"
    await update.message.reply_text("📱 Enter the app name.")

async def postqr(update, context):
    if update.effective_user.id not in ADMIN_IDS: return
    if not GROUP_ID: await update.message.reply_text("Set GROUP_ID in Railway Variables first."); return
    if not context.args: await update.message.reply_text("Usage: /postqr QR_ID"); return
    c=db(); r=c.execute("SELECT * FROM qrs WHERE id=? AND active=1",(context.args[0],)).fetchone(); c.close()
    if not r: await update.message.reply_text("QR not found."); return
    kb=InlineKeyboardMarkup([[InlineKeyboardButton("💳 CLAIM QR",callback_data=f"claim:{r['id']}")]])
    await context.bot.send_message(chat_id=GROUP_ID,text=f"🎯 QR Claim Available\n\n📱 App: {r['app']}\n💰 Reward: ₹{r['reward']:.2f}\n\nClaim the QR to receive it in your private chat.",reply_markup=kb)
    await update.message.reply_text("✅ Posted to group. QR image was not posted.")

async def claim(update, context):
    q=update.callback_query; await q.answer()
    qid=int(q.data.split(":")[1]); c=db(); r=c.execute("SELECT * FROM qrs WHERE id=? AND active=1",(qid,)).fetchone()
    if not r: c.close(); await q.answer("QR unavailable.",show_alert=True); return
    c.execute("INSERT INTO claims(qr_id,user_id) VALUES(?,?)",(qid,q.from_user.id)); c.commit(); c.close()
    try:
        await context.bot.send_photo(q.from_user.id,r["file_id"],caption=f"🎯 QR CLAIM\n\n📱 App: {r['app']}\n💰 Reward: ₹{r['reward']:.2f}\n\nComplete the required action and upload your proof.",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📤 UPLOAD PROOF",callback_data=f"proof:{qid}")]]))
        await q.answer("QR sent to your private chat.")
    except Exception:
        await q.answer("Please start the bot in private chat first.",show_alert=True)

def main():
    if not TOKEN: raise RuntimeError("BOT_TOKEN is missing")
    init_db()
    app=Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start",start)); app.add_handler(CommandHandler("admin",admin))
    app.add_handler(CommandHandler("addqr",addqr)); app.add_handler(CommandHandler("postqr",postqr))
    app.add_handler(CallbackQueryHandler(claim,pattern=r"^claim:\d+$"))
    app.add_handler(CallbackQueryHandler(callbacks))
    app.add_handler(MessageHandler(filters.PHOTO,photo_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,text_handler))
    app.run_polling(drop_pending_updates=True)

if __name__=="__main__": main()
