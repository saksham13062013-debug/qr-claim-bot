import asyncio, io, os, sqlite3, time
from datetime import datetime, timezone
import qrcode
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, CallbackQuery, BufferedInputFile, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN", "")
DB_PATH = os.getenv("DB_PATH", "qr_work.db")
ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}
CURRENCY = os.getenv("CURRENCY", "USDT")
TASK_TTL = int(os.getenv("TASK_TTL_MINUTES", "30")) * 60
OFFLINE_AFTER = int(os.getenv("OFFLINE_AFTER_MINUTES", "30")) * 60

if not TOKEN:
    raise RuntimeError("BOT_TOKEN is missing")

db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row

def now(): return int(time.time())
def init_db():
    db.executescript("""
    CREATE TABLE IF NOT EXISTS users(
      id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
      balance REAL NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 0,
      last_action INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS tasks(
      id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
      qr_payload TEXT NOT NULL, reward REAL NOT NULL,
      status TEXT NOT NULL DEFAULT 'open', claimed_by INTEGER,
      claimed_at INTEGER, expires_at INTEGER, verified_at INTEGER,
      created_at INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS task_history(
      id INTEGER PRIMARY KEY AUTOINCREMENT, task_id INTEGER NOT NULL,
      user_id INTEGER NOT NULL, action TEXT NOT NULL, created_at INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS settings(
      key TEXT PRIMARY KEY, value TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS admin_state(
      admin_id INTEGER PRIMARY KEY, step TEXT NOT NULL, data TEXT NOT NULL DEFAULT ''
    );
    """)
    db.commit()
    for key, value in STYLE_DEFAULTS.items():
        if get_setting(key, "") == "":
            set_setting(key, value)
    if get_setting("style", "") == "":
        set_setting("style", "modern")

def ensure_user(u):
    db.execute("""INSERT INTO users(id,username,first_name,last_action,created_at)
      VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
      username=excluded.username, first_name=excluded.first_name, last_action=excluded.last_action""",
      (u.id,u.username or "",u.first_name or "",now(),now()))
    db.commit()


STYLE_DEFAULTS = {
    "style": "modern",
    "task_header": "🆕 <b>New task #{task_id} · {title}</b>",
    "task_reward": "💰 Your reward: <b>{reward:.2f} {currency}</b>",
    "task_expiry": "⏳ Expires: <b>{expires}</b>",
    "task_warning": "⚠️ <b>Claim before opening the payment link.</b>",
    "task_footer": "The system assigns the earliest-expiring task currently open to you. Task numbers in older messages do not select a particular task.",
}

STYLE_PRESETS = {
    "modern": STYLE_DEFAULTS,
    "minimal": {
        "style": "minimal",
        "task_header": "🆕 <b>Task #{task_id}</b> · {title}",
        "task_reward": "💰 Reward: <b>{reward:.2f} {currency}</b>",
        "task_expiry": "⏳ Expires: <b>{expires}</b>",
        "task_warning": "⚠️ Claim before opening the payment link.",
        "task_footer": "Earliest-expiring open task is assigned automatically.",
    },
    "premium": {
        "style": "premium",
        "task_header": "╭━━━ ✦ <b>NEW TASK</b> ✦ ━━━╮\n┃ #{task_id} · {title}",
        "task_reward": "┃ 💰 Reward: <b>{reward:.2f} {currency}</b>",
        "task_expiry": "┃ ⏳ Expires: <b>{expires}</b>",
        "task_warning": "┃ ⚠️ Claim before opening the payment link.",
        "task_footer": "╰━━━━━━━━━━━━━━━━━━━━╯\nThe earliest-expiring open task is assigned automatically.",
    },
}

def get_setting(key, default=""):
    row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default

def set_setting(key, value):
    db.execute("""INSERT INTO settings(key,value) VALUES(?,?)
                  ON CONFLICT(key) DO UPDATE SET value=excluded.value""", (key, value))
    db.commit()

def get_style():
    name = get_setting("style", "modern")
    preset = STYLE_PRESETS.get(name, STYLE_PRESETS["modern"]).copy()
    # Allow saved custom fields to override presets.
    for key in ("task_header","task_reward","task_expiry","task_warning","task_footer"):
        value = get_setting(key, "")
        if value:
            preset[key] = value
    return preset

def render_task_message(task):
    st = get_style()
    values = {
        "task_id": task["id"],
        "title": task["title"],
        "reward": task["reward"],
        "currency": CURRENCY,
        "expires": fmt(task["expires_at"]),
    }
    return "\n".join(
        part.format(**values) for part in (
            st["task_header"], st["task_reward"], st["task_expiry"],
            "", st["task_warning"], "", st["task_footer"]
        )
    )


