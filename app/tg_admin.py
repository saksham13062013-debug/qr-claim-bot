"""Telegram-side admin panel — owner / DB admins only."""

from __future__ import annotations

import logging
from typing import Optional

from aiogram import Bot, F, Router
from aiogram.enums import ChatAction, ChatType, ParseMode
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import func, select

from app.config import get_settings
from app.db import get_db_context
from app.models import (
    Admin,
    ClaimStatus,
    QRClaim,
    QRCode,
    QRStatus,
    User,
    UserStatus,
    Withdrawal,
    WithdrawalStatus,
)
from app.services import claim_service, withdrawal_service

logger = logging.getLogger(__name__)
router = Router(name="tg_admin")


def _settings():
    return get_settings()


class AdminFSM(StatesGroup):
    broadcast_text = State()
    reject_reason = State()
    reject_claim_id = State()
    upload_qr_id = State()
    upload_qr_photo = State()
    create_qr = State()


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

async def is_tg_admin(telegram_id: int) -> bool:
    try:
        tid = int(telegram_id)
    except (TypeError, ValueError):
        return False
    try:
        owner = int(_settings().owner_telegram_id) if _settings().owner_telegram_id else 0
    except (TypeError, ValueError):
        owner = 0
    if owner and tid == owner:
        return True
    try:
        async with get_db_context() as session:
            row = (
                await session.execute(
                    select(Admin.id).where(
                        Admin.is_active.is_(True),
                        Admin.telegram_id == tid,
                    )
                )
            ).scalar_one_or_none()
            return row is not None
    except Exception:
        logger.exception("is_tg_admin db error")
        return bool(owner and tid == owner)


async def require_tg_admin(message: Message) -> bool:
    if not message.from_user:
        return False
    if message.chat.type != ChatType.PRIVATE:
        await message.answer("Admin panel: open in private chat with the bot.")
        return False
    tid = message.from_user.id
    if not await is_tg_admin(tid):
        owner = getattr(_settings(), "owner_telegram_id", None)
        await message.answer(
            "<b>Access denied</b>\n\n"
            f"Your Telegram ID: <code>{tid}</code>\n"
            f"OWNER_TELEGRAM_ID: <code>{owner}</code>\n\n"
            "Set Railway variable <code>OWNER_TELEGRAM_ID</code> to your ID,\n"
            "or set this Telegram ID on an Admin in the web panel.",
            parse_mode=ParseMode.HTML,
        )
        return False
    return True


async def act(bot: Bot, chat_id: int, action: ChatAction = ChatAction.TYPING) -> None:
    try:
        await bot.send_chat_action(chat_id, action)
    except Exception:
        pass


def admin_home_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Dashboard", callback_data="tga:dash", style="primary"),
                InlineKeyboardButton(text="Claims", callback_data="tga:claims", style="danger"),
            ],
            [
                InlineKeyboardButton(text="Withdrawals", callback_data="tga:wd", style="success"),
                InlineKeyboardButton(text="QR codes", callback_data="tga:qrs", style="primary"),
            ],
            [
                InlineKeyboardButton(text="Users", callback_data="tga:users", style="primary"),
                InlineKeyboardButton(text="Tickets", callback_data="tga:tickets", style="primary"),
            ],
            [
                InlineKeyboardButton(text="Broadcast", callback_data="tga:bc", style="success"),
                InlineKeyboardButton(text="Upload QR", callback_data="tga:upqr", style="success"),
            ],
            [
                InlineKeyboardButton(text="Create QR", callback_data="tga:cqr", style="success"),
                InlineKeyboardButton(text="Channels", callback_data="tga:channels", style="primary"),
            ],
            [
                InlineKeyboardButton(text="Settings", callback_data="tga:settings", style="primary"),
                InlineKeyboardButton(text="Del broadcast", callback_data="tga:delbc", style="danger"),
            ],
            [
                InlineKeyboardButton(text="Close", callback_data="tga:close", style="danger"),
            ],
        ]
    )


