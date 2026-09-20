#!/usr/bin/env python3
"""
QR Claim Bot - Railway Ready
Telegram bot with FastAPI admin dashboard
Runs both bot and web server concurrently
"""

import os
import sys
import json
import hmac
import hashlib
import logging
import asyncio
import threading
from datetime import datetime, timezone
from typing import Optional, List, Dict
from contextlib import asynccontextmanager

import aiosqlite
from dotenv import load_dotenv

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    WebAppInfo, User as TGUser, Bot
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes, MessageHandler, filters
)
from telegram.error import BadRequest, Forbidden
from telegram.constants import ChatAction

from fastapi import FastAPI, Request, HTTPException, Form
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

# Load environment variables
load_dotenv()

# Configuration
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://localhost:8000")
DB_PATH = os.getenv("DB_PATH", "/data/qr_claim_bot.db")
FORCE_JOIN_CHANNELS = [x.strip() for x in os.getenv("FORCE_JOIN_CHANNEL", "").split(",") if x.strip()]
FORCE_JOIN_URLS = [x.strip() for x in os.getenv("FORCE_JOIN_URL", "").split(",") if x.strip()]
FORCE_JOIN_STRICT = int(os.getenv("FORCE_JOIN_STRICT", "0"))
GROUP_IDS = [int(x.strip()) for x in os.getenv("GROUP_IDS", "").split(",") if x.strip()]
USD_TO_INR = float(os.getenv("USD_TO_INR", "85"))
MIN_WITHDRAWAL = float(os.getenv("MIN_WITHDRAWAL", "100"))
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "@support")

# Logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Global references
bot_app: Optional[Application] = None
fastapi_app: Optional[FastAPI] = None

# ============================================================================
# DATABASE
# ============================================================================

@asynccontextmanager
async def get_db():
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    try:
        yield db
    finally:
        await db.close()

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY,
            username TEXT, first_name TEXT, last_name TEXT,
            balance REAL DEFAULT 0, total_earned REAL DEFAULT 0,
            total_withdrawn REAL DEFAULT 0, total_orders INTEGER DEFAULT 0,
            is_banned INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")

        await db.execute("""CREATE TABLE IF NOT EXISTS qrs (
            qr_id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_id TEXT NOT NULL, app_name TEXT NOT NULL,
            reward REAL NOT NULL, status TEXT DEFAULT 'AVAILABLE',
            claimed_by INTEGER, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            claimed_at TIMESTAMP, completed_at TIMESTAMP,
            group_message_ids TEXT
        )""")

        await db.execute("""CREATE TABLE IF NOT EXISTS orders (
            order_id INTEGER PRIMARY KEY AUTOINCREMENT,
            qr_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
            app_name TEXT NOT NULL, reward REAL NOT NULL,
            status TEXT DEFAULT 'CLAIMED',
            claimed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            confirmed_at TIMESTAMP, reviewed_at TIMESTAMP, reviewed_by INTEGER
        )""")

        await db.execute("""CREATE TABLE IF NOT EXISTS withdrawals (
            withdrawal_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL, amount_inr REAL NOT NULL,
            amount_usd REAL NOT NULL, conversion_rate REAL NOT NULL,
            binance_id TEXT NOT NULL, status TEXT DEFAULT 'PENDING',
            requested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            processed_at TIMESTAMP, paid_at TIMESTAMP,
            processed_by INTEGER, paid_by INTEGER, admin_note TEXT
        )""")

        await db.execute("""CREATE TABLE IF NOT EXISTS rewards (
            reward_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL, order_id INTEGER NOT NULL,
            amount REAL NOT NULL, status TEXT DEFAULT 'APPROVED',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")

        await db.execute("""CREATE TABLE IF NOT EXISTS logs (
            log_id INTEGER PRIMARY KEY AUTOINCREMENT,
            log_type TEXT NOT NULL, message TEXT NOT NULL,
            user_id INTEGER, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")

        await db.execute("""INSERT OR IGNORE INTO settings (key, value) VALUES 
            ('usd_to_inr', ?), ('min_withdrawal', ?)""",
            (str(USD_TO_INR), str(MIN_WITHDRAWAL)))

        await db.commit()
        logger.info("Database initialized")

async def log_action(log_type: str, message: str, user_id: Optional[int] = None):
    try:
        async with get_db() as db:
            await db.execute("INSERT INTO logs (log_type, message, user_id) VALUES (?, ?, ?)",
                           (log_type, message, user_id))
            await db.commit()
    except Exception as e:
        logger.error(f"Log error: {e}")

# ============================================================================
# ANIMATION HELPERS
# ============================================================================

async def send_with_typing(bot: Bot, chat_id: int, text: str, **kwargs):
    try:
        await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        await asyncio.sleep(0.5)
    except: pass
    return await bot.send_message(chat_id=chat_id, text=text, **kwargs)

async def send_photo_with_upload(bot: Bot, chat_id: int, photo: str, **kwargs):
    try:
        await bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_PHOTO)
        await asyncio.sleep(1.0)
    except: pass
    return await bot.send_photo(chat_id=chat_id, photo=photo, **kwargs)

# ============================================================================
# USER MANAGEMENT
# ============================================================================

async def get_or_create_user(telegram_user: TGUser) -> Dict:
    async with get_db() as db:
        cursor = await db.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_user.id,))
        row = await cursor.fetchone()

        if row:
            await db.execute("UPDATE users SET username=?, first_name=?, last_name=? WHERE telegram_id=?",
                           (telegram_user.username, telegram_user.first_name, telegram_user.last_name, telegram_user.id))
            await db.commit()
            return dict(row)
        else:
            await db.execute("INSERT INTO users (telegram_id, username, first_name, last_name) VALUES (?,?,?,?)",
                           (telegram_user.id, telegram_user.username, telegram_user.first_name, telegram_user.last_name))
            await db.commit()
            await log_action("NEW_USER", f"New user: {telegram_user.first_name} [{telegram_user.id}]")
            cursor = await db.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_user.id,))
            return dict(await cursor.fetchone()) if await cursor.fetchone() else {}

