"""
SQLite data layer. All money-moving operations (claim, complete, approve, reject,
withdrawal state changes) run inside a single immediate-mode transaction so two
concurrent requests can never both succeed against the same row.
"""
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime

import config

_local = threading.local()
_write_lock = threading.Lock()  # extra belt-and-suspenders around SQLite's own locking


def _connect():
    conn = sqlite3.connect(config.DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def get_conn():
    if not hasattr(_local, "conn"):
        _local.conn = _connect()
    return _local.conn


@contextmanager
def tx():
    """Exclusive write transaction. Use for anything that mutates balances/status."""
    conn = get_conn()
    with _write_lock:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def now():
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    telegram_id INTEGER PRIMARY KEY,
    username TEXT,
    first_name TEXT,
    last_name TEXT,
    balance_usd REAL NOT NULL DEFAULT 0,
    total_earned_usd REAL NOT NULL DEFAULT 0,
    total_withdrawn_usd REAL NOT NULL DEFAULT 0,
    pending_withdrawal_usd REAL NOT NULL DEFAULT 0,
    banned INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    task_id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id TEXT,
    app_name TEXT NOT NULL,
    task_description TEXT NOT NULL DEFAULT '',
    reward_usd REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'AVAILABLE',  -- AVAILABLE, CLAIMED, COMPLETED, VERIFIED, REJECTED
    claimed_by INTEGER,
    created_at TEXT NOT NULL,
    claimed_at TEXT,
    group_message_ids TEXT,  -- JSON list of {chat_id, message_id}
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS orders (
    order_id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    telegram_id INTEGER NOT NULL,
    app_name TEXT NOT NULL,
    reward_usd REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'CLAIMED',  -- CLAIMED, VERIFICATION_PENDING, SUCCESS, FAILED
    claimed_at TEXT NOT NULL,
    submitted_at TEXT,
    reviewed_at TEXT,
    reviewed_by INTEGER,
    FOREIGN KEY (task_id) REFERENCES tasks(task_id),
    FOREIGN KEY (telegram_id) REFERENCES users(telegram_id)
);

CREATE TABLE IF NOT EXISTS withdrawals (
    withdrawal_id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER NOT NULL,
    amount_inr REAL NOT NULL,
    amount_usd REAL NOT NULL,
    conversion_rate REAL NOT NULL,
    binance_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING',  -- PENDING, PROCESSING, PAID, REJECTED
    created_at TEXT NOT NULL,
    processed_at TEXT,
    processing_admin INTEGER,
    payment_admin INTEGER,
    admin_note TEXT,
    FOREIGN KEY (telegram_id) REFERENCES users(telegram_id)
);

CREATE TABLE IF NOT EXISTS admins (
    telegram_id INTEGER PRIMARY KEY,
    added_at TEXT NOT NULL,
    is_owner INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS force_join_channels (
    channel_id TEXT PRIMARY KEY,  -- @username or -100... chat id
    invite_url TEXT,
    enabled INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS logs (
    log_id INTEGER PRIMARY KEY AUTOINCREMENT,
    event TEXT NOT NULL,
    detail TEXT,
    actor_id INTEGER,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def init_db():
    conn = get_conn()
    conn.executescript(SCHEMA)
    conn.commit()
    # seed admins from config on first boot without ever removing existing ones
    with tx() as c:
        for i, admin_id in enumerate(config.ADMIN_IDS):
            c.execute(
                "INSERT OR IGNORE INTO admins (telegram_id, added_at, is_owner) VALUES (?, ?, ?)",
                (admin_id, now(), 1 if i == 0 else 0),
            )
        # seed force-join channels from env on first boot only; never overwrite rows an admin has since edited
        for i, channel in enumerate(config.FORCE_JOIN_CHANNELS):
            invite_url = config.FORCE_JOIN_URLS[i] if i < len(config.FORCE_JOIN_URLS) else ""
            c.execute(
                "INSERT OR IGNORE INTO force_join_channels (channel_id, invite_url, enabled) VALUES (?, ?, 1)",
                (channel, invite_url),
            )


def log(event: str, detail: str = "", actor_id: int = None):
    with tx() as c:
        c.execute(
            "INSERT INTO logs (event, detail, actor_id, created_at) VALUES (?, ?, ?, ?)",
            (event, detail, actor_id, now()),
        )


# ---------------- Users ----------------

def upsert_user(telegram_id, username, first_name, last_name):
    conn = get_conn()
    row = conn.execute("SELECT telegram_id FROM users WHERE telegram_id=?", (telegram_id,)).fetchone()
    is_new = row is None
    with tx() as c:
        if is_new:
            c.execute(
                "INSERT INTO users (telegram_id, username, first_name, last_name, created_at, last_seen_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (telegram_id, username, first_name, last_name, now(), now()),
            )
        else:
            c.execute(
                "UPDATE users SET username=?, first_name=?, last_name=?, last_seen_at=? WHERE telegram_id=?",
                (username, first_name, last_name, now(), telegram_id),
            )
    return is_new


def get_user(telegram_id):
    return get_conn().execute("SELECT * FROM users WHERE telegram_id=?", (telegram_id,)).fetchone()


def is_banned(telegram_id):
    row = get_user(telegram_id)
    return bool(row and row["banned"])


# ---------------- Admins ----------------

def is_admin(telegram_id):
    row = get_conn().execute("SELECT 1 FROM admins WHERE telegram_id=?", (telegram_id,)).fetchone()
    return row is not None


def list_admins():
    return get_conn().execute("SELECT * FROM admins ORDER BY added_at").fetchall()


def add_admin(telegram_id):
    with tx() as c:
        c.execute(
            "INSERT OR IGNORE INTO admins (telegram_id, added_at, is_owner) VALUES (?, ?, 0)",
            (telegram_id, now()),
        )


def remove_admin(telegram_id):
    row = get_conn().execute("SELECT is_owner FROM admins WHERE telegram_id=?", (telegram_id,)).fetchone()
    if not row:
        return False, "Admin not found"
    count = get_conn().execute("SELECT COUNT(*) c FROM admins").fetchone()["c"]
    if count <= 1:
        return False, "Cannot remove the last remaining admin"
    with tx() as c:
        c.execute("DELETE FROM admins WHERE telegram_id=?", (telegram_id,))
    return True, "Removed"


# ---------------- Tasks (QR reward tasks) ----------------

def create_task(file_id, app_name, task_description, reward_usd):
    with tx() as c:
        cur = c.execute(
            "INSERT INTO tasks (file_id, app_name, task_description, reward_usd, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (file_id, app_name, task_description, reward_usd, now()),
        )
        return cur.lastrowid


def get_task(task_id):
    return get_conn().execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()


def set_task_group_messages(task_id, group_message_ids_json):
    with tx() as c:
        c.execute("UPDATE tasks SET group_message_ids=? WHERE task_id=?", (group_message_ids_json, task_id))


def claim_task(task_id: int, telegram_id: int):
    """
    Atomically reserve a task for a user. Returns (order_id, task_row) on success,
    or (None, reason_string) on failure. The UPDATE...WHERE status='AVAILABLE' clause
    is what makes double-claims impossible under concurrent requests.
    """
    with tx() as c:
        cur = c.execute(
            "UPDATE tasks SET status='CLAIMED', claimed_by=?, claimed_at=? "
            "WHERE task_id=? AND status='AVAILABLE' AND active=1",
            (telegram_id, now(), task_id),
        )
        if cur.rowcount == 0:
            return None, "This task is no longer available."
        task = c.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        order_cur = c.execute(
            "INSERT INTO orders (task_id, telegram_id, app_name, reward_usd, status, claimed_at) "
            "VALUES (?, ?, ?, ?, 'CLAIMED', ?)",
            (task_id, telegram_id, task["app_name"], task["reward_usd"], now()),
        )
        return order_cur.lastrowid, dict(task)


def submit_completion(order_id: int, telegram_id: int):
    """User marks their claimed task as done -> goes to VERIFICATION_PENDING."""
    with tx() as c:
        order = c.execute("SELECT * FROM orders WHERE order_id=?", (order_id,)).fetchone()
        if not order or order["telegram_id"] != telegram_id:
            return False, "Order not found."
        if order["status"] != "CLAIMED":
            return False, "This order has already been submitted or reviewed."
        c.execute(
            "UPDATE orders SET status='VERIFICATION_PENDING', submitted_at=? WHERE order_id=?",
            (now(), order_id),
        )
        c.execute("UPDATE tasks SET status='COMPLETED' WHERE task_id=?", (order["task_id"],))
        return True, dict(order)


def approve_order(order_id: int, admin_id: int):
    with tx() as c:
        order = c.execute("SELECT * FROM orders WHERE order_id=?", (order_id,)).fetchone()
        if not order:
            return False, "Order not found."
        if order["status"] != "VERIFICATION_PENDING":
            return False, "Order is not pending verification."
        c.execute(
            "UPDATE orders SET status='SUCCESS', reviewed_at=?, reviewed_by=? WHERE order_id=?",
            (now(), admin_id, order_id),
        )
        c.execute("UPDATE tasks SET status='VERIFIED' WHERE task_id=?", (order["task_id"],))
        c.execute(
            "UPDATE users SET balance_usd = balance_usd + ?, total_earned_usd = total_earned_usd + ? "
            "WHERE telegram_id=?",
            (order["reward_usd"], order["reward_usd"], order["telegram_id"]),
        )
        return True, dict(order)


def reject_order(order_id: int, admin_id: int, note: str = ""):
    with tx() as c:
        order = c.execute("SELECT * FROM orders WHERE order_id=?", (order_id,)).fetchone()
        if not order:
            return False, "Order not found."
        if order["status"] != "VERIFICATION_PENDING":
            return False, "Order is not pending verification."
        c.execute(
            "UPDATE orders SET status='FAILED', reviewed_at=?, reviewed_by=? WHERE order_id=?",
            (now(), admin_id, order_id),
        )
        c.execute("UPDATE tasks SET status='REJECTED' WHERE task_id=?", (order["task_id"],))
        return True, dict(order)


def user_orders(telegram_id, limit=5, offset=0):
    rows = get_conn().execute(
        "SELECT o.*, t.file_id FROM orders o JOIN tasks t ON o.task_id=t.task_id "
        "WHERE o.telegram_id=? ORDER BY o.claimed_at DESC LIMIT ? OFFSET ?",
        (telegram_id, limit, offset),
    ).fetchall()
    total = get_conn().execute("SELECT COUNT(*) c FROM orders WHERE telegram_id=?", (telegram_id,)).fetchone()["c"]
    return rows, total


def user_rewards_summary(telegram_id):
    approved = get_conn().execute(
        "SELECT COUNT(*) c, COALESCE(SUM(reward_usd),0) s FROM orders WHERE telegram_id=? AND status='SUCCESS'",
        (telegram_id,),
    ).fetchone()
    pending = get_conn().execute(
        "SELECT COUNT(*) c FROM orders WHERE telegram_id=? AND status IN ('CLAIMED','VERIFICATION_PENDING')",
        (telegram_id,),
    ).fetchone()
    rejected = get_conn().execute(
        "SELECT COUNT(*) c FROM orders WHERE telegram_id=? AND status='FAILED'", (telegram_id,)
    ).fetchone()
    recent = get_conn().execute(
        "SELECT * FROM orders WHERE telegram_id=? AND status='SUCCESS' ORDER BY reviewed_at DESC LIMIT 5",
        (telegram_id,),
    ).fetchall()
    return approved, pending, rejected, recent


# ---------------- Withdrawals ----------------

def request_withdrawal(telegram_id: int, amount_inr: float, binance_id: str):
    rate = config.USD_TO_INR
    amount_usd = round(amount_inr / rate, 4)
    with tx() as c:
        user = c.execute("SELECT * FROM users WHERE telegram_id=?", (telegram_id,)).fetchone()
        if not user:
            return None, "User not found."
        available = user["balance_usd"]
        if amount_usd < config.MIN_WITHDRAWAL_USD:
            return None, f"Minimum withdrawal is ${config.MIN_WITHDRAWAL_USD:.2f}."
        if amount_usd > available:
            return None, "Insufficient balance."
        c.execute(
            "UPDATE users SET balance_usd = balance_usd - ?, pending_withdrawal_usd = pending_withdrawal_usd + ? "
            "WHERE telegram_id=?",
            (amount_usd, amount_usd, telegram_id),
        )
        cur = c.execute(
            "INSERT INTO withdrawals (telegram_id, amount_inr, amount_usd, conversion_rate, binance_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (telegram_id, amount_inr, amount_usd, rate, binance_id, now()),
        )
        return cur.lastrowid, None


def set_withdrawal_status(withdrawal_id: int, status: str, admin_id: int, note: str = ""):
    with tx() as c:
        w = c.execute("SELECT * FROM withdrawals WHERE withdrawal_id=?", (withdrawal_id,)).fetchone()
        if not w:
            return False, "Withdrawal not found."
        if w["status"] in ("PAID", "REJECTED"):
            return False, "Withdrawal already finalized."

        fields = ["status=?", "admin_note=?"]
        params = [status, note]

        if status == "PROCESSING":
            fields.append("processing_admin=?")
            params.append(admin_id)
        elif status == "PAID":
            fields.append("payment_admin=?")
            params.append(admin_id)
            fields.append("processed_at=?")
            params.append(now())
            c.execute(
                "UPDATE users SET pending_withdrawal_usd = pending_withdrawal_usd - ?, "
                "total_withdrawn_usd = total_withdrawn_usd + ? WHERE telegram_id=?",
                (w["amount_usd"], w["amount_usd"], w["telegram_id"]),
            )
        elif status == "REJECTED":
            fields.append("processed_at=?")
            params.append(now())
            # refund reserved balance
            c.execute(
                "UPDATE users SET pending_withdrawal_usd = pending_withdrawal_usd - ?, balance_usd = balance_usd + ? "
                "WHERE telegram_id=?",
                (w["amount_usd"], w["amount_usd"], w["telegram_id"]),
            )

        params.append(withdrawal_id)
        c.execute(f"UPDATE withdrawals SET {', '.join(fields)} WHERE withdrawal_id=?", params)
        return True, dict(w)


# ---------------- Admin balance adjustments ----------------

def adjust_balance(telegram_id: int, delta_usd: float, admin_id: int, note: str = ""):
    with tx() as c:
        user = c.execute("SELECT * FROM users WHERE telegram_id=?", (telegram_id,)).fetchone()
        if not user:
            return False, "User not found."
        if delta_usd < 0 and user["balance_usd"] + delta_usd < 0:
            return False, "Cannot deduct more than the user's balance."
        c.execute("UPDATE users SET balance_usd = balance_usd + ? WHERE telegram_id=?", (delta_usd, telegram_id))
        if delta_usd > 0:
            c.execute(
                "UPDATE users SET total_earned_usd = total_earned_usd + ? WHERE telegram_id=?",
                (delta_usd, telegram_id),
            )
        return True, "OK"


def set_ban(telegram_id: int, banned: bool):
    with tx() as c:
        c.execute("UPDATE users SET banned=? WHERE telegram_id=?", (1 if banned else 0, telegram_id))


# ---------------- Force join ----------------

def list_force_join_channels():
    return get_conn().execute("SELECT * FROM force_join_channels ORDER BY channel_id").fetchall()


def add_force_join_channel(channel_id: str, invite_url: str = ""):
    with tx() as c:
        c.execute(
            "INSERT OR REPLACE INTO force_join_channels (channel_id, invite_url, enabled) VALUES (?, ?, 1)",
            (channel_id, invite_url),
        )


def remove_force_join_channel(channel_id: str):
    with tx() as c:
        c.execute("DELETE FROM force_join_channels WHERE channel_id=?", (channel_id,))


def set_force_join_enabled(channel_id: str, enabled: bool):
    with tx() as c:
        c.execute("UPDATE force_join_channels SET enabled=? WHERE channel_id=?", (1 if enabled else 0, channel_id))


# ---------------- Settings ----------------

def get_setting(key, default=None):
    row = get_conn().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key, value):
    with tx() as c:
        c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))