def back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="« Menu", callback_data="tga:home", style="primary"),
                InlineKeyboardButton(text="Close", callback_data="tga:close", style="danger"),
            ]
        ]
    )


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------

@router.message(Command("admin"))
@router.message(Command("panel"))
@router.message(Command("ap"))
@router.message(F.text.regexp(r"(?i)^/(admin|panel|ap)(@\w+)?\s*$"))
async def cmd_admin(message: Message, bot: Bot, state: FSMContext):
    logger.info(
        "/admin from uid=%s chat=%s type=%s",
        getattr(message.from_user, "id", None),
        message.chat.id,
        getattr(message.chat, "type", None),
    )
    if not await require_tg_admin(message):
        return
    await state.clear()
    await act(bot, message.chat.id)
    try:
        await message.answer(
            "<b>Admin Panel</b>\n"
            "Web-equivalent controls in Telegram.\n"
            "Pick a section below.",
            parse_mode=ParseMode.HTML,
            reply_markup=admin_home_kb(),
        )
    except Exception:
        logger.exception("admin panel send failed")
        await message.answer("Admin Panel — use buttons below.", reply_markup=admin_home_kb())


@router.callback_query(F.data.startswith("tga:"))
async def tga_router(callback: CallbackQuery, bot: Bot, state: FSMContext):
    if not callback.from_user or not callback.message:
        await callback.answer()
        return
    if not await is_tg_admin(callback.from_user.id):
        await callback.answer("Access denied", show_alert=True)
        return

    action = callback.data.split(":", 1)[1]
    await callback.answer()
    await act(bot, callback.message.chat.id)

    if action == "close":
        try:
            await callback.message.delete()
        except Exception:
            await callback.message.edit_text("Closed.", reply_markup=None)
        return

    if action == "home":
        await state.clear()
        await callback.message.edit_text(
            "<b>Admin Panel</b>\nChoose a section.",
            parse_mode=ParseMode.HTML,
            reply_markup=admin_home_kb(),
        )
        return

    if action == "dash":
        await show_dashboard(callback.message)
        return
    if action == "claims":
        await show_claims(callback.message)
        return
    if action == "wd":
        await show_withdrawals(callback.message)
        return
    if action == "qrs":
        await show_qrs(callback.message)
        return
    if action == "users":
        await show_users(callback.message)
        return
    if action == "upqr":
        await state.set_state(AdminFSM.upload_qr_id)
        await callback.message.edit_text(
            "<b>Upload QR image</b>\n\n"
            "Send the <b>QR id</b> (number from QR list).\n"
            "Then send the photo.\n"
            "/cancel to abort.",
            parse_mode=ParseMode.HTML,
            reply_markup=back_kb(),
        )
        return

    if action.startswith("uqr:"):
        qid = int(action.split(":")[1])
        await state.set_state(AdminFSM.upload_qr_photo)
        await state.update_data(upload_qr_id=qid)
        await callback.message.answer(
            f"Send photo for QR #{qid} now.",
            reply_markup=back_kb(),
        )
        return

    if action == "tickets":
        await show_tickets(callback.message)
        return
    if action == "channels":
        await show_channels(callback.message)
        return
    if action == "settings":
        await show_settings(callback.message)
        return
    if action == "cqr":
        await state.set_state(AdminFSM.create_qr)
        await callback.message.edit_text(
            "<b>Create QR</b>\n\n"
            "Send in one line:\n"
            "<code>amount app_name expiry_minutes</code>\n\n"
            "Example: <code>0.50 PhonePe 60</code>\n"
            "/cancel to abort.",
            parse_mode=ParseMode.HTML,
            reply_markup=back_kb(),
        )
        return

    if action == "delbc":
        await do_delete_last_broadcast(callback.message, bot)
        return

    if action == "bc":
        await state.set_state(AdminFSM.broadcast_text)
        await callback.message.edit_text(
            "<b>Broadcast</b>\n\n"
            "Send the message text now.\n"
            "You can also send a <b>photo</b> or <b>video</b> with a caption.\n"
            "Type /cancel to abort.",
            parse_mode=ParseMode.HTML,
            reply_markup=back_kb(),
        )
        return

    # claim actions: tga:cap:ID / tga:crej:ID / tga:chold:ID
    if action.startswith("cap:"):
        claim_id = int(action.split(":")[1])
        await do_approve_claim(callback.message, claim_id, callback.from_user.id)
        return
    if action.startswith("crej:"):
        claim_id = int(action.split(":")[1])
        await state.set_state(AdminFSM.reject_reason)
        await state.update_data(reject_claim_id=claim_id)
        await callback.message.answer(
            f"Reject claim #{claim_id}\nSend reason text (or photo+caption as reason):",
            reply_markup=back_kb(),
        )
        return
    if action.startswith("chold:"):
        claim_id = int(action.split(":")[1])
        await do_hold_claim(callback.message, claim_id, callback.from_user.id)
        return

    # withdrawal: tga:wpay:ID / tga:wrej:ID
    if action.startswith("wpay:"):
        wid = int(action.split(":")[1])
        await do_pay_withdrawal(callback.message, wid, callback.from_user.id)
        return
    if action.startswith("wrej:"):
        wid = int(action.split(":")[1])
        await do_reject_withdrawal(callback.message, wid, callback.from_user.id)
        return


