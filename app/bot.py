"""Telegram bot using aiogram 3.x – single polling instance."""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from aiogram.client.default import DefaultBotProperties

from app.config import get_settings
from app.db import get_db_context
from app.models import ClaimStatus, QRStatus, UserStatus
from app.services import force_join_service, qr_service, settings_service, user_service
from app.services.notification_service import set_bot
from app.services.wallet_service import get_transactions
from app.services.withdrawal_service import create_withdrawal

logger = logging.getLogger(__name__)
settings = get_settings()

router = Router()

# ---------------------------------------------------------------------------
# Keyboards
# ---------------------------------------------------------------------------

def main_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="🎟 How to Claim"),
                KeyboardButton(text="💰 Balance"),
            ],
            [
                KeyboardButton(text="💳 Wallet"),
                KeyboardButton(text="📜 Transaction History"),
            ],
            [
                KeyboardButton(text="💸 Withdraw"),
                KeyboardButton(text="📊 My Claims"),
            ],
            [KeyboardButton(text="❓ Help")],
        ],
        resize_keyboard=True,
    )


def force_join_kb(channels) -> InlineKeyboardMarkup:
    buttons = []
    for ch in channels:
        url = ch.invite_url
        if not url and ch.username:
            url = f"https://t.me/{ch.username.lstrip('@')}"
        if url:
            buttons.append(
                [InlineKeyboardButton(text=f"📢 Join {ch.title}", url=url)]
            )
    buttons.append(
        [InlineKeyboardButton(text="✅ Check Membership", callback_data="check_membership")]
    )
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def claim_button(code: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🎁 CLAIM REWARD",
                    callback_data=f"claim:{code}",
                )
            ]
        ]
    )


# ---------------------------------------------------------------------------
# FSM
# ---------------------------------------------------------------------------

class WithdrawStates(StatesGroup):
    waiting_amount = State()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def ensure_user(message_or_cb) -> Optional[object]:
    """Register / update user from Telegram update."""
    user = message_or_cb.from_user
    if not user:
        return None
    async with get_db_context() as session:
        db_user = await user_service.get_or_create_user(
            session,
            telegram_id=user.id,
            username=user.username,
            first_name=user.first_name,
            last_name=user.last_name,
        )
        if db_user.status != UserStatus.ACTIVE:
            return None
        return db_user


def cents_to_usd(cents: int) -> str:
    return f"${cents / 100:.2f}"


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

@router.message(CommandStart())
async def cmd_start(message: Message):
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

    await message.answer(
        welcome,
        reply_markup=main_menu_kb(),
    )


@router.message(F.text == "🎟 How to Claim")
async def how_to_claim(message: Message):
    await message.answer(
        "🎟 <b>How to Claim a QR Reward</b>\n\n"
        "1. Wait for a QR post in the channel or receive a QR code.\n"
        "2. Tap the <b>🎁 CLAIM REWARD</b> button under the post.\n"
        "3. Join all required channels if prompted.\n"
        "4. Your claim is submitted for admin approval.\n"
        "5. Once approved, the reward is added to your wallet.\n\n"
        "Each QR can be claimed by only <b>one</b> user.",
        parse_mode=ParseMode.HTML,
    )


@router.message(F.text == "💰 Balance")
@router.message(F.text == "💳 Wallet")
async def wallet_info(message: Message):
    async with get_db_context() as session:
        user = await user_service.get_or_create_user(
            session,
            telegram_id=message.from_user.id,
            username=message.from_user.username,
            first_name=message.from_user.first_name,
            last_name=message.from_user.last_name,
        )
        if user.status != UserStatus.ACTIVE:
            await message.answer("Your account is disabled.")
            return

        # Pending = sum of pending claims
        from sqlalchemy import func, select
        from app.models import QRClaim

        pending = (
            await session.execute(
                select(func.coalesce(func.sum(QRClaim.reward_cents), 0)).where(
                    QRClaim.user_id == user.id,
                    QRClaim.status == ClaimStatus.PENDING,
                )
            )
        ).scalar() or 0

    await message.answer(
        f"💰 <b>Wallet Balance</b>\n\n"
        f"Available Balance:\n{cents_to_usd(user.balance_cents)}\n\n"
        f"Pending:\n{cents_to_usd(int(pending))}\n\n"
        f"Total Earned:\n{cents_to_usd(user.total_earned_cents)}",
        parse_mode=ParseMode.HTML,
    )