async def get_user(telegram_id: int) -> Optional[Dict]:
    async with get_db() as db:
        cursor = await db.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None

# ============================================================================
# KEYBOARDS (NO EMOJIS IN BUTTONS)
# ============================================================================

def get_main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("WALLET", callback_data="menu_wallet"),
         InlineKeyboardButton("MY REWARDS", callback_data="menu_rewards")],
        [InlineKeyboardButton("WITHDRAW", callback_data="menu_withdraw"),
         InlineKeyboardButton("ORDER HISTORY", callback_data="menu_orders")],
        [InlineKeyboardButton("PROFILE", callback_data="menu_profile"),
         InlineKeyboardButton("HELP", callback_data="menu_help")]
    ])

def get_wallet_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("WITHDRAW", callback_data="wallet_withdraw")],
        [InlineKeyboardButton("BACK TO MENU", callback_data="back_menu")]
    ])

def get_rewards_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("WALLET", callback_data="menu_wallet"),
         InlineKeyboardButton("ORDER HISTORY", callback_data="menu_orders")],
        [InlineKeyboardButton("BACK TO MENU", callback_data="back_menu")]
    ])

def get_profile_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("ORDER HISTORY", callback_data="menu_orders")],
        [InlineKeyboardButton("BACK TO MENU", callback_data="back_menu")]
    ])

def get_order_history_keyboard(page: int = 0) -> InlineKeyboardMarkup:
    keyboard = []
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("PREVIOUS", callback_data=f"orders_page_{page-1}"))
    nav_row.append(InlineKeyboardButton("NEXT", callback_data=f"orders_page_{page+1}"))
    nav_row.append(InlineKeyboardButton("BACK TO MENU", callback_data="back_menu"))
    keyboard.append(nav_row)
    return InlineKeyboardMarkup(keyboard)

def get_claim_keyboard(qr_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("CLAIM QR", callback_data=f"claim_{qr_id}")]])

def get_confirm_keyboard(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("CONFIRM PAYMENT", callback_data=f"confirm_{order_id}")]])

def get_review_keyboard(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("APPROVE", callback_data=f"approve_{order_id}"),
         InlineKeyboardButton("REJECT", callback_data=f"reject_{order_id}")]
    ])