# ---------------------------------------------------------------------------
# Dashboard / lists
# ---------------------------------------------------------------------------

async def show_dashboard(message: Message):
    async with get_db_context() as session:
        users = (await session.execute(select(func.count()).select_from(User))).scalar() or 0
        active = (
            await session.execute(
                select(func.count()).select_from(User).where(User.status == UserStatus.ACTIVE)
            )
        ).scalar() or 0
        pending_claims = (
            await session.execute(
                select(func.count())
                .select_from(QRClaim)
                .where(QRClaim.status == ClaimStatus.PENDING)
            )
        ).scalar() or 0
        pending_wd = (
            await session.execute(
                select(func.count())
                .select_from(Withdrawal)
                .where(Withdrawal.status == WithdrawalStatus.PENDING)
            )
        ).scalar() or 0
        qrs = (
            await session.execute(
                select(func.count())
                .select_from(QRCode)
                .where(QRCode.status.in_([QRStatus.AVAILABLE, QRStatus.POSTED]))
            )
        ).scalar() or 0

    text = (
        "<b>Dashboard</b>\n\n"
        f"Users: <b>{users}</b> (active {active})\n"
        f"Pending claims: <b>{pending_claims}</b>\n"
        f"Pending withdrawals: <b>{pending_wd}</b>\n"
        f"Open QR codes: <b>{qrs}</b>"
    )
    try:
        await message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=back_kb())
    except Exception:
        await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=back_kb())


async def show_claims(message: Message):
    async with get_db_context() as session:
        rows = (
            await session.execute(
                select(QRClaim)
                .where(QRClaim.status.in_([ClaimStatus.PENDING, ClaimStatus.HELD]))
                .order_by(QRClaim.created_at.desc())
                .limit(10)
            )
        ).scalars().all()

    if not rows:
        text = "<b>Claims</b>\n\nNo pending claims."
        kb = back_kb()
    else:
        lines = ["<b>Pending claims</b>\n"]
        buttons = []
        for c in rows:
            lines.append(
                f"#{c.id} · ${c.reward_cents / 100:.2f} · {c.status.value}"
            )
            buttons.append(
                [
                    InlineKeyboardButton(
                        text=f"#{c.id} Approve",
                        callback_data=f"tga:cap:{c.id}",
                        style="success",
                    ),
                    InlineKeyboardButton(
                        text="Reject",
                        callback_data=f"tga:crej:{c.id}",
                        style="danger",
                    ),
                    InlineKeyboardButton(
                        text="Hold",
                        callback_data=f"tga:chold:{c.id}",
                        style="primary",
                    ),
                ]
            )
        buttons.append(
            [
                InlineKeyboardButton(text="« Menu", callback_data="tga:home", style="primary"),
            ]
        )
        text = "\n".join(lines)
        kb = InlineKeyboardMarkup(inline_keyboard=buttons)

    try:
        await message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
    except Exception:
        await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=kb)


