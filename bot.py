
import os, time, sqlite3, asyncio, hmac, hashlib, json
from urllib.parse import parse_qsl
from contextlib import closing
from dotenv import load_dotenv

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
import uvicorn

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    WebAppInfo, MenuButtonWebApp
)
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing")

GROUP_ID_RAW = os.getenv("GROUP_ID", "").strip()
GROUP_ID = int(GROUP_ID_RAW) if GROUP_ID_RAW.lstrip("-").isdigit() else None
ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS","").split(",") if x.strip().isdigit()}
SUPPORT_ID = os.getenv("SUPPORT_ID", "@itsvanshu")
FORCE_JOIN_CHANNEL = os.getenv("FORCE_JOIN_CHANNEL", "").strip()
FORCE_JOIN_URL = os.getenv("FORCE_JOIN_URL", "").strip()
WEBAPP_URL = os.getenv("WEBAPP_URL", "").strip().rstrip("/")
DB_PATH = os.getenv("DB_PATH", "qr_claim_bot.db")
MIN_WITHDRAWAL = float(os.getenv("MIN_WITHDRAWAL", "50"))
ENABLE_UPI = os.getenv("ENABLE_UPI", "1") == "1"
ENABLE_USDT = os.getenv("ENABLE_USDT", "1") == "1"

app = FastAPI(title="QR Claim Bot")
dp = Dispatcher()
bot = None
pending = {}

