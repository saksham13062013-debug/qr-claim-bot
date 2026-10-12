"""Telegram bot using aiogram 3.x – single polling instance."""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatAction, ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

from app.config import get_settings
from app.db import get_db_context
from app.models import UserStatus
from app.services import force_join_service, qr_service, settings_service, user_service
from app.services.notification_service import set_bot
from app.services.wallet_service import get_transactions
from app.services.withdrawal_service import create_withdrawal

logger = logging.getLogger(__name__)
settings = get_settings()

router = Router()

_bot: Optional[Bot] = None
_dp: Optional[Dispatcher] = None
_polling_task: Optional[asyncio.Task] = None


def cents_to_usd(cents: int) -> str:
    return f"${cents / 100:.2f}"


# ---------------------------------------------------------------------------
# Keyboards – coloured styles (Bot API 9.4 / aiogram 3.20+)
# ---------------------------------------------------------------------------

def main_menu_kb() -> ReplyKeyboardMarkup:
    """Bottom reply keyboard with coloured styles (Bot API 9.4)."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="How to Claim", style="primary"),
                KeyboardButton(text="Balance", style="primary"),
            ],
            [
                KeyboardButton(text="My Claims", style="primary"),
                KeyboardButton(text="Withdraw", style="success"),
            ],
            [
                KeyboardButton(text="History", style="primary"),
                KeyboardButton(text="Support", style="primary"),
            ],
            [KeyboardButton(text="Help", style="primary")],
        ],
        resize_keyboard=True,
        input_field_placeholder="Choose an option…",
    )


def nav_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Balance",
                    callback_data="menu:balance",
                    style="primary",
                ),
                InlineKeyboardButton(
                    text="Withdraw",
                    callback_data="menu:withdraw",
                    style="success",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="My Claims",
                    callback_data="menu:claims",
                    style="primary",
                ),
                InlineKeyboardButton(
                    text="Help",
                    callback_data="menu:help",
                    style="primary",
                ),
            ],
        ]
    )


def force_join_kb(channels) -> InlineKeyboardMarkup:
    buttons = []
    for ch in channels:
        url = ch.invite_url
        if not url and ch.username:
            url = f"https://t.me/{ch.username.lstrip('@')}"
        if url:
            buttons.append(
                [InlineKeyboardButton(text=f"Join {ch.title}", url=url, style="primary")]
            )
    buttons.append(
        [
            InlineKeyboardButton(
                text="I Joined – Check Again",
                callback_data="check_membership",
                style="success",
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def claim_button(code: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="CLAIM REWARD",
                    callback_data=f"claim:{code}",
                    style="success",
                )
            ]
        ]
    )


def submit_claim_button(code: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Submit Claim",
                    callback_data=f"submit_claim:{code}",
                    style="success",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Cancel",
                    callback_data="cancel_claim",
                    style="danger",
                )
            ],
        ]
    )


def withdraw_cancel_kb() -> InlineKeyboardMarkup:
    """Shown under withdraw prompts so user can exit anytime."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Cancel",
                    callback_data="payout:cancel",
                    style="danger",
                )
            ]
        ]
    )


def payout_method_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="UPI ID",
                    callback_data="payout:upi",
                    style="primary",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Binance ID",
                    callback_data="payout:binance",
                    style="primary",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Cancel",
                    callback_data="payout:cancel",
                    style="danger",
                )
            ],
        ]
    )


def after_claim_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="View Balance",
                    callback_data="menu:balance",
                    style="primary",
                ),
                InlineKeyboardButton(
                    text="My Claims",
                    callback_data="menu:claims",
                    style="primary",
                ),
            ]
        ]
    )


# ---------------------------------------------------------------------------
# FSM
# ---------------------------------------------------------------------------

class WithdrawStates(StatesGroup):
    waiting_amount = State()
    waiting_payout_details = State()


class SupportStates(StatesGroup):
    waiting_message = State()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def typing(bot: Bot, chat_id: int, seconds: float = 0.15) -> None:
    try:
        await bot.send_chat_action(chat_id, ChatAction.TYPING)
        if seconds > 0:
            await asyncio.sleep(min(seconds, 0.25))
    except Exception:
        pass