async def show_withdrawals(message: Message):
    async with get_db_context() as session:
        rows = (
            await session.execute(
                select(Withdrawal)
                .where(
                    Withdrawal.status.in_(
                        [WithdrawalStatus.PENDING, WithdrawalStatus.HELD]
                    )
                )
                .order_by(Withdrawal.created_at.desc())
                .limit(10)
            )
        ).scalars().all()

    if not rows:
        text = "<b>Withdrawals</b>\n\nNo pending withdrawals."
        kb = back_kb()
    else:
        lines = ["<b>Pending withdrawals</b>\n"]
        buttons = []
        for w in rows:
            method = getattr(w, "payout_method", None) or "—"
            details = getattr(w, "payout_details", None) or "—"
            lines.append(
                f"#{w.id} · ${w.amount_cents / 100:.2f} · {method}\n"
                f"<code>{details}</code>"
            )
            buttons.append(
                [
                    InlineKeyboardButton(
                        text=f"#{w.id} Mark Paid",
                        callback_data=f"tga:wpay:{w.id}",
                        style="success",
                    ),
                    InlineKeyboardButton(
                        text="Reject",
                        callback_data=f"tga:wrej:{w.id}",
                        style="danger",
                    ),
                ]
            )
        buttons.append(
            [InlineKeyboardButton(text="« Menu", callback_data="tga:home", style="primary")]
        )
        text = "\n".join(lines)
        kb = InlineKeyboardMarkup(inline_keyboard=buttons)

    try:
        await message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
    except Exception:
        await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=kb)


async def show_qrs(message: Message):
    async with get_db_context() as session:
        rows = (
            await session.execute(
                select(QRCode).order_by(QRCode.created_at.desc()).limit(12)
            )
        ).scalars().all()
    if not rows:
        text = "<b>Recent QR</b>\n\nNo QR codes yet."
        kb = back_kb()
    else:
        lines = ["<b>Recent QR</b>\nTap Upload to set image.\n"]
        buttons = []
        for q in rows:
            app = (getattr(q, "app_name", None) or "—")
            has = "img" if getattr(q, "image_png", None) else "no-img"
            lines.append(
                f"#{q.id} · ${q.reward_cents / 100:.2f} · {q.status.value} · {app} · {has}"
            )
            buttons.append(
                [
                    InlineKeyboardButton(
                        text=f"#{q.id} Upload",
                        callback_data=f"tga:uqr:{q.id}",
                        style="success",
                    )
                ]
            )
        buttons.append(
            [
                InlineKeyboardButton(text="« Menu", callback_data="tga:home", style="primary"),
                InlineKeyboardButton(text="Upload by ID", callback_data="tga:upqr", style="primary"),
            ]
        )
        text = "\n".join(lines)
        kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    try:
        await message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
    except Exception:
        await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=kb)


async def show_users(message: Message):
    async with get_db_context() as session:
        total = (await session.execute(select(func.count()).select_from(User))).scalar() or 0
        active = (
            await session.execute(
                select(func.count()).select_from(User).where(User.status == UserStatus.ACTIVE)
            )
        ).scalar() or 0
        recent = (
            await session.execute(select(User).order_by(User.created_at.desc()).limit(8))
        ).scalars().all()
    lines = [
        f"<b>Users</b>\nTotal: {total} · Active: {active}\n\n<b>Recent</b>"
    ]
    for u in recent:
        un = f"@{u.username}" if u.username else str(u.telegram_id)
        lines.append(f"• {un} · bal ${u.balance_cents / 100:.2f}")
    try:
        await message.edit_text(
            "\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=back_kb()
        )
    except Exception:
        await message.answer(
            "\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=back_kb()
        )


