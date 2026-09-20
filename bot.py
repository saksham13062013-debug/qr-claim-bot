"""
QR Reward Bot — Telegram bot.

Flow: admin posts a task (QR) to groups -> first eligible user claims it in DM ->
user completes the real advertiser/app task -> user marks it complete -> admin
verifies completion -> reward is credited to the user's balance -> user withdraws
via Binance UID. No payment ever flows from the user to the bot or an admin.
"""
import json
import logging
from datetime import datetime

from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, WebAppInfo,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    ConversationHandler, ContextTypes, filters,
)
from telegram.error import TelegramError

import config
import database as db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("qr_reward_bot")

# ---- Reply keyboard (main menu) ----
MENU = ReplyKeyboardMarkup(
    [
        [KeyboardButton("WALLET"), KeyboardButton("MY REWARDS")],
        [KeyboardButton("WITHDRAW"), KeyboardButton("ORDER HISTORY")],
        [KeyboardButton("PROFILE"), KeyboardButton("HELP")],
    ],
    resize_keyboard=True,
)

WITHDRAW_AMOUNT, WITHDRAW_BINANCE_ID = range(2)
ADDTASK_NAME, ADDTASK_DESC, ADDTASK_REWARD, ADDTASK_IMAGE = range(10, 14)

ORDERS_PAGE_SIZE = 5


def full_name(user):
    return " ".join(p for p in [user.first_name, user.last_name] if p) or "User"


def mention_html(telegram_id: int, name: str) -> str:
    safe = name.replace("<", "").replace(">", "")
    return f'<a href="tg://user?id={telegram_id}">{safe}</a>'


async def ensure_registered(update: Update):
    u = update.effective_user
    is_new = db.upsert_user(u.id, u.username, u.first_name, u.last_name)
    if is_new:
        db.log("new_user", f"{u.id} @{u.username}", u.id)
    else:
        db.log("returning_user", str(u.id), u.id)
    return u


# =========================================================
# Force join
# =========================================================

async def check_force_join(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not config.FORCE_JOIN_ENABLED:
        return True
    channels = db.list_force_join_channels()
    channels = [c for c in channels if c["enabled"]]
    if not channels:
        return True

    user_id = update.effective_user.id
    joined = 0
    not_joined = []
    for ch in channels:
        try:
            member = await context.bot.get_chat_member(ch["channel_id"], user_id)
            if member.status in ("member", "administrator", "creator"):
                joined += 1
            else:
                not_joined.append(ch)
        except TelegramError:
            not_joined.append(ch)

    required = config.FORCE_JOIN_STRICT or len(channels)
    if joined >= required:
        return True

    buttons = []
    for ch in not_joined:
        url = ch["invite_url"] or f"https://t.me/{str(ch['channel_id']).lstrip('@')}"
        buttons.append([InlineKeyboardButton(f"JOIN {ch['channel_id']}", url=url)])
    buttons.append([InlineKeyboardButton("CHECK JOIN", callback_data="checkjoin")])

    text = f"Please join {required} of the {len(channels)} required channel(s) below, then tap CHECK JOIN."
    if update.callback_query:
        await update.callback_query.message.reply_text(text, reply_markup=InlineKeyboardMarkup(buttons))
    else:
        await update.effective_message.reply_text(text, reply_markup=InlineKeyboardMarkup(buttons))
    return False


async def checkjoin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    ok = await check_force_join(update, context)
    if ok:
        await q.message.reply_text("You're all set. Use the menu below to continue.", reply_markup=MENU)


# =========================================================
# /start and menu
# =========================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_registered(update)
    await update.message.reply_text(
        "Welcome to QR Reward Bot\n\nComplete simple app tasks posted in our groups and earn real rewards.",
        reply_markup=MENU,
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "HOW IT WORKS\n\n"
        "1. Watch our groups for new task posts.\n"
        "2. Tap CLAIM to reserve a task — first come, first served.\n"
        "3. Complete the task inside the app named in your DM.\n"
        "4. Tap MARK TASK COMPLETE.\n"
        "5. An admin verifies completion and your reward is added to your balance.\n"
        "6. Withdraw your balance any time via WITHDRAW.\n\n"
        f"Support: {config.SUPPORT_USERNAME or 'contact an admin'}"
    )
    await update.effective_message.reply_text(text, reply_markup=MENU)


# =========================================================
# Wallet / Rewards / Profile / Order history
# =========================================================

async def show_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = db.get_user(update.effective_user.id)
    if not user:
        await ensure_registered(update)
        user = db.get_user(update.effective_user.id)
    text = (
        "WALLET\n\n"
        f"Available Balance: ${user['balance_usd']:.2f}\n"
        f"Total Earned: ${user['total_earned_usd']:.2f}\n"
        f"Total Withdrawn: ${user['total_withdrawn_usd']:.2f}\n"
        f"Pending Withdrawal: ${user['pending_withdrawal_usd']:.2f}"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("WITHDRAW", callback_data="menu:withdraw")],
        [InlineKeyboardButton("BACK TO MENU", callback_data="menu:back")],
    ])
    await update.effective_message.reply_text(text, reply_markup=kb)