async def uploading_photo(bot: Bot, chat_id: int, seconds: float = 0.2) -> None:
    try:
        await bot.send_chat_action(chat_id, ChatAction.UPLOAD_PHOTO)
        if seconds > 0:
            await asyncio.sleep(min(seconds, 0.3))
    except Exception:
        pass


async def _send_already_claimed(
    target,
    bot: Bot,
    code: str,
    *,
    session=None,
    qr=None,
) -> None:
    """Already-claimed message with clickable claimer name + profile button."""
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload
    from app.models import QRClaim, QRCode as QRModel, User

    claimer = None
    code_u = code.upper().strip()

    async def _load_claimer(sess) -> Optional[User]:
        # 1) QR.claimed_by relationship
        row = (
            await sess.execute(
                select(QRModel)
                .options(selectinload(QRModel.claimed_by))
                .where(QRModel.code == code_u)
            )
        ).scalar_one_or_none()
        if row and row.claimed_by is not None:
            return row.claimed_by
        # 2) Direct user id on QR
        if row and row.claimed_by_user_id:
            u = (
                await sess.execute(
                    select(User).where(User.id == row.claimed_by_user_id)
                )
            ).scalar_one_or_none()
            if u:
                return u
        # 3) Latest claim row for this QR
        qr_id = row.id if row else None
        if qr_id is None and qr is not None:
            qr_id = getattr(qr, "id", None)
        if qr_id is not None:
            claim = (
                await sess.execute(
                    select(QRClaim)
                    .options(selectinload(QRClaim.user))
                    .where(QRClaim.qr_code_id == qr_id)
                    .order_by(QRClaim.created_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if claim is not None:
                if getattr(claim, "user", None) is not None:
                    return claim.user
                u = (
                    await sess.execute(
                        select(User).where(User.id == claim.user_id)
                    )
                ).scalar_one_or_none()
                if u:
                    return u
        return None

    try:
        if session is not None:
            claimer = await _load_claimer(session)
        if claimer is None:
            async with get_db_context() as sess:
                claimer = await _load_claimer(sess)
    except Exception:
        logger.exception("Failed to resolve claimer for %s", code)
        claimer = None

    claimer_line = "another user"
    buttons = []

    if claimer is not None:
        first = (claimer.first_name or "").strip()
        last = (claimer.last_name or "").strip()
        uname = (claimer.username or "").strip()
        display = " ".join(x for x in (first, last) if x).strip()
        if not display:
            display = f"@{uname}" if uname else f"User {claimer.telegram_id}"
        safe = (
            display.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )
        if uname:
            claimer_line = f'<a href="https://t.me/{uname}">{safe}</a>'
            if not display.startswith("@"):
                claimer_line += f" (<a href=\"https://t.me/{uname}\">@{uname}</a>)"
            buttons.append(
                [
                    InlineKeyboardButton(
                        text="Open claimer profile",
                        url=f"https://t.me/{uname}",
                    )
                ]
            )
        else:
            # text_mention style link by telegram id
            claimer_line = (
                f'<a href="tg://user?id={claimer.telegram_id}">{safe}</a>'
            )

    buttons.append(
        [
            InlineKeyboardButton(
                text="Balance",
                callback_data="menu:balance",
                style="primary",
            ),
            InlineKeyboardButton(
                text="Help",
                callback_data="menu:help",
                style="primary",
            ),
        ]
    )
    claimer_kb = InlineKeyboardMarkup(inline_keyboard=buttons)

    text = (
        "<b>Already claimed</b>\n\n"
        f"This QR was claimed by {claimer_line}.\n"
        "Each QR can only be claimed once."
    )
    try:
        if hasattr(target, "answer"):
            await target.answer(
                text, parse_mode=ParseMode.HTML, reply_markup=claimer_kb
            )
        else:
            await bot.send_message(
                chat_id=target.chat.id,
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=claimer_kb,
            )
    except Exception:
        # Fallback without HTML if Telegram rejects entities
        plain = (
            "Already claimed\n\n"
            f"This QR was claimed by {getattr(claimer, 'username', None) and '@' + claimer.username or (claimer.first_name if claimer else 'another user')}.\n"
            "Each QR can only be claimed once."
        )
        if hasattr(target, "answer"):
            await target.answer(plain, reply_markup=claimer_kb)
        else:
            await bot.send_message(
                chat_id=target.chat.id, text=plain, reply_markup=claimer_kb
            )


# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------

@router.message(CommandStart())
async def cmd_start(message: Message, bot: Bot):
    async with get_db_context() as session:
        await user_service.get_or_create_user(
            session,
            telegram_id=message.from_user.id,
            username=message.from_user.username,
            first_name=message.from_user.first_name,
            last_name=message.from_user.last_name,
        )
        welcome = await settings_service.get_setting(session, "bot_welcome_message")
        if not welcome:
            welcome = settings.bot_welcome_message

    await typing(bot, message.chat.id)

    parts = (message.text or "").split(maxsplit=1)
    payload = parts[1].strip() if len(parts) > 1 else ""
    if payload.startswith("claim_"):
        code = payload[len("claim_") :].strip().upper()
        if code:
            await message.answer(welcome, reply_markup=main_menu_kb())
            await _send_claim_preview(message, bot, code)
            return

    name = message.from_user.first_name or "there"
    text = (
        f"<b>Welcome, {name}!</b>\n\n"
        f"{welcome}\n\n"
        f"Use the menu below or the buttons to get started."
    )
    await message.answer(text, reply_markup=main_menu_kb(), parse_mode=ParseMode.HTML)
    await message.answer(
        "Quick actions:",
        reply_markup=nav_kb(),
        parse_mode=ParseMode.HTML,
    )


# ---------------------------------------------------------------------------
# Menu handlers (text + callback)
# ---------------------------------------------------------------------------

@router.message(F.text == "How to Claim")
@router.callback_query(F.data == "menu:help")
async def how_to_claim(event: Message | CallbackQuery, bot: Bot):
    text = (
        "<b>How to claim a reward</b>\n\n"
        "1. When a reward is live, you get a <b>private message</b> from this bot.\n"
        "2. Tap <b>CLAIM REWARD</b> (green).\n"
        "3. Join any required channels if asked.\n"
        "4. Review the QR image, then tap <b>Submit Claim</b>.\n"
        "5. An admin reviews your claim. Once approved, the reward is credited to your wallet.\n"
        "6. Withdraw via <b>UPI</b> or <b>Binance ID</b> when you reach the minimum.\n\n"
        "<i>Each QR can be claimed by only one user.</i>"
    )
    if isinstance(event, CallbackQuery):
        await event.answer()
        await typing(bot, event.message.chat.id)
        await event.message.answer(text, parse_mode=ParseMode.HTML, reply_markup=nav_kb())
    else:
        await typing(bot, event.chat.id)
        await event.answer(text, parse_mode=ParseMode.HTML, reply_markup=nav_kb())


@router.message(F.text.in_({"Balance", "Wallet"}))
@router.callback_query(F.data == "menu:balance")
async def balance_cmd(event: Message | CallbackQuery, bot: Bot):
    user_obj = event.from_user if isinstance(event, CallbackQuery) else event.from_user
    chat_id = event.message.chat.id if isinstance(event, CallbackQuery) else event.chat.id
    if isinstance(event, CallbackQuery):
        await event.answer()
    await typing(bot, chat_id)

    async with get_db_context() as session:
        user = await user_service.get_or_create_user(
            session,
            telegram_id=user_obj.id,
            username=user_obj.username,
            first_name=user_obj.first_name,
            last_name=user_obj.last_name,
        )
        text = (
            f"<b>Your wallet</b>\n\n"
            f"Available: <b>{cents_to_usd(user.balance_cents)}</b>\n"
            f"Pending: {cents_to_usd(int(user.pending_cents or 0))}\n"
            f"Total earned: {cents_to_usd(user.total_earned_cents)}\n\n"
            f"Withdraw when you reach the minimum balance."
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Withdraw",
                        callback_data="menu:withdraw",
                        style="success",
                    ),
                    InlineKeyboardButton(
                        text="History",
                        callback_data="menu:history",
                        style="primary",
                    ),
                ]
            ]
        )
        if isinstance(event, CallbackQuery):
            await event.message.answer(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        else:
            await event.answer(text, parse_mode=ParseMode.HTML, reply_markup=kb)


@router.message(F.text.in_({"History", "Transaction History"}))
@router.callback_query(F.data == "menu:history")
async def tx_history(event: Message | CallbackQuery, bot: Bot):
    user_obj = event.from_user
    chat_id = event.message.chat.id if isinstance(event, CallbackQuery) else event.chat.id
    if isinstance(event, CallbackQuery):
        await event.answer()
    await typing(bot, chat_id)

    async with get_db_context() as session:
        user = await user_service.get_or_create_user(
            session,
            telegram_id=user_obj.id,
            username=user_obj.username,
            first_name=user_obj.first_name,
            last_name=user_obj.last_name,
        )
        txs, total = await get_transactions(session, user.id, offset=0, limit=10)
    lines = [f"<b>Recent transactions</b> ({len(txs)} of {total})\n"]
    if not txs:
        lines.append("No transactions yet. Claim a reward to get started.")
    for t in txs:
        sign = "+" if t.amount_cents > 0 else ""
        lines.append(
            f"• <b>{t.type.value}</b>: {sign}{cents_to_usd(t.amount_cents)} "
            f"→ balance {cents_to_usd(t.balance_after_cents)}"
        )
    text = "\n".join(lines)
    if isinstance(event, CallbackQuery):
        await event.message.answer(text, parse_mode=ParseMode.HTML, reply_markup=nav_kb())
    else:
        await event.answer(text, parse_mode=ParseMode.HTML, reply_markup=nav_kb())


@router.message(F.text == "My Claims")
@router.callback_query(F.data == "menu:claims")
async def my_claims(event: Message | CallbackQuery, bot: Bot):
    from sqlalchemy import select
    from app.models import QRClaim

    user_obj = event.from_user
    chat_id = event.message.chat.id if isinstance(event, CallbackQuery) else event.chat.id
    if isinstance(event, CallbackQuery):
        await event.answer()
    await typing(bot, chat_id)

    async with get_db_context() as session:
        user = await user_service.get_or_create_user(
            session,
            telegram_id=user_obj.id,
            username=user_obj.username,
            first_name=user_obj.first_name,
            last_name=user_obj.last_name,
        )
        result = await session.execute(
            select(QRClaim)
            .where(QRClaim.user_id == user.id)
            .order_by(QRClaim.created_at.desc())
            .limit(15)
        )
        claims = result.scalars().all()
    lines = ["<b>Your claims</b>\n"]
    if not claims:
        lines.append("No claims yet. Watch for reward notifications in this chat.")
    for c in claims:
        lines.append(
            f"#{c.id} — {cents_to_usd(c.reward_cents)} — <b>{c.status.value}</b>"
        )
    text = "\n".join(lines)
    if isinstance(event, CallbackQuery):
        await event.message.answer(text, parse_mode=ParseMode.HTML, reply_markup=nav_kb())
    else:
        await event.answer(text, parse_mode=ParseMode.HTML, reply_markup=nav_kb())


@router.message(F.text == "Help")
async def help_cmd(message: Message, bot: Bot):
    await typing(bot, message.chat.id)
    await message.answer(
        "<b>Help</b>\n\n"
        "• <b>How to Claim</b> — step-by-step guide\n"
        "• <b>Balance</b> — wallet balance and pending funds\n"
        "• <b>My Claims</b> — status of your reward claims\n"
        "• <b>Withdraw</b> — request payout via UPI or Binance\n"
        "• <b>History</b> — recent wallet transactions\n"
        "• <b>Support</b> — message the admin team\n\n"
        "Rewards are sent as private messages. Only one user can claim each QR.",
        parse_mode=ParseMode.HTML,
        reply_markup=nav_kb(),
    )


# ---------------------------------------------------------------------------
# Withdraw: amount → UPI / Binance → details
# ---------------------------------------------------------------------------

@router.message(F.text == "Withdraw")
@router.callback_query(F.data == "menu:withdraw")
async def withdraw_start(event: Message | CallbackQuery, state: FSMContext, bot: Bot):
    user_obj = event.from_user
    chat_id = event.message.chat.id if isinstance(event, CallbackQuery) else event.chat.id
    if isinstance(event, CallbackQuery):
        await event.answer()
    await typing(bot, chat_id)

    async with get_db_context() as session:
        wd_on = await settings_service.get_setting_bool(
            session, "withdrawals_enabled", True
        )
        if not wd_on:
            text = "Withdrawals are temporarily disabled. Please try again later."
            if isinstance(event, CallbackQuery):
                await event.message.answer(text)
            else:
                await event.answer(text)
            return
        user = await user_service.get_or_create_user(
            session,
            telegram_id=user_obj.id,
            username=user_obj.username,
            first_name=user_obj.first_name,
            last_name=user_obj.last_name,
        )
        min_c = await settings_service.get_setting_int(
            session, "min_withdrawal_cents", settings.min_withdrawal_cents
        )
        max_c = await settings_service.get_setting_int(
            session, "max_withdrawal_cents", settings.max_withdrawal_cents
        )
        text = (
            f"<b>Withdraw funds</b>\n\n"
            f"Available: <b>{cents_to_usd(user.balance_cents)}</b>\n"
            f"Minimum: {cents_to_usd(min_c)}\n"
            f"Maximum: {cents_to_usd(max_c)}\n\n"
            f"Send the amount in USD (example: <code>5.00</code>).\n"
            f"Or tap <b>Cancel</b> below to exit."
        )
        if isinstance(event, CallbackQuery):
            await event.message.answer(
                text, parse_mode=ParseMode.HTML, reply_markup=withdraw_cancel_kb()
            )
        else:
            await event.answer(
                text, parse_mode=ParseMode.HTML, reply_markup=withdraw_cancel_kb()
            )
    await state.set_state(WithdrawStates.waiting_amount)


@router.message(WithdrawStates.waiting_amount)
async def withdraw_amount(message: Message, state: FSMContext, bot: Bot):
    raw = (message.text or "").strip().replace("$", "")
    try:
        amount = float(raw)
        if amount <= 0:
            raise ValueError
    except ValueError:
        await message.answer(
            "Invalid amount. Send a number like <code>5.00</code>.",
            parse_mode=ParseMode.HTML,
        )
        return
    amount_cents = int(round(amount * 100))
    await state.update_data(amount_cents=amount_cents)
    await typing(bot, message.chat.id)
    await message.answer(
        f"<b>Payout method</b>\n\n"
        f"Amount: <b>{cents_to_usd(amount_cents)}</b>\n\n"
        f"How would you like to receive payment?\n"
        f"An admin will pay you manually after review.",
        parse_mode=ParseMode.HTML,
        reply_markup=payout_method_kb(),
    )
    await state.set_state(WithdrawStates.waiting_payout_details)


@router.callback_query(F.data.startswith("payout:"))
async def payout_method_cb(callback: CallbackQuery, state: FSMContext, bot: Bot):
    action = callback.data.split(":", 1)[1]
    await callback.answer()
    if action == "cancel":
        await state.clear()
        await callback.message.answer(
            "Withdrawal cancelled.",
            reply_markup=main_menu_kb(),
        )
        return
    if action not in ("upi", "binance"):
        return
    data = await state.get_data()
    if not data.get("amount_cents"):
        await state.clear()
        await callback.message.answer("Session expired. Start again with Withdraw.")
        return
    await state.update_data(payout_method=action.upper())
    prompt = (
        "Send your <b>UPI ID</b> (example: <code>name@upi</code>)."
        if action == "upi"
        else "Send your <b>Binance ID</b> / email / UID used for receiving funds."
    )
    await typing(bot, callback.message.chat.id)
    await callback.message.answer(
        prompt + "\n\nOr tap <b>Cancel</b> below to exit.",
        parse_mode=ParseMode.HTML,
        reply_markup=withdraw_cancel_kb(),
    )
    await state.set_state(WithdrawStates.waiting_payout_details)


@router.message(WithdrawStates.waiting_payout_details)
async def withdraw_payout_details(message: Message, state: FSMContext, bot: Bot):
    details = (message.text or "").strip()
    if len(details) < 3:
        await message.answer("Please send a valid UPI ID or Binance ID.")
        return
    data = await state.get_data()
    amount_cents = data.get("amount_cents")
    method = data.get("payout_method", "UPI")
    if not amount_cents:
        await state.clear()
        await message.answer("Session expired. Use Withdraw from the menu.")
        return

    await typing(bot, message.chat.id, 0.15)
    async with get_db_context() as session:
        user = await user_service.get_or_create_user(
            session,
            telegram_id=message.from_user.id,
            username=message.from_user.username,
            first_name=message.from_user.first_name,
            last_name=message.from_user.last_name,
        )
        try:
            w = await create_withdrawal(
                session,
                user.id,
                amount_cents,
                payout_method=method,
                payout_details=details[:255],
            )
            await message.answer(
                f"<b>Withdrawal submitted</b>\n\n"
                f"Amount: <b>{cents_to_usd(w.amount_cents)}</b>\n"
                f"Method: <b>{method}</b>\n"
                f"Details: <code>{details[:80]}</code>\n"
                f"Status: <b>Pending</b>\n\n"
                f"An admin will process this manually and mark it paid when complete.",
                parse_mode=ParseMode.HTML,
                reply_markup=main_menu_kb(),
            )
            # Notify admin panel via push event store is handled on API poll; optional TG to owner
            try:
                if settings.owner_telegram_id:
                    await bot.send_message(
                        settings.owner_telegram_id,
                        f"New withdrawal #{w.id}\n"
                        f"User: {message.from_user.id}\n"
                        f"Amount: {cents_to_usd(w.amount_cents)}\n"
                        f"{method}: {details[:80]}",
                    )
            except Exception:
                pass
        except ValueError as e:
            err = str(e)
            if err.startswith("MINIMUM"):
                await message.answer("Amount is below the minimum withdrawal.")
            elif err.startswith("MAXIMUM"):
                await message.answer("Amount exceeds the maximum withdrawal.")
            elif err == "INSUFFICIENT_BALANCE":
                await message.answer("Insufficient balance.")
            else:
                await message.answer("Could not create withdrawal. Please try again.")
            await message.answer("Use the menu to continue.", reply_markup=main_menu_kb())
    await state.clear()


# ---------------------------------------------------------------------------
# Support
# ---------------------------------------------------------------------------

@router.message(F.text == "Support")
@router.message(Command("support"))
async def support_start(message: Message, state: FSMContext, bot: Bot):
    await typing(bot, message.chat.id)
    await state.set_state(SupportStates.waiting_message)
    await message.answer(
        "<b>Support</b>\n\n"
        "Describe your issue in one message. An admin will reply here.",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Cancel",
                        callback_data="support:cancel",
                        style="danger",
                    )
                ]
            ]
        ),
    )


