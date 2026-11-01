"""Telegram bot using aiogram 3.x – single polling instance."""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType, ParseMode
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
    ReplyKeyboardRemove,
    WebAppInfo,
)

from app.config import get_settings
from app.db import get_db_context
from app.models import UserStatus
from app.services import force_join_service, qr_service, settings_service, user_service
from app.services import message_templates as msg_tpl
from app.services.notification_service import set_bot
from app.services.wallet_service import get_transactions
from app.services.withdrawal_service import create_withdrawal

logger = logging.getLogger(__name__)
settings = get_settings()

# User bot (claim / wallet / menu) — PRIVATE chats only
router = Router(name="user_bot")
router.message.filter(F.chat.type == "private")
# Allow callbacks even if message is missing chat type edge-cases
router.callback_query.filter(
    (F.message.chat.type == "private") | (F.message.chat.type == ChatType.PRIVATE)
)

# Group: ignore user menu; optional soft redirect for /start
group_guard = Router(name="group_guard")
group_guard.message.filter(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))

_bot: Optional[Bot] = None
_dp: Optional[Dispatcher] = None
_polling_task: Optional[asyncio.Task] = None


def cents_to_usd(cents: int) -> str:
    return f"${cents / 100:.2f}"

async def sending_status(bot: Bot, chat_id: int):
    """Progressive Sending... then delete when finished. Returns (msg, finish_cb)."""
    stop = asyncio.Event()
    status = None
    task = None
    try:
        status = await bot.send_message(chat_id, "Sending.")
    except Exception:
        status = None

    if status is not None:
        async def _animate() -> None:
            n = 1
            while not stop.is_set():
                n = n % 3 + 1
                try:
                    await status.edit_text("Sending" + ("." * n))
                except Exception:
                    pass
                try:
                    await asyncio.wait_for(stop.wait(), timeout=0.4)
                except asyncio.TimeoutError:
                    continue

        task = asyncio.create_task(_animate())

    async def _finish() -> None:
        stop.set()
        if task is not None:
            try:
                task.cancel()
            except Exception:
                pass
        if status is not None:
            try:
                await status.delete()
            except Exception:
                pass

    return status, _finish




# ---------------------------------------------------------------------------
# Keyboards – coloured styles (Bot API 9.4 / aiogram 3.20+)
# ---------------------------------------------------------------------------

def _kb_btn(cfg: dict, key: str) -> KeyboardButton:
    from app.services import button_config as btn_cfg

    b = cfg.get(key) or btn_cfg.DEFAULT_BUTTONS[key]
    kwargs = {"text": b["text"], "style": b.get("style") or "primary"}
    if b.get("icon"):
        kwargs["icon_custom_emoji_id"] = str(b["icon"])
    try:
        return KeyboardButton(**kwargs)
    except TypeError:
        kwargs.pop("icon_custom_emoji_id", None)
        return KeyboardButton(**kwargs)


def _ik_btn(cfg: dict, key: str, **extra) -> InlineKeyboardButton:
    from app.services import button_config as btn_cfg

    b = cfg.get(key) or btn_cfg.DEFAULT_BUTTONS.get(key) or {
        "text": key,
        "style": "primary",
    }
    kwargs = {"text": b["text"], "style": b.get("style") or "primary", **extra}
    if b.get("icon"):
        kwargs["icon_custom_emoji_id"] = str(b["icon"])
    try:
        return InlineKeyboardButton(**kwargs)
    except TypeError:
        kwargs.pop("icon_custom_emoji_id", None)
        return InlineKeyboardButton(**kwargs)


async def load_btn_cfg() -> dict:
    from app.services import button_config as btn_cfg

    async with get_db_context() as session:
        return await btn_cfg.get_all_buttons(session)


def main_menu_kb(cfg: dict | None = None) -> ReplyKeyboardMarkup:
    """Reply keyboard with optional custom emoji icons on each button."""
    from app.services import button_config as btn_cfg

    c = cfg or btn_cfg.DEFAULT_BUTTONS
    return ReplyKeyboardMarkup(
        keyboard=[
            [_kb_btn(c, "how_to_claim"), _kb_btn(c, "balance")],
            [_kb_btn(c, "my_claims"), _kb_btn(c, "withdraw")],
            [_kb_btn(c, "history"), _kb_btn(c, "support")],
            [_kb_btn(c, "help")],
        ],
        resize_keyboard=True,
        input_field_placeholder="Choose an option…",
    )