# ============================================================================
# BOT HANDLERS
# ============================================================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user: return

    db_user = await get_or_create_user(user)
    if db_user.get('is_banned'):
        await update.message.reply_text("You are banned.")
        return

    await context.bot.send_chat_action(chat_id=user.id, action=ChatAction.TYPING)
    await asyncio.sleep(0.5)
    await update.message.reply_text("Welcome to QR Claim Bot", reply_markup=get_main_menu_keyboard())

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or user.id not in ADMIN_IDS:
        await update.message.reply_text("Admin only.")
        return

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("OPEN ADMIN PANEL", web_app=WebAppInfo(url=WEBAPP_URL))
    ]])
    await update.message.reply_text("Admin Dashboard", reply_markup=keyboard)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"""QR Claim Bot Help

1. Click menu buttons
2. Watch for QRs in groups
3. Claim and confirm
4. Get rewards
5. Withdraw to Binance

Support: {SUPPORT_USERNAME}""")

async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user
    if not user: return

    db_user = await get_user(user.id)
    if not db_user: db_user = await get_or_create_user(user)

    data = query.data

    if data == "menu_wallet":
        text = f"""WALLET

Available: ${db_user['balance']:.2f}
Total Earned: ${db_user['total_earned']:.2f}
Total Withdrawn: ${db_user['total_withdrawn']:.2f}"""
        await query.edit_message_text(text, reply_markup=get_wallet_keyboard())

    elif data == "menu_rewards":
        async with get_db() as db:
            cursor = await db.execute(
                "SELECT status, COUNT(*), COALESCE(SUM(amount),0) FROM rewards WHERE user_id=? GROUP BY status",
                (user.id,))
            rows = await cursor.fetchall()
        stats = {r[0]: (r[1], r[2]) for r in rows}
        text = f"""MY REWARDS

Approved: {stats.get('APPROVED', (0,0))[0]} (${stats.get('APPROVED', (0,0))[1]:.2f})
Pending: {stats.get('PENDING', (0,0))[0]} (${stats.get('PENDING', (0,0))[1]:.2f})
Rejected: {stats.get('REJECTED', (0,0))[0]} (${stats.get('REJECTED', (0,0))[1]:.2f})"""
        await query.edit_message_text(text, reply_markup=get_rewards_keyboard())

    elif data == "menu_withdraw":
        await query.edit_message_text("Enter withdrawal amount in INR:",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("BACK", callback_data="back_menu")]]))
        context.user_data['awaiting_withdraw'] = True

    elif data == "menu_orders":
        async with get_db() as db:
            cursor = await db.execute("SELECT * FROM orders WHERE user_id=? ORDER BY claimed_at DESC LIMIT 10", (user.id,))
            orders = await cursor.fetchall()
        if not orders:
            text = "ORDER HISTORY\n\nNo orders yet."
        else:
            text = "ORDER HISTORY\n\n" + "\n".join([
                f"#{o['order_id']} | {o['app_name']} | ${o['reward']} | {o['status']}"
                for o in orders
            ])
        await query.edit_message_text(text, reply_markup=get_order_history_keyboard(0))

    elif data == "menu_profile":
        text = f"""PROFILE

Name: {db_user['first_name']} {db_user['last_name'] or ''}
Username: @{db_user['username'] or 'N/A'}
ID: {db_user['telegram_id']}

Balance: ${db_user['balance']:.2f}
Total Earned: ${db_user['total_earned']:.2f}
Total Orders: {db_user['total_orders']}"""
        await query.edit_message_text(text, reply_markup=get_profile_keyboard())

    elif data == "menu_help":
        await help_command(update, context)

    elif data == "back_menu":
        await query.edit_message_text("Welcome to QR Claim Bot", reply_markup=get_main_menu_keyboard())

    elif data.startswith("orders_page_"):
        page = int(data.split("_")[-1])
        async with get_db() as db:
            cursor = await db.execute("SELECT * FROM orders WHERE user_id=? ORDER BY claimed_at DESC LIMIT 10 OFFSET ?",
                                    (user.id, page*10))
            orders = await cursor.fetchall()
        text = "ORDER HISTORY\n\n" + "\n".join([f"#{o['order_id']} | {o['app_name']} | ${o['reward']} | {o['status']}" for o in orders]) if orders else "No more orders"
        await query.edit_message_text(text, reply_markup=get_order_history_keyboard(page))

    await query.answer()

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user: return

    if context.user_data.get('awaiting_withdraw'):
        try:
            amount_inr = float(update.message.text)
            if amount_inr < MIN_WITHDRAWAL:
                await update.message.reply_text(f"Minimum: ₹{MIN_WITHDRAWAL}")
                return
            db_user = await get_user(user.id)
            amount_usd = amount_inr / USD_TO_INR
            if not db_user or db_user['balance'] < amount_usd:
                await update.message.reply_text("Insufficient balance")
                return
            context.user_data['withdraw_inr'] = amount_inr
            context.user_data['awaiting_withdraw'] = False
            context.user_data['awaiting_binance'] = True
            await update.message.reply_text("Enter Binance ID:")
        except:
            await update.message.reply_text("Invalid amount")
        return

    if context.user_data.get('awaiting_binance'):
        binance_id = update.message.text.strip()
        amount_inr = context.user_data.get('withdraw_inr', 0)
        amount_usd = amount_inr / USD_TO_INR

        context.user_data.clear()

        async with get_db() as db:
            await db.execute("UPDATE users SET balance=balance-? WHERE telegram_id=?", (amount_usd, user.id))
            cursor = await db.execute(
                "INSERT INTO withdrawals (user_id, amount_inr, amount_usd, conversion_rate, binance_id) VALUES (?,?,?,?,?) RETURNING *",
                (user.id, amount_inr, amount_usd, USD_TO_INR, binance_id))
            wd = await cursor.fetchone()
            await db.commit()

        await send_with_typing(context.bot, user.id,
            f"Withdrawal Requested\n\n₹{amount_inr} (${amount_usd:.2f})\nBinance: {binance_id}\nStatus: PENDING")

        for admin_id in ADMIN_IDS:
            try:
                await context.bot.send_message(admin_id,
                    f"NEW WITHDRAWAL\n\nID: #{wd['withdrawal_id']}\nUser: {user.first_name}\n₹{amount_inr} (${amount_usd:.2f})\nBinance: {binance_id}")
            except: pass

        await log_action("WITHDRAWAL", f"#{wd['withdrawal_id']} by {user.id}")
        return