@router.callback_query(F.data == "support:cancel")
async def support_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer("Cancelled")
    await callback.message.answer("Support cancelled.", reply_markup=main_menu_kb())


@router.message(SupportStates.waiting_message)
async def support_message(message: Message, state: FSMContext, bot: Bot):
    from app.models import SupportTicket, TicketStatus

    text = (message.text or "").strip()
    if not text or len(text) < 3:
        await message.answer("Please send a longer message.")
        return
    await typing(bot, message.chat.id)
    async with get_db_context() as session:
        user = await user_service.get_or_create_user(
            session,
            telegram_id=message.from_user.id,
            username=message.from_user.username,
            first_name=message.from_user.first_name,
            last_name=message.from_user.last_name,
        )
        ticket = SupportTicket(
            user_id=user.id,
            subject=text[:80],
            message=text[:4000],
            status=TicketStatus.OPEN,
        )
        session.add(ticket)
        await session.commit()
        tid = ticket.id
    await state.clear()
    await message.answer(
        f"<b>Ticket #{tid} created</b>\n\nWe will reply in this chat.",
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu_kb(),
    )


# ---------------------------------------------------------------------------
# Claim flow
# ---------------------------------------------------------------------------

async def _send_claim_preview(target, bot: Bot, code: str) -> None:
    from app.services.qr_image_service import claim_payload, generate_qr_png_bytes

    code = code.upper().strip()
    chat_id = target.chat.id if hasattr(target, "chat") else target.message.chat.id
    await uploading_photo(bot, chat_id)

    async with get_db_context() as session:
        qr = await qr_service.get_qr_by_code(session, code)
        if not qr:
            await target.answer("Invalid or unknown QR code.")
            return
        if qr.status.value in ("CLAIMED", "PENDING", "APPROVED"):
            await _send_already_claimed(target, bot, code, session=session, qr=qr)
            return
        if qr.status.value in ("DISABLED", "EXPIRED", "REJECTED"):
            await target.answer(
                f"This QR is not available ({qr.status.value}).",
                reply_markup=nav_kb(),
            )
            return

        reward = cents_to_usd(qr.reward_cents)
        caption = (
            f"<b>Your QR reward</b>\n\n"
            f"Code: <code>{qr.code}</code>\n"
            f"Reward: <b>{reward}</b>\n"
            f"Status: {qr.status.value}\n\n"
            f"Tap <b>Submit Claim</b> to send this for admin approval."
        )
        if qr.image_png:
            png = qr.image_png
        else:
            payload = claim_payload(qr.code, settings.bot_username or None)
            png = generate_qr_png_bytes(payload)
        photo = BufferedInputFile(png, filename=f"{qr.code}.png")

        try:
            if hasattr(target, "answer_photo"):
                await target.answer_photo(
                    photo=photo,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    reply_markup=submit_claim_button(qr.code),
                )
            else:
                await bot.send_photo(
                    chat_id=chat_id,
                    photo=photo,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    reply_markup=submit_claim_button(qr.code),
                )
        except Exception:
            await target.answer(
                caption,
                parse_mode=ParseMode.HTML,
                reply_markup=submit_claim_button(qr.code),
            )