def nav_kb(cfg: dict | None = None) -> InlineKeyboardMarkup:
    from app.services import button_config as btn_cfg

    c = cfg or btn_cfg.DEFAULT_BUTTONS
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                _ik_btn(c, "balance", callback_data="menu:balance"),
                _ik_btn(c, "withdraw", callback_data="menu:withdraw"),
            ],
            [
                _ik_btn(c, "my_claims", callback_data="menu:claims"),
                _ik_btn(c, "help", callback_data="menu:help"),
            ],
        ]
    )


def force_join_kb(channels, cfg: dict | None = None) -> InlineKeyboardMarkup:
    from app.services import button_config as btn_cfg

    c = cfg or btn_cfg.DEFAULT_BUTTONS
    buttons = []
    for ch in channels:
        url = ch.invite_url
        if not url and ch.username:
            url = f"https://t.me/{ch.username.lstrip('@')}"
        if url:
            buttons.append(
                [
                    InlineKeyboardButton(
                        text=f"Join {ch.title}",
                        url=url,
                        style="primary",
                    )
                ]
            )
    buttons.append(
        [_ik_btn(c, "check_join", callback_data="check_membership")]
    )
    return InlineKeyboardMarkup(inline_keyboard=buttons)



def claim_flow_kb(code: str, payment_link: str | None = None, cfg: dict | None = None) -> InlineKeyboardMarkup:
    """Submit + Cancel, plus optional Open Link button."""
    from app.services import button_config as btn_cfg
    c = cfg or btn_cfg.DEFAULT_BUTTONS
    rows = []
    if payment_link:
        rows.append([
            InlineKeyboardButton(text="Open Link", url=payment_link, style="primary")
        ])
    rows.append([_ik_btn(c, "submit_claim", callback_data=f"submit_claim:{code}")])
    rows.append([_ik_btn(c, "cancel", callback_data="cancel_claim")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def notify_claim_kb(code: str, payment_link: str | None = None, cfg: dict | None = None) -> InlineKeyboardMarkup:
    from app.services import button_config as btn_cfg
    c = cfg or btn_cfg.DEFAULT_BUTTONS
    rows = []
    if payment_link:
        rows.append([InlineKeyboardButton(text="Open Link", url=payment_link, style="primary")])
    rows.append([_ik_btn(c, "claim_qr", callback_data=f"claim:{code}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def claim_button(code: str, cfg: dict | None = None) -> InlineKeyboardMarkup:
    from app.services import button_config as btn_cfg

    c = cfg or btn_cfg.DEFAULT_BUTTONS
    return InlineKeyboardMarkup(
        inline_keyboard=[[_ik_btn(c, "claim_qr", callback_data=f"claim:{code}")]]
    )


def submit_claim_button(code: str, cfg: dict | None = None) -> InlineKeyboardMarkup:
    from app.services import button_config as btn_cfg

    c = cfg or btn_cfg.DEFAULT_BUTTONS
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_ik_btn(c, "submit_claim", callback_data=f"submit_claim:{code}")],
            [_ik_btn(c, "cancel", callback_data="cancel_claim")],
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
                    style="danger",  # red
                )
            ]
        ]
    )


def payout_method_kb(cfg: dict | None = None) -> InlineKeyboardMarkup:
    from app.services import button_config as btn_cfg
    c = cfg or btn_cfg.DEFAULT_BUTTONS
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_ik_btn(c, "upi", callback_data="payout:upi")],
            [_ik_btn(c, "binance", callback_data="payout:binance")],
            [_ik_btn(c, "cancel", callback_data="payout:cancel")],
        ]
    )


def after_claim_kb(cfg: dict | None = None) -> InlineKeyboardMarkup:
    from app.services import button_config as btn_cfg
    c = cfg or btn_cfg.DEFAULT_BUTTONS
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                _ik_btn(c, "view_balance", callback_data="menu:balance"),
                _ik_btn(c, "withdraw", callback_data="menu:withdraw"),
            ],
            [_ik_btn(c, "my_claims", callback_data="menu:claims")],
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

async def typing(bot: Bot, chat_id: int, seconds: float = 0) -> None:
    """No-op — typing bubbles removed for maximum response speed."""
    return


