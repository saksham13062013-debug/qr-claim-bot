"""
FastAPI admin backend. Every /api/admin/* route requires a valid Telegram
WebApp initData string (verified server-side with BOT_TOKEN) belonging to a
Telegram ID that exists in the admins table. The bot token is never sent to
the frontend and never logged.
"""
import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import config
import database as db

app = FastAPI(title="QR Reward Bot Admin API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

INIT_DATA_MAX_AGE = 24 * 3600  # seconds


def verify_init_data(init_data: str) -> dict:
    """Validates Telegram WebApp initData per Telegram's documented HMAC scheme.
    Returns the parsed user dict on success, raises HTTPException otherwise."""
    if not init_data:
        raise HTTPException(status_code=401, detail="Missing initData")

    parsed = dict(parse_qsl(init_data, strict_parsing=True))
    received_hash = parsed.pop("hash", None)
    if not received_hash:
        raise HTTPException(status_code=401, detail="Invalid initData")

    auth_date = parsed.get("auth_date")
    if auth_date and time.time() - int(auth_date) > INIT_DATA_MAX_AGE:
        raise HTTPException(status_code=401, detail="initData expired")

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
    secret_key = hmac.new(b"WebAppData", config.BOT_TOKEN.encode(), hashlib.sha256).digest()
    computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(computed_hash, received_hash):
        raise HTTPException(status_code=401, detail="Invalid initData signature")

    user_json = parsed.get("user")
    if not user_json:
        raise HTTPException(status_code=401, detail="No user in initData")
    return json.loads(user_json)


def require_admin(x_telegram_init_data: str = Header(default="")) -> int:
    user = verify_init_data(x_telegram_init_data)
    telegram_id = user["id"]
    if not db.is_admin(telegram_id):
        raise HTTPException(status_code=403, detail="Not an admin")
    return telegram_id


def row_to_dict(row):
    return dict(row) if row else None


def rows_to_list(rows):
    return [dict(r) for r in rows]


# ---------------- Static dashboard ----------------

@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/")
def root():
    return FileResponse("dashboard/index.html")


@app.get("/admin")
def admin_page():
    return FileResponse("dashboard/index.html")


# ---------------- Overview ----------------

@app.get("/api/admin/overview")
def overview(admin_id: int = None, x_telegram_init_data: str = Header(default="")):
    require_admin(x_telegram_init_data)
    c = db.get_conn()
    def count(sql, *p):
        return c.execute(sql, p).fetchone()["n"]
    return {
        "total_users": count("SELECT COUNT(*) n FROM users"),
        "total_tasks": count("SELECT COUNT(*) n FROM tasks"),
        "available_tasks": count("SELECT COUNT(*) n FROM tasks WHERE status='AVAILABLE'"),
        "claimed_tasks": count("SELECT COUNT(*) n FROM tasks WHERE status='CLAIMED'"),
        "successful_orders": count("SELECT COUNT(*) n FROM orders WHERE status='SUCCESS'"),
        "failed_orders": count("SELECT COUNT(*) n FROM orders WHERE status='FAILED'"),
        "verification_pending": count("SELECT COUNT(*) n FROM orders WHERE status='VERIFICATION_PENDING'"),
        "total_rewards_paid": c.execute(
            "SELECT COALESCE(SUM(reward_usd),0) s FROM orders WHERE status='SUCCESS'"
        ).fetchone()["s"],
        "pending_withdrawals": count("SELECT COUNT(*) n FROM withdrawals WHERE status='PENDING'"),
        "processing_withdrawals": count("SELECT COUNT(*) n FROM withdrawals WHERE status='PROCESSING'"),
        "paid_withdrawals": count("SELECT COUNT(*) n FROM withdrawals WHERE status='PAID'"),
    }


# ---------------- Tasks (QR management) ----------------

class NewTask(BaseModel):
    app_name: str
    task_description: str = ""
    reward_usd: float
    file_id: str | None = None


@app.get("/api/admin/qrs")
def list_tasks(status: str | None = None, x_telegram_init_data: str = Header(default="")):
    require_admin(x_telegram_init_data)
    c = db.get_conn()
    if status:
        rows = c.execute("SELECT * FROM tasks WHERE status=? ORDER BY created_at DESC", (status,)).fetchall()
    else:
        rows = c.execute("SELECT * FROM tasks ORDER BY created_at DESC").fetchall()
    return rows_to_list(rows)


@app.post("/api/admin/qr")
def create_task(body: NewTask, x_telegram_init_data: str = Header(default="")):
    require_admin(x_telegram_init_data)
    task_id = db.create_task(body.file_id, body.app_name, body.task_description, body.reward_usd)
    db.log("task_created", f"task={task_id}")
    return {"task_id": task_id}


@app.post("/api/admin/qr/{task_id}/deactivate")
def deactivate_task(task_id: int, x_telegram_init_data: str = Header(default="")):
    require_admin(x_telegram_init_data)
    with db.tx() as c:
        c.execute("UPDATE tasks SET active=0 WHERE task_id=?", (task_id,))
    return {"ok": True}


class PostTaskBody(BaseModel):
    task_id: int


@app.post("/api/admin/post-qr")
async def post_task(body: PostTaskBody, x_telegram_init_data: str = Header(default="")):
    require_admin(x_telegram_init_data)
    # Imported lazily to avoid a circular import at module load time.
    import bot as bot_module
    application = app.state.bot_application
    await bot_module.post_task_to_groups(application, body.task_id)
    return {"ok": True}


# ---------------- Orders ----------------

@app.get("/api/admin/orders")
def list_orders(status: str | None = None, x_telegram_init_data: str = Header(default="")):
    require_admin(x_telegram_init_data)
    c = db.get_conn()
    sql = (
        "SELECT o.*, u.username, u.first_name, u.last_name FROM orders o "
        "JOIN users u ON o.telegram_id = u.telegram_id "
    )
    params = ()
    if status:
        sql += "WHERE o.status=? "
        params = (status,)
    sql += "ORDER BY o.claimed_at DESC"
    rows = c.execute(sql, params).fetchall()
    return rows_to_list(rows)


@app.get("/api/admin/confirmations")
def list_confirmations(x_telegram_init_data: str = Header(default="")):
    require_admin(x_telegram_init_data)
    c = db.get_conn()
    rows = c.execute(
        "SELECT o.*, u.username, u.first_name, u.last_name FROM orders o "
        "JOIN users u ON o.telegram_id = u.telegram_id "
        "WHERE o.status='VERIFICATION_PENDING' ORDER BY o.submitted_at ASC"
    ).fetchall()
    return rows_to_list(rows)


class OrderAction(BaseModel):
    order_id: int
    action: str  # approve | reject


@app.post("/api/admin/order-action")
async def order_action(body: OrderAction, x_telegram_init_data: str = Header(default="")):
    admin_id = require_admin(x_telegram_init_data)
    if body.action == "approve":
        ok, result = db.approve_order(body.order_id, admin_id)
    elif body.action == "reject":
        ok, result = db.reject_order(body.order_id, admin_id)
    else:
        raise HTTPException(status_code=400, detail="Invalid action")
    if not ok:
        raise HTTPException(status_code=400, detail=result)

    import bot as bot_module
    application = app.state.bot_application
    order = result
    task = db.get_task(order["task_id"])
    try:
        target_user = await application.bot.get_chat(order["telegram_id"])
        name = bot_module.full_name(target_user)
    except Exception:
        name = f"user {order['telegram_id']}"

    if body.action == "approve":
        try:
            await application.bot.send_message(
                order["telegram_id"],
                f"TASK SUCCESS\n\nApp: {order['app_name']}\nReward: ${order['reward_usd']:.2f} has been added to your balance.",
            )
        except Exception:
            pass
        await bot_module.update_group_messages(
            application, task,
            f"TASK SUCCESS\n\nApp: {task['app_name']}\nReward: ${task['reward_usd']:.2f}\nClaimed by: {name}",
        )
    else:
        try:
            await application.bot.send_message(
                order["telegram_id"],
                f"TASK FAILED\n\nApp: {order['app_name']}\nYour submission was not verified.",
            )
        except Exception:
            pass
        await bot_module.update_group_messages(
            application, task, f"TASK FAILED\n\nApp: {task['app_name']}\nClaimed by: {name}",
        )

    db.log(f"task_{body.action}d_via_dashboard", f"order={body.order_id}", admin_id)
    return {"ok": True}


# ---------------- Withdrawals ----------------

@app.get("/api/admin/withdrawals")
def list_withdrawals(status: str | None = None, x_telegram_init_data: str = Header(default="")):
    require_admin(x_telegram_init_data)
    c = db.get_conn()
    sql = (
        "SELECT w.*, u.username, u.first_name, u.last_name FROM withdrawals w "
        "JOIN users u ON w.telegram_id = u.telegram_id "
    )
    params = ()
    if status:
        sql += "WHERE w.status=? "
        params = (status,)
    sql += "ORDER BY w.created_at DESC"
    rows = c.execute(sql, params).fetchall()
    return rows_to_list(rows)


class WithdrawalAction(BaseModel):
    withdrawal_id: int
    action: str  # process | paid | reject
    note: str = ""


@app.post("/api/admin/withdrawal-action")
async def withdrawal_action(body: WithdrawalAction, x_telegram_init_data: str = Header(default="")):
    admin_id = require_admin(x_telegram_init_data)
    status_map = {"process": "PROCESSING", "paid": "PAID", "reject": "REJECTED"}
    status = status_map.get(body.action)
    if not status:
        raise HTTPException(status_code=400, detail="Invalid action")
    ok, result = db.set_withdrawal_status(body.withdrawal_id, status, admin_id, body.note)
    if not ok:
        raise HTTPException(status_code=400, detail=result)

    import bot as bot_module
    application = app.state.bot_application
    w = result
    notice = {
        "PROCESSING": "Your withdrawal is now being processed.",
        "PAID": "Your withdrawal has been paid.",
        "REJECTED": "Your withdrawal was rejected and the balance has been refunded to your wallet.",
    }[status]
    try:
        await application.bot.send_message(w["telegram_id"], f"WITHDRAWAL UPDATE\n\n{notice}")
    except Exception:
        pass

    db.log(f"withdrawal_{status.lower()}", f"id={body.withdrawal_id}", admin_id)
    return {"ok": True}


# ---------------- Users ----------------

@app.get("/api/admin/users")
def list_users(q: str | None = None, x_telegram_init_data: str = Header(default="")):
    require_admin(x_telegram_init_data)
    c = db.get_conn()
    if q:
        like = f"%{q}%"
        rows = c.execute(
            "SELECT * FROM users WHERE CAST(telegram_id AS TEXT) LIKE ? OR username LIKE ? OR first_name LIKE ? "
            "ORDER BY created_at DESC LIMIT 100",
            (like, like, like),
        ).fetchall()
    else:
        rows = c.execute("SELECT * FROM users ORDER BY created_at DESC LIMIT 100").fetchall()
    return rows_to_list(rows)


@app.get("/api/admin/users/{telegram_id}")
def user_detail(telegram_id: int, x_telegram_init_data: str = Header(default="")):
    require_admin(x_telegram_init_data)
    c = db.get_conn()
    user = c.execute("SELECT * FROM users WHERE telegram_id=?", (telegram_id,)).fetchone()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    orders = c.execute(
        "SELECT * FROM orders WHERE telegram_id=? ORDER BY claimed_at DESC LIMIT 50", (telegram_id,)
    ).fetchall()
    withdrawals = c.execute(
        "SELECT * FROM withdrawals WHERE telegram_id=? ORDER BY created_at DESC LIMIT 50", (telegram_id,)
    ).fetchall()
    return {"user": dict(user), "orders": rows_to_list(orders), "withdrawals": rows_to_list(withdrawals)}


class BalanceAdjust(BaseModel):
    telegram_id: int
    amount_usd: float
    note: str = ""


@app.post("/api/admin/add-balance")
def add_balance(body: BalanceAdjust, x_telegram_init_data: str = Header(default="")):
    admin_id = require_admin(x_telegram_init_data)
    ok, result = db.adjust_balance(body.telegram_id, abs(body.amount_usd), admin_id, body.note)
    if not ok:
        raise HTTPException(status_code=400, detail=result)
    db.log("balance_added", f"user={body.telegram_id} amt={body.amount_usd}", admin_id)
    return {"ok": True}


@app.post("/api/admin/deduct-balance")
def deduct_balance(body: BalanceAdjust, x_telegram_init_data: str = Header(default="")):
    admin_id = require_admin(x_telegram_init_data)
    ok, result = db.adjust_balance(body.telegram_id, -abs(body.amount_usd), admin_id, body.note)
    if not ok:
        raise HTTPException(status_code=400, detail=result)
    db.log("balance_deducted", f"user={body.telegram_id} amt={body.amount_usd}", admin_id)
    return {"ok": True}


class BanBody(BaseModel):
    telegram_id: int
    banned: bool


@app.post("/api/admin/ban")
def ban_user(body: BanBody, x_telegram_init_data: str = Header(default="")):
    admin_id = require_admin(x_telegram_init_data)
    db.set_ban(body.telegram_id, body.banned)
    db.log("user_banned" if body.banned else "user_unbanned", f"user={body.telegram_id}", admin_id)
    return {"ok": True}


# ---------------- Broadcast ----------------

class BroadcastBody(BaseModel):
    message: str


@app.post("/api/admin/broadcast")
async def broadcast(body: BroadcastBody, x_telegram_init_data: str = Header(default="")):
    admin_id = require_admin(x_telegram_init_data)
    application = app.state.bot_application
    c = db.get_conn()
    users = c.execute("SELECT telegram_id FROM users WHERE banned=0").fetchall()
    sent, failed = 0, 0
    for u in users:
        try:
            await application.bot.send_message(u["telegram_id"], body.message)
            sent += 1
        except Exception:
            failed += 1
    db.log("broadcast", f"sent={sent} failed={failed}", admin_id)
    return {"total_users": len(users), "sent": sent, "failed": failed}


# ---------------- Force join ----------------

@app.get("/api/admin/force-join")
def get_force_join(x_telegram_init_data: str = Header(default="")):
    require_admin(x_telegram_init_data)
    return rows_to_list(db.list_force_join_channels())


class ForceJoinBody(BaseModel):
    action: str  # add | remove | enable | disable
    channel_id: str
    invite_url: str = ""


@app.post("/api/admin/force-join")
def force_join_action(body: ForceJoinBody, x_telegram_init_data: str = Header(default="")):
    admin_id = require_admin(x_telegram_init_data)
    if body.action == "add":
        db.add_force_join_channel(body.channel_id, body.invite_url)
    elif body.action == "remove":
        db.remove_force_join_channel(body.channel_id)
    elif body.action == "enable":
        db.set_force_join_enabled(body.channel_id, True)
    elif body.action == "disable":
        db.set_force_join_enabled(body.channel_id, False)
    else:
        raise HTTPException(status_code=400, detail="Invalid action")
    db.log("force_join_changed", f"{body.action} {body.channel_id}", admin_id)
    return {"ok": True}


# ---------------- Admin management ----------------

@app.get("/api/admin/admins")
def get_admins(x_telegram_init_data: str = Header(default="")):
    require_admin(x_telegram_init_data)
    return rows_to_list(db.list_admins())


class AdminBody(BaseModel):
    telegram_id: int


@app.post("/api/admin/admins")
def add_admin_route(body: AdminBody, x_telegram_init_data: str = Header(default="")):
    admin_id = require_admin(x_telegram_init_data)
    db.add_admin(body.telegram_id)
    db.log("admin_added", f"{body.telegram_id}", admin_id)
    return {"ok": True}


@app.delete("/api/admin/admins/{telegram_id}")
def remove_admin_route(telegram_id: int, x_telegram_init_data: str = Header(default="")):
    admin_id = require_admin(x_telegram_init_data)
    ok, msg = db.remove_admin(telegram_id)
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    db.log("admin_removed", f"{telegram_id}", admin_id)
    return {"ok": True}


# ---------------- Settings ----------------

@app.get("/api/admin/settings")
def get_settings(x_telegram_init_data: str = Header(default="")):
    require_admin(x_telegram_init_data)
    return {
        "usd_to_inr": config.USD_TO_INR,
        "min_withdrawal_usd": config.MIN_WITHDRAWAL_USD,
        "force_join_enabled": config.FORCE_JOIN_ENABLED,
        "group_ids": config.GROUP_IDS,
        "support_username": config.SUPPORT_USERNAME,
        "webapp_url": config.WEBAPP_URL,
    }


# ---------------- Logs ----------------

@app.get("/api/admin/logs")
def get_logs(limit: int = 100, x_telegram_init_data: str = Header(default="")):
    require_admin(x_telegram_init_data)
    rows = db.get_conn().execute(
        "SELECT * FROM logs ORDER BY created_at DESC LIMIT ?", (min(limit, 500),)
    ).fetchall()
    return rows_to_list(rows)


app.mount("/static", StaticFiles(directory="dashboard"), name="static")
