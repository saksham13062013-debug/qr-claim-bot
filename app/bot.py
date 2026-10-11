"""Telegram bot using aiogram 3.x – single polling instance."""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
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


def main_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="How to Claim"),
                KeyboardButton(text="Balance"),
            ],
            [
                KeyboardButton(text="Wallet"),
                KeyboardButton(text="Transaction History"),
            ],
            [
                KeyboardButton(text="Withdraw"),
                KeyboardButton(text="My Claims"),
            ],
            [
                KeyboardButton(text="Help"),
                KeyboardButton(text="Support"),
            ],
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
                [InlineKeyboardButton(text=f"Join {ch.title}", url=url)]
            )
    buttons.append(
        [
            InlineKeyboardButton(
                text="Check Membership", callback_data="check_membership"
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
                    style="primary",
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


class WithdrawStates(StatesGroup):
    waiting_amount = State()


class SupportStates(StatesGroup):
    waiting_message = State()


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

    parts = (message.text or "").split(maxsplit=1)
    payload = parts[1].strip() if len(parts) > 1 else ""
    if payload.startswith("claim_"):
        code = payload[len("claim_") :].strip().upper()
        if code:
            await message.answer(welcome, reply_markup=main_menu_kb())
            await _send_claim_preview(message, bot, code)
            return

    await message.answer(welcome, reply_markup=main_menu_kb())


@router.message(F.text == "How to Claim")
async def how_to_claim(message: Message):
    await message.answer(
        "<b>How to Claim a QR Reward</b>\n\n"
        "1. Wait for a QR post (with QR image) in the channel.\n"
        "2. Tap <b>CLAIM REWARD</b> under the post.\n"
        "3. Join required channels if asked.\n"
        "4. You will receive the <b>QR image</b> — then tap <b>Submit Claim</b>.\n"
        "5. Wait for admin approval — reward goes to your wallet.\n\n"
        "Each QR can be claimed by only <b>one</b> user.",
        parse_mode=ParseMode.HTML,
    )


@router.message(F.text == "Balance")
@router.message(F.text == "Wallet")
async def balance_cmd(message: Message):
    async with get_db_context() as session:
        user = await user_service.get_or_create_user(
            session,
            telegram_id=message.from_user.id,
            username=message.from_user.username,
            first_name=message.from_user.first_name,
            last_name=message.from_user.last_name,
        )
        pending = user.pending_cents
        await message.answer(
            f"<b>Wallet Balance</b>\n\n"
            f"Available Balance:\n{cents_to_usd(user.balance_cents)}\n\n"
            f"Pending:\n{cents_to_usd(int(pending))}\n\n"
            f"Total Earned:\n{cents_to_usd(user.total_earned_cents)}",
            parse_mode=ParseMode.HTML,
        )


@router.message(F.text == "Transaction History")
async def tx_history(message: Message):
    async with get_db_context() as session:
        user = await user_service.get_or_create_user(
            session,
            telegram_id=message.from_user.id,
            username=message.from_user.username,
            first_name=message.from_user.first_name,
            last_name=message.from_user.last_name,
        )
        txs, total = await get_transactions(session, user.id, offset=0, limit=10)
    lines = [f"<b>Recent Transactions</b> (showing {len(txs)} of {total})\n"]
    if not txs:
        lines.append("No transactions yet.")
    for t in txs:
        lines.append(
            f"{t.type.value}: {cents_to_usd(t.amount_cents)} "
            f"-> {cents_to_usd(t.balance_after_cents)}\n"
            f"{t.created_at}"
        )
    await message.answer("\n".join(lines), parse_mode=ParseMode.HTML)


@router.message(F.text == "My Claims")
async def my_claims(message: Message):
    from sqlalchemy import select
    from app.models import QRClaim

    async with get_db_context() as session:
        user = await user_service.get_or_create_user(
            session,
            telegram_id=message.from_user.id,
            username=message.from_user.username,
            first_name=message.from_user.first_name,
            last_name=message.from_user.last_name,
        )
        result = await session.execute(
            select(QRClaim)
            .where(QRClaim.user_id == user.id)
            .order_by(QRClaim.created_at.desc())
            .limit(15)
        )
        claims = result.scalars().all()
    lines = ["<b>My Claims</b>\n"]
    if not claims:
        lines.append("No claims yet.")
    for c in claims:
        lines.append(
            f"#{c.id} {cents_to_usd(c.reward_cents)} — <b>{c.status.value}</b>\n"
            f"{c.created_at}"
        )
    await message.answer("\n".join(lines), parse_mode=ParseMode.HTML)


@router.message(F.text == "Help")
async def help_cmd(message: Message):
    await message.answer(
        "<b>Help</b>\n\n"
        "• Use the menu buttons to navigate.\n"
        "• Claim QR rewards from channel posts.\n"
        "• Check Balance / Wallet for your funds.\n"
        "• Request withdrawals when you reach the minimum.\n"
        "• Contact support via Support if needed.\n",
        parse_mode=ParseMode.HTML,
    )


@router.message(F.text == "Withdraw")
async def withdraw_start(message: Message, state: FSMContext):
    async with get_db_context() as session:
        wd_on = await settings_service.get_setting_bool(
            session, "withdrawals_enabled", True
        )
        if not wd_on:
            await message.answer("Withdrawals are temporarily disabled.")
            return
        user = await user_service.get_or_create_user(
            session,
            telegram_id=message.from_user.id,
            username=message.from_user.username,
            first_name=message.from_user.first_name,
            last_name=message.from_user.last_name,
        )
        min_c = await settings_service.get_setting_int(
            session, "min_withdrawal_cents", settings.min_withdrawal_cents
        )
        max_c = await settings_service.get_setting_int(
            session, "max_withdrawal_cents", settings.max_withdrawal_cents
        )
        await message.answer(
            f"<b>Withdraw</b>\n\n"
            f"Available: {cents_to_usd(user.balance_cents)}\n"
            f"Min: {cents_to_usd(min_c)} | Max: {cents_to_usd(max_c)}\n\n"
            f"Send the amount in USD (e.g. 5.00)",
            parse_mode=ParseMode.HTML,
        )
    await state.set_state(WithdrawStates.waiting_amount)


@router.message(WithdrawStates.waiting_amount)
async def withdraw_amount(message: Message, state: FSMContext):
    raw = (message.text or "").strip().replace("$", "")
    try:
        amount = float(raw)
        if amount <= 0:
            raise ValueError
    except ValueError:
        await message.answer("Invalid amount. Send a number like 5.00")
        return
    amount_cents = int(round(amount * 100))
    async with get_db_context() as session:
        user = await user_service.get_or_create_user(
            session,
            telegram_id=message.from_user.id,
            username=message.from_user.username,
            first_name=message.from_user.first_name,
            last_name=message.from_user.last_name,
        )
        try:
            w = await create_withdrawal(session, user.id, amount_cents)
            await message.answer(
                f"<b>Withdrawal Request Submitted</b>\n\n"
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


async def _send_claim_preview(target, bot: Bot, code: str) -> None:
    from app.services.qr_image_service import claim_payload, generate_qr_png_bytes

    code = code.upper().strip()
    async with get_db_context() as session:
        qr = await qr_service.get_qr_by_code(session, code)
        if not qr:
            await target.answer("Invalid or unknown QR code.")
            return
        if qr.status.value in ("CLAIMED", "PENDING", "APPROVED"):
            await target.answer(
                "<b>QR Already Claimed</b>\n\n"
                "This QR has already been successfully claimed by another user.",
                parse_mode=ParseMode.HTML,
            )
            return
        if qr.status.value in ("DISABLED", "EXPIRED", "REJECTED"):
            await target.answer(f"This QR is not available ({qr.status.value}).")
            return

        reward = cents_to_usd(qr.reward_cents)
        caption = (
            f"<b>Your QR Reward</b>\n\n"
            f"Code: <code>{qr.code}</code>\n"
            f"Reward: <b>{reward}</b>\n"
            f"Status: {qr.status.value}\n\n"
            f"Press <b>Submit Claim</b> below to submit for approval."
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
                    chat_id=target.chat.id,
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
                        "Please join all required channels first.",
                        reply_markup=force_join_kb(missing),
                    )
                    return

    await _send_claim_preview(callback.message, bot, code)


@router.callback_query(F.data.startswith("submit_claim:"))
async def submit_claim_callback(callback: CallbackQuery, bot: Bot):
    code = callback.data.split(":", 1)[1]
    user = callback.from_user
    await callback.answer("Submitting…")

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
                    f"<b>Claim Approved</b>\n\n"
                    f"QR: <code>{qr.code}</code>\n"
                    f"Reward: {cents_to_usd(qr.reward_cents)}\n"
                    f"Status: Approved (auto)\n\n"
                    f"Reward credited to your wallet."
                )
            else:
                text = (
                    f"<b>Claim Submitted</b>\n\n"
                    f"QR: <code>{qr.code}</code>\n"
                    f"Reward: {cents_to_usd(qr.reward_cents)}\n"
                    f"Status: Pending Approval\n\n"
                    f"Your claim is waiting for admin approval."
                )
            try:
                await callback.message.edit_caption(
                    caption=text, parse_mode=ParseMode.HTML, reply_markup=None
                )
            except Exception:
                await callback.message.answer(text, parse_mode=ParseMode.HTML)
        except ValueError as e:
            err = str(e)
            if err == "QR_ALREADY_CLAIMED":
                await callback.message.answer(
                    "<b>QR Already Claimed</b>\n\n"
                    "This QR has already been successfully claimed by another user.",
                    parse_mode=ParseMode.HTML,
                )
            elif err == "QR_NOT_FOUND":
                await callback.message.answer("Invalid or unknown QR code.")
            elif err == "QR_EXPIRED":
                await callback.message.answer("This QR has expired.")
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
            caption="Claim cancelled.", reply_markup=None
        )
    except Exception:
        await callback.message.answer("Claim cancelled.")


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
            "You have joined all required channels!\n"
            "You can now claim QR rewards."
        )
    else:
        await callback.message.answer(
            "You still need to join the following channels:",
            reply_markup=force_join_kb(missing),
        )


@router.message(F.text == "Support")
@router.message(Command("support"))
async def support_start(message: Message, state: FSMContext):
    await state.set_state(SupportStates.waiting_message)
    await message.answer(
        "<b>Support</b>\n\nSend your message in one text (problem / question).",
        parse_mode=ParseMode.HTML,
    )


@router.message(SupportStates.waiting_message)
async def support_message(message: Message, state: FSMContext):
    from app.models import SupportTicket, TicketStatus

    text = (message.text or "").strip()
    if not text or len(text) < 3:
        await message.answer("Please send a longer message.")
        return
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
        f"Ticket #{tid} created. An admin will reply here.",
        reply_markup=main_menu_kb(),
    )


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