@router.message(F.text == "📜 Transaction History")
async def tx_history(message: Message):
    async with get_db_context() as session:
        user = await user_service.get_user_by_telegram_id(
            session, message.from_user.id
        )
        if not user:
            await message.answer("Please /start first.")
            return
        txs, total = await get_transactions(session, user.id, limit=10)

    if not txs:
        await message.answer("No transactions yet.")
        return

    lines = [f"📜 <b>Recent Transactions</b> (showing {len(txs)} of {total})\n"]
    for t in txs:
        sign = "+" if t.amount_cents > 0 else ""
        lines.append(
            f"• {t.type.value}: {sign}{cents_to_usd(t.amount_cents)} "
            f"→ {cents_to_usd(t.balance_after_cents)}\n"
            f"  {t.created_at.strftime('%Y-%m-%d %H:%M')}"
        )
    await message.answer("\n".join(lines), parse_mode=ParseMode.HTML)


@router.message(F.text == "📊 My Claims")
async def my_claims(message: Message):
    async with get_db_context() as session:
        user = await user_service.get_user_by_telegram_id(
            session, message.from_user.id
        )
        if not user:
            await message.answer("Please /start first.")
            return
        from sqlalchemy import select
        from app.models import QRClaim
        from sqlalchemy.orm import selectinload

        result = await session.execute(
            select(QRClaim)
            .options(selectinload(QRClaim.qr_code))
            .where(QRClaim.user_id == user.id)
            .order_by(QRClaim.created_at.desc())
            .limit(15)
        )
        claims = result.scalars().all()

    if not claims:
        await message.answer("You have no claims yet.")
        return

    lines = ["📊 <b>My Claims</b>\n"]
    for c in claims:
        code = c.qr_code.code if c.qr_code else "?"
        lines.append(
            f"• {code} — {cents_to_usd(c.reward_cents)} — "
            f"<b>{c.status.value}</b>\n"
            f"  {c.created_at.strftime('%Y-%m-%d %H:%M')}"
        )
    await message.answer("\n".join(lines), parse_mode=ParseMode.HTML)


@router.message(F.text == "❓ Help")
async def help_cmd(message: Message):
    await message.answer(
        "❓ <b>Help</b>\n\n"
        "• Use the menu buttons to navigate.\n"
        "• Claim QR rewards from channel posts.\n"
        "• Check Balance / Wallet for your funds.\n"
        "• Request withdrawals when you reach the minimum.\n"
        "• Contact support via the channel admins if needed.\n\n"
        "Each QR is single-use and requires admin approval.",
        parse_mode=ParseMode.HTML,
    )


@router.message(F.text == "💸 Withdraw")
async def withdraw_start(message: Message, state: FSMContext):
    async with get_db_context() as session:
        user = await user_service.get_user_by_telegram_id(
            session, message.from_user.id
        )
        if not user or user.status != UserStatus.ACTIVE:
            await message.answer("Account not available.")
            return
        cfg = await settings_service.get_all_settings(session)
        min_c = cfg["min_withdrawal_cents"]
        max_c = cfg["max_withdrawal_cents"]

    await state.set_state(WithdrawStates.waiting_amount)
    await message.answer(
        f"💸 <b>Withdraw</b>\n\n"
        f"Available: {cents_to_usd(user.balance_cents)}\n"
        f"Min: {cents_to_usd(min_c)} | Max: {cents_to_usd(max_c)}\n\n"
        f"Enter the amount in USD (e.g. 10.00):",
        parse_mode=ParseMode.HTML,
        reply_markup=ReplyKeyboardRemove(),
    )