async def claim_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user
    if not user: return

    qr_id = int(query.data.split("_")[1])

    async with get_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute("SELECT * FROM qrs WHERE qr_id=? AND status='AVAILABLE' FOR UPDATE", (qr_id,))
        qr = await cursor.fetchone()
        if not qr:
            await db.execute("COMMIT")
            await query.answer("Already claimed!", show_alert=True)
            return

        qr = dict(qr)
        await db.execute("UPDATE qrs SET status='CLAIMED', claimed_by=?, claimed_at=CURRENT_TIMESTAMP WHERE qr_id=?", (user.id, qr_id))
        cursor = await db.execute(
            "INSERT INTO orders (qr_id, user_id, app_name, reward) VALUES (?,?,?,?) RETURNING order_id",
            (qr_id, user.id, qr['app_name'], qr['reward']))
        order_id = (await cursor.fetchone())[0]
        await db.execute("UPDATE users SET total_orders=total_orders+1 WHERE telegram_id=?", (user.id,))
        await db.commit()

    await query.answer("Claimed! Check DM")
    await send_photo_with_upload(context.bot, user.id, qr['file_id'],
        caption=f"QR CLAIMED\n\nApp: {qr['app_name']}\nReward: ${qr['reward']}\n\nComplete the action.",
        reply_markup=get_confirm_keyboard(order_id))
    await query.edit_message_text("QR claimed! Check messages.", reply_markup=get_main_menu_keyboard())
    await log_action("CLAIM", f"QR {qr_id} by {user.id}")