async def uploading_photo(bot: Bot, chat_id: int, seconds: float = 0) -> None:
    """No-op — upload bubbles removed for maximum response speed."""
    return


async def uploading_video(bot: Bot, chat_id: int, seconds: float = 0) -> None:
    """No-op — upload bubbles removed for maximum response speed."""
    return


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
        "⛔ <b>Already claimed</b>\n\n"
        f"This reward was claimed by {claimer_line}.\n"
        "Each QR can be claimed by only one user.\n\n"
        "Watch this chat for the next available reward."
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
# Groups: do not run user bot here
# ---------------------------------------------------------------------------

@group_guard.message(CommandStart())
@group_guard.message(Command("start"))
async def group_start_redirect(message: Message):
    """In groups: no menu keyboard — point users to private chat."""
    try:
        async with get_db_context() as session:
            gtxt = await msg_tpl.get_template(session, "msg_group_private_only")
        await message.reply(
            gtxt,
            parse_mode=ParseMode.HTML,
            reply_markup=ReplyKeyboardRemove(),
        )
    except Exception:
        pass


@group_guard.message(
    F.text.in_(
        {
            "How to Claim",
            "Balance",
            "Wallet",
            "My Claims",
            "Withdraw",
            "History",
            "Transaction History",
            "Support",
            "Help",
        }
    )
)
async def group_ignore_menu(message: Message):
    """Strip reply-keyboard spam in groups if a user still has the private menu open."""
    try:
        await message.reply(
            "Menu works only in private chat with the bot.",
            reply_markup=ReplyKeyboardRemove(),
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------

@router.message(CommandStart())
@router.message(Command("start"))
async def cmd_start(message: Message, bot: Bot):
    """Welcome + menu. Always responds (fallback on errors)."""
    if not message.from_user:
        return
    chat_id = message.chat.id
    _, finish = await sending_status(bot, chat_id)
    try:
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

            force_enabled = await force_join_service.is_force_join_enabled(session)
            if force_enabled:
                channels = await force_join_service.get_active_required_channels(session)
                if channels:
                    all_ok, missing = await force_join_service.check_membership(
                        bot, message.from_user.id, channels
                    )
                    if not all_ok:
                        fj = await msg_tpl.get_template(session, "msg_force_join")
                        await finish()
                        try:
                            await message.answer(
                                fj,
                                parse_mode=ParseMode.HTML,
                                reply_markup=force_join_kb(missing),
                            )
                        except Exception:
                            await message.answer(
                                "Please join the required channels first.",
                                reply_markup=force_join_kb(missing),
                            )
                        return

        parts = (message.text or "").split(maxsplit=1)
        payload = parts[1].strip() if len(parts) > 1 else ""
        if payload.startswith("claim_"):
            code = payload[len("claim_") :].strip().upper()
            if code:
                cfg = await load_btn_cfg()
                await finish()
                try:
                    await message.answer(
                        welcome or "Welcome!",
                        reply_markup=main_menu_kb(cfg),
                        parse_mode=ParseMode.HTML,
                    )
                except Exception:
                    await message.answer("Welcome!", reply_markup=main_menu_kb(cfg))
                await _send_claim_preview(message, bot, code)
                return

        name = message.from_user.first_name or "there"
        async with get_db_context() as session:
            body = welcome or await msg_tpl.get_template(session, "msg_welcome_body")
            text = await msg_tpl.get_rendered(
                session, "msg_welcome", name=name, welcome_body=body
            )
        cfg = await load_btn_cfg()
        await finish()
        try:
            await message.answer(
                text, reply_markup=main_menu_kb(cfg), parse_mode=ParseMode.HTML
            )
        except Exception as e:
            logger.warning("Welcome HTML failed: %s", e)
            await message.answer(
                f"Welcome, {name}!\nUse the menu below.",
                reply_markup=main_menu_kb(cfg),
            )
        try:
            await message.answer("Quick actions:", reply_markup=nav_kb(cfg))
        except Exception:
            pass
    except Exception:
        logger.exception("/start failed for %s", message.from_user.id)
        try:
            await finish()
        except Exception:
            pass
        try:
            cfg = await load_btn_cfg()
        except Exception:
            cfg = None
        await message.answer(
            "Welcome! Use the menu below to continue.",
            reply_markup=main_menu_kb(cfg),
        )


# ---------------------------------------------------------------------------
# Menu handlers (text + callback)
# ---------------------------------------------------------------------------


@router.message(F.text)
async def menu_label_dispatcher(message: Message, state: FSMContext, bot: Bot):
    """Match customized reply-keyboard labels (incl. premium emoji labels)."""
    if await state.get_state():
        return
    text = (message.text or "").strip()
    if not text or text.startswith("/"):
        return
    cfg = await load_btn_cfg()
    key = None
    for k in (
        "how_to_claim",
        "balance",
        "my_claims",
        "withdraw",
        "history",
        "support",
        "help",
    ):
        if text == (cfg.get(k) or {}).get("text"):
            key = k
            break
    if not key:
        return
    # Delegate
    if key == "how_to_claim":
        await how_to_claim(message, bot)
    elif key == "balance":
        await balance_cmd(message, bot)
    elif key == "history":
        await tx_history(message, bot)
    elif key == "my_claims":
        await my_claims(message, bot)
    elif key == "help":
        await help_cmd(message, bot)
    elif key == "withdraw":
        await withdraw_start(message, state, bot)
    elif key == "support":
        await support_start(message, state, bot)


@router.message(F.text == "How to Claim")
@router.callback_query(F.data == "menu:help")
async def how_to_claim(event: Message | CallbackQuery, bot: Bot):
    async with get_db_context() as _s:
        text = await msg_tpl.get_template(_s, "msg_how_to_claim")
    if isinstance(event, CallbackQuery):
        await event.answer()
        await event.message.answer(text, parse_mode=ParseMode.HTML, reply_markup=nav_kb())
    else:
        await event.answer(text, parse_mode=ParseMode.HTML, reply_markup=nav_kb())


@router.message(F.text.in_({"Balance", "Wallet"}))
@router.callback_query(F.data == "menu:balance")
async def balance_cmd(event: Message | CallbackQuery, bot: Bot):
    user_obj = event.from_user if isinstance(event, CallbackQuery) else event.from_user
    chat_id = event.message.chat.id if isinstance(event, CallbackQuery) else event.chat.id
    if isinstance(event, CallbackQuery):
        await event.answer()
    async with get_db_context() as session:
        user = await user_service.get_or_create_user(
            session,
            telegram_id=user_obj.id,
            username=user_obj.username,
            first_name=user_obj.first_name,
            last_name=user_obj.last_name,
        )
        text = await msg_tpl.get_rendered(
            session,
            "msg_balance",
            balance=cents_to_usd(user.balance_cents),
            pending=cents_to_usd(int(user.pending_cents or 0)),
            total_earned=cents_to_usd(user.total_earned_cents),
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
    async with get_db_context() as session:
        user = await user_service.get_or_create_user(
            session,
            telegram_id=user_obj.id,
            username=user_obj.username,
            first_name=user_obj.first_name,
            last_name=user_obj.last_name,
        )
        txs, total = await get_transactions(session, user.id, offset=0, limit=10)
    lines = [f"📜 <b>Recent transactions</b> ({len(txs)} of {total})\n"]
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
        text = await msg_tpl.get_rendered(
            session,
            "msg_withdraw_start",
            balance=cents_to_usd(user.balance_cents),
            min_amount=cents_to_usd(min_c),
            max_amount=cents_to_usd(max_c),
        )
        text = text + "\n\nSend the amount in USD (example: <code>5.00</code>).\nOr tap <b>Cancel</b> below to exit."
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
    await state.set_state(SupportStates.waiting_message)
    async with get_db_context() as session:
        text = await msg_tpl.get_template(session, "msg_support_start")
    await message.answer(
        text,
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
        app_label = (getattr(qr, "app_name", None) or "QR task").strip()
        exp_line = "No expiry"
        if qr.expires_at:
            exp_line = qr.expires_at.strftime("%H:%M:%S UTC")
        pay_link = (getattr(qr, "payment_link", None) or "").strip()
        link_line = f"🔗 Link: {pay_link}\n" if pay_link else ""
        caption = (
            f"🆕 <b>New task #{qr.id}</b> · {app_label}\n"
            f"💰 Your reward: <b>{reward}</b>\n"
            f"⏰ Expires: {exp_line}\n"
            f"⚡ Claim before opening the payment link.\n\n"
            f"{link_line}"
            f"🔖 Code: <code>{qr.code}</code>\n"
            f"✅ Tap <b>Submit Claim</b> after you scan."
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
                    reply_markup=claim_flow_kb(qr.code, getattr(qr, "payment_link", None)),
                )
            else:
                await bot.send_photo(
                    chat_id=chat_id,
                    photo=photo,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    reply_markup=claim_flow_kb(qr.code, getattr(qr, "payment_link", None)),
                )
        except Exception:
            await target.answer(
                caption,
                parse_mode=ParseMode.HTML,
                reply_markup=claim_flow_kb(qr.code, getattr(qr, "payment_link", None)),
            )


@router.callback_query(F.data.startswith("claim:"))
async def claim_callback(callback: CallbackQuery, bot: Bot):
    if not callback.from_user or not callback.data:
        await callback.answer()
        return
    code = callback.data.split(":", 1)[1].strip().upper()
    user = callback.from_user
    try:
        await callback.answer()
    except Exception:
        pass
    chat_id = callback.message.chat.id if callback.message else user.id
    _, finish = await sending_status(bot, chat_id)
    try:
        async with get_db_context() as session:
            db_user = await user_service.get_or_create_user(
                session,
                telegram_id=user.id,
                username=user.username,
                first_name=user.first_name,
                last_name=user.last_name,
            )
            if db_user.status != UserStatus.ACTIVE:
                await finish()
                await bot.send_message(chat_id, "Your account is disabled.")
                return

            maint = await settings_service.get_setting_bool(
                session, "maintenance_mode", False
            )
            if maint:
                await finish()
                await bot.send_message(
                    chat_id, "The bot is under maintenance. Please try later."
                )
                return
            claims_on = await settings_service.get_setting_bool(
                session, "claims_enabled", True
            )
            if not claims_on:
                await finish()
                await bot.send_message(chat_id, "Claims are temporarily disabled.")
                return

            force_enabled = await force_join_service.is_force_join_enabled(session)
            if force_enabled:
                channels = await force_join_service.get_active_required_channels(session)
                if channels:
                    all_ok, missing = await force_join_service.check_membership(
                        bot, user.id, channels
                    )
                    if not all_ok:
                        await finish()
                        await bot.send_message(
                            chat_id,
                            "<b>Join required channels</b>\n\n"
                            "Join the channels below, then tap "
                            "<b>I Joined – Check Again</b>.",
                            parse_mode=ParseMode.HTML,
                            reply_markup=force_join_kb(missing),
                        )
                        return
        await finish()
        target = callback.message or callback
        await _send_claim_preview(target, bot, code)
    except Exception:
        logger.exception("claim_callback failed code=%s", code)
        try:
            await finish()
        except Exception:
            pass
        try:
            await bot.send_message(
                chat_id, "Could not load this QR. Please try again or contact Support."
            )
        except Exception:
            pass


@router.callback_query(F.data.startswith("submit_claim:"))
async def submit_claim_callback(callback: CallbackQuery, bot: Bot):
    if not callback.from_user or not callback.data:
        try:
            await callback.answer()
        except Exception:
            pass
        return
    code = callback.data.split(":", 1)[1].strip().upper()
    user = callback.from_user
    try:
        await callback.answer("Submitting…")
    except Exception:
        pass
    chat_id = callback.message.chat.id if callback.message else user.id
    _, finish = await sending_status(bot, chat_id)
    try:
        async with get_db_context() as session:
            db_user = await user_service.get_or_create_user(
                session,
                telegram_id=user.id,
                username=user.username,
                first_name=user.first_name,
                last_name=user.last_name,
            )
            if db_user.status != UserStatus.ACTIVE:
                await finish()
                await bot.send_message(chat_id, "Your account is disabled.")
                return

            force_enabled = await force_join_service.is_force_join_enabled(session)
            if force_enabled:
                channels = await force_join_service.get_active_required_channels(session)
                if channels:
                    all_ok, missing = await force_join_service.check_membership(
                        bot, user.id, channels
                    )
                    if not all_ok:
                        await finish()
                        await bot.send_message(
                            chat_id,
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

                app_label = (getattr(qr, "app_name", None) or "").strip()
                title_line = f"Task #{qr.id}" + (f" · {app_label}" if app_label else "")
                if claim.status.value == "APPROVED":
                    text = await msg_tpl.get_rendered(
                        session,
                        "msg_claim_approved",
                        title=title_line,
                        reward=cents_to_usd(qr.reward_cents),
                    )
                else:
                    text = await msg_tpl.get_rendered(
                        session,
                        "msg_claim_submitted",
                        title=title_line,
                        reward=cents_to_usd(qr.reward_cents),
                    )
                await finish()
                if callback.message:
                    try:
                        await callback.message.edit_caption(
                            caption=text,
                            parse_mode=ParseMode.HTML,
                            reply_markup=after_claim_kb(),
                        )
                    except Exception:
                        await callback.message.answer(
                            text,
                            parse_mode=ParseMode.HTML,
                            reply_markup=after_claim_kb(),
                        )
                else:
                    await bot.send_message(
                        chat_id,
                        text,
                        parse_mode=ParseMode.HTML,
                        reply_markup=after_claim_kb(),
                    )
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
                await finish()
                err = str(e)
                if err == "QR_ALREADY_CLAIMED":
                    await _send_already_claimed(
                        callback.message or callback, bot, code
                    )
                elif err == "QR_NOT_FOUND":
                    await bot.send_message(chat_id, "Invalid or unknown QR code.")
                elif err == "QR_EXPIRED":
                    await bot.send_message(
                        chat_id, "This QR has expired.", reply_markup=nav_kb()
                    )
                elif err.startswith("QR_"):
                    await bot.send_message(
                        chat_id,
                        f"This QR is not available ({err.replace('QR_', '')}).",
                    )
                else:
                    logger.exception("Claim error: %s", e)
                    await bot.send_message(
                        chat_id, "Something went wrong. Please try again later."
                    )
    except Exception:
        logger.exception("submit_claim failed code=%s", code)
        try:
            await finish()
        except Exception:
            pass
        try:
            await bot.send_message(
                chat_id, "Could not submit claim. Please try again."
            )
        except Exception:
            pass


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
    await callback.answer()
    async with get_db_context() as session:
        channels = await force_join_service.get_active_required_channels(session)
        all_ok, missing = await force_join_service.check_membership(
            bot, callback.from_user.id, channels
        )
        welcome = await settings_service.get_setting(session, "bot_welcome_message")
        if not welcome:
            welcome = settings.bot_welcome_message
    if all_ok:
        name = callback.from_user.first_name or "there"
        try:
            await callback.message.edit_text(
                f"<b>Verified</b> · Welcome, {name}!\n\n{welcome}",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            await callback.message.answer(
                f"<b>Verified</b> · Welcome, {name}!\n\n{welcome}",
                parse_mode=ParseMode.HTML,
            )
        await callback.message.answer(
            "You can use the bot now.",
            reply_markup=main_menu_kb(),
        )
        await callback.message.answer("Quick actions:", reply_markup=nav_kb())
        return
    await callback.message.answer(
        "<b>Still missing channels</b>\n\n"
        "Join all required channels, then tap check again.",
        parse_mode=ParseMode.HTML,
        reply_markup=force_join_kb(missing),
    )



def get_bot() -> Optional[Bot]:
    return _bot


@router.message(Command("web"))
async def cmd_web_admin(message: Message):
    """Open web admin panel (owner / linked admins)."""
    from app.tg_admin import is_tg_admin

    if not message.from_user:
        return
    if not await is_tg_admin(message.from_user.id):
        await message.answer("Admin only.")
        return
    base = (getattr(settings, "public_base_url", None) or "").rstrip("/")
    if not base:
        await message.answer(
            "<b>Admin panel URL not set</b>\n\n"
            "Railway Variables add:\n"
            "<code>PUBLIC_BASE_URL=https://your-app.up.railway.app</code>\n\n"
            "Redeploy, then /web again.",
            parse_mode=ParseMode.HTML,
        )
        return
    path = getattr(settings, "admin_panel_path", None) or "/admin"
    if not path.startswith("/"):
        path = "/" + path
    admin_url = base + path
    login_url = base + "/"
    mini = []
    try:
        from aiogram.types import WebAppInfo

        mini = [
            [
                InlineKeyboardButton(
                    text="Open Mini App",
                    web_app=WebAppInfo(url=login_url),
                    style="primary",
                )
            ]
        ]
    except Exception:
        pass
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Open Admin Panel",
                    url=admin_url,
                    style="success",
                )
            ],
            *mini,
            [
                InlineKeyboardButton(
                    text="Login page",
                    url=login_url,
                    style="primary",
                )
            ],
        ]
    )
    await message.answer(
        "<b>Web Admin Panel</b>\n\n"
        "Tap a button to open the dashboard.\n"
        f"<code>{login_url}</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=kb,
    )


@router.message(Command("mychannels"))
@router.message(Command("botchannels"))
async def cmd_bot_channels(message: Message):
    """List configured force-join channels and known groups."""
    from sqlalchemy import select

    from app.models import ForceJoinChannel, GroupChatSettings
    from app.tg_admin import is_tg_admin

    if not message.from_user or not await is_tg_admin(message.from_user.id):
        await message.answer("Admin only.")
        return
    async with get_db_context() as session:
        fj = (await session.execute(select(ForceJoinChannel))).scalars().all()
        groups = (
            await session.execute(select(GroupChatSettings).limit(50))
        ).scalars().all()
    lines = [
        "<b>Bot channels / groups</b>",
        "",
        "<b>Force-join channels</b>",
    ]
    if not fj:
        lines.append("• None (add in Web Admin → Channels)")
    else:
        for ch in fj:
            title = getattr(ch, "title", None) or getattr(ch, "username", None) or "?"
            cid = getattr(ch, "chat_id", "")
            active = "ON" if getattr(ch, "is_active", True) else "OFF"
            lines.append(f"• {title} (<code>{cid}</code>) · {active}")
    lines.append("")
    lines.append("<b>Groups (group tools used)</b>")
    if not groups:
        lines.append("• None yet")
    else:
        for gr in groups:
            lines.append(f"• <code>{gr.chat_id}</code>")
    lines.append("")
    lines.append(
        "<i>Telegram cannot list every chat the bot joined. "
        "Use Web Admin → Channels for force-join.</i>"
    )
    await message.answer("\n".join(lines), parse_mode=ParseMode.HTML)




@router.message(Command("scanner"))
async def cmd_scanner(message: Message):
    """Issue Scanner ID + link to web scanner."""
    from app.services import scanner_service

    if not message.from_user:
        return
    async with get_db_context() as session:
        user = await user_service.get_or_create_user(
            session,
            telegram_id=message.from_user.id,
            username=message.from_user.username,
            first_name=message.from_user.first_name,
            last_name=message.from_user.last_name,
        )
        sid = await scanner_service.ensure_scanner_id(session, user)
        attempts = int(user.scan_attempts or 0)
        successes = int(user.scan_successes or 0)
        rate = scanner_service.success_rate(attempts, successes)
        await session.commit()
    base = (getattr(settings, "public_base_url", None) or "").rstrip("/")
    url = f"{base}/scanner?sid={sid}" if base else f"(set PUBLIC_BASE_URL)/scanner?sid={sid}"
    kb = None
    if base:
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Open Scanner",
                        url=url,
                        style="success",
                    )
                ]
            ]
        )
    await message.answer(
        f"<b>Web QR Scanner</b>\n\n"
        f"Scanner ID: <code>{sid}</code>\n"
        f"Attempts: <b>{attempts}</b>\n"
        f"Success: <b>{successes}</b>\n"
        f"Success rate: <b>{rate}%</b>\n\n"
        f"Open: {url}\n\n"
        f"Scan task QR codes on the website. Stats update live.",
        parse_mode=ParseMode.HTML,
        reply_markup=kb,
    )


async def start_bot() -> None:
    global _bot, _dp, _polling_task
    if _bot is not None:
        logger.warning("Bot already started")
        return

    token = settings.bot_token
    _bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    _dp = Dispatcher(storage=MemoryStorage())
    try:
        from app.tg_admin import router as tg_admin_router

        _dp.include_router(tg_admin_router)
        logger.info("Telegram admin panel router loaded (first)")
    except Exception:
        logger.exception("Failed to load Telegram admin panel router")

    _dp.include_router(group_guard)
    _dp.include_router(router)
    try:
        from app.group_mod import router as group_router

        _dp.include_router(group_router)
        logger.info("Group moderation router loaded")
    except Exception:
        logger.exception("Failed to load group moderation router")
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