# ---------------------------------------------------------------------------
# Claim / withdrawal actions
# ---------------------------------------------------------------------------

async def _admin_db_id(telegram_id: int) -> Optional[int]:
    async with get_db_context() as session:
        if _settings().owner_telegram_id and int(telegram_id) == int(_settings().owner_telegram_id):
            row = (
                await session.execute(
                    select(Admin).where(Admin.is_active.is_(True)).order_by(Admin.id).limit(1)
                )
            ).scalar_one_or_none()
            return row.id if row else None
        row = (
            await session.execute(
                select(Admin).where(
                    Admin.telegram_id == telegram_id, Admin.is_active.is_(True)
                )
            )
        ).scalar_one_or_none()
        return row.id if row else None


async def do_approve_claim(message: Message, claim_id: int, tg_id: int):
    admin_id = await _admin_db_id(tg_id)
    if not admin_id:
        await message.answer("Admin account not linked.")
        return
    try:
        async with get_db_context() as session:
            claim = await claim_service.approve_claim(session, claim_id, admin_id)
            status = claim.status.value
        await message.answer(f"Claim #{claim_id} approved ({status}).", reply_markup=back_kb())
    except Exception as e:
        await message.answer(f"Approve failed: {e}", reply_markup=back_kb())


async def do_hold_claim(message: Message, claim_id: int, tg_id: int):
    admin_id = await _admin_db_id(tg_id)
    if not admin_id:
        await message.answer("Admin account not linked.")
        return
    try:
        async with get_db_context() as session:
            await claim_service.hold_claim(session, claim_id, admin_id)
        await message.answer(f"Claim #{claim_id} on hold.", reply_markup=back_kb())
    except Exception as e:
        await message.answer(f"Hold failed: {e}", reply_markup=back_kb())


@router.message(AdminFSM.reject_reason)
async def reject_reason_msg(message: Message, bot: Bot, state: FSMContext):
    if not await is_tg_admin(message.from_user.id if message.from_user else 0):
        return
    data = await state.get_data()
    claim_id = data.get("reject_claim_id")
    if not claim_id:
        await state.clear()
        return
    reason = (message.caption or message.text or "Rejected").strip()[:500]
    photo_bytes = None
    if message.photo:
        await act(bot, message.chat.id, ChatAction.UPLOAD_PHOTO)
        try:
            f = await bot.get_file(message.photo[-1].file_id)
            buf = await bot.download_file(f.file_path)
            photo_bytes = buf.read() if hasattr(buf, "read") else bytes(buf)
        except Exception:
            photo_bytes = None
    admin_id = await _admin_db_id(message.from_user.id)
    if not admin_id:
        await message.answer("Admin account not linked.")
        await state.clear()
        return
    try:
        async with get_db_context() as session:
            await claim_service.reject_claim(
                session,
                claim_id,
                admin_id,
                reason,
                photo_bytes=photo_bytes,
                photo_filename="reject.jpg",
            )
        await state.clear()
        await message.answer(
            f"Claim #{claim_id} rejected."
            + (" Photo sent." if photo_bytes else ""),
            reply_markup=back_kb(),
        )
    except Exception as e:
        await state.clear()
        await message.answer(f"Reject failed: {e}", reply_markup=back_kb())


async def do_pay_withdrawal(message: Message, wid: int, tg_id: int):
    admin_id = await _admin_db_id(tg_id)
    if not admin_id:
        await message.answer("Admin account not linked.")
        return
    try:
        async with get_db_context() as session:
            await withdrawal_service.mark_withdrawal_paid(
                session, wid, admin_id, payment_reference=f"TG-{wid}"
            )
        await message.answer(f"Withdrawal #{wid} marked paid.", reply_markup=back_kb())
    except Exception as e:
        await message.answer(f"Pay failed: {e}", reply_markup=back_kb())