def is_admin(uid):
    return uid in ADMIN_IDS

def admin_menu():
    b = InlineKeyboardBuilder()
    items = [
        ("📊 Statistics", "adm:stats"),
        ("➕ Create Task", "adm:create"),
        ("📋 All Tasks", "adm:tasks"),
        ("🟢 Open Tasks", "adm:open"),
        ("⏳ Claimed Tasks", "adm:claimed"),
        ("✅ Pending Reviews", "adm:reviews"),
        ("👥 Users", "adm:users"),
        ("💰 Balances", "adm:balances"),
        ("🎨 Message Style", "adm:style"),
        ("📢 Broadcast", "adm:broadcast"),
        ("🔄 Refresh", "adm:menu"),
        ("⬅️ Main Menu", "menu"),
    ]
    for label, data in items:
        b.button(text=label, callback_data=data)
    b.adjust(2, 2, 2, 2, 2, 2)
    return b.as_markup()

def admin_style_menu():
    b = InlineKeyboardBuilder()
    for label, data in [
        ("🟦 Modern", "admstyle:modern"),
        ("⚪ Minimal", "admstyle:minimal"),
        ("💎 Premium", "admstyle:premium"),
        ("⬅️ Admin Panel", "adm:menu"),
    ]:
        b.button(text=label, callback_data=data)
    b.adjust(1)
    return b.as_markup()

def admin_state(uid):
    row = db.execute("SELECT step,data FROM admin_state WHERE admin_id=?", (uid,)).fetchone()
    return (row["step"], row["data"]) if row else ("", "")

def set_admin_state(uid, step, data=""):
    db.execute("""INSERT INTO admin_state(admin_id,step,data) VALUES(?,?,?)
                  ON CONFLICT(admin_id) DO UPDATE SET step=excluded.step,data=excluded.data""",
               (uid, step, data))
    db.commit()

def clear_admin_state(uid):
    db.execute("DELETE FROM admin_state WHERE admin_id=?", (uid,))
    db.commit()

async def admin_panel(m: Message):
    if not is_admin(m.from_user.id):
        await m.answer("⛔ <b>Admin access denied.</b>", parse_mode="HTML")
        return
    await m.answer("🔐 <b>ADMIN PANEL</b>\n\nSelect an option:",
                   reply_markup=admin_menu(), parse_mode="HTML")

async def admin_text(m: Message):
    uid = m.from_user.id
    if not is_admin(uid) or not m.text:
        return
    step, data = admin_state(uid)
    if not step:
        return

    if step == "broadcast":
        rows = db.execute("SELECT id FROM users").fetchall()
        sent = 0
        for row in rows:
            try:
                await m.bot.send_message(row["id"], m.text)
                sent += 1
            except Exception:
                pass
        clear_admin_state(uid)
        await m.answer(f"📢 <b>Broadcast complete</b>\nSent: {sent}",
                       reply_markup=admin_menu(), parse_mode="HTML")
        return

    if step == "task_title":
        set_admin_state(uid, "task_reward", m.text.strip())
        await m.answer("2/4 💰 Send reward, e.g. <code>0.70</code>", parse_mode="HTML")
        return

    if step == "task_reward":
        try:
            float(m.text.strip())
        except ValueError:
            await m.answer("❌ Send a valid number, e.g. <code>0.70</code>.", parse_mode="HTML")
            return
        set_admin_state(uid, "task_payload", data + "\n" + m.text.strip())
        await m.answer("3/4 🔗 Send the QR/payment payload.", parse_mode="HTML")
        return

    if step == "task_payload":
        set_admin_state(uid, "task_expiry", data + "\n" + m.text.strip())
        await m.answer(f"4/4 ⏳ Send expiry in minutes. Default: {TASK_TTL//60}.")
        return

    if step == "task_expiry":
        try:
            minutes = max(1, int(m.text.strip()))
        except ValueError:
            minutes = TASK_TTL // 60

        parts = data.split("\n", 2)
        title = parts[0]
        reward = float(parts[1])
        payload = parts[2] if len(parts) > 2 else ""

        cur = db.execute(
            """INSERT INTO tasks(title,qr_payload,reward,status,expires_at,created_at)
               VALUES(?,?,?,'open',?,?)""",
            (title, payload, reward, now() + minutes * 60, now())
        )
        tid = cur.lastrowid
        db.commit()
        clear_admin_state(uid)

        task = db.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        await m.answer(
            f"✅ <b>Task #{tid} created</b>\n\n{render_task_message(task)}",
            reply_markup=admin_menu(), parse_mode="HTML"
        )