def db():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    with closing(db()) as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY,
            username TEXT DEFAULT '',
            first_name TEXT DEFAULT '',
            balance REAL DEFAULT 0,
            total_earned REAL DEFAULT 0,
            total_withdrawn REAL DEFAULT 0,
            created_at INTEGER
        );
        CREATE TABLE IF NOT EXISTS qr_orders(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            file_id TEXT NOT NULL,
            app_name TEXT DEFAULT '',
            reward REAL NOT NULL,
            status TEXT DEFAULT 'available',
            claimed_by INTEGER,
            claimed_at INTEGER,
            created_at INTEGER
        );
        CREATE TABLE IF NOT EXISTS proofs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            qr_id INTEGER,
            user_id INTEGER,
            file_id TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            created_at INTEGER
        );
        CREATE TABLE IF NOT EXISTS transactions(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            kind TEXT,
            amount REAL,
            note TEXT,
            created_at INTEGER
        );
        CREATE TABLE IF NOT EXISTS withdrawals(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            amount REAL,
            method TEXT,
            value TEXT,
            status TEXT DEFAULT 'pending',
            created_at INTEGER,
            updated_at INTEGER
        );
        CREATE TABLE IF NOT EXISTS admins(
            user_id INTEGER PRIMARY KEY,
            added_at INTEGER
        );
        """)
        for aid in ADMIN_IDS:
            con.execute("INSERT OR IGNORE INTO admins(user_id,added_at) VALUES(?,?)",(aid,int(time.time())))
        con.commit()

def is_admin(uid):
    if uid in ADMIN_IDS: return True
    with closing(db()) as con:
        return con.execute("SELECT 1 FROM admins WHERE user_id=?",(uid,)).fetchone() is not None

def ensure_user(u):
    with closing(db()) as con:
        con.execute("""INSERT INTO users(id,username,first_name,created_at)
                       VALUES(?,?,?,?)
                       ON CONFLICT(id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name""",
                    (u.id,u.username or "",u.first_name or "",int(time.time())))
        con.commit()

def user_row(uid):
    with closing(db()) as con:
        return con.execute("SELECT * FROM users WHERE id=?",(uid,)).fetchone()

def tx(uid, kind, amount, note):
    with closing(db()) as con:
        con.execute("INSERT INTO transactions(user_id,kind,amount,note,created_at) VALUES(?,?,?,?,?)",
                    (uid,kind,amount,note,int(time.time())))
        con.commit()

def menu():
    rows = [
        [InlineKeyboardButton(text="💰 WALLET", callback_data="wallet"),
         InlineKeyboardButton(text="🎁 MY REWARDS", callback_data="rewards")],
        [InlineKeyboardButton(text="💸 WITHDRAW", callback_data="withdraw"),
         InlineKeyboardButton(text="📜 TRANSACTIONS", callback_data="transactions")],
        [InlineKeyboardButton(text="👤 PROFILE", callback_data="profile"),
         InlineKeyboardButton(text="ℹ️ HELP", callback_data="help")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)

def back_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 BACK TO MENU", callback_data="home")]
    ])

def admin_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 DASHBOARD", web_app=WebAppInfo(url=WEBAPP_URL+"/admin") if WEBAPP_URL else None)],
        [InlineKeyboardButton(text="➕ ADD QR", callback_data="a_addqr"),
         InlineKeyboardButton(text="📢 POST QR", callback_data="a_postqr")],
        [InlineKeyboardButton(text="🧾 PROOF REVIEW", callback_data="a_proofs"),
         InlineKeyboardButton(text="💸 WITHDRAWALS", callback_data="a_withdrawals")],
        [InlineKeyboardButton(text="📱 QR LIST", callback_data="a_qrs"),
         InlineKeyboardButton(text="👥 USERS", callback_data="a_users")],
    ])

async def force_join_ok(uid):
    if not FORCE_JOIN_CHANNEL:
        return True
    try:
        m = await bot.get_chat_member(FORCE_JOIN_CHANNEL, uid)
        return m.status in ("member","administrator","creator")
    except Exception:
        return False

async def require_join(message):
    if await force_join_ok(message.from_user.id):
        return True
    url = FORCE_JOIN_URL or (f"https://t.me/{FORCE_JOIN_CHANNEL.lstrip('@')}" if FORCE_JOIN_CHANNEL else "")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 JOIN CHANNEL", url=url)] if url else [],
        [InlineKeyboardButton(text="✅ I JOINED", callback_data="check_join")]
    ])
    await message.answer("🔒 Please join our required channel before using the bot.", reply_markup=kb)
    return False

async def safe_edit(q, text, kb=None):
    try:
        await q.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e).lower():
            raise

@dp.message(CommandStart())
async def start(message: Message):
    if message.chat.type != "private":
        return
    ensure_user(message.from_user)
    if not await require_join(message): return
    u = user_row(message.from_user.id)
    await message.answer(
        "👋 <b>Welcome to QR Claim Bot</b>\n\n"
        "🎯 Claim QR orders, complete tasks, earn rewards and withdraw your balance.\n\n"
        f"💰 <b>Balance:</b> ₹{u['balance']:.2f}",
        parse_mode="HTML", reply_markup=menu()
    )

@dp.callback_query(F.data == "check_join")
async def check_join(q: CallbackQuery):
    await q.answer()
    if await force_join_ok(q.from_user.id):
        u=user_row(q.from_user.id)
        await safe_edit(q,f"👋 <b>Welcome to QR Claim Bot</b>\n\n🎯 Claim QR orders, complete tasks, earn rewards and withdraw your balance.\n\n💰 <b>Balance:</b> ₹{u['balance']:.2f}",menu())
    else:
        await q.answer("You have not joined yet.", show_alert=True)

@dp.callback_query(F.data == "home")
async def home(q: CallbackQuery):
    await q.answer()
    u=user_row(q.from_user.id)
    await safe_edit(q,f"👋 <b>Welcome to QR Claim Bot</b>\n\n🎯 Claim QR orders, complete tasks, earn rewards and withdraw your balance.\n\n💰 <b>Balance:</b> ₹{u['balance']:.2f}",menu())

@dp.callback_query(F.data == "wallet")
async def wallet(q):
    await q.answer(); u=user_row(q.from_user.id)
    with closing(db()) as con:
        pending_w=con.execute("SELECT COALESCE(SUM(amount),0) FROM withdrawals WHERE user_id=? AND status IN ('pending','processing')",(q.from_user.id,)).fetchone()[0]
    kb=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💸 WITHDRAW",callback_data="withdraw"),
         InlineKeyboardButton(text="📜 TRANSACTIONS",callback_data="transactions")],
        [InlineKeyboardButton(text="🔙 BACK TO MENU",callback_data="home")]
    ])
    await safe_edit(q,f"💰 <b>MY WALLET</b>\n\n💵 Available Balance: ₹{u['balance']:.2f}\n🏆 Total Earned: ₹{u['total_earned']:.2f}\n💸 Total Withdrawn: ₹{u['total_withdrawn']:.2f}\n⏳ Pending Withdrawal: ₹{pending_w:.2f}",kb)

@dp.callback_query(F.data == "rewards")
async def rewards(q):
    await q.answer()
    with closing(db()) as con:
        approved=con.execute("SELECT COALESCE(SUM(amount),0) FROM transactions WHERE user_id=? AND kind='reward'",(q.from_user.id,)).fetchone()[0]
        pending_r=con.execute("SELECT COALESCE(SUM(q.reward),0) FROM proofs p JOIN qr_orders q ON q.id=p.qr_id WHERE p.user_id=? AND p.status='pending'",(q.from_user.id,)).fetchone()[0]
        recent=con.execute("SELECT amount,note FROM transactions WHERE user_id=? AND kind='reward' ORDER BY id DESC LIMIT 5",(q.from_user.id,)).fetchall()
    text=f"🎁 <b>MY REWARDS</b>\n\n✅ Approved: ₹{approved:.2f}\n⏳ Pending: ₹{pending_r:.2f}\n❌ Rejected: ₹0.00\n\n"
    text += "\n".join(f"✅ ₹{r['amount']:.2f} — {r['note']}" for r in recent) or "No approved rewards yet."
    kb=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📜 REWARD HISTORY",callback_data="transactions"),
         InlineKeyboardButton(text="💰 WALLET",callback_data="wallet")],
        [InlineKeyboardButton(text="🔙 BACK TO MENU",callback_data="home")]
    ])
    await safe_edit(q,text,kb)

@dp.callback_query(F.data == "transactions")
async def transactions(q):
    await q.answer()
    with closing(db()) as con:
        rows=con.execute("SELECT kind,amount,note,created_at FROM transactions WHERE user_id=? ORDER BY id DESC LIMIT 10",(q.from_user.id,)).fetchall()
    lines=[]
    for r in rows:
        sign="➕" if r["amount"]>=0 else "➖"
        lines.append(f"{sign} ₹{abs(r['amount']):.2f} — {r['note'] or r['kind']}")
    kb=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 BACK TO MENU",callback_data="home")]
    ])
    await safe_edit(q,"📜 <b>TRANSACTION HISTORY</b>\n\n"+("\n".join(lines) or "No transactions yet."),kb)

@dp.callback_query(F.data == "profile")
async def profile(q):
    await q.answer(); u=user_row(q.from_user.id)
    username="@"+u["username"] if u["username"] else "—"
    kb=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📜 TRANSACTIONS",callback_data="transactions")],
        [InlineKeyboardButton(text="🔙 BACK TO MENU",callback_data="home")]
    ])
    await safe_edit(q,f"👤 <b>MY PROFILE</b>\n\nName: {u['first_name'] or '—'}\nUsername: {username}\nTelegram ID: {u['id']}\n\n💰 Balance: ₹{u['balance']:.2f}\n🎁 Total Rewards: ₹{u['total_earned']:.2f}\n💸 Total Withdrawn: ₹{u['total_withdrawn']:.2f}",kb)

@dp.callback_query(F.data == "help")
async def help_cb(q):
    await q.answer()
    kb=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📞 SUPPORT",url=f"https://t.me/{SUPPORT_ID.lstrip('@')}")],
        [InlineKeyboardButton(text="🔙 BACK TO MENU",callback_data="home")]
    ])
    await safe_edit(q,"ℹ️ <b>HOW IT WORKS</b>\n\n1️⃣ Claim an available QR.\n2️⃣ Receive the QR in your private chat.\n3️⃣ Complete the required payment/action.\n4️⃣ Upload your proof.\n5️⃣ Submit it for review.\n6️⃣ After approval, your reward is added to your wallet.\n7️⃣ Withdraw your available balance.",kb)

@dp.callback_query(F.data == "withdraw")
async def withdraw_start(q):
    await q.answer()
    u=user_row(q.from_user.id)
    pending[q.from_user.id]="withdraw_amount"
    await q.message.answer(f"💸 <b>WITHDRAW</b>\n\n💰 Available Balance: ₹{u['balance']:.2f}\n\nMinimum withdrawal: ₹{MIN_WITHDRAWAL:g}\n\nEnter the amount you want to withdraw.",parse_mode="HTML")

@dp.callback_query(F.data == "a_addqr")
async def admin_addqr(q):
    if not is_admin(q.from_user.id): return await q.answer("Not authorized",show_alert=True)
    await q.answer()
    pending[q.from_user.id]="addqr"
    await q.message.answer("➕ <b>ADD QR</b>\n\nSend the QR image now. Then I will ask for:\n1. QR app name\n2. Reward amount\n3. Title",parse_mode="HTML")

@dp.callback_query(F.data == "a_postqr")
async def admin_postqr(q):
    if not is_admin(q.from_user.id): return await q.answer("Not authorized",show_alert=True)
    await q.answer()
    with closing(db()) as con:
        row=con.execute("SELECT * FROM qr_orders WHERE status='available' ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        return await q.message.answer("❌ No available QR. Add one first.")
    if not GROUP_ID:
        return await q.message.answer("❌ GROUP_ID is not configured.")
    kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💳 CLAIM QR",callback_data=f"claim:{row['id']}")]])
    text=f"🎯 <b>QR CLAIM AVAILABLE</b>\n\n📱 App: {row['app_name'] or 'QR App'}\n💰 Reward: ₹{row['reward']:.2f}\n\nClaim it to receive the QR privately.\n⚠️ Only one user can claim this QR."
    await bot.send_message(GROUP_ID,text,parse_mode="HTML",reply_markup=kb)
    await q.message.answer(f"✅ QR #{row['id']} posted to group.")

@dp.callback_query(F.data.startswith("claim:"))
async def claim(q):
    await q.answer()
    if not await force_join_ok(q.from_user.id):
        await q.message.answer("🔒 Please join the required channel first.")
        return
    ensure_user(q.from_user)
    qr_id=int(q.data.split(":")[1])
    with closing(db()) as con:
        con.execute("BEGIN IMMEDIATE")
        row=con.execute("SELECT * FROM qr_orders WHERE id=?",(qr_id,)).fetchone()
        if not row or row["status"]!="available":
            con.rollback()
            return await q.answer("❌ This QR has already been claimed.",show_alert=True)
        con.execute("UPDATE qr_orders SET status='claimed',claimed_by=?,claimed_at=? WHERE id=? AND status='available'",
                    (q.from_user.id,int(time.time()),qr_id))
        con.commit()
    try:
        await bot.send_photo(q.from_user.id,row["file_id"],
            caption=f"🎯 <b>QR CLAIMED</b>\n\n📱 App: {row['app_name'] or 'QR App'}\n💰 Reward: ₹{row['reward']:.2f}\n\nComplete the required action, then upload your proof.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📤 UPLOAD PROOF",callback_data=f"proof:{qr_id}")]
            ]))
        await q.answer("✅ QR claimed! Check your private chat.")
        try:
            await q.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=f"🔒 CLAIMED BY {q.from_user.full_name[:20]}",callback_data="claimed")]
            ]))
        except Exception:
            pass
    except Exception:
        with closing(db()) as con:
            con.execute("UPDATE qr_orders SET status='available',claimed_by=NULL,claimed_at=NULL WHERE id=?",(qr_id,))
            con.commit()
        await q.answer("❌ I could not send the QR to your DM. Start the bot privately first.",show_alert=True)

@dp.callback_query(F.data.startswith("proof:"))
async def proof_start(q):
    await q.answer()
    qr_id=int(q.data.split(":")[1])
    with closing(db()) as con:
        row=con.execute("SELECT claimed_by FROM qr_orders WHERE id=?",(qr_id,)).fetchone()
    if not row or row["claimed_by"]!=q.from_user.id:
        return await q.message.answer("❌ This QR is not assigned to you.")
    pending[q.from_user.id]=f"proof:{qr_id}"
    await q.message.answer("📤 Send your proof screenshot/photo now.")

@dp.message(F.photo)
async def photo_handler(message: Message):
    uid=message.from_user.id
    action=pending.get(uid)
    if not action: return
    if action=="addqr":
        pending[uid]="addqr_app:"+message.photo[-1].file_id
        await message.answer("📱 Enter the QR app name (example: PhonePe / GPay):")
    elif action.startswith("proof:"):
        qr_id=int(action.split(":")[1])
        fid=message.photo[-1].file_id
        with closing(db()) as con:
            old=con.execute("SELECT id FROM proofs WHERE qr_id=? AND user_id=? AND status='pending'",(qr_id,uid)).fetchone()
            if old:
                pending.pop(uid,None)
                return await message.answer("⏳ Proof already under review.")
            con.execute("INSERT INTO proofs(qr_id,user_id,file_id,status,created_at) VALUES(?,?,?,?,?)",
                        (qr_id,uid,fid,"pending",int(time.time())))
            con.commit()
        pending[uid]=None
        kb=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🟢 APPROVE",callback_data=f"approveproof:{qr_id}:{uid}"),
            InlineKeyboardButton(text="🔴 REJECT",callback_data=f"rejectproof:{qr_id}:{uid}")
        ]])
        with closing(db()) as con:
            qr=con.execute("SELECT reward,app_name FROM qr_orders WHERE id=?",(qr_id,)).fetchone()
            u=con.execute("SELECT username,first_name FROM users WHERE id=?",(uid,)).fetchone()
        for aid in list(ADMIN_IDS):
            try:
                await bot.send_photo(aid,fid,caption=f"🧾 <b>PROOF REVIEW</b>\n\nQR ID: #{qr_id}\nUser: {('@'+u['username']) if u['username'] else u['first_name']}\nUser ID: {uid}\nApp: {qr['app_name']}\nReward: ₹{qr['reward']:.2f}",parse_mode="HTML",reply_markup=kb)
            except Exception:
                pass
        await message.answer("⏳ <b>UNDER REVIEW</b>\n\nYour proof has been submitted. Please wait for admin review.",parse_mode="HTML")

@dp.message(F.text)
async def text_handler(message: Message):
    uid=message.from_user.id
    action=pending.get(uid)
    if not action: return
    text=message.text.strip()
    try:
        if action=="withdraw_amount":
            amount=float(text)
            if amount<MIN_WITHDRAWAL: return await message.answer(f"❌ Minimum withdrawal is ₹{MIN_WITHDRAWAL:g}")
            u=user_row(uid)
            if amount>u["balance"]: return await message.answer("❌ Insufficient balance.")
            methods=[]
            if ENABLE_UPI: methods.append([InlineKeyboardButton(text="🏦 UPI",callback_data=f"wdmethod:{amount}:UPI")])
            if ENABLE_USDT: methods.append([InlineKeyboardButton(text="🪙 USDT",callback_data=f"wdmethod:{amount}:USDT")])
            methods.append([InlineKeyboardButton(text="🔙 CANCEL",callback_data="home")])
            pending[uid]=f"withdraw_method:{amount}"
            await message.answer(f"💸 <b>WITHDRAWAL REQUEST</b>\n\nAmount: ₹{amount:.2f}\nAvailable Balance: ₹{u['balance']:.2f}\n\nSelect withdrawal method:",parse_mode="HTML",reply_markup=InlineKeyboardMarkup(inline_keyboard=methods))
        elif action.startswith("addqr_app:"):
            fid=action.split(":",1)[1]
            pending[uid]=f"addqr_reward:{fid}:{text}"
            await message.answer("💰 Enter the reward amount in ₹ (example: 50):")
        elif action.startswith("addqr_reward:"):
            _,fid,app_name=action.split(":",2)
            reward=float(text)
            pending[uid]=f"addqr_title:{fid}:{app_name}:{reward}"
            await message.answer("📝 Enter a title for this QR:")
        elif action.startswith("addqr_title:"):
            _,fid,app_name,reward=action.split(":",3)
            with closing(db()) as con:
                con.execute("INSERT INTO qr_orders(title,file_id,app_name,reward,status,created_at) VALUES(?,?,?,?,?,?)",
                            (text,fid,app_name,float(reward),"available",int(time.time())))
                con.commit()
            pending.pop(uid,None)
            await message.answer("✅ QR added successfully. It is available to post.")
        elif action.startswith("withdraw_method:"):
            amount=float(action.split(":")[1])
            method=text.upper()
            if method not in ("UPI","USDT"):
                return await message.answer("❌ Send UPI or USDT.")
            pending[uid]=f"withdraw_value:{amount}:{method}"
            await message.answer(f"Enter your {method} ID/address:")
        elif action.startswith("withdraw_value:"):
            _,amount,method=action.split(":",2)
            amount=float(amount)
            with closing(db()) as con:
                u=con.execute("SELECT balance FROM users WHERE id=?",(uid,)).fetchone()
                if not u or u["balance"]<amount: return await message.answer("❌ Balance changed. Please try again.")
                now=int(time.time())
                con.execute("UPDATE users SET balance=balance-? WHERE id=?",(amount,uid))
                con.execute("INSERT INTO withdrawals(user_id,amount,method,value,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                            (uid,amount,method,text,"pending",now,now))
                wid=con.execute("SELECT last_insert_rowid()").fetchone()[0]
                con.execute("INSERT INTO transactions(user_id,kind,amount,note,created_at) VALUES(?,?,?,?,?)",
                            (uid,"withdrawal",-amount,f"Withdrawal #{wid}",now))
                con.commit()
            pending.pop(uid,None)
            await message.answer(f"✅ Withdrawal request #{wid} submitted for ₹{amount:.2f}.")
            for aid in list(ADMIN_IDS):
                try:
                    await bot.send_message(aid,f"💸 <b>NEW WITHDRAWAL</b>\n\n#{wid}\nUser: {uid}\nAmount: ₹{amount:.2f}\nMethod: {method}\nValue: {text}",
                        parse_mode="HTML",reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                            [InlineKeyboardButton(text="🟢 PROCESSING",callback_data=f"wdstatus:{wid}:processing"),
                             InlineKeyboardButton(text="🔴 REJECT",callback_data=f"wdstatus:{wid}:rejected")]
                        ]))
                except Exception: pass
    except ValueError:
        await message.answer("❌ Please enter a valid value.")
    except Exception:
        await message.answer("❌ Something went wrong. Try again.")

@dp.callback_query(F.data.startswith("wdmethod:"))
async def wdmethod(q):
    await q.answer()
    _,amount,method=q.data.split(":")
    pending[q.from_user.id]=f"withdraw_value:{amount}:{method}"
    await q.message.answer(f"Enter your {method} ID/address:")

@dp.callback_query(F.data.startswith("approveproof:"))
async def approveproof(q):
    if not is_admin(q.from_user.id): return await q.answer("Not authorized",show_alert=True)
    await q.answer()
    _,qrid,uid=q.data.split(":"); qrid=int(qrid); uid=int(uid)
    with closing(db()) as con:
        p=con.execute("SELECT id,status FROM proofs WHERE qr_id=? AND user_id=? ORDER BY id DESC LIMIT 1",(qrid,uid)).fetchone()
        qr=con.execute("SELECT reward,status FROM qr_orders WHERE id=?",(qrid,)).fetchone()
        if not p or p["status"]!="pending" or not qr: return await q.answer("Already processed",show_alert=True)
        con.execute("UPDATE proofs SET status='approved' WHERE id=?",(p["id"],))
        con.execute("UPDATE qr_orders SET status='completed' WHERE id=?",(qrid,))
        con.execute("UPDATE users SET balance=balance+?,total_earned=total_earned+? WHERE id=?",(qr["reward"],qr["reward"],uid))
        con.execute("INSERT INTO transactions(user_id,kind,amount,note,created_at) VALUES(?,?,?,?,?)",(uid,"reward",qr["reward"],f"QR Reward #{qrid}",int(time.time())))
        con.commit()
    try: await bot.send_message(uid,f"🟢 <b>QR PROOF APPROVED</b>\n\nReward ₹{qr['reward']:.2f} has been added to your wallet.",parse_mode="HTML")
    except Exception: pass
    await q.message.edit_caption((q.message.caption or "")+"\n\n🟢 APPROVED",reply_markup=None)

@dp.callback_query(F.data.startswith("rejectproof:"))
async def rejectproof(q):
    if not is_admin(q.from_user.id): return await q.answer("Not authorized",show_alert=True)
    await q.answer()
    _,qrid,uid=q.data.split(":"); qrid=int(qrid); uid=int(uid)
    with closing(db()) as con:
        p=con.execute("SELECT id,status FROM proofs WHERE qr_id=? AND user_id=? ORDER BY id DESC LIMIT 1",(qrid,uid)).fetchone()
        if not p or p["status"]!="pending": return await q.answer("Already processed",show_alert=True)
        con.execute("UPDATE proofs SET status='rejected' WHERE id=?",(p["id"],))
        con.execute("UPDATE qr_orders SET status='rejected' WHERE id=?",(qrid,))
        con.commit()
    try: await bot.send_message(uid,"🔴 <b>QR PROOF REJECTED</b>\n\nYour proof was rejected by admin.",parse_mode="HTML")
    except Exception: pass
    await q.message.edit_caption((q.message.caption or "")+"\n\n🔴 REJECTED",reply_markup=None)

@dp.callback_query(F.data.startswith("wdstatus:"))
async def wdstatus(q):
    if not is_admin(q.from_user.id): return await q.answer("Not authorized",show_alert=True)
    await q.answer()
    _,wid,status=q.data.split(":"); wid=int(wid)
    with closing(db()) as con:
        w=con.execute("SELECT * FROM withdrawals WHERE id=?",(wid,)).fetchone()
        if not w: return
        if status=="processing" and w["status"]=="pending":
            con.execute("UPDATE withdrawals SET status='processing',updated_at=? WHERE id=?",(int(time.time()),wid))
            con.commit()
            msg=f"🔄 Withdrawal #{wid} is now <b>PROCESSING</b>."
            await q.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ MARK PAID",callback_data=f"wdstatus:{wid}:paid"),
                 InlineKeyboardButton(text="🔴 REJECT",callback_data=f"wdstatus:{wid}:rejected")]
            ]))
        elif status=="paid" and w["status"]=="processing":
            con.execute("UPDATE withdrawals SET status='paid',updated_at=? WHERE id=?",(int(time.time()),wid))
            con.execute("UPDATE users SET total_withdrawn=total_withdrawn+? WHERE id=?",(w["amount"],w["user_id"]))
            con.commit()
            msg=f"✅ Withdrawal #{wid} marked <b>PAID</b>."
            await q.message.edit_reply_markup(reply_markup=None)
        elif status=="rejected" and w["status"] in ("pending","processing"):
            con.execute("UPDATE withdrawals SET status='rejected',updated_at=? WHERE id=?",(int(time.time()),wid))
            con.execute("UPDATE users SET balance=balance+? WHERE id=?",(w["amount"],w["user_id"]))
            con.execute("INSERT INTO transactions(user_id,kind,amount,note,created_at) VALUES(?,?,?,?,?)",(w["user_id"],"refund",w["amount"],f"Withdrawal #{wid} refund",int(time.time())))
            con.commit()
            msg=f"🔴 Withdrawal #{wid} rejected and refunded."
            await q.message.edit_reply_markup(reply_markup=None)
        else:
            return await q.answer("Invalid status transition",show_alert=True)
    try: await bot.send_message(w["user_id"],msg,parse_mode="HTML")
    except Exception: pass

@dp.message(Command("admin"))
async def admin_cmd(message: Message):
    if not is_admin(message.from_user.id):
        return
    if not WEBAPP_URL:
        return await message.answer("❌ Mini App is not configured. Set WEBAPP_URL in Railway.")
    await message.answer("🛠 <b>ADMIN PANEL</b>\n\nChoose an option:",parse_mode="HTML",reply_markup=admin_menu())

@dp.callback_query(F.data == "a_qrs")
async def a_qrs(q):
    if not is_admin(q.from_user.id): return await q.answer("Not authorized",show_alert=True)
    await q.answer()
    with closing(db()) as con:
        rows=con.execute("SELECT id,title,app_name,reward,status,claimed_by FROM qr_orders ORDER BY id DESC LIMIT 20").fetchall()
    text="📱 <b>QR LIST</b>\n\n" + "\n".join(f"#{r['id']} | ₹{r['reward']:.2f} | {r['status']} | {r['app_name']}" for r in rows)
    await q.message.answer(text or "No QR orders.")

@dp.callback_query(F.data == "a_proofs")
async def a_proofs(q):
    if not is_admin(q.from_user.id): return await q.answer("Not authorized",show_alert=True)
    await q.answer()
    with closing(db()) as con:
        rows=con.execute("""SELECT p.id,p.qr_id,p.user_id,q.reward,p.status
                            FROM proofs p JOIN qr_orders q ON q.id=p.qr_id
                            WHERE p.status='pending' ORDER BY p.id DESC LIMIT 20""").fetchall()
    if not rows: return await q.message.answer("🧾 No pending proofs.")
    for r in rows:
        await q.message.answer(f"🧾 Proof #{r['id']}\nQR #{r['qr_id']} • User {r['user_id']} • ₹{r['reward']:.2f}",reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🟢 APPROVE",callback_data=f"approveproof:{r['qr_id']}:{r['user_id']}"),
             InlineKeyboardButton(text="🔴 REJECT",callback_data=f"rejectproof:{r['qr_id']}:{r['user_id']}")]
        ]))

@dp.callback_query(F.data == "a_withdrawals")
async def a_withdrawals(q):
    if not is_admin(q.from_user.id): return await q.answer("Not authorized",show_alert=True)
    await q.answer()
    with closing(db()) as con:
        rows=con.execute("SELECT * FROM withdrawals WHERE status IN ('pending','processing') ORDER BY id DESC LIMIT 20").fetchall()
    if not rows: return await q.message.answer("💸 No pending/processing withdrawals.")
    for r in rows:
        buttons=[]
        if r["status"]=="pending":
            buttons.append(InlineKeyboardButton(text="🔄 PROCESSING",callback_data=f"wdstatus:{r['id']}:processing"))
        if r["status"]=="processing":
            buttons.append(InlineKeyboardButton(text="✅ PAID",callback_data=f"wdstatus:{r['id']}:paid"))
        buttons.append(InlineKeyboardButton(text="🔴 REJECT",callback_data=f"wdstatus:{r['id']}:rejected"))
        await q.message.answer(f"💸 Withdrawal #{r['id']}\nUser: {r['user_id']}\nAmount: ₹{r['amount']:.2f}\nMethod: {r['method']}\nValue: {r['value']}\nStatus: {r['status']}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[buttons]))

@dp.callback_query(F.data == "a_users")
async def a_users(q):
    if not is_admin(q.from_user.id): return await q.answer("Not authorized",show_alert=True)
    await q.answer()
    with closing(db()) as con:
        n=con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        bal=con.execute("SELECT COALESCE(SUM(balance),0) FROM users").fetchone()[0]
        earned=con.execute("SELECT COALESCE(SUM(total_earned),0) FROM users").fetchone()[0]
    await q.message.answer(f"👥 USERS\n\nTotal users: {n}\nWallet liability: ₹{bal:.2f}\nTotal earned: ₹{earned:.2f}")

# Mini App API
def verify_init_data(init_data):
    data=dict(parse_qsl(init_data or "",keep_blank_values=True))
    received=data.pop("hash",None)
    if not received: raise HTTPException(401,"Telegram initData required")
    check="\n".join(f"{k}={v}" for k,v in sorted(data.items()))
    secret=hmac.new(b"WebAppData",BOT_TOKEN.encode(),hashlib.sha256).digest()
    calc=hmac.new(secret,check.encode(),hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc,received): raise HTTPException(401,"Invalid Telegram signature")
    if abs(time.time()-int(data.get("auth_date","0"))) > 86400: raise HTTPException(401,"Expired initData")
    return json.loads(data.get("user","{}"))

def admin_from_header(v):
    u=verify_init_data(v)
    if not is_admin(int(u["id"])): raise HTTPException(403,"Admin access required")
    return int(u["id"])

@app.get("/",response_class=HTMLResponse)
async def root():
    return HTMLResponse(INDEX_HTML)

@app.get("/admin",response_class=HTMLResponse)
async def admin_page():
    return HTMLResponse(ADMIN_HTML)

@app.get("/api/admin/overview")
async def overview(x_telegram_init_data: str|None=Header(default=None)):
    admin_from_header(x_telegram_init_data)
    with closing(db()) as con:
        users=con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        available=con.execute("SELECT COUNT(*) FROM qr_orders WHERE status='available'").fetchone()[0]
        claimed=con.execute("SELECT COUNT(*) FROM qr_orders WHERE status='claimed'").fetchone()[0]
        proofs=con.execute("SELECT COUNT(*) FROM proofs WHERE status='pending'").fetchone()[0]
        pending_w=con.execute("SELECT COUNT(*) FROM withdrawals WHERE status='pending'").fetchone()[0]
        processing=con.execute("SELECT COUNT(*) FROM withdrawals WHERE status='processing'").fetchone()[0]
        paid=con.execute("SELECT COALESCE(SUM(amount),0) FROM withdrawals WHERE status='paid'").fetchone()[0]
        total_w=con.execute("SELECT COUNT(*) FROM withdrawals").fetchone()[0]
    return {"users":users,"qr_available":available,"qr_claimed":claimed,"proofs_pending":proofs,
            "withdrawals_pending":pending_w,"withdrawals_processing":processing,
            "withdrawals_total":total_w,"withdrawals_paid_amount":paid}

@app.get("/api/admin/qrs")
async def qrs(x_telegram_init_data: str|None=Header(default=None)):
    admin_from_header(x_telegram_init_data)
    with closing(db()) as con:
        rows=con.execute("SELECT id,title,app_name,reward,status,claimed_by,created_at FROM qr_orders ORDER BY id DESC LIMIT 100").fetchall()
    return [dict(r) for r in rows]

@app.get("/api/admin/withdrawals")
async def withdrawals_api(x_telegram_init_data: str|None=Header(default=None)):
    admin_from_header(x_telegram_init_data)
    with closing(db()) as con:
        rows=con.execute("""SELECT w.*,u.username,u.first_name FROM withdrawals w
                            LEFT JOIN users u ON u.id=w.user_id ORDER BY w.id DESC LIMIT 100""").fetchall()
    return [dict(r) for r in rows]

@app.get("/api/admin/proofs")
async def proofs_api(x_telegram_init_data: str|None=Header(default=None)):
    admin_from_header(x_telegram_init_data)
    with closing(db()) as con:
        rows=con.execute("""SELECT p.id,p.qr_id,p.user_id,p.status,p.created_at,q.reward,q.app_name
                            FROM proofs p JOIN qr_orders q ON q.id=p.qr_id
                            ORDER BY p.id DESC LIMIT 100""").fetchall()
    return [dict(r) for r in rows]

INDEX_HTML = """<!doctype html><html><body style='font-family:Arial;padding:25px'><h2>QR Claim Bot</h2><p>Open this page from the Telegram bot.</p></body></html>"""
ADMIN_HTML = """<!doctype html><html><head><meta name='viewport' content='width=device-width,initial-scale=1'>
<style>body{font-family:Arial;margin:0;background:#f4f7fb;color:#172033}.wrap{max-width:900px;margin:auto;padding:16px}.card{background:white;border-radius:16px;padding:16px;margin:10px 0;box-shadow:0 3px 14px #0001}.grid{display:grid;grid-template-columns:repeat(2,1fr);gap:10px}.num{font-size:26px;font-weight:700}button{padding:12px;border:0;border-radius:10px;background:#1976d2;color:white;margin:4px}h1{font-size:24px}@media(max-width:600px){.grid{grid-template-columns:1fr}}</style>
</head><body><div class='wrap'><h1>🛠 QR Claim Admin Dashboard</h1><div id='app'>Loading...</div></div>
<script>
const tg=window.Telegram?.WebApp; tg?.ready(); tg?.expand();
const init=tg?.initData||'';
async function api(path){let r=await fetch(path,{headers:{'X-Telegram-Init-Data':init}});if(!r.ok)throw new Error(await r.text());return r.json()}
async function load(){let d=await api('/api/admin/overview');document.getElementById('app').innerHTML=`
<div class='grid'>
<div class='card'>👥 Users<div class='num'>${d.users}</div></div>
<div class='card'>📱 Available QR<div class='num'>${d.qr_available}</div></div>
<div class='card'>🎯 Claimed QR<div class='num'>${d.qr_claimed}</div></div>
<div class='card'>🧾 Proof Pending<div class='num'>${d.proofs_pending}</div></div>
<div class='card'>💸 Pending Withdrawals<div class='num'>${d.withdrawals_pending}</div></div>
<div class='card'>🔄 Processing<div class='num'>${d.withdrawals_processing}</div></div>
<div class='card'>📊 Total Withdrawals<div class='num'>${d.withdrawals_total}</div></div>
<div class='card'>✅ Paid Amount<div class='num'>₹${Number(d.withdrawals_paid_amount).toFixed(2)}</div></div>
</div>
<div class='card'><h3>Quick Actions</h3>
<button onclick='showQrs()'>📱 QR List</button><button onclick='showProofs()'>🧾 Proof Review</button><button onclick='showWithdrawals()'>💸 Withdrawals</button>
</div><div id='list'></div>`}
async function showQrs(){let a=await api('/api/admin/qrs');document.getElementById('list').innerHTML='<div class="card"><h3>📱 QR Orders</h3>'+a.map(x=>`<p>#${x.id} — ₹${x.reward} — ${x.app_name} — <b>${x.status}</b>${x.claimed_by?' — user '+x.claimed_by:''}</p>`).join('')+'</div>'}
async function showProofs(){let a=await api('/api/admin/proofs');document.getElementById('list').innerHTML='<div class="card"><h3>🧾 Proofs</h3>'+a.map(x=>`<p>#${x.id} — QR #${x.qr_id} — user ${x.user_id} — ₹${x.reward} — <b>${x.status}</b></p>`).join('')+'</div>'}
async function showWithdrawals(){let a=await api('/api/admin/withdrawals');document.getElementById('list').innerHTML='<div class="card"><h3>💸 Withdrawals</h3>'+a.map(x=>`<p>#${x.id} — user ${x.user_id} — ₹${x.amount} — ${x.method} — <b>${x.status}</b></p>`).join('')+'</div>'}
load().catch(e=>document.getElementById('app').innerHTML='<div class="card">❌ '+e.message+'</div>');
</script></body></html>"""

async def main():
    global bot
    init_db()
    bot=Bot(BOT_TOKEN)
    if WEBAPP_URL:
        try:
            await bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(text="📱 Dashboard",web_app=WebAppInfo(url=WEBAPP_URL))
            )
        except Exception:
            pass
    config=uvicorn.Config(app,host="0.0.0.0",port=int(os.getenv("PORT","8080")),log_level="info")
    server=uvicorn.Server(config)
    await asyncio.gather(dp.start_polling(bot),server.serve())

if __name__=="__main__":
    asyncio.run(main())