async def do_reject_withdrawal(message: Message, wid: int, tg_id: int):
    admin_id = await _admin_db_id(tg_id)
    if not admin_id:
        await message.answer("Admin account not linked.")
        return
    try:
        async with get_db_context() as session:
            await withdrawal_service.reject_withdrawal(
                session, wid, admin_id, reason="Rejected via Telegram admin"
            )
        await message.answer(f"Withdrawal #{wid} rejected & refunded.", reply_markup=back_kb())
    except Exception as e:
        await message.answer(f"Reject failed: {e}", reply_markup=back_kb())


# ---------------------------------------------------------------------------
# Broadcast from Telegram
# ---------------------------------------------------------------------------

@router.message(AdminFSM.broadcast_text)
async def broadcast_from_tg(message: Message, bot: Bot, state: FSMContext):
    if not message.from_user or not await is_tg_admin(message.from_user.id):
        return
    if message.text and message.text.strip().lower() in ("/cancel", "cancel"):
        await state.clear()
        await message.answer("Broadcast cancelled.", reply_markup=admin_home_kb())
        return

    text = (message.caption or message.text or "").strip() or " "
    photo_bytes = None
    video_bytes = None
    video_filename = "broadcast.mp4"
    photo_filename = "broadcast.jpg"

    if message.photo:
        await act(bot, message.chat.id, ChatAction.UPLOAD_PHOTO)
        try:
            f = await bot.get_file(message.photo[-1].file_id)
            buf = await bot.download_file(f.file_path)
            photo_bytes = buf.read() if hasattr(buf, "read") else bytes(buf)
        except Exception as e:
            await message.answer(f"Photo download failed: {e}")
            return
    elif message.video:
        await act(bot, message.chat.id, ChatAction.UPLOAD_VIDEO)
        try:
            f = await bot.get_file(message.video.file_id)
            buf = await bot.download_file(f.file_path)
            video_bytes = buf.read() if hasattr(buf, "read") else bytes(buf)
            video_filename = "broadcast.mp4"
        except Exception as e:
            await message.answer(f"Video download failed: {e}")
            return
    elif not text.strip():
        await message.answer("Send text, photo, or video.")
        return

    # Optional simple buttons: last line BUTTONS: Label|url, Label2|url2
    reply_markup = None
    if "BUTTONS:" in text:
        main, _, btn_part = text.partition("BUTTONS:")
        text = main.strip() or " "
        row = []
        for part in btn_part.strip().split(","):
            part = part.strip()
            if "|" not in part:
                continue
            label, url = part.split("|", 1)
            label, url = label.strip()[:64], url.strip()
            if label and url.startswith("http"):
                row.append(InlineKeyboardButton(text=label, url=url, style="primary"))
        if row:
            # Telegram max 8 per row practically — split by 2
            rows = [row[i : i + 2] for i in range(0, len(row), 2)]
            reply_markup = InlineKeyboardMarkup(inline_keyboard=rows)

    await act(bot, message.chat.id, ChatAction.TYPING)
    async with get_db_context() as session:
        users = (
            await session.execute(
                select(User).where(User.status == UserStatus.ACTIVE)
            )
        ).scalars().all()

    sent = failed = 0
    for u in users:
        try:
            if video_bytes:
                await bot.send_video(
                    chat_id=u.telegram_id,
                    video=BufferedInputFile(video_bytes, filename=video_filename),
                    caption=text[:1024],
                    parse_mode=ParseMode.HTML,
                    reply_markup=reply_markup,
                )
            elif photo_bytes:
                await bot.send_photo(
                    chat_id=u.telegram_id,
                    photo=BufferedInputFile(photo_bytes, filename=photo_filename),
                    caption=text[:1024],
                    parse_mode=ParseMode.HTML,
                    reply_markup=reply_markup,
                )
            else:
                await bot.send_message(
                    chat_id=u.telegram_id,
                    text=text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=reply_markup,
                )
            sent += 1
        except Exception:
            failed += 1

    await state.clear()
    await message.answer(
        f"Broadcast done.\nSent: {sent}\nFailed: {failed}\nTotal: {len(users)}",
        reply_markup=admin_home_kb(),
    )