def main_menu():
    b=InlineKeyboardBuilder()
    b.button(text="💼 Work status",callback_data="work_status")
    b.button(text="📋 Tasks",callback_data="tasks")
    b.button(text="💰 Wallet",callback_data="wallet")
    b.button(text="👥 My team",callback_data="team")
    b.button(text="⚙️ More",callback_data="more")
    b.adjust(2,2,1); return b.as_markup()

def work_menu():
    b=InlineKeyboardBuilder()
    b.button(text="📥 Claim next task",callback_data="claim")
    b.button(text="🛑 Stop work",callback_data="stop")
    b.button(text="🔄 Refresh",callback_data="work_status")
    b.button(text="⬅️ Menu",callback_data="menu")
    b.adjust(1,2,1); return b.as_markup()

def task_menu():
    b=InlineKeyboardBuilder()
    b.button(text="📥 Claim next task",callback_data="claim")
    b.button(text="📌 My tasks",callback_data="my_tasks")
    b.button(text="🗂 Task history",callback_data="history")
    b.button(text="💰 Wallet",callback_data="wallet")
    b.button(text="🔄 Refresh",callback_data="tasks")
    b.button(text="⬅️ Menu",callback_data="menu")
    b.adjust(1,2,2,1); return b.as_markup()

def active_task(uid):
    return db.execute("""SELECT * FROM tasks WHERE claimed_by=? AND status='claimed'
      AND expires_at>? ORDER BY expires_at ASC LIMIT 1""",(uid,now())).fetchone()