@router.callback_query(F.data.startswith("claim:"))
async def claim_callback(callback: CallbackQuery, bot: Bot):
    code = callback.data.split(":", 1)[1]
    user = callback.from_user
    await callback.answer()
    await typing(bot, callback.message.chat.id)

    async with get_db_context() as session:
        db_user = await user_service.get_or_create_user(
            session,
            telegram_id=user.id,
            username=user.username,
            first_name=user.first_name,
            last_name=user.last_name,
        )
        if db_user.status != UserStatus.ACTIVE:
            await callback.message.answer("Your account is disabled.")
            return

        maint = await settings_service.get_setting_bool(
            session, "maintenance_mode", False
        )
        if maint:
            await callback.message.answer(
                "The bot is under maintenance. Please try later."
            )
            return
        claims_on = await settings_service.get_setting_bool(
            session, "claims_enabled", True
        )
        if not claims_on:
            await callback.message.answer("Claims are temporarily disabled.")
            return

        force_enabled = await force_join_service.is_force_join_enabled(session)
        if force_enabled:
            channels = await force_join_service.get_active_required_channels(session)
            if channels:
                all_ok, missing = await force_join_service.check_membership(
                    bot, user.id, channels
                )
                if not all_ok:
                    await callback.message.answer(
                        "<b>Join required channels</b>\n\n"
                        "Please join the channels below, then tap "
                        "<b>I Joined – Check Again</b>.",
                        parse_mode=ParseMode.HTML,
                        reply_markup=force_join_kb(missing),
                    )
                    return

    await callback.message.answer("Preparing your QR…")
    await _send_claim_preview(callback.message, bot, code)