async def confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user
    if not user: return

    order_id = int(query.data.split("_")[1])

    async with get_db() as db:
        cursor = await db.execute("SELECT * FROM orders WHERE order_id=?", (order_id,))
        order = await cursor.fetchone()
        if not order or order['user_id'] != user.id or order['status'] != 'CLAIMED':
            await query.answer("Invalid!", show_alert=True)
            return

        await db.execute("UPDATE orders SET status='CONFIRMATION PENDING', confirmed_at=CURRENT_TIMESTAMP WHERE order_id=?", (order_id,))
        await db.commit()

    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(admin_id,
                f"CONFIRMATION REVIEW\n\nOrder: #{order_id}\nUser: {user.first_name} ({user.id})\nApp: {order['app_name']}\nReward: ${order['reward']}",
                reply_markup=get_review_keyboard(order_id))
        except: pass

    await query.edit_message_text(f"Confirmation submitted\n\nOrder: #{order_id}\nStatus: PENDING",
        reply_markup=get_main_menu_keyboard())
    await log_action("CONFIRM", f"Order {order_id} by {user.id}")

async def review_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user
    if not user or user.id not in ADMIN_IDS:
        await query.answer("Admin only!", show_alert=True)
        return

    data = query.data
    parts = data.split("_")
    action, order_id = parts[1], int(parts[2])

    async with get_db() as db:
        cursor = await db.execute("SELECT * FROM orders WHERE order_id=?", (order_id,))
        order = await cursor.fetchone()
        if not order: return
        order = dict(order)

        if action == "approve":
            await db.execute("UPDATE orders SET status='SUCCESS', reviewed_at=CURRENT_TIMESTAMP, reviewed_by=? WHERE order_id=?", (user.id, order_id))
            await db.execute("UPDATE qrs SET status='COMPLETED' WHERE qr_id=?", (order['qr_id'],))
            await db.execute("UPDATE users SET balance=balance+?, total_earned=total_earned+? WHERE telegram_id=?",
                           (order['reward'], order['reward'], order['user_id']))
            await db.execute("INSERT INTO rewards (user_id, order_id, amount) VALUES (?,?,?)", (order['user_id'], order_id, order['reward']))
            await db.commit()

            try:
                await send_with_typing(context.bot, order['user_id'],
                    f"APPROVED!\n\nOrder: #{order_id}\n${order['reward']} added to balance!")
            except: pass
            await query.edit_message_text(f"Order #{order_id} approved!")
            await log_action("APPROVE", f"Order {order_id} by {user.id}")

        elif action == "reject":
            await db.execute("UPDATE orders SET status='FAILED', reviewed_at=CURRENT_TIMESTAMP, reviewed_by=? WHERE order_id=?", (user.id, order_id))
            await db.execute("UPDATE qrs SET status='FAILED' WHERE qr_id=?", (order['qr_id'],))
            await db.commit()

            try:
                await send_with_typing(context.bot, order['user_id'], f"REJECTED\n\nOrder: #{order_id}")
            except: pass
            await query.edit_message_text(f"Order #{order_id} rejected!")
            await log_action("REJECT", f"Order {order_id} by {user.id}")

    await query.answer()

async def withdrawal_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user
    if not user or user.id not in ADMIN_IDS:
        await query.answer("Admin only!", show_alert=True)
        return

    data = query.data
    parts = data.split("_")
    action, wd_id = parts[1], int(parts[2])

    async with get_db() as db:
        cursor = await db.execute("SELECT * FROM withdrawals WHERE withdrawal_id=?", (wd_id,))
        wd = await cursor.fetchone()
        if not wd: return
        wd = dict(wd)

        if action == "process":
            await db.execute("UPDATE withdrawals SET status='PROCESSING', processed_at=CURRENT_TIMESTAMP, processed_by=? WHERE withdrawal_id=?", (user.id, wd_id))
        elif action == "paid":
            await db.execute("UPDATE withdrawals SET status='PAID', paid_at=CURRENT_TIMESTAMP, paid_by=? WHERE withdrawal_id=?", (user.id, wd_id))
            await db.execute("UPDATE users SET total_withdrawn=total_withdrawn+? WHERE telegram_id=?", (wd['amount_usd'], wd['user_id']))
        elif action == "reject":
            await db.execute("UPDATE withdrawals SET status='REJECTED' WHERE withdrawal_id=?", (wd_id,))
            await db.execute("UPDATE users SET balance=balance+? WHERE telegram_id=?", (wd['amount_usd'], wd['user_id']))
        await db.commit()

    await query.edit_message_text(f"Withdrawal #{wd_id} {action}ed!")
    await log_action(f"WD_{action.upper()}", f"#{wd_id} by {user.id}")

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Error: {context.error}")