def fmt(ts): return datetime.fromtimestamp(ts,timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

def qr(payload):
    img=qrcode.make(payload); out=io.BytesIO(); img.save(out,"PNG"); return out.getvalue()

async def send_task(bot, chat_id, task):
    cap = render_task_message(task)
    kb=InlineKeyboardMarkup(inline_keyboard=[
      [InlineKeyboardButton(text="✅ Done · verify completion",callback_data=f"done:{task['id']}")],
      [InlineKeyboardButton(text="🔄 Show QR code",callback_data=f"qr:{task['id']}")]])
    await bot.send_photo(chat_id,BufferedInputFile(qr(task["qr_payload"]),"task_qr.png"),
                         caption=cap,reply_markup=kb,parse_mode="HTML")

async def start(m:Message):
    ensure_user(m.from_user)
    db.execute("UPDATE users SET active=1,last_action=? WHERE id=?",(now(),m.from_user.id)); db.commit()
    await m.answer("👋 <b>Welcome to QR Work</b>\n\n🟢 Work mode is active.",reply_markup=work_menu(),parse_mode="HTML")

async def menu(m:Message):
    ensure_user(m.from_user); await m.answer("🏠 <b>Main menu</b>",reply_markup=main_menu(),parse_mode="HTML")

async def callback(c:CallbackQuery,bot:Bot):
    ensure_user(c.from_user); uid=c.from_user.id
    db.execute("UPDATE users SET last_action=? WHERE id=?",(now(),uid)); db.commit()
    d=c.data or ""

    if d == "adm:menu":
        if not is_admin(uid):
            await c.answer("Access denied.", show_alert=True); return
        await c.message.edit_text("🔐 <b>ADMIN PANEL</b>\n\nSelect an option:",
                                  reply_markup=admin_menu(), parse_mode="HTML")

    elif d == "adm:stats":
        if not is_admin(uid):
            await c.answer("Access denied.", show_alert=True); return
        users = db.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
        open_n = db.execute("SELECT COUNT(*) n FROM tasks WHERE status='open'").fetchone()["n"]
        claimed = db.execute("SELECT COUNT(*) n FROM tasks WHERE status='claimed'").fetchone()["n"]
        pending = db.execute("SELECT COUNT(*) n FROM tasks WHERE status='pending_review'").fetchone()["n"]
        verified = db.execute("SELECT COUNT(*) n FROM tasks WHERE status='verified'").fetchone()["n"]
        await c.message.edit_text(
            f"📊 <b>Statistics</b>\n\n👥 Users: {users}\n🟢 Open: {open_n}\n"
            f"⏳ Claimed: {claimed}\n✅ Pending: {pending}\n🏁 Verified: {verified}",
            reply_markup=admin_menu(), parse_mode="HTML")

    elif d == "adm:create":
        if not is_admin(uid):
            await c.answer("Access denied.", show_alert=True); return
        set_admin_state(uid, "task_title")
        await c.message.answer("1/4 📝 Send task title, e.g. <code>UPI 扫码任务</code>", parse_mode="HTML")

    elif d in ("adm:tasks", "adm:open", "adm:claimed", "adm:reviews"):
        if not is_admin(uid):
            await c.answer("Access denied.", show_alert=True); return
        conditions = {
            "adm:tasks": "1=1",
            "adm:open": "status='open'",
            "adm:claimed": "status='claimed'",
            "adm:reviews": "status='pending_review'"
        }
        titles = {
            "adm:tasks": "📋 All Tasks",
            "adm:open": "🟢 Open Tasks",
            "adm:claimed": "⏳ Claimed Tasks",
            "adm:reviews": "✅ Pending Reviews"
        }
        rows = db.execute(f"SELECT * FROM tasks WHERE {conditions[d]} ORDER BY id DESC LIMIT 20").fetchall()
        body = "\n".join(
            f"#{r['id']} · {r['title']} — {r['status']} — {r['reward']:.2f} {CURRENCY}"
            for r in rows
        ) or "No records."
        await c.message.edit_text(f"{titles[d]}\n\n{body}", reply_markup=admin_menu(), parse_mode="HTML")

    elif d in ("adm:users", "adm:balances"):
        if not is_admin(uid):
            await c.answer("Access denied.", show_alert=True); return
        rows = db.execute(
            "SELECT id,username,balance,active FROM users ORDER BY last_action DESC LIMIT 20"
        ).fetchall()
        title = "👥 Users" if d == "adm:users" else "💰 Balances"
        body = "\n".join(
            f"👤 {r['username'] or r['id']} — {r['balance']:.2f} {CURRENCY}"
            for r in rows
        ) or "No users."
        await c.message.edit_text(f"{title}\n\n{body}", reply_markup=admin_menu(), parse_mode="HTML")

    elif d == "adm:style":
        if not is_admin(uid):
            await c.answer("Access denied.", show_alert=True); return
        current = get_setting("style", "modern").title()
        await c.message.edit_text(f"🎨 <b>Message Style</b>\n\nCurrent: <b>{current}</b>",
                                  reply_markup=admin_style_menu(), parse_mode="HTML")

    elif d.startswith("admstyle:"):
        if not is_admin(uid):
            await c.answer("Access denied.", show_alert=True); return
        style = d.split(":", 1)[1]
        preset = STYLE_PRESETS.get(style, STYLE_PRESETS["modern"])
        set_setting("style", style)
        for key in ("task_header", "task_reward", "task_expiry", "task_warning", "task_footer"):
            set_setting(key, preset[key])
        await c.message.edit_text(f"🎨 <b>{style.title()} style saved.</b>",
                                  reply_markup=admin_menu(), parse_mode="HTML")

    elif d == "adm:broadcast":
        if not is_admin(uid):
            await c.answer("Access denied.", show_alert=True); return
        set_admin_state(uid, "broadcast")
        await c.message.answer("📢 Send the broadcast message now.")

    if d=="menu":
        await c.message.edit_text("🏠 <b>Main menu</b>",reply_markup=main_menu(),parse_mode="HTML")
    elif d=="work_status":
        u=db.execute("SELECT * FROM users WHERE id=?",(uid,)).fetchone()
        t=active_task(uid)
        await c.message.edit_text(f"💼 <b>Work status</b>\n\n{'🟢 Online' if u['active'] else '🔴 Offline'}"
                                  + (f"\n📌 Active task: #{t['id']}" if t else "\n📌 Active task: none"),
                                  reply_markup=work_menu(),parse_mode="HTML")
    elif d=="tasks":
        await c.message.edit_text("📋 <b>Tasks</b>\n\nClaiming assigns the earliest-expiring open task.",
                                  reply_markup=task_menu(),parse_mode="HTML")
    elif d=="claim":
        u=db.execute("SELECT active FROM users WHERE id=?",(uid,)).fetchone()
        if not u["active"]: await c.answer("Start work first.",show_alert=True); return
        t=active_task(uid)
        if t: await send_task(bot,uid,t); await c.answer("You already have an active task."); return
        t=db.execute("""SELECT * FROM tasks WHERE status='open' AND expires_at>?
                        ORDER BY expires_at ASC,id ASC LIMIT 1""",(now(),)).fetchone()
        if not t: await c.message.answer("📭 No tasks are available right now."); return
        db.execute("""UPDATE tasks SET status='claimed',claimed_by=?,claimed_at=? WHERE id=? AND status='open'""",
                   (uid,now(),t["id"]))
        db.execute("INSERT INTO task_history(task_id,user_id,action,created_at) VALUES(?,?,?,?)",
                   (t["id"],uid,"claimed",now())); db.commit()
        t=db.execute("SELECT * FROM tasks WHERE id=?",(t["id"],)).fetchone()
        await send_task(bot,uid,t)
    elif d.startswith("qr:"):
        tid=int(d.split(":")[1]); t=db.execute("SELECT * FROM tasks WHERE id=? AND claimed_by=?",(tid,uid)).fetchone()
        if not t: await c.answer("Task not found.",show_alert=True); return
        await bot.send_photo(uid,BufferedInputFile(qr(t["qr_payload"]),"task_qr.png"),caption=f"📱 QR for task #{tid}")
    elif d.startswith("done:"):
        tid=int(d.split(":")[1]); t=db.execute("SELECT * FROM tasks WHERE id=? AND claimed_by=? AND status='claimed'",(tid,uid)).fetchone()
        if not t: await c.answer("Task is no longer active.",show_alert=True); return
        if t["expires_at"]<=now():
            db.execute("UPDATE tasks SET status='expired' WHERE id=?",(tid,)); db.commit()
            await c.message.answer("⏰ This task has expired."); return
        db.execute("UPDATE tasks SET status='pending_review',verified_at=? WHERE id=?",(now(),tid))
        db.execute("INSERT INTO task_history(task_id,user_id,action,created_at) VALUES(?,?,?,?)",(tid,uid,"submitted_for_review",now()))
        db.commit()
        await c.message.answer(f"🕐 <b>Task #{tid} submitted for review.</b>\nReward is credited after authorized verification.",parse_mode="HTML")
    elif d=="wallet":
        u=db.execute("SELECT balance FROM users WHERE id=?",(uid,)).fetchone()
        await c.message.edit_text(f"💰 <b>Wallet</b>\n\nBalance: <b>{u['balance']:.2f} {CURRENCY}</b>",reply_markup=main_menu(),parse_mode="HTML")
    elif d=="team":
        await c.message.edit_text("👥 <b>My team</b>\n\nReferral features can be configured separately.",reply_markup=main_menu(),parse_mode="HTML")
    elif d=="my_tasks":
        rows=db.execute("SELECT id,title,status,reward FROM tasks WHERE claimed_by=? ORDER BY id DESC LIMIT 10",(uid,)).fetchall()
        txt="📌 <b>My tasks</b>\n\n"+("\n".join(f"#{r['id']} — {r['status']} — {r['reward']:.2f}" for r in rows) or "No tasks yet.")
        await c.message.edit_text(txt,reply_markup=task_menu(),parse_mode="HTML")
    elif d=="history":
        rows=db.execute("SELECT task_id,action FROM task_history WHERE user_id=? ORDER BY id DESC LIMIT 10",(uid,)).fetchall()
        txt="🗂 <b>Task history</b>\n\n"+("\n".join(f"#{r['task_id']} — {r['action']}" for r in rows) or "No history yet.")
        await c.message.edit_text(txt,reply_markup=task_menu(),parse_mode="HTML")
    elif d=="more":
        await c.message.edit_text("⚙️ <b>More</b>\n\nUse /help for commands.",reply_markup=main_menu(),parse_mode="HTML")
    elif d=="stop":
        db.execute("UPDATE users SET active=0 WHERE id=?",(uid,)); db.commit()
        await c.message.edit_text("🔴 <b>Work stopped.</b>",reply_markup=main_menu(),parse_mode="HTML")
    await c.answer()

async def help_cmd(m:Message):
    await m.answer("ℹ️ /start — start work\n/menu — menu\n/help — help\n\nAdmin dashboard: use the web panel configured in .env")

async def inactivity(bot):
    while True:
        cut=now()-OFFLINE_AFTER
        ids=db.execute("SELECT id FROM users WHERE active=1 AND last_action<?",(cut,)).fetchall()
        for r in ids:
            db.execute("UPDATE users SET active=0 WHERE id=?",(r["id"],))
            try: await bot.send_message(r["id"],"⏰ You were set offline after inactivity. Tap /start to receive new tasks.")
            except Exception: pass
        db.execute("UPDATE tasks SET status='expired' WHERE status IN ('open','claimed') AND expires_at<=?",(now(),)); db.commit()
        await asyncio.sleep(60)

async def main():
    init_db(); bot=Bot(TOKEN); dp=Dispatcher()
    dp.message.register(start,CommandStart()); dp.message.register(menu,Command("menu")); dp.message.register(admin_panel,Command("admin")); dp.message.register(help_cmd,Command("help")); dp.message.register(admin_text,F.text)
    dp.callback_query.register(callback,F.data)
    asyncio.create_task(inactivity(bot)); await dp.start_polling(bot)

if __name__=="__main__": asyncio.run(main())
