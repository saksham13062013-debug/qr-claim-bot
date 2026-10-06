import asyncio
import logging
from datetime import datetime, timezone

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from sqlalchemy import select

from .config import settings
from .db import SessionLocal
from .models import User, ForceJoinChannel, QRCode, QRClaim, Notification

logger = logging.getLogger("qr-claim-bot")

bot = Bot(settings.bot_token)
dp = Dispatcher()

async def is_joined(user_id: int) -> bool:
    if not settings.force_join_enabled:
        return True

    async with SessionLocal() as s:
        channels = (await s.scalars(
            select(ForceJoinChannel).where(
                ForceJoinChannel.status == "ACTIVE",
                ForceJoinChannel.required == True,
            )
        )).all()

    for c in channels:
        try:
            member = await bot.get_chat_member(c.channel_id, user_id)
            if member.status not in ("member", "administrator", "creator"):
                return False
        except Exception:
            logger.exception("Membership check failed for channel %s", c.channel_id)
            return False
    return True

async def join_keyboard():
    async with SessionLocal() as s:
        channels = (await s.scalars(
            select(ForceJoinChannel).where(
                ForceJoinChannel.status == "ACTIVE",
                ForceJoinChannel.required == True,
            )
        )).all()

    rows = []
    for c in channels:
        if c.username:
            url = f"https://t.me/{c.username.lstrip('@')}"
        else:
            url = f"https://t.me/c/{str(c.channel_id).replace('-100', '')}"
        rows.append([InlineKeyboardButton(
            text=f"📢 Join {c.title or c.username or 'Channel'}",
            url=url,
        )])

    rows.append([InlineKeyboardButton(
        text="✅ Check Membership",
        callback_data="check_membership",
    )])
    return InlineKeyboardMarkup(inline_keyboard=rows)

async def claim_qr(telegram_id: int, username: str | None, code: str):
    """Atomically claim one QR directly in PostgreSQL."""
    async with SessionLocal() as s:
        async with s.begin():
            user = await s.scalar(select(User).where(User.telegram_id == telegram_id))
            if not user:
                user = User(
                    telegram_id=telegram_id,
                    username=username,
                )
                s.add(user)
                await s.flush()
            else:
                user.username = username

            qr = await s.scalar(
                select(QRCode).where(QRCode.code == code).with_for_update()
            )

            if not qr or qr.status not in ("AVAILABLE", "POSTED", "RECLAIMED"):
                return None, "QR_UNAVAILABLE"

            if qr.expires_at and qr.expires_at < datetime.now(timezone.utc):
                qr.status = "EXPIRED"
                return None, "QR_EXPIRED"

            qr.status = "PENDING"
            claim = QRClaim(
                qr_id=qr.id,
                user_id=user.id,
                reward_cents=qr.reward_cents,
                status="PENDING",
            )
            s.add(claim)
            s.add(Notification(
                user_id=user.id,
                type="CLAIM_PENDING",
                message=f"Your QR claim {qr.code} is pending approval.",
            ))
            await s.flush()
            return claim, None

async def submit_claim(m: Message, code: str):
    if not await is_joined(m.from_user.id):
        await m.answer(
            "📢 Please join all required channels first.",
            reply_markup=await join_keyboard(),
        )
        return

    try:
        claim, error = await claim_qr(
            m.from_user.id,
            m.from_user.username,
            code,
        )

        if error == "QR_EXPIRED":
            await m.answer("⌛ This QR has expired.")
            return
        if error == "QR_UNAVAILABLE":
            await m.answer("❌ QR already claimed or unavailable.")
            return

        await m.answer(
            f"⏳ Claim submitted!\n\n"
            f"🎟 {code}\n"
            f"💰 Reward: pending\n"
            f"📌 Status: PENDING\n\n"
            f"Your claim is waiting for admin approval."
        )
    except Exception:
        logger.exception("Claim failed for QR %s", code)
        await m.answer("❌ Claim failed. Please try again later.")

@dp.message(CommandStart())
async def start(m: Message):
    await m.answer(
        "🎁 Welcome to QR Claim Bot!\n\n"
        "Claim one-time QR rewards, track your balance and request withdrawals.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🎟 How to Claim", callback_data="claim_help")],
            [InlineKeyboardButton(text="💰 Balance", callback_data="balance")],
        ]),
    )

@dp.callback_query(F.data == "claim_help")
async def help_cb(c: CallbackQuery):
    await c.answer()
    await c.message.answer("🎟 Send the QR code, e.g. QR-AB12CD")

@dp.callback_query(F.data == "check_membership")
async def check_cb(c: CallbackQuery):
    await c.answer()
    await c.message.answer(
        "✅ Membership verified. Now send your QR code."
        if await is_joined(c.from_user.id)
        else "❌ You still need to join all required channels."
    )

@dp.callback_query(F.data == "balance")
async def balance_cb(c: CallbackQuery):
    await c.answer()
    async with SessionLocal() as s:
        user = await s.scalar(select(User).where(User.telegram_id == c.from_user.id))
        balance = (user.balance_cents / 100) if user else 0
    await c.message.answer(f"💰 Balance\n\n${balance:.2f}")

@dp.message(F.text.regexp(r"^QR-[A-Za-z0-9]+$"))
async def qr_message(m: Message):
    await m.bot.send_chat_action(m.chat.id, "typing")
    await asyncio.sleep(0.5)
    await submit_claim(m, m.text.strip().upper())

@dp.message()
async def fallback(m: Message):
    if m.text and not m.text.startswith("/"):
        await m.answer("Send a valid QR code like QR-AB12CD.")

async def run_bot():
    logger.info("Starting Telegram bot...")
    try:
        me = await bot.get_me()
        logger.info("BOT AUTH OK: @%s (id=%s)", me.username, me.id)

        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("WEBHOOK CLEARED - STARTING POLLING")

        await dp.start_polling(bot, handle_signals=False)
    except Exception:
        logger.exception("TELEGRAM BOT FAILED")
        raise
    finally:
        await bot.session.close()