@router.message(AdminFSM.upload_qr_id)
async def upload_qr_id_msg(message: Message, state: FSMContext):
    if not message.from_user or not await is_tg_admin(message.from_user.id):
        return
    text = (message.text or "").strip()
    if text.lower() in ("/cancel", "cancel"):
        await state.clear()
        await message.answer("Cancelled.", reply_markup=admin_home_kb())
        return
    if not text.isdigit():
        await message.answer("Send numeric QR id.")
        return
    qid = int(text)
    async with get_db_context() as session:
        qr = (
            await session.execute(select(QRCode).where(QRCode.id == qid))
        ).scalar_one_or_none()
    if not qr:
        await message.answer(f"QR #{qid} not found.")
        return
    await state.set_state(AdminFSM.upload_qr_photo)
    await state.update_data(upload_qr_id=qid)
    await message.answer(f"Now send the QR image photo for #{qid}.")


@router.message(AdminFSM.upload_qr_photo, F.photo)
async def upload_qr_photo_msg(message: Message, bot: Bot, state: FSMContext):
    if not message.from_user or not await is_tg_admin(message.from_user.id):
        return
    data = await state.get_data()
    qid = data.get("upload_qr_id")
    if not qid:
        await state.clear()
        await message.answer("Session expired. Use /admin again.")
        return
    await act(bot, message.chat.id, ChatAction.UPLOAD_PHOTO)
    try:
        f = await bot.get_file(message.photo[-1].file_id)
        buf = await bot.download_file(f.file_path)
        photo_bytes = buf.read() if hasattr(buf, "read") else bytes(buf)
    except Exception as e:
        await message.answer(f"Download failed: {e}")
        return
    if len(photo_bytes) > 8 * 1024 * 1024:
        await message.answer("Image too large (max 8MB).")
        return
    async with get_db_context() as session:
        qr = (
            await session.execute(select(QRCode).where(QRCode.id == qid))
        ).scalar_one_or_none()
        if not qr:
            await state.clear()
            await message.answer("QR not found.")
            return
        qr.image_png = photo_bytes
        session.add(qr)
    await state.clear()
    await message.answer(
        f"QR #{qid} image uploaded. Users will get this image on claim.",
        reply_markup=admin_home_kb(),
    )


@router.message(AdminFSM.upload_qr_photo)
async def upload_qr_photo_need(message: Message, state: FSMContext):
    if (message.text or "").strip().lower() in ("/cancel", "cancel"):
        await state.clear()
        await message.answer("Cancelled.", reply_markup=admin_home_kb())
        return
    await message.answer("Please send a photo (image).")


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    if await state.get_state():
        await state.clear()
        await message.answer("Cancelled.", reply_markup=admin_home_kb())