async def show_rewards(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    approved, pending, rejected, recent = db.user_rewards_summary(telegram_id)
    lines = [
        "MY REWARDS\n",
        f"Approved rewards: {approved['c']} (${approved['s']:.2f})",
        f"Pending rewards: {pending['c']}",
        f"Rejected rewards: {rejected['c']}",
        "",
        "Recent successful rewards:",
    ]
    if recent:
        for r in recent:
            lines.append(f"- {r['app_name']}: ${r['reward_usd']:.2f} ({r['reviewed_at'][:10]})")
    else:
        lines.append("- None yet")
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("WALLET", callback_data="menu:wallet"),
         InlineKeyboardButton("ORDER HISTORY", callback_data="orders:0")],
        [InlineKeyboardButton("BACK TO MENU", callback_data="menu:back")],
    ])
    await update.effective_message.reply_text("\n".join(lines), reply_markup=kb)


def _status_label(s):
    return {
        "AVAILABLE": "AVAILABLE",
        "CLAIMED": "CLAIMED",
        "VERIFICATION_PENDING": "CONFIRMATION PENDING",
        "SUCCESS": "SUCCESS",
        "FAILED": "FAILED",
    }.get(s, s)


async def show_orders(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 0):
    telegram_id = update.effective_user.id
    offset = page * ORDERS_PAGE_SIZE
    rows, total = db.user_orders(telegram_id, limit=ORDERS_PAGE_SIZE, offset=offset)
    if not rows:
        text = "ORDER HISTORY\n\nYou have no orders yet."
    else:
        lines = ["ORDER HISTORY\n"]
        for r in rows:
            lines.append(
                f"Order #{r['order_id']} · QR #{r['task_id']}\n"
                f"App: {r['app_name']}\n"
                f"Reward: ${r['reward_usd']:.2f}\n"
                f"Claimed: {r['claimed_at'][:16]}\n"
                f"Status: {_status_label(r['status'])}\n"
            )
        text = "\n".join(lines)

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("PREVIOUS", callback_data=f"orders:{page-1}"))
    if offset + ORDERS_PAGE_SIZE < total:
        nav.append(InlineKeyboardButton("NEXT", callback_data=f"orders:{page+1}"))
    rows_kb = [nav] if nav else []
    rows_kb.append([InlineKeyboardButton("BACK TO MENU", callback_data="menu:back")])

    if update.callback_query:
        await update.callback_query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(rows_kb))
    else:
        await update.effective_message.reply_text(text, reply_markup=InlineKeyboardMarkup(rows_kb))


async def show_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    user = db.get_user(u.id)
    total_orders = db.get_conn().execute(
        "SELECT COUNT(*) c FROM orders WHERE telegram_id=?", (u.id,)
    ).fetchone()["c"]
    username_line = f"Username: @{u.username}\n" if u.username else "Username: not set\n"
    text = (
        "PROFILE\n\n"
        f"Name: {full_name(u)}\n"
        f"{username_line}"
    )
    text += (
        f"Telegram ID: {u.id}\n"
        f"Balance: ${user['balance_usd']:.2f}\n"
        f"Total Earned: ${user['total_earned_usd']:.2f}\n"
        f"Total Withdrawn: ${user['total_withdrawn_usd']:.2f}\n"
        f"Total Orders: {total_orders}"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("ORDER HISTORY", callback_data="orders:0")],
        [InlineKeyboardButton("BACK TO MENU", callback_data="menu:back")],
    ])
    await update.effective_message.reply_text(text, reply_markup=kb)