# ============================================================================
# FASTAPI SERVER
# ============================================================================

def create_fastapi():
    app = FastAPI()
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

    try:
        app.mount("/static", StaticFiles(directory="dashboard/static"), name="static")
    except: pass

    def verify_auth(init_data: str) -> Optional[Dict]:
        if not init_data or not BOT_TOKEN: return None
        try:
            data = dict(item.split("=", 1) for item in init_data.split("&") if "=" in item)
            if "hash" not in data: return None
            received_hash = data.pop("hash")
            data_check = "\n".join(f"{k}={data[k]}" for k in sorted(data.keys()))
            secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
            calc_hash = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
            if calc_hash != received_hash: return None
            return json.loads(data["user"]) if "user" in data else None
        except: return None

    @app.get("/")
    @app.get("/admin")
    async def root():
        return FileResponse("dashboard/index.html")

    @app.get("/health")
    async def health():
        return {"status": "healthy"}

    @app.get("/api/admin/overview")
    async def overview():
        async with get_db() as db:
            cursor = await db.execute("SELECT COUNT(*) FROM users")
            total_users = (await cursor.fetchone())[0]
            cursor = await db.execute("SELECT status, COUNT(*) FROM qrs GROUP BY status")
            qr_stats = {r[0]: r[1] for r in await cursor.fetchall()}
            cursor = await db.execute("SELECT status, COUNT(*) FROM orders GROUP BY status")
            order_stats = {r[0]: r[1] for r in await cursor.fetchall()}
            cursor = await db.execute("SELECT COALESCE(SUM(amount),0) FROM rewards WHERE status='APPROVED'")
            total_rewards = (await cursor.fetchone())[0] or 0
            cursor = await db.execute("SELECT status, COUNT(*) FROM withdrawals GROUP BY status")
            wd_stats = {r[0]: r[1] for r in await cursor.fetchall()}
        return {
            "total_users": total_users,
            "total_qrs": sum(qr_stats.values()),
            "available_qrs": qr_stats.get("AVAILABLE", 0),
            "successful_orders": order_stats.get("SUCCESS", 0),
            "total_rewards_paid": total_rewards,
            "pending_withdrawals": wd_stats.get("PENDING", 0)
        }

    @app.get("/api/admin/qrs")
    async def get_qrs():
        async with get_db() as db:
            cursor = await db.execute("SELECT q.*, u.first_name FROM qrs q LEFT JOIN users u ON q.claimed_by=u.telegram_id ORDER BY q.created_at DESC")
            return [dict(r) for r in await cursor.fetchall()]

    @app.get("/api/admin/orders")
    async def get_orders():
        async with get_db() as db:
            cursor = await db.execute("SELECT o.*, u.first_name FROM orders o LEFT JOIN users u ON o.user_id=u.telegram_id ORDER BY o.claimed_at DESC")
            return [dict(r) for r in await cursor.fetchall()]

    @app.get("/api/admin/confirmations")
    async def get_confirmations():
        async with get_db() as db:
            cursor = await db.execute("SELECT o.*, u.first_name FROM orders o LEFT JOIN users u ON o.user_id=u.telegram_id WHERE o.status='CONFIRMATION PENDING'")
            return [dict(r) for r in await cursor.fetchall()]

    @app.get("/api/admin/withdrawals")
    async def get_withdrawals():
        async with get_db() as db:
            cursor = await db.execute("SELECT w.*, u.first_name FROM withdrawals w LEFT JOIN users u ON w.user_id=u.telegram_id ORDER BY w.requested_at DESC")
            return [dict(r) for r in await cursor.fetchall()]

    @app.get("/api/admin/users")
    async def get_users():
        async with get_db() as db:
            cursor = await db.execute("SELECT * FROM users ORDER BY created_at DESC")
            return [dict(r) for r in await cursor.fetchall()]

    @app.get("/api/admin/logs")
    async def get_logs():
        async with get_db() as db:
            cursor = await db.execute("SELECT * FROM logs ORDER BY created_at DESC LIMIT 100")
            return [dict(r) for r in await cursor.fetchall()]

    @app.post("/api/admin/order-action")
    async def order_action(order_id: int = Form(...), action: str = Form(...)):
        async with get_db() as db:
            if action == "approve":
                cursor = await db.execute("SELECT * FROM orders WHERE order_id=?", (order_id,))
                order = await cursor.fetchone()
                if order:
                    await db.execute("UPDATE orders SET status='SUCCESS', reviewed_at=CURRENT_TIMESTAMP WHERE order_id=?", (order_id,))
                    await db.execute("UPDATE qrs SET status='COMPLETED' WHERE qr_id=?", (order['qr_id'],))
                    await db.execute("UPDATE users SET balance=balance+?, total_earned=total_earned+? WHERE telegram_id=?",
                                   (order['reward'], order['reward'], order['user_id']))
                    await db.execute("INSERT INTO rewards (user_id, order_id, amount) VALUES (?,?,?)",
                                   (order['user_id'], order_id, order['reward']))
                    await db.commit()
        return {"success": True}

    @app.post("/api/admin/withdrawal-action")
    async def withdrawal_action(withdrawal_id: int = Form(...), action: str = Form(...)):
        async with get_db() as db:
            cursor = await db.execute("SELECT * FROM withdrawals WHERE withdrawal_id=?", (withdrawal_id,))
            wd = await cursor.fetchone()
            if wd:
                if action == "paid":
                    await db.execute("UPDATE withdrawals SET status='PAID', paid_at=CURRENT_TIMESTAMP WHERE withdrawal_id=?", (withdrawal_id,))
                    await db.execute("UPDATE users SET total_withdrawn=total_withdrawn+? WHERE telegram_id=?", (wd['amount_usd'], wd['user_id']))
                elif action == "reject":
                    await db.execute("UPDATE withdrawals SET status='REJECTED' WHERE withdrawal_id=?", (withdrawal_id,))
                    await db.execute("UPDATE users SET balance=balance+? WHERE telegram_id=?", (wd['amount_usd'], wd['user_id']))
                await db.commit()
        return {"success": True}

    @app.post("/api/admin/ban-user")
    async def ban_user(user_id: int = Form(...)):
        async with get_db() as db:
            await db.execute("UPDATE users SET is_banned=1 WHERE telegram_id=?", (user_id,))
            await db.commit()
        return {"success": True}

    @app.post("/api/admin/add-balance")
    async def add_balance(user_id: int = Form(...), amount: float = Form(...)):
        async with get_db() as db:
            await db.execute("UPDATE users SET balance=balance+?, total_earned=total_earned+? WHERE telegram_id=?", (amount, amount, user_id))
            await db.commit()
        return {"success": True}

    return app