async def do_delete_last_broadcast(message: Message, bot: Bot):
    from app.models import BroadcastBatch, BroadcastDelivery

    async with get_db_context() as session:
        batch = (
            await session.execute(
                select(BroadcastBatch)
                .where(BroadcastBatch.deleted.is_(False))
                .order_by(BroadcastBatch.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if not batch:
            await message.answer("No broadcast to delete.", reply_markup=back_kb())
            return
        rows = (
            await session.execute(
                select(BroadcastDelivery).where(
                    BroadcastDelivery.batch_id == batch.id,
                    BroadcastDelivery.deleted.is_(False),
                )
            )
        ).scalars().all()
        deleted = failed = 0
        for r in rows:
            try:
                await bot.delete_message(chat_id=r.telegram_id, message_id=r.message_id)
                r.deleted = True
                deleted += 1
            except Exception:
                failed += 1
        batch.deleted = True
        session.add(batch)
    await message.answer(
        f"Broadcast #{batch.id} delete done.\nDeleted: {deleted}\nFailed: {failed}",
        reply_markup=back_kb(),
    )



async def show_tickets(message: Message):
    from app.models import SupportTicket, TicketStatus

    async with get_db_context() as session:
        rows = (
            await session.execute(
                select(SupportTicket)
                .where(SupportTicket.status != TicketStatus.CLOSED)
                .order_by(SupportTicket.created_at.desc())
                .limit(10)
            )
        ).scalars().all()
    if not rows:
        text = "<b>Support tickets</b>\n\nNo open tickets."
    else:
        lines = ["<b>Open tickets</b>\n"]
        for t in rows:
            lines.append(f"#{t.id} · {t.status.value}\n{t.subject[:60]}")
        text = "\n".join(lines)
    try:
        await message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=back_kb())
    except Exception:
        await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=back_kb())


async def show_channels(message: Message):
    from app.services import force_join_service

    async with get_db_context() as session:
        channels = await force_join_service.get_active_required_channels(session)
        enabled = await force_join_service.is_force_join_enabled(session)
    lines = [f"<b>Force-join</b> · {'ON' if enabled else 'OFF'}\n"]
    if not channels:
        lines.append("No required channels.")
    else:
        for c in channels:
            title = getattr(c, "title", None) or getattr(c, "chat_id", c)
            lines.append(f"• {title}")
    text = "\n".join(lines)
    try:
        await message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=back_kb())
    except Exception:
        await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=back_kb())


async def show_settings(message: Message):
    from app.services import settings_service

    keys = [
        "force_join_enabled",
        "claim_approval_required",
        "min_withdrawal_usd",
        "max_withdrawal_usd",
        "maintenance_mode",
        "dm_notify_on_post",
    ]
    lines = ["<b>Settings</b>\n"]
    async with get_db_context() as session:
        for k in keys:
            try:
                v = await settings_service.get_setting(session, k)
            except Exception:
                v = "?"
            lines.append(f"{k}: <code>{v}</code>")
    text = "\n".join(lines)
    try:
        await message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=back_kb())
    except Exception:
        await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=back_kb())


@router.message(AdminFSM.create_qr)
async def create_qr_from_tg(message: Message, state: FSMContext):
    if not message.from_user or not await is_tg_admin(message.from_user.id):
        return
    text = (message.text or "").strip()
    if text.lower() in ("/cancel", "cancel"):
        await state.clear()
        await message.answer("Cancelled.", reply_markup=admin_home_kb())
        return
    parts = text.split()
    if len(parts) < 2:
        await message.answer("Format: amount app_name [expiry_minutes]\nExample: 0.50 PhonePe 60")
        return
    try:
        amount = float(parts[0])
    except ValueError:
        await message.answer("Invalid amount")
        return
    app_name = parts[1]
    expiry_min = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else None
    if amount <= 0:
        await message.answer("Amount must be > 0")
        return
    from datetime import datetime, timedelta, timezone
    from app.services import qr_service

    admin_id = await _admin_db_id(message.from_user.id)
    if not admin_id:
        await message.answer("Admin DB id missing.")
        return
    expires_at = None
    if expiry_min:
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=expiry_min)
    async with get_db_context() as session:
        codes = await qr_service.create_qr_codes(
            session,
            reward_cents=int(round(amount * 100)),
            quantity=1,
            expires_at=expires_at,
            campaign_name=None,
            description=None,
            admin_id=admin_id,
            app_name=app_name,
        )
        code = codes[0].code if codes else "?"
        qid = codes[0].id if codes else "?"
    await state.clear()
    await message.answer(
        f"QR created.\\nTask #{qid}\\nCode: <code>{code}</code>\\n"
        f"Reward: ${amount:.2f}\\nApp: {app_name}\\n\\n"
        f"Use Upload QR to attach image, then notify from web or post.",
        parse_mode=ParseMode.HTML,
        reply_markup=admin_home_kb(),
    )