@router.callback_query(F.data.startswith("submit_claim:"))
async def submit_claim_callback(callback: CallbackQuery, bot: Bot):
    code = callback.data.split(":", 1)[1]
    user = callback.from_user
    await callback.answer("Submitting…")
    await typing(bot, callback.message.chat.id, 0.15)

    async with get_db_context() as session:
        db_user = await user_service.get_or_create_user(
            session,
            telegram_id=user.id,
            username=user.username,
            first_name=user.first_name,
            last_name=user.last_name,
        )
        if db_user.status != UserStatus.ACTIVE:
            await callback.message.answer("Your account is disabled.")
            return

        force_enabled = await force_join_service.is_force_join_enabled(session)
        if force_enabled:
            channels = await force_join_service.get_active_required_channels(session)
            if channels:
                all_ok, missing = await force_join_service.check_membership(
                    bot, user.id, channels
                )
                if not all_ok:
                    await callback.message.answer(
                        "Please join all required channels first.",
                        reply_markup=force_join_kb(missing),
                    )
                    return

        try:
            claim, qr = await qr_service.claim_qr_atomic(
                session, code=code, user_id=db_user.id
            )
            from app.services import claim_service as _claim_svc

            auto = await _claim_svc.try_auto_approve(session, claim.id)
            if auto:
                claim = auto

            if claim.status.value == "APPROVED":
                text = (
                    f"<b>Claim approved</b>\n\n"
                    f"QR: <code>{qr.code}</code>\n"
                    f"Reward: <b>{cents_to_usd(qr.reward_cents)}</b>\n"
                    f"Status: Approved\n\n"
                    f"The reward has been credited to your wallet."
                )
            else:
                text = (
                    f"<b>Claim submitted</b>\n\n"
                    f"QR: <code>{qr.code}</code>\n"
                    f"Reward: <b>{cents_to_usd(qr.reward_cents)}</b>\n"
                    f"Status: Pending approval\n\n"
                    f"You will be notified when an admin reviews this claim."
                )
            try:
                await callback.message.edit_caption(
                    caption=text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=after_claim_kb(),
                )
            except Exception:
                await callback.message.answer(
                    text, parse_mode=ParseMode.HTML, reply_markup=after_claim_kb()
                )
            # Alert owner
            try:
                if settings.owner_telegram_id:
                    await bot.send_message(
                        settings.owner_telegram_id,
                        f"New claim #{claim.id}\n"
                        f"QR: {qr.code}\n"
                        f"User: {user.id} (@{user.username or '—'})\n"
                        f"Reward: {cents_to_usd(qr.reward_cents)}\n"
                        f"Status: {claim.status.value}",
                    )
            except Exception:
                pass
        except ValueError as e:
            err = str(e)
            if err == "QR_ALREADY_CLAIMED":
                await _send_already_claimed(callback.message, bot, code)
            elif err == "QR_NOT_FOUND":
                await callback.message.answer("Invalid or unknown QR code.")
            elif err == "QR_EXPIRED":
                await callback.message.answer(
                    "This QR has expired.", reply_markup=nav_kb()
                )
            elif err.startswith("QR_"):
                await callback.message.answer(
                    f"This QR is not available ({err.replace('QR_', '')})."
                )
            else:
                logger.exception("Claim error: %s", e)
                await callback.message.answer(
                    "Something went wrong. Please try again later."
                )