async def menu_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().upper()
    if db.is_banned(update.effective_user.id):
        await update.message.reply_text("Your account has been suspended. Contact support for help.")
        return
    if text == "WALLET":
        await show_wallet(update, context)
    elif text == "MY REWARDS":
        await show_rewards(update, context)
    elif text == "WITHDRAW":
        return await withdraw_start(update, context)
    elif text == "ORDER HISTORY":
        await show_orders(update, context, 0)
    elif text == "PROFILE":
        await show_profile(update, context)
    elif text == "HELP":
        await help_cmd(update, context)
    # anything else: stay silent, do not resend the full menu on every random message


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    action = q.data.split(":", 1)[1]
    if action == "wallet":
        await show_wallet(update, context)
    elif action == "withdraw":
        await q.message.reply_text("Use the WITHDRAW button on your menu to start a withdrawal.", reply_markup=MENU)
    elif action == "back":
        await q.message.reply_text("Main menu:", reply_markup=MENU)


async def orders_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    page = int(q.data.split(":", 1)[1])
    await show_orders(update, context, page)


# =========================================================
# Claim flow
# =========================================================

def task_group_caption(task, claimed_name=None, status=None):
    if status == "claimed":
        return f"TASK CLAIMED\n\nApp: {task['app_name']}\nClaimed by: {claimed_name}"
    if status == "success":
        return f"TASK SUCCESS\n\nApp: {task['app_name']}\nReward: ${task['reward_usd']:.2f}\nClaimed by: {claimed_name}"
    if status == "failed":
        return f"TASK FAILED\n\nApp: {task['app_name']}\nClaimed by: {claimed_name}"
    return (
        f"TASK AVAILABLE\n\nApp: {task['app_name']}\nReward: ${task['reward_usd']:.2f}\n\n"
        "First eligible user gets the task in DM."
    )


async def post_task_to_groups(context: ContextTypes.DEFAULT_TYPE, task_id: int):
    task = db.get_task(task_id)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("CLAIM TASK", callback_data=f"claim:{task_id}")]])
    message_refs = []
    for chat_id in config.GROUP_IDS:
        try:
            msg = await context.bot.send_message(
                chat_id, task_group_caption(task), reply_markup=kb,
            )
            message_refs.append({"chat_id": chat_id, "message_id": msg.message_id})
        except TelegramError as e:
            logger.warning("Failed to post task %s to group %s: %s", task_id, chat_id, e)
    db.set_task_group_messages(task_id, json.dumps(message_refs))
    db.log("task_posted", f"task={task_id} groups={len(message_refs)}")


async def update_group_messages(context: ContextTypes.DEFAULT_TYPE, task, caption, remove_buttons=True):
    refs = json.loads(task["group_message_ids"] or "[]")
    for ref in refs:
        try:
            await context.bot.edit_message_text(
                chat_id=ref["chat_id"], message_id=ref["message_id"], text=caption,
                reply_markup=None if remove_buttons else InlineKeyboardMarkup([]),
            )
        except TelegramError as e:
            logger.warning("Failed to update group message %s: %s", ref, e)


async def claim_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    user = update.effective_user
    await ensure_registered(update)

    if db.is_banned(user.id):
        await q.answer("Your account has been suspended.", show_alert=True)
        return

    if not await check_force_join(update, context):
        await q.answer()
        return

    task_id = int(q.data.split(":", 1)[1])
    order_id, result = db.claim_task(task_id, user.id)
    if order_id is None:
        await q.answer(result, show_alert=True)
        return

    await q.answer("Task claimed! Check your DM.")
    task = result
    db.log("task_claimed", f"task={task_id} order={order_id}", user.id)

    name = full_name(user)
    refs = json.loads(task["group_message_ids"] or "[]")
    for ref in refs:
        try:
            await context.bot.edit_message_text(
                chat_id=ref["chat_id"], message_id=ref["message_id"],
                text=task_group_caption(task, mention_html(user.id, name), "claimed"),
                parse_mode=ParseMode.HTML,
            )
        except TelegramError as e:
            logger.warning("Failed to update group message %s: %s", ref, e)

    kb = InlineKeyboardMarkup([[InlineKeyboardButton("MARK TASK COMPLETE", callback_data=f"complete:{order_id}")]])
    caption = f"TASK CLAIMED\n\nApp: {task['app_name']}\nReward: ${task['reward_usd']:.2f}\n\n"
    if task["task_description"]:
        caption += f"{task['task_description']}\n\n"
    caption += "Complete the task inside the app above, then tap the button below."

    try:
        if task["file_id"]:
            await context.bot.send_photo(user.id, task["file_id"], caption=caption, reply_markup=kb)
        else:
            await context.bot.send_message(user.id, caption, reply_markup=kb)
    except TelegramError as e:
        logger.warning("Could not DM user %s: %s", user.id, e)
        await q.message.reply_text(
            "I couldn't message you privately. Please start a DM with me first, then try claiming again."
        )