@router.message(WithdrawStates.waiting_amount)
async def withdraw_amount(message: Message, state: FSMContext):
    text = (message.text or "").strip().replace("$", "").replace(",", "")
    try:
        amount = float(text)
        if amount <= 0:
            raise ValueError
        amount_cents = int(round(amount * 100))
    except ValueError:
        await message.answer("Invalid amount. Enter a positive number like 10.00")
        return

    async with get_db_context() as session:
        user = await user_service.get_user_by_telegram_id(
            session, message.from_user.id
        )
        if not user:
            await message.answer("Please /start first.")
            await state.clear()
            return
        try:
            w = await create_withdrawal(
                session, user=user, amount_cents=amount_cents
            )
            await message.answer(
                f"💸 <b>Withdrawal Request Submitted</b>\n\n"
                f"Amount: {cents_to_usd(w.amount_cents)}\n"
                f"Status: Pending\n\n"
                f"An admin will process it manually.",
                parse_mode=ParseMode.HTML,
                reply_markup=main_menu_kb(),
            )
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
# Claim callback
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("claim:"))
async def claim_callback(callback: CallbackQuery, bot: Bot):
    code = callback.data.split(":", 1)[1]
    user = callback.from_user
    await callback.answer()

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

        # Maintenance check
        maint = await settings_service.get_setting_bool(
            session, "maintenance_mode", False
        )
        if maint:
            await callback.message.answer(
                "🛠 The bot is under maintenance. Please try later."
            )
            return

        # Force join
        force_enabled = await force_join_service.is_force_join_enabled(session)
        if force_enabled:
            channels = await force_join_service.get_active_required_channels(session)
            if channels:
                all_ok, missing = await force_join_service.check_membership(
                    bot, user.id, channels
                )
                if not all_ok:
                    await callback.message.answer(
                        "📢 Please join all required channels first.",
                        reply_markup=force_join_kb(missing),
                    )
                    # Store pending claim code in a simple way – user will re-click
                    return

        # Atomic claim
        try:
            claim, qr = await qr_service.claim_qr_atomic(
                session, code=code, user_id=db_user.id
            )
            await callback.message.answer(
                f"⏳ <b>Claim Submitted</b>\n\n"
                f"🎟 QR: {qr.code}\n"
                f"💰 Reward: {cents_to_usd(qr.reward_cents)}\n"
                f"📌 Status: Pending Approval\n\n"
                f"Your claim is waiting for admin approval.",
                parse_mode=ParseMode.HTML,
            )
        except ValueError as e:
            err = str(e)
            if err == "QR_ALREADY_CLAIMED":
                await callback.message.answer(
                    "❌ <b>QR Already Claimed</b>\n\n"
                    "This QR has already been successfully claimed by another user.",
                    parse_mode=ParseMode.HTML,
                )
            elif err == "QR_NOT_FOUND":
                await callback.message.answer("❌ Invalid or unknown QR code.")
            elif err == "QR_EXPIRED":
                await callback.message.answer("❌ This QR has expired.")
            elif err.startswith("QR_"):
                await callback.message.answer(
                    f"❌ This QR is not available ({err.replace('QR_', '')})."
                )
            else:
                logger.exception("Claim error: %s", e)
                await callback.message.answer(
                    "❌ Something went wrong. Please try again later."
                )


@router.callback_query(F.data == "check_membership")
async def check_membership_cb(callback: CallbackQuery, bot: Bot):
    await callback.answer("Checking…")
    user = callback.from_user
    async with get_db_context() as session:
        channels = await force_join_service.get_active_required_channels(session)
        all_ok, missing = await force_join_service.check_membership(
            bot, user.id, channels
        )
    if all_ok:
        await callback.message.answer(
            "✅ You have joined all required channels!\n"
            "You can now claim QR rewards."
        )
    else:
        await callback.message.answer(
            "📢 You still need to join the following channels:",
            reply_markup=force_join_kb(missing),
        )


# ---------------------------------------------------------------------------
# Bot lifecycle – SINGLE polling instance
# ---------------------------------------------------------------------------

_bot: Optional[Bot] = None
_dp: Optional[Dispatcher] = None
_polling_task: Optional[asyncio.Task] = None


async def start_bot() -> None:
    """Start the Telegram bot with exactly one polling process."""
    global _bot, _dp, _polling_task

    if _bot is not None:
        logger.warning("Bot already started – skipping duplicate start")
        return

    logger.info("Starting Telegram bot...")
    _bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    set_bot(_bot)

    try:
        me = await _bot.get_me()
        logger.info("BOT AUTH OK: @%s (id=%s)", me.username, me.id)
        if settings.bot_username and me.username != settings.bot_username.lstrip("@"):
            logger.warning(
                "BOT_USERNAME env (%s) differs from actual @%s",
                settings.bot_username,
                me.username,
            )
    except Exception as exc:
        logger.exception("Bot authentication failed: %s", exc)
        raise

    # Clear any existing webhook so polling can start cleanly
    try:
        await _bot.delete_webhook(drop_pending_updates=False)
        logger.info("WEBHOOK CLEARED - STARTING POLLING")
    except Exception as exc:
        logger.warning("Could not delete webhook: %s", exc)

    storage = MemoryStorage()
    _dp = Dispatcher(storage=storage)
    _dp.include_router(router)

    async def _run_polling():
        try:
            await _dp.start_polling(
                _bot,
                allowed_updates=["message", "callback_query"],
                handle_signals=False,
            )
        except Exception as exc:
            # Conflict (another instance) or network errors
            logger.exception("Polling stopped with error: %s", exc)
            raise

    _polling_task = asyncio.create_task(_run_polling())
    logger.info("Telegram bot polling started")


async def stop_bot() -> None:
    """Gracefully stop the bot."""
    global _bot, _dp, _polling_task
    logger.info("Stopping Telegram bot...")
    if _dp:
        await _dp.stop_polling()
    if _polling_task and not _polling_task.done():
        _polling_task.cancel()
        try:
            await _polling_task
        except asyncio.CancelledError:
            pass
    if _bot:
        await _bot.session.close()
    _bot = None
    _dp = None
    _polling_task = None
    logger.info("Telegram bot stopped")


def get_bot() -> Optional[Bot]:
    return _bot