def run_fastapi(port: int):
    global fastapi_app
    fastapi_app = create_fastapi()
    uvicorn.run(fastapi_app, host="0.0.0.0", port=port, log_level="info")

# ============================================================================
# MAIN
# ============================================================================

def run_bot():
    global bot_app
    application = Application.builder().token(BOT_TOKEN).build()
    bot_app = application

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CallbackQueryHandler(menu_callback, pattern="^menu_"))
    application.add_handler(CallbackQueryHandler(claim_callback, pattern="^claim_"))
    application.add_handler(CallbackQueryHandler(confirm_callback, pattern="^confirm_"))
    application.add_handler(CallbackQueryHandler(review_callback, pattern="^(approve|reject)_"))
    application.add_handler(CallbackQueryHandler(withdrawal_callback, pattern="^wd_(process|paid|reject)_"))
    application.add_handler(CallbackQueryHandler(lambda u, c: u.data == "wallet_withdraw", pattern="^wallet_"))
    application.add_handler(CallbackQueryHandler(lambda u, c: u.data == "back_menu", pattern="^back_menu$"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_error_handler(error_handler)

    logger.info("Bot starting...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

def main():
    asyncio.run(init_db())

    port = int(os.getenv("PORT", "8000"))

    server_thread = threading.Thread(target=run_fastapi, args=(port,), daemon=True)
    server_thread.start()
    logger.info(f"FastAPI on port {port}")

    run_bot()

if __name__ == "__main__":
    main()