async def complete_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    order_id = int(q.data.split(":", 1)[1])
    ok, result = db.submit_completion(order_id, update.effective_user.id)
    if not ok:
        await q.answer(result, show_alert=True)
        return
    await q.answer("Submitted for verification.")
    await q.edit_message_reply_markup(reply_markup=None)
    await q.message.reply_text(
        "Thanks! Your task is now pending admin verification. You'll be notified once it's reviewed.",
        reply_markup=MENU,
    )

    order = result
    task = db.get_task(order["task_id"])
    db.log("task_submitted", f"order={order_id}", update.effective_user.id)

    user = update.effective_user
    name = full_name(user)
    review_text = (
        "TASK VERIFICATION REQUEST\n\n"
        f"Order ID: {order['order_id']}\n"
        f"Task ID: {order['task_id']}\n"
        f"User: {name}\n"
        f"Telegram ID: {user.id}\n"
        f"App: {order['app_name']}\n"
        f"Reward: ${order['reward_usd']:.2f}\n"
        f"Claimed: {order['claimed_at'][:16]}\n"
        f"Submitted: {db.now()[:16]}"
    )
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("APPROVE", callback_data=f"approve:{order_id}"),
        InlineKeyboardButton("REJECT", callback_data=f"reject:{order_id}"),
    ]])
    for admin_id in config.ADMIN_IDS:
        try:
            await context.bot.send_message(admin_id, review_text, reply_markup=kb)
        except TelegramError as e:
            logger.warning("Could not notify admin %s: %s", admin_id, e)


async def approve_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    admin_id = update.effective_user.id
    if not db.is_admin(admin_id):
        await q.answer("Admins only.", show_alert=True)
        return
    order_id = int(q.data.split(":", 1)[1])
    ok, result = db.approve_order(order_id, admin_id)
    if not ok:
        await q.answer(result, show_alert=True)
        return
    await q.answer("Approved.")
    await q.edit_message_reply_markup(reply_markup=None)
    order = result
    task = db.get_task(order["task_id"])
    db.log("task_approved", f"order={order_id}", admin_id)

    try:
        await context.bot.send_message(
            order["telegram_id"],
            f"TASK SUCCESS\n\nApp: {order['app_name']}\nReward: ${order['reward_usd']:.2f} has been added to your balance.",
        )
    except TelegramError:
        pass

    try:
        target_user = await context.bot.get_chat(order["telegram_id"])
        name = full_name(target_user)
    except TelegramError:
        name = f"user {order['telegram_id']}"
    await update_group_messages(
        context, task,
        f"TASK SUCCESS\n\nApp: {task['app_name']}\nReward: ${task['reward_usd']:.2f}\nClaimed by: {name}",
    )


async def reject_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    admin_id = update.effective_user.id
    if not db.is_admin(admin_id):
        await q.answer("Admins only.", show_alert=True)
        return
    order_id = int(q.data.split(":", 1)[1])
    ok, result = db.reject_order(order_id, admin_id)
    if not ok:
        await q.answer(result, show_alert=True)
        return
    await q.answer("Rejected.")
    await q.edit_message_reply_markup(reply_markup=None)
    order = result
    task = db.get_task(order["task_id"])
    db.log("task_rejected", f"order={order_id}", admin_id)

    try:
        await context.bot.send_message(
            order["telegram_id"],
            f"TASK FAILED\n\nApp: {order['app_name']}\nYour submission was not verified. Contact support if you believe this is a mistake.",
        )
    except TelegramError:
        pass

    try:
        target_user = await context.bot.get_chat(order["telegram_id"])
        name = full_name(target_user)
    except TelegramError:
        name = f"user {order['telegram_id']}"
    await update_group_messages(
        context, task, f"TASK FAILED\n\nApp: {task['app_name']}\nClaimed by: {name}",
    )