@router.callback_query(F.data == "cancel_claim")
async def cancel_claim_callback(callback: CallbackQuery):
    await callback.answer("Cancelled")
    try:
        await callback.message.edit_caption(
            caption="Claim cancelled.",
            reply_markup=nav_kb(),
        )
    except Exception:
        await callback.message.answer("Claim cancelled.", reply_markup=main_menu_kb())


@router.callback_query(F.data == "check_membership")
async def check_membership_cb(callback: CallbackQuery, bot: Bot):
    await callback.answer("Checking…")
    await typing(bot, callback.message.chat.id)
    user = callback.from_user
    async with get_db_context() as session:
        channels = await force_join_service.get_active_required_channels(session)
        all_ok, missing = await force_join_service.check_membership(
            bot, user.id, channels
        )
    if all_ok:
        await callback.message.answer(
            "<b>Membership verified</b>\n\n"
            "You can now claim QR rewards when they are sent to you.",
            parse_mode=ParseMode.HTML,
            reply_markup=nav_kb(),
        )
    else:
        await callback.message.answer(
            "You still need to join the channels below:",
            reply_markup=force_join_kb(missing),
        )


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

def get_bot() -> Optional[Bot]:
    return _bot


async def start_bot() -> None:
    global _bot, _dp, _polling_task
    if _bot is not None:
        logger.warning("Bot already started")
        return

    token = settings.bot_token
    _bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    _dp = Dispatcher(storage=MemoryStorage())
    _dp.include_router(router)
    set_bot(_bot)

    me = await _bot.get_me()
    logger.info("BOT AUTH OK: @%s", me.username)

    try:
        await _bot.delete_webhook(drop_pending_updates=False)
        logger.info("WEBHOOK CLEARED - STARTING POLLING")
    except Exception as exc:
        logger.warning("Webhook clear failed: %s", exc)

    async def _run():
        try:
            await _dp.start_polling(_bot, handle_signals=False)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Polling crashed")

    _polling_task = asyncio.create_task(_run())


async def stop_bot() -> None:
    global _bot, _dp, _polling_task
    if _polling_task:
        _polling_task.cancel()
        try:
            await _polling_task
        except asyncio.CancelledError:
            pass
        _polling_task = None
    if _dp:
        await _dp.storage.close()
        _dp = None
    if _bot:
        await _bot.session.close()
        _bot = None
    logger.info("Bot stopped")