# =========================================================
# Withdraw conversation
# =========================================================

async def withdraw_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = db.get_user(update.effective_user.id)
    await update.message.reply_text(
        f"WITHDRAW\n\nAvailable balance: ${user['balance_usd']:.2f}\n"
        f"Rate: {config.USD_TO_INR} INR = $1\n\n"
        "Enter the amount you want to withdraw in INR (e.g. 100):"
    )
    return WITHDRAW_AMOUNT


async def withdraw_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        amount_inr = float(text)
        if amount_inr <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Please enter a valid positive number (INR amount).")
        return WITHDRAW_AMOUNT
    context.user_data["withdraw_inr"] = amount_inr
    usd = amount_inr / config.USD_TO_INR
    await update.message.reply_text(
        f"That's approximately ${usd:.2f}.\n\nNow send your Binance ID / UID to receive payment:"
    )
    return WITHDRAW_BINANCE_ID


async def withdraw_binance_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    binance_id = update.message.text.strip()
    amount_inr = context.user_data.pop("withdraw_inr")
    wid, err = db.request_withdrawal(update.effective_user.id, amount_inr, binance_id)
    if err:
        await update.message.reply_text(err, reply_markup=MENU)
        return ConversationHandler.END

    db.log("withdrawal_requested", f"id={wid} inr={amount_inr}", update.effective_user.id)
    await update.message.reply_text(
        f"Withdrawal request #{wid} submitted for ₹{amount_inr:.2f}. "
        "We'll notify you once it's processed.",
        reply_markup=MENU,
    )
    w = db.get_conn().execute("SELECT * FROM withdrawals WHERE withdrawal_id=?", (wid,)).fetchone()
    u = update.effective_user
    text = (
        "NEW WITHDRAWAL REQUEST\n\n"
        f"Withdrawal ID: {wid}\n"
        f"User: {full_name(u)} (@{u.username or 'n/a'})\n"
        f"Telegram ID: {u.id}\n"
        f"INR: {w['amount_inr']:.2f}\n"
        f"USD: {w['amount_usd']:.2f}\n"
        f"Binance ID: {w['binance_id']}"
    )
    for admin_id in config.ADMIN_IDS:
        try:
            await context.bot.send_message(admin_id, text)
        except TelegramError:
            pass
    return ConversationHandler.END


async def cancel_conv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Cancelled.", reply_markup=MENU)
    return ConversationHandler.END


# =========================================================
# Admin commands
# =========================================================

def admin_only(func):
    async def wrapper(update, context):
        if not db.is_admin(update.effective_user.id):
            return
        return await func(update, context)
    return wrapper


@admin_only
async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not config.WEBAPP_URL:
        await update.message.reply_text("WEBAPP_URL is not configured.")
        return
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("OPEN ADMIN PANEL", web_app=WebAppInfo(url=config.WEBAPP_URL))]])
    await update.message.reply_text("Admin dashboard:", reply_markup=kb)


@admin_only
async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    c = db.get_conn()
    total_users = c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
    total_tasks = c.execute("SELECT COUNT(*) n FROM tasks").fetchone()["n"]
    available = c.execute("SELECT COUNT(*) n FROM tasks WHERE status='AVAILABLE'").fetchone()["n"]
    success = c.execute("SELECT COUNT(*) n FROM orders WHERE status='SUCCESS'").fetchone()["n"]
    pending_rev = c.execute("SELECT COUNT(*) n FROM orders WHERE status='VERIFICATION_PENDING'").fetchone()["n"]
    paid = c.execute("SELECT COALESCE(SUM(reward_usd),0) s FROM orders WHERE status='SUCCESS'").fetchone()["s"]
    pending_wd = c.execute("SELECT COUNT(*) n FROM withdrawals WHERE status='PENDING'").fetchone()["n"]
    await update.message.reply_text(
        "STATS\n\n"
        f"Total Users: {total_users}\n"
        f"Total Tasks: {total_tasks}\n"
        f"Available: {available}\n"
        f"Successful Orders: {success}\n"
        f"Pending Verification: {pending_rev}\n"
        f"Total Rewards Paid: ${paid:.2f}\n"
        f"Pending Withdrawals: {pending_wd}"
    )


@admin_only
async def addtask_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Enter the app name for this task:")
    return ADDTASK_NAME


async def addtask_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_task_name"] = update.message.text.strip()
    await update.message.reply_text("Enter a short task description (what the user must do):")
    return ADDTASK_DESC


async def addtask_desc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_task_desc"] = update.message.text.strip()
    await update.message.reply_text("Enter the reward amount in USD (e.g. 0.50):")
    return ADDTASK_REWARD


async def addtask_reward(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        reward = float(update.message.text.strip())
        if reward <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Please enter a valid positive USD amount.")
        return ADDTASK_REWARD
    context.user_data["new_task_reward"] = reward
    await update.message.reply_text("Send the QR/task image now, or send /skip to post without an image:")
    return ADDTASK_IMAGE


async def addtask_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    file_id = None
    if update.message.photo:
        file_id = update.message.photo[-1].file_id
    task_id = db.create_task(
        file_id,
        context.user_data.pop("new_task_name"),
        context.user_data.pop("new_task_desc"),
        context.user_data.pop("new_task_reward"),
    )
    db.log("task_created", f"task={task_id}", update.effective_user.id)
    await update.message.reply_text(
        f"Task #{task_id} created. Use /postqr {task_id} to broadcast it to your configured groups."
    )
    return ConversationHandler.END


async def addtask_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    task_id = db.create_task(
        None,
        context.user_data.pop("new_task_name"),
        context.user_data.pop("new_task_desc"),
        context.user_data.pop("new_task_reward"),
    )
    db.log("task_created", f"task={task_id}", update.effective_user.id)
    await update.message.reply_text(
        f"Task #{task_id} created. Use /postqr {task_id} to broadcast it to your configured groups."
    )
    return ConversationHandler.END


@admin_only
async def postqr_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /postqr <task_id>")
        return
    try:
        task_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Task ID must be a number.")
        return
    task = db.get_task(task_id)
    if not task:
        await update.message.reply_text("Task not found.")
        return
    if not config.GROUP_IDS:
        await update.message.reply_text("No GROUP_IDS configured.")
        return
    await post_task_to_groups(context, task_id)
    await update.message.reply_text(f"Task #{task_id} posted to {len(config.GROUP_IDS)} group(s).")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Unhandled exception", exc_info=context.error)
    db.log("error", str(context.error))


def build_application() -> Application:
    db.init_db()
    app = Application.builder().token(config.BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("postqr", postqr_cmd))

    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("addqr", addtask_start)],
        states={
            ADDTASK_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, addtask_name)],
            ADDTASK_DESC: [MessageHandler(filters.TEXT & ~filters.COMMAND, addtask_desc)],
            ADDTASK_REWARD: [MessageHandler(filters.TEXT & ~filters.COMMAND, addtask_reward)],
            ADDTASK_IMAGE: [
                MessageHandler(filters.PHOTO, addtask_image),
                CommandHandler("skip", addtask_skip),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_conv)],
    ))

    app.add_handler(ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^WITHDRAW$"), withdraw_start)],
        states={
            WITHDRAW_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, withdraw_amount)],
            WITHDRAW_BINANCE_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, withdraw_binance_id)],
        },
        fallbacks=[CommandHandler("cancel", cancel_conv)],
    ))

    app.add_handler(CallbackQueryHandler(claim_callback, pattern=r"^claim:\d+$"))
    app.add_handler(CallbackQueryHandler(complete_callback, pattern=r"^complete:\d+$"))
    app.add_handler(CallbackQueryHandler(approve_callback, pattern=r"^approve:\d+$"))
    app.add_handler(CallbackQueryHandler(reject_callback, pattern=r"^reject:\d+$"))
    app.add_handler(CallbackQueryHandler(checkjoin_callback, pattern=r"^checkjoin$"))
    app.add_handler(CallbackQueryHandler(orders_page_callback, pattern=r"^orders:\d+$"))
    app.add_handler(CallbackQueryHandler(menu_callback, pattern=r"^menu:"))

    app.add_handler(MessageHandler(
        filters.Regex("^(WALLET|MY REWARDS|ORDER HISTORY|PROFILE|HELP)$"), menu_router
    ))

    app.add_error_handler(error_handler)
    return app


if __name__ == "__main__":
    application = build_application()
    application.run_polling()
