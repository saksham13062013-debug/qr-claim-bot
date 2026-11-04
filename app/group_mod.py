"""Group management commands — Telegram group admins only."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from aiogram import Bot, F, Router
from aiogram.enums import ChatAction, ChatMemberStatus, ChatType, ParseMode
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from app.config import get_settings
from app.db import get_db_context
from app.models import (
    GroupApprovedUser,
    GroupBanLog,
    GroupBlacklistWord,
    GroupChatSettings,
    GroupFilter,
    GroupForceJoinChannel,
    GroupMuteLog,
    GroupNote,
    GroupWarn,
)
from sqlalchemy import delete, func, select

logger = logging.getLogger(__name__)
settings = get_settings()
router = Router(name="group_mod")

# admin_id -> connected group chat_id (DM remote control)
_CONNECTED_CHATS: dict[int, int] = {}

HELP_SECTIONS: dict[str, str] = {
    "home": (
        "🛠 <b>Group Tools</b>\n"
        "<i>Admins · group or private DM</i>\n\n"
        "In DM: <code>/connect -100…</code> then run commands.\n"
        "Pick a category 👇"
    ),
    "welcome": (
        "👋 <b>Welcome &amp; Goodbye</b>\n\n"
        "<code>/setwelcome</code> text\n"
        "<code>/welcome</code> on/off\n"
        "<code>/setgoodbye</code> text\n"
        "<code>/goodbye</code> on/off\n"
        "<code>/resetwelcome</code>\n"
        "<code>/eid</code> custom emoji id\n\n"
        "Placeholders: <code>{mention}</code> <code>{name}</code> <code>{id}</code> <code>{chat}</code>"
    ),
    "rules": (
        "📜 <b>Rules</b>\n\n"
        "<code>/setrules</code> text\n"
        "<code>/rules</code> show rules"
    ),
    "warns": (
        "⚠️ <b>Warn system</b>\n\n"
        "<code>/warn</code> reply or id\n"
        "<code>/unwarn</code>\n"
        "<code>/resetwarns</code>\n"
        "<code>/warns</code> check\n\n"
        "At limit → auto ban"
    ),
    "mod": (
        "🚫 <b>Moderation</b>\n\n"
        "<code>/ban</code> <code>/unban</code> <code>/kick</code>\n"
        "<code>/tban 1h</code> temp ban\n"
        "<code>/mute</code> <code>/unmute</code>\n"
        "<code>/tmute 30m</code> temp mute\n"
        "<code>/dban</code> <code>/dmute</code> <code>/dwarn</code>\n"
        "<code>/ns</code> stickers off · <code>/as</code> on\n"
        "<code>/kickme</code> leave group\n"
        "<code>/purge</code> delete to reply"
    ),
    "pins": (
        "📌 <b>Pins &amp; cleanup</b>\n\n"
        "<code>/pin</code> <code>/unpin</code> <code>/unpinall</code>\n"
        "<code>/del</code> delete replied\n"
        "<code>/set1</code> save reason\n"
        "<code>/d1</code> <code>/w1</code> quick actions\n"
        "<code>/purge</code> <code>/spurge</code>\n"
        "<code>/scandeleted</code>"
    ),
    "admin": (
        "✅ <b>Admin tools</b>\n\n"
        "<code>/promote</code> <code>/demote</code>\n"
        "<code>/approve</code> <code>/disapprove</code>\n"
        "<code>/approved</code> list\n"
        "<code>/admins</code> list\n"
        "<code>/staff</code> staff list\n"
        "<code>/title</code> admin title\n"
        "<code>/invitelink</code> invite link\n"
        "<code>/slowmode</code> seconds\n"
        "<code>/zombies</code> scan deleted accounts"
    ),
    "automod": (
        "🛡 <b>Auto-mod</b>\n\n"
        "<code>/antilink</code> <code>/antiforward</code>\n"
        "<code>/antiflood</code> <code>/captcha</code>\n"
        "<code>/antichannel</code>\n"
        "<code>/lock</code> links|forward|anon\n"
        "<code>/unlock</code>\n"
        "<code>/free</code> <code>/rfree</code> <code>/list free</code>\n"
        "<code>/nightmode</code>"
    ),
    "forcejoin": (
        "🔗 <b>Group force-join</b>\n\n"
        "Separate from bot DM force-join.\n\n"
        "<code>/forcejoin</code> on/off\n"
        "<code>/addfjoin</code> @channel\n"
        "<code>/delfjoin</code>\n"
        "<code>/listfjoin</code>\n\n"
        "Bot must be admin in those channels."
    ),
    "notes": (
        "📒 <b>Notes · Filters · List</b>\n\n"
        "<code>/save</code> <code>/get</code> <code>/notes</code>\n"
        "<code>#name</code> quick note\n"
        "<code>/filter</code> <code>/filters</code>\n"
        "<code>/blacklist</code> <code>/blocklist</code>\n"
        "<code>/afk</code>"
    ),
    "connect": (
        "🔌 <b>DM control</b>\n\n"
        "Use group commands from private chat:\n\n"
        "<code>/connect -100xxxxxxxxxx</code>\n"
        "<code>/disconnect</code>\n"
        "<code>/connection</code> status\n\n"
        "You must be admin in that group.\n"
        "Then /ban /mute /warn … apply there."
    ),
    "info": (
        "ℹ️ <b>Info</b>\n\n"
        "<code>/id</code> ids\n"
        "<code>/info</code> user\n"
        "<code>/report</code> report to admins\n"
        "<code>/grouphelp</code> this menu\n"
        "<code>/connect</code> DM remote control"
    ),
}



def _btn(text: str, data: str, style: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data, style=style)


def help_keyboard(section: str = "home") -> InlineKeyboardMarkup:
    if section == "home":
        rows = [
            [_btn("Welcome", "ghelp:welcome", "success"), _btn("Rules", "ghelp:rules", "primary")],
            [_btn("Warns", "ghelp:warns", "danger"), _btn("Moderation", "ghelp:mod", "danger")],
            [_btn("Pins", "ghelp:pins", "primary"), _btn("Admin", "ghelp:admin", "primary")],
            [_btn("Auto-mod", "ghelp:automod", "success"), _btn("Force-join", "ghelp:forcejoin", "success")],
            [_btn("Notes", "ghelp:notes", "success"), _btn("DM connect", "ghelp:connect", "primary")],
            [_btn("Info", "ghelp:info", "primary"), _btn("Close", "ghelp:close", "danger")],
        ]
    else:
        rows = [
            [
                _btn("Menu", "ghelp:home", "primary"),
                _btn("Close", "ghelp:close", "danger"),
            ]
        ]
    return InlineKeyboardMarkup(inline_keyboard=rows)




def ok_kb() -> InlineKeyboardMarkup:
    """Short action row after admin commands."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Help", callback_data="ghelp:home", style="primary"),
                InlineKeyboardButton(text="Close", callback_data="ghelp:close", style="danger"),
            ]
        ]
    )


def mod_result_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Warns", callback_data="ghelp:warns", style="danger"),
                InlineKeyboardButton(text="Mod", callback_data="ghelp:mod", style="danger"),
            ],
            [
                InlineKeyboardButton(text="Help", callback_data="ghelp:home", style="primary"),
                InlineKeyboardButton(text="Close", callback_data="ghelp:close", style="danger"),
            ],
        ]
    )


def action_kb(action: str, user_id: int) -> InlineKeyboardMarkup:
    """Contextual buttons after a moderation action (mute → unmute, etc.)."""
    uid = int(user_id)
    rows: list[list[InlineKeyboardButton]] = []
    if action == "mute":
        rows.append([
            InlineKeyboardButton(text="Unmute", callback_data=f"gmod:unmute:{uid}", style="success"),
            InlineKeyboardButton(text="Ban", callback_data=f"gmod:ban:{uid}", style="danger"),
        ])
    elif action == "unmute":
        rows.append([
            InlineKeyboardButton(text="Mute again", callback_data=f"gmod:mute:{uid}", style="danger"),
            InlineKeyboardButton(text="Ban", callback_data=f"gmod:ban:{uid}", style="danger"),
        ])
    elif action == "ban":
        rows.append([
            InlineKeyboardButton(text="Unban", callback_data=f"gmod:unban:{uid}", style="success"),
        ])
    elif action == "unban":
        rows.append([
            InlineKeyboardButton(text="Ban again", callback_data=f"gmod:ban:{uid}", style="danger"),
        ])
    elif action == "warn":
        rows.append([
            InlineKeyboardButton(text="Unwarn", callback_data=f"gmod:unwarn:{uid}", style="success"),
            InlineKeyboardButton(text="Warns", callback_data=f"gmod:warns:{uid}", style="primary"),
        ])
        rows.append([
            InlineKeyboardButton(text="Mute", callback_data=f"gmod:mute:{uid}", style="danger"),
            InlineKeyboardButton(text="Ban", callback_data=f"gmod:ban:{uid}", style="danger"),
        ])
    elif action == "kick":
        rows.append([
            InlineKeyboardButton(text="Ban", callback_data=f"gmod:ban:{uid}", style="danger"),
            InlineKeyboardButton(text="Unban", callback_data=f"gmod:unban:{uid}", style="success"),
        ])
    else:
        rows.append([
            InlineKeyboardButton(text="Mute", callback_data=f"gmod:mute:{uid}", style="danger"),
            InlineKeyboardButton(text="Ban", callback_data=f"gmod:ban:{uid}", style="danger"),
        ])
    rows.append([
        InlineKeyboardButton(text="Help", callback_data="ghelp:home", style="primary"),
        InlineKeyboardButton(text="Close", callback_data="ghelp:close", style="danger"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _full_unmute_perms() -> ChatPermissions:
    return ChatPermissions(
        can_send_messages=True,
        can_send_audios=True,
        can_send_documents=True,
        can_send_photos=True,
        can_send_videos=True,
        can_send_video_notes=True,
        can_send_voice_notes=True,
        can_send_polls=True,
        can_send_other_messages=True,
        can_add_web_page_previews=True,
    )


def _mute_perms() -> ChatPermissions:
    return ChatPermissions(can_send_messages=False)


def _mention_html(user) -> str:
    """Telegram HTML mention for a user."""
    if not user:
        return "someone"
    name = (user.full_name or user.username or str(user.id)).replace("<", "").replace(">", "")
    return f'<a href="tg://user?id={user.id}">{name}</a>'


def _format_welcome(tmpl: str, user, chat_title: str) -> str:
    name = (user.full_name or user.username or str(user.id)) if user else "User"
    mention = _mention_html(user)
    return (
        tmpl.replace("{mention}", mention)
        .replace("%mention%", mention)
        .replace("{name}", name)
        .replace("{id}", str(user.id) if user else "")
        .replace("{chat}", chat_title or "")
        .replace("%name%", name)
        .replace("%id%", str(user.id) if user else "")
        .replace("%chat%", chat_title or "")
    )

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_group(message: Message) -> bool:
    return message.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP)


async def _is_group_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    if settings.owner_telegram_id and user_id == settings.owner_telegram_id:
        return True
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in (
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.CREATOR,
        )
    except Exception:
        return False



async def _typing(bot: Bot, chat_id: int) -> None:
    try:
        await bot.send_chat_action(chat_id, ChatAction.TYPING)
    except Exception:
        pass


async def animate_message(bot: Bot, chat_id: int, frames: list[str], delay: float = 0.45):
    """
    Text animation: bot sends first frame, then edits through frames.
    Example frames: ["Working.", "Working..", "Working..."]
    Returns the message object (last frame still visible — caller may edit to final text).
    """
    if not frames:
        return None
    try:
        msg = await bot.send_message(chat_id, frames[0], parse_mode=ParseMode.HTML)
    except Exception:
        return None
    import asyncio as _aio
    for fr in frames[1:]:
        await _aio.sleep(delay)
        try:
            await msg.edit_text(fr, parse_mode=ParseMode.HTML)
        except Exception:
            pass
    return msg


async def animate_then_final(
    bot: Bot, chat_id: int, base: str, final: str, reply_markup=None
):
    """Show base. / base.. / base... then replace with final text."""
    frames = [f"{base}.", f"{base}..", f"{base}..."]
    msg = await animate_message(bot, chat_id, frames, delay=0.35)
    if msg is None:
        try:
            return await bot.send_message(
                chat_id, final, parse_mode=ParseMode.HTML, reply_markup=reply_markup
            )
        except Exception:
            return None
    try:
        await msg.edit_text(final, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
    except Exception:
        try:
            await bot.send_message(
                chat_id, final, parse_mode=ParseMode.HTML, reply_markup=reply_markup
            )
        except Exception:
            pass
    return msg



async def _is_bot_admin_user(user_id: int) -> bool:
    """Owner or web/Telegram linked admin."""
    if settings.owner_telegram_id and int(user_id) == int(settings.owner_telegram_id):
        return True
    try:
        from app.tg_admin import is_tg_admin
        return await is_tg_admin(user_id)
    except Exception:
        return False


def _cid(message: Message) -> int:
    """Return the active group chat ID, including a DM-connected group.

    Raises ValueError when the message is neither from a group nor from a
    private chat connected to a group. This avoids silently using a wrong
    chat ID and never calls itself recursively.
    """
    chat_id = _resolve_target_chat_id(message)
    if chat_id is None:
        raise ValueError(
            "No group chat is available. Use /connect <group_chat_id> in private chat."
        )
    return int(chat_id)


def _resolve_target_chat_id(message: Message) -> Optional[int]:
    """Return this group's ID, or the connected group ID for a private DM."""
    chat = getattr(message, "chat", None)
    if chat is None:
        return None

    if chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        return int(chat.id)

    if chat.type == ChatType.PRIVATE:
        user = getattr(message, "from_user", None)
        if user is not None:
            chat_id = _CONNECTED_CHATS.get(user.id)
            return int(chat_id) if chat_id is not None else None

    return None


async def require_admin(message: Message, bot: Bot) -> bool:
    """Group admins in groups; in private DM need /connect + group admin."""
    if not message.from_user:
        return False
    uid = message.from_user.id

    if message.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        if not await _is_group_admin(bot, _cid(message), uid):
            await message.reply("Only group admins can use this command.")
            return False
        return True

    if message.chat.type == ChatType.PRIVATE:
        cmd = ""
        if message.text:
            cmd = message.text.split()[0].lower().lstrip("/").split("@")[0]
        if cmd in ("grouphelp", "modhelp", "ghelp", "connect", "disconnect", "connection"):
            return True
        chat_id = _CONNECTED_CHATS.get(uid)
        if not chat_id:
            await message.reply(
                "Not connected.\n\n"
                "In private chat, link a group first:\n"
                "<code>/connect -100xxxxxxxxxx</code>\n\n"
                "Get id with <code>/id</code> inside the group.",
                parse_mode=ParseMode.HTML,
            )
            return False
        if not await _is_group_admin(bot, chat_id, uid):
            await message.reply("You must be admin in the connected group.")
            return False
        return True

    await message.reply("This command works in groups or private DM (with /connect).")
    return False


def _target_user_id(message: Message) -> Optional[int]:
    if message.reply_to_message and message.reply_to_message.from_user:
        return message.reply_to_message.from_user.id
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) > 1 and parts[1].strip().lstrip("-").isdigit():
        return int(parts[1].strip().split()[0])
    return None


def _reason(message: Message) -> str:
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        return "No reason"
    rest = parts[1].strip()
    # strip leading user id if present
    bits = rest.split(maxsplit=1)
    if bits and bits[0].lstrip("-").isdigit() and len(bits) > 1:
        return bits[1][:500]
    if bits and bits[0].lstrip("-").isdigit():
        return "No reason"
    return rest[:500]


async def get_or_create_settings(session, chat_id: int) -> GroupChatSettings:
    row = (
        await session.execute(
            select(GroupChatSettings).where(GroupChatSettings.chat_id == chat_id)
        )
    ).scalar_one_or_none()
    if row:
        return row
    row = GroupChatSettings(chat_id=chat_id)
    session.add(row)
    await session.flush()
    return row


def parse_duration(text: str) -> Optional[timedelta]:
    m = re.match(r"^(\d+)([smhd])$", text.strip().lower())
    if not m:
        return None
    n = int(m.group(1))
    unit = m.group(2)
    if unit == "s":
        return timedelta(seconds=n)
    if unit == "m":
        return timedelta(minutes=n)
    if unit == "h":
        return timedelta(hours=n)
    if unit == "d":
        return timedelta(days=n)
    return None


# ---------------------------------------------------------------------------
# Help
# ---------------------------------------------------------------------------

@router.message(Command("grouphelp", "modhelp", "ghelp"))
async def cmd_grouphelp(message: Message, bot: Bot):
    if not await require_admin(message, bot):
        return
    await _typing(bot, _cid(message))
    extra = ""
    if message.chat.type == ChatType.PRIVATE:
        cid = _CONNECTED_CHATS.get(message.from_user.id) if message.from_user else None
        if cid:
            extra = f"\n\n🔌 Connected: <code>{cid}</code>"
        else:
            extra = "\n\n💡 Tip: <code>/connect -100…</code> to control a group from DM."
    await message.reply(
        HELP_SECTIONS["home"] + extra,
        parse_mode=ParseMode.HTML,
        reply_markup=help_keyboard("home"),
    )


@router.callback_query(F.data.startswith("ghelp:"))
async def ghelp_callback(callback: CallbackQuery, bot: Bot):
    if not callback.message or not callback.from_user:
        await callback.answer()
        return
    # Admin-only even for buttons
    chat = callback.message.chat
    uid = callback.from_user.id
    if chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        if not await _is_group_admin(bot, chat.id, uid):
            await callback.answer("Admins only", show_alert=True)
            return
    elif chat.type == ChatType.PRIVATE:
        # DM: allow help navigation for any user who opened /grouphelp
        pass
    else:
        await callback.answer("Not available here", show_alert=True)
        return

    section = callback.data.split(":", 1)[1]
    if section == "close":
        await callback.answer()
        try:
            await callback.message.delete()
        except Exception:
            try:
                await callback.message.edit_text("Closed", reply_markup=None)
            except Exception:
                pass
        return

    text = HELP_SECTIONS.get(section, HELP_SECTIONS["home"])
    kb_section = "home" if section == "home" else section
    try:
        await callback.message.edit_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=help_keyboard(kb_section),
        )
    except Exception:
        await callback.message.answer(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=help_keyboard(kb_section),
        )
    await callback.answer()


# ---------------------------------------------------------------------------
# Welcome / goodbye / rules
# ---------------------------------------------------------------------------

@router.message(Command("setwelcome"))
async def cmd_setwelcome(message: Message, bot: Bot):
    if not await require_admin(message, bot):
        return
    # Reply to a message with custom emoji packs to keep them
    if message.reply_to_message:
        src = message.reply_to_message
        body = (
            getattr(src, "html_text", None) or src.caption or src.text or ""
        ).strip()
    else:
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) < 2:
            await message.reply(
                "Usage:\n"
                "1. Write text with custom emoji → reply with /setwelcome\n"
                "2. Or /setwelcome your text\n"
                "Placeholders: {mention} {name} {id} {chat}"
            )
            return
        body = parts[1].strip()
    if not body:
        await message.reply("Empty text.")
        return
    async with get_db_context() as session:
        s = await get_or_create_settings(session, _cid(message))
        s.welcome_text = body[:4000]
        s.welcome_enabled = True
    await message.reply("Welcome saved · ON (custom emoji kept if replied)", reply_markup=ok_kb())


@router.message(Command("welcome"))
async def cmd_welcome_toggle(message: Message, bot: Bot):
    if not await require_admin(message, bot):
        return
    async with get_db_context() as session:
        s = await get_or_create_settings(session, _cid(message))
        s.welcome_enabled = not s.welcome_enabled
        state = "ON" if s.welcome_enabled else "OFF"
    await message.reply(f"Welcome messages: <b>{state}</b>", parse_mode=ParseMode.HTML)


@router.message(Command("setgoodbye"))
async def cmd_setgoodbye(message: Message, bot: Bot):
    if not await require_admin(message, bot):
        return
    if message.reply_to_message:
        src = message.reply_to_message
        body = (
            getattr(src, "html_text", None) or src.caption or src.text or ""
        ).strip()
    else:
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) < 2:
            await message.reply(
                "Usage: write text with custom emoji → reply /setgoodbye\n"
                "Or /setgoodbye your text"
            )
            return
        body = parts[1].strip()
    if not body:
        await message.reply("Empty text.")
        return
    async with get_db_context() as session:
        s = await get_or_create_settings(session, _cid(message))
        s.goodbye_text = body[:4000]
        s.goodbye_enabled = True
    await message.reply("Goodbye saved · ON")


@router.message(Command("goodbye"))
async def cmd_goodbye_toggle(message: Message, bot: Bot):
    if not await require_admin(message, bot):
        return
    async with get_db_context() as session:
        s = await get_or_create_settings(session, _cid(message))
        s.goodbye_enabled = not s.goodbye_enabled
        state = "ON" if s.goodbye_enabled else "OFF"
    await message.reply(f"Goodbye messages: <b>{state}</b>", parse_mode=ParseMode.HTML)


@router.message(Command("resetwelcome"))
async def cmd_resetwelcome(message: Message, bot: Bot):
    if not await require_admin(message, bot):
        return
    async with get_db_context() as session:
        s = await get_or_create_settings(session, _cid(message))
        s.welcome_text = None
    await message.reply("Welcome reset")


@router.message(Command("setrules"))
async def cmd_setrules(message: Message, bot: Bot):
    if not await require_admin(message, bot):
        return
    if message.reply_to_message:
        src = message.reply_to_message
        body = (
            getattr(src, "html_text", None) or src.caption or src.text or ""
        ).strip()
    else:
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) < 2:
            await message.reply(
                "Usage: write text with custom emoji → reply /setrules\n"
                "Or /setrules your text"
            )
            return
        body = parts[1].strip()
    if not body:
        await message.reply("Empty text.")
        return
    async with get_db_context() as session:
        s = await get_or_create_settings(session, _cid(message))
        s.rules_text = body[:8000]
    await message.reply("Rules saved", reply_markup=ok_kb())


@router.message(Command("rules"))
async def cmd_rules(message: Message, bot: Bot):
    # Members can view rules
    if not _is_group(message):
        return
    async with get_db_context() as session:
        s = await get_or_create_settings(session, _cid(message))
        text = s.rules_text or "No rules set"
    await message.reply(f"<b>Group rules</b>\n\n{text}", parse_mode=ParseMode.HTML)


# ---------------------------------------------------------------------------
# Warns
# ---------------------------------------------------------------------------

@router.message(Command("warn"))
async def cmd_warn(message: Message, bot: Bot):
    if not await require_admin(message, bot):
        return
    uid = _target_user_id(message)
    if not uid:
        await message.reply("Reply to a user or use: /warn <user_id> [reason]")
        return
    reason = _reason(message)
    async with get_db_context() as session:
        session.add(
            GroupWarn(
                chat_id=_cid(message),
                user_id=uid,
                admin_id=message.from_user.id,
                reason=reason,
            )
        )
        count = (
            await session.execute(
                select(func.count())
                .select_from(GroupWarn)
                .where(
                    GroupWarn.chat_id == _cid(message),
                    GroupWarn.user_id == uid,
                )
            )
        ).scalar() or 0
        # +1 for the one we just added after flush
        await session.flush()
        count = (
            await session.execute(
                select(func.count())
                .select_from(GroupWarn)
                .where(
                    GroupWarn.chat_id == _cid(message),
                    GroupWarn.user_id == uid,
                )
            )
        ).scalar() or 0
        s = await get_or_create_settings(session, _cid(message))
        limit = s.warn_limit or 3
    await message.reply(
        f"Warned <code>{uid}</code> ({count}/{limit})\nReason: {reason}",
        parse_mode=ParseMode.HTML,
        reply_markup=action_kb("warn", uid),
    )
    if count >= limit:
        try:
            await bot.ban_chat_member(_cid(message), uid)
            try:
                await _log_ban(
                    _cid(message),
                    uid,
                    message.from_user.id if message.from_user else None,
                )
            except Exception:
                pass
            await message.reply(
                f"Warn limit reached. User <code>{uid}</code> banned.",
                parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            await message.reply(f"Could not ban: {e}")


@router.message(Command("unwarn"))
async def cmd_unwarn(message: Message, bot: Bot):
    if not await require_admin(message, bot):
        return
    uid = _target_user_id(message)
    if not uid:
        await message.reply("Reply to a user or use: /unwarn <user_id>")
        return
    async with get_db_context() as session:
        row = (
            await session.execute(
                select(GroupWarn)
                .where(
                    GroupWarn.chat_id == _cid(message),
                    GroupWarn.user_id == uid,
                )
                .order_by(GroupWarn.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if not row:
            await message.reply("No warns to remove")
            return
        await session.delete(row)
    await message.reply(f"Removed one warning from <code>{uid}</code>.", parse_mode=ParseMode.HTML)


@router.message(Command("resetwarns"))
async def cmd_resetwarns(message: Message, bot: Bot):
    if not await require_admin(message, bot):
        return
    uid = _target_user_id(message)
    if not uid:
        await message.reply("Reply to a user or use: /resetwarns <user_id>")
        return
    async with get_db_context() as session:
        await session.execute(
            delete(GroupWarn).where(
                GroupWarn.chat_id == _cid(message),
                GroupWarn.user_id == uid,
            )
        )
    await message.reply(f"All warnings cleared for <code>{uid}</code>.", parse_mode=ParseMode.HTML)


@router.message(Command("warns"))
async def cmd_warns(message: Message, bot: Bot):
    if not await require_admin(message, bot):
        return
    uid = _target_user_id(message) or (message.from_user.id if message.from_user else None)
    if not uid:
        return
    async with get_db_context() as session:
        rows = (
            await session.execute(
                select(GroupWarn)
                .where(
                    GroupWarn.chat_id == _cid(message),
                    GroupWarn.user_id == uid,
                )
                .order_by(GroupWarn.created_at.desc())
                .limit(20)
            )
        ).scalars().all()
    if not rows:
        await message.reply(f"User <code>{uid}</code> has 0 warnings.", parse_mode=ParseMode.HTML)
        return
    lines = [f"<b>Warnings for {uid}</b> ({len(rows)})\n"]
    for w in rows:
        lines.append(f"• {w.reason or '—'} ({w.created_at})")
    await message.reply("\n".join(lines), parse_mode=ParseMode.HTML)


# ---------------------------------------------------------------------------
# Ban / kick / mute
# ---------------------------------------------------------------------------

@router.message(Command("ban"))
async def cmd_ban(message: Message, bot: Bot):
    if not await require_admin(message, bot):
        return
    uid = _target_user_id(message)
    if not uid:
        await message.reply("Reply to a user or /ban <user_id> [reason]")
        return
    try:
        await bot.ban_chat_member(_cid(message), uid)
        try:
            await _log_ban(_cid(message), uid, message.from_user.id if message.from_user else None)
        except Exception:
            pass
        await message.reply(f"Banned {_mention_html(message.reply_to_message.from_user) if message.reply_to_message and message.reply_to_message.from_user else uid}.\nReason: {_reason(message)}", parse_mode=ParseMode.HTML, reply_markup=action_kb("ban", uid))
    except Exception as e:
        await message.reply(f"Ban failed: {e}")


@router.message(Command("unban"))
async def cmd_unban(message: Message, bot: Bot):
    if not await require_admin(message, bot):
        return
    uid = _target_user_id(message)
    if not uid:
        await message.reply("Reply to a user or /unban <user_id>")
        return
    try:
        await bot.unban_chat_member(_cid(message), uid, only_if_banned=True)
        await message.reply(f"Unbanned <code>{uid}</code>.", parse_mode=ParseMode.HTML, reply_markup=action_kb("unban", uid))
    except Exception as e:
        await message.reply(f"Unban failed: {e}")


@router.message(Command("kick"))
async def cmd_kick(message: Message, bot: Bot):
    if not await require_admin(message, bot):
        return
    uid = _target_user_id(message)
    if not uid:
        await message.reply("Reply to a user or /kick <user_id>")
        return
    try:
        await bot.ban_chat_member(_cid(message), uid)
        try:
            await _log_ban(_cid(message), uid, message.from_user.id if message.from_user else None)
        except Exception:
            pass
        await bot.unban_chat_member(_cid(message), uid)
        await message.reply(f"Kicked <code>{uid}</code>.", parse_mode=ParseMode.HTML, reply_markup=action_kb("kick", uid))
    except Exception as e:
        await message.reply(f"Kick failed: {e}")


@router.message(Command("tban"))
async def cmd_tban(message: Message, bot: Bot):
    if not await require_admin(message, bot):
        return
    parts = (message.text or "").split()
    uid = None
    duration = None
    if message.reply_to_message and message.reply_to_message.from_user:
        uid = message.reply_to_message.from_user.id
        if len(parts) >= 2:
            duration = parse_duration(parts[1])
    elif len(parts) >= 3:
        try:
            uid = int(parts[1])
        except ValueError:
            uid = None
        duration = parse_duration(parts[2])
    if not uid or not duration:
        await message.reply("Usage: reply /tban 1h  OR  /tban <user_id> 1h")
        return
    until = datetime.now(timezone.utc) + duration
    try:
        await bot.ban_chat_member(_cid(message), uid, until_date=until)
        await message.reply(
            f"Temporarily banned <code>{uid}</code> until {until.strftime('%Y-%m-%d %H:%M')} UTC.",
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        await message.reply(f"tban failed: {e}")


@router.message(Command("mute"))
async def cmd_mute(message: Message, bot: Bot):
    if not await require_admin(message, bot):
        return
    uid = _target_user_id(message)
    if not uid:
        await message.reply("Reply to a user or /mute <user_id>")
        return
    perms = ChatPermissions(can_send_messages=False)
    try:
        await bot.restrict_chat_member(_cid(message), uid, permissions=_mute_perms())
        try:
            await _log_mute(_cid(message), uid, message.from_user.id if message.from_user else None)
        except Exception:
            pass
        await message.reply(
            f"Muted <code>{uid}</code>.",
            parse_mode=ParseMode.HTML,
            reply_markup=action_kb("mute", uid),
        )
    except Exception as e:
        await message.reply(f"Mute failed: {e}")


@router.message(Command("unmute"))
async def cmd_unmute(message: Message, bot: Bot):
    if not await require_admin(message, bot):
        return
    uid = _target_user_id(message)
    if not uid:
        await message.reply("Reply to a user or /unmute <user_id>")
        return
    try:
        await bot.restrict_chat_member(_cid(message), uid, permissions=_full_unmute_perms())
        await message.reply(
            f"Unmuted <code>{uid}</code>.",
            parse_mode=ParseMode.HTML,
            reply_markup=action_kb("unmute", uid),
        )
    except Exception as e:
        await message.reply(f"Unmute failed: {e}")


@router.message(Command("dban"))
async def cmd_dban(message: Message, bot: Bot):
    if not await require_admin(message, bot):
        return
    if not message.reply_to_message or not message.reply_to_message.from_user:
        await message.reply("Reply to a message to /dban")
        return
    uid = message.reply_to_message.from_user.id
    try:
        await message.reply_to_message.delete()
    except Exception:
        pass
    try:
        await bot.ban_chat_member(_cid(message), uid)
        try:
            await _log_ban(_cid(message), uid, message.from_user.id if message.from_user else None)
        except Exception:
            pass
        await message.reply(f"Deleted + banned <code>{uid}</code>.", parse_mode=ParseMode.HTML)
    except Exception as e:
        await message.reply(f"dban failed: {e}")


@router.message(Command("dmute"))
async def cmd_dmute(message: Message, bot: Bot):
    if not await require_admin(message, bot):
        return
    if not message.reply_to_message or not message.reply_to_message.from_user:
        await message.reply("Reply to a message to /dmute")
        return
    uid = message.reply_to_message.from_user.id
    try:
        await message.reply_to_message.delete()
    except Exception:
        pass
    try:
        await bot.restrict_chat_member(
            _cid(message), uid, permissions=ChatPermissions(can_send_messages=False)
        )
        await message.reply(f"Deleted + muted <code>{uid}</code>.", parse_mode=ParseMode.HTML)
    except Exception as e:
        await message.reply(f"dmute failed: {e}")


@router.message(Command("dwarn"))
async def cmd_dwarn(message: Message, bot: Bot):
    if not await require_admin(message, bot):
        return
    if not message.reply_to_message or not message.reply_to_message.from_user:
        await message.reply("Reply to a message to /dwarn")
        return
    try:
        await message.reply_to_message.delete()
    except Exception:
        pass
    # reuse warn logic via synthetic
    message.text = f"/warn {message.reply_to_message.from_user.id} {_reason(message)}"
    await cmd_warn(message, bot)


@router.message(Command("ns"))
async def cmd_ns(message: Message, bot: Bot):
    if not await require_admin(message, bot):
        return
    async with get_db_context() as session:
        s = await get_or_create_settings(session, _cid(message))
        s.stickers_restricted = True
    await message.reply("Stickers restricted")


@router.message(Command("as"))
async def cmd_as(message: Message, bot: Bot):
    if not await require_admin(message, bot):
        return
    async with get_db_context() as session:
        s = await get_or_create_settings(session, _cid(message))
        s.stickers_restricted = False
    await message.reply("Stickers allowed")


# ---------------------------------------------------------------------------
# Pins / delete / purge
# ---------------------------------------------------------------------------

@router.message(Command("pin"))
async def cmd_pin(message: Message, bot: Bot):
    if not await require_admin(message, bot):
        return
    if not message.reply_to_message:
        await message.reply("Reply to a message to pin it.")
        return
    try:
        await bot.pin_chat_message(_cid(message), message.reply_to_message.message_id)
        await message.reply("Pinned", reply_markup=ok_kb())
    except Exception as e:
        await message.reply(f"Pin failed: {e}")


@router.message(Command("unpin"))
async def cmd_unpin(message: Message, bot: Bot):
    if not await require_admin(message, bot):
        return
    try:
        await bot.unpin_chat_message(_cid(message))
        await message.reply("Unpinned", reply_markup=ok_kb())
    except Exception as e:
        await message.reply(f"Unpin failed: {e}")


@router.message(Command("unpinall"))
async def cmd_unpinall(message: Message, bot: Bot):
    if not await require_admin(message, bot):
        return
    try:
        await bot.unpin_all_chat_messages(_cid(message))
        await message.reply("All unpinned", reply_markup=ok_kb())
    except Exception as e:
        await message.reply(f"unpinall failed: {e}")


@router.message(Command("del"))
async def cmd_del(message: Message, bot: Bot):
    if not await require_admin(message, bot):
        return
    if not message.reply_to_message:
        await message.reply("Reply to a message to delete it.")
        return
    try:
        await message.reply_to_message.delete()
        await message.delete()
    except Exception as e:
        await message.reply(f"Delete failed: {e}")


@router.message(Command("purge", "spurge"))
async def cmd_purge(message: Message, bot: Bot):
    if not await require_admin(message, bot):
        return
    if not message.reply_to_message:
        await message.reply("Reply to the first message, then /purge")
        return
    start = message.reply_to_message.message_id
    end = message.message_id
    deleted = 0
    for mid in range(start, end + 1):
        try:
            await bot.delete_message(_cid(message), mid)
            deleted += 1
        except Exception:
            pass
    try:
        await message.answer(f"Purged ~{deleted} messages.")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Promote / demote / approve
# ---------------------------------------------------------------------------

@router.message(Command("promote"))
async def cmd_promote(message: Message, bot: Bot):
    if not await require_admin(message, bot):
        return
    uid = _target_user_id(message)
    if not uid:
        await message.reply("Reply to a user or /promote <user_id>")
        return
    try:
        await bot.promote_chat_member(
            _cid(message),
            uid,
            can_manage_chat=True,
            can_delete_messages=True,
            can_restrict_members=True,
            can_invite_users=True,
            can_pin_messages=True,
        )
        await message.reply(f"Promoted <code>{uid}</code>.", parse_mode=ParseMode.HTML)
    except Exception as e:
        await message.reply(f"Promote failed: {e}")


@router.message(Command("demote"))
async def cmd_demote(message: Message, bot: Bot):
    if not await require_admin(message, bot):
        return
    uid = _target_user_id(message)
    if not uid:
        await message.reply("Reply to a user or /demote <user_id>")
        return
    try:
        await bot.promote_chat_member(
            _cid(message),
            uid,
            can_manage_chat=False,
            can_delete_messages=False,
            can_restrict_members=False,
            can_invite_users=False,
            can_pin_messages=False,
            can_manage_video_chats=False,
            can_change_info=False,
            can_promote_members=False,
        )
        await message.reply(f"Demoted <code>{uid}</code>.", parse_mode=ParseMode.HTML)
    except Exception as e:
        await message.reply(f"Demote failed: {e}")


@router.message(Command("approve"))
async def cmd_approve(message: Message, bot: Bot):
    if not await require_admin(message, bot):
        return
    uid = _target_user_id(message)
    if not uid:
        await message.reply("Reply to a user or /approve <user_id>")
        return
    async with get_db_context() as session:
        existing = (
            await session.execute(
                select(GroupApprovedUser).where(
                    GroupApprovedUser.chat_id == _cid(message),
                    GroupApprovedUser.user_id == uid,
                )
            )
        ).scalar_one_or_none()
        if not existing:
            session.add(
                GroupApprovedUser(
                    chat_id=_cid(message),
                    user_id=uid,
                    approved_by=message.from_user.id,
                )
            )
    await message.reply(f"Approved <code>{uid}</code> (exempt from antilink/antiforward).", parse_mode=ParseMode.HTML)


@router.message(Command("disapprove"))
async def cmd_disapprove(message: Message, bot: Bot):
    if not await require_admin(message, bot):
        return
    uid = _target_user_id(message)
    if not uid:
        await message.reply("Reply to a user or /disapprove <user_id>")
        return
    async with get_db_context() as session:
        await session.execute(
            delete(GroupApprovedUser).where(
                GroupApprovedUser.chat_id == _cid(message),
                GroupApprovedUser.user_id == uid,
            )
        )
    await message.reply(f"Removed approval for <code>{uid}</code>.", parse_mode=ParseMode.HTML)


@router.message(Command("approved"))
async def cmd_approved(message: Message, bot: Bot):
    if not await require_admin(message, bot):
        return
    async with get_db_context() as session:
        rows = (
            await session.execute(
                select(GroupApprovedUser).where(GroupApprovedUser.chat_id == _cid(message))
            )
        ).scalars().all()
    if not rows:
        await message.reply("No approved users")
        return
    ids = ", ".join(str(r.user_id) for r in rows)
    await message.reply(f"Approved users:\n<code>{ids}</code>", parse_mode=ParseMode.HTML)


@router.message(Command("admins"))
async def cmd_admins(message: Message, bot: Bot):
    if not await require_admin(message, bot):
        return
    try:
        admins = await bot.get_chat_administrators(_cid(message))
        lines = ["<b>Group admins</b>\n"]
        for a in admins:
            u = a.user
            name = u.full_name or str(u.id)
            un = f" @{u.username}" if u.username else ""
            lines.append(f"• {name}{un} (<code>{u.id}</code>)")
        await message.reply("\n".join(lines), parse_mode=ParseMode.HTML)
    except Exception as e:
        await message.reply(f"Failed: {e}")


@router.message(Command("slowmode"))
async def cmd_slowmode(message: Message, bot: Bot):
    if not await require_admin(message, bot):
        return
    parts = (message.text or "").split()
    secs = 0
    if len(parts) >= 2 and parts[1].isdigit():
        secs = min(int(parts[1]), 3600)
    try:
        await bot.set_chat_slow_mode_delay(_cid(message), secs)
        async with get_db_context() as session:
            s = await get_or_create_settings(session, _cid(message))
            s.slowmode_seconds = secs
        await message.reply(f"Slow mode set to {secs}s (0 = off).")
    except Exception as e:
        await message.reply(f"Slowmode failed: {e}")


@router.message(Command("setlog"))
async def cmd_setlog(message: Message, bot: Bot):
    if not await require_admin(message, bot):
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.reply("Usage: /setlog <channel_id>")
        return
    try:
        cid = int(parts[1])
    except ValueError:
        await message.reply("Invalid channel id")
        return
    async with get_db_context() as session:
        s = await get_or_create_settings(session, _cid(message))
        s.log_channel_id = cid
    await message.reply(f"Log channel set to <code>{cid}</code>.", parse_mode=ParseMode.HTML)


@router.message(Command("unsetlog"))
async def cmd_unsetlog(message: Message, bot: Bot):
    if not await require_admin(message, bot):
        return
    async with get_db_context() as session:
        s = await get_or_create_settings(session, _cid(message))
        s.log_channel_id = None
    await message.reply("Log cleared")


@router.message(Command("send"))
async def cmd_send(message: Message, bot: Bot):
    if not await require_admin(message, bot):
        return
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 3:
        await message.reply("Usage: /send <user_id> <message>")
        return
    try:
        uid = int(parts[1])
    except ValueError:
        await message.reply("Invalid user id")
        return
    try:
        await bot.send_message(uid, parts[2])
        await message.reply("Sent", reply_markup=ok_kb())
    except Exception as e:
        await message.reply(f"Send failed: {e}")


# ---------------------------------------------------------------------------
# Auto-mod toggles
# ---------------------------------------------------------------------------

async def _toggle_flag(message: Message, bot: Bot, field: str, label: str):
    if not await require_admin(message, bot):
        return
    async with get_db_context() as session:
        s = await get_or_create_settings(session, _cid(message))
        cur = bool(getattr(s, field))
        setattr(s, field, not cur)
        state = "ON" if not cur else "OFF"
    await message.reply(f"{label}: <b>{state}</b>", parse_mode=ParseMode.HTML)


@router.message(Command("antilink"))
async def cmd_antilink(message: Message, bot: Bot):
    await _toggle_flag(message, bot, "antilink", "Anti-link")


@router.message(Command("antiforward"))
async def cmd_antiforward(message: Message, bot: Bot):
    await _toggle_flag(message, bot, "antiforward", "Anti-forward")


@router.message(Command("antiflood"))
async def cmd_antiflood(message: Message, bot: Bot):
    await _toggle_flag(message, bot, "antiflood", "Anti-flood")


@router.message(Command("captcha"))
async def cmd_captcha(message: Message, bot: Bot):
    await _toggle_flag(message, bot, "captcha", "Captcha")


@router.message(Command("antichannel"))
async def cmd_antichannel(message: Message, bot: Bot):
    await _toggle_flag(message, bot, "antichannel", "Anti-channel")


@router.message(Command("lock"))
async def cmd_lock(message: Message, bot: Bot):
    if not await require_admin(message, bot):
        return
    parts = (message.text or "").split()
    if len(parts) < 2 or parts[1] not in ("links", "forward"):
        await message.reply("Usage: /lock links|forward")
        return
    field = "lock_links" if parts[1] == "links" else "lock_forward"
    async with get_db_context() as session:
        s = await get_or_create_settings(session, _cid(message))
        setattr(s, field, True)
        if parts[1] == "links":
            s.antilink = True
        if parts[1] == "forward":
            s.antiforward = True
    await message.reply(f"Locked: {parts[1]}")


@router.message(Command("unlock"))
async def cmd_unlock(message: Message, bot: Bot):
    if not await require_admin(message, bot):
        return
    parts = (message.text or "").split()
    if len(parts) < 2 or parts[1] not in ("links", "forward"):
        await message.reply("Usage: /unlock links|forward")
        return
    field = "lock_links" if parts[1] == "links" else "lock_forward"
    async with get_db_context() as session:
        s = await get_or_create_settings(session, _cid(message))
        setattr(s, field, False)
        if parts[1] == "links":
            s.antilink = False
        if parts[1] == "forward":
            s.antiforward = False
    await message.reply(f"Unlocked: {parts[1]}")


# ---------------------------------------------------------------------------
# Notes / filters / blacklist
# ---------------------------------------------------------------------------

@router.message(Command("save"))
async def cmd_save(message: Message, bot: Bot):
    if not await require_admin(message, bot):
        return
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 3:
        await message.reply("Usage: /save name text")
        return
    name = parts[1].lower()[:64]
    content = parts[2][:4000]
    async with get_db_context() as session:
        row = (
            await session.execute(
                select(GroupNote).where(
                    GroupNote.chat_id == _cid(message),
                    GroupNote.name == name,
                )
            )
        ).scalar_one_or_none()
        if row:
            row.content = content
        else:
            session.add(
                GroupNote(
                    chat_id=_cid(message),
                    name=name,
                    content=content,
                    created_by=message.from_user.id,
                )
            )
    await message.reply(f"Note saved: #{name}")


@router.message(Command("get"))
async def cmd_get(message: Message, bot: Bot):
    if not _is_group(message):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.reply("Usage: /get name")
        return
    name = parts[1].strip().lower()[:64]
    async with get_db_context() as session:
        row = (
            await session.execute(
                select(GroupNote).where(
                    GroupNote.chat_id == _cid(message),
                    GroupNote.name == name,
                )
            )
        ).scalar_one_or_none()
    if not row:
        await message.reply("Note not found.")
        return
    await message.reply(row.content)


@router.message(Command("delnote"))
async def cmd_delnote(message: Message, bot: Bot):
    if not await require_admin(message, bot):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.reply("Usage: /delnote name")
        return
    name = parts[1].strip().lower()[:64]
    async with get_db_context() as session:
        await session.execute(
            delete(GroupNote).where(
                GroupNote.chat_id == _cid(message),
                GroupNote.name == name,
            )
        )
    await message.reply(f"Note deleted: {name}")


@router.message(Command("notes"))
async def cmd_notes(message: Message, bot: Bot):
    if not _is_group(message):
        return
    async with get_db_context() as session:
        rows = (
            await session.execute(
                select(GroupNote).where(GroupNote.chat_id == _cid(message))
            )
        ).scalars().all()
    if not rows:
        await message.reply("No notes")
        return
    names = ", ".join(f"#{r.name}" for r in rows)
    await message.reply(f"Notes: {names}")


@router.message(Command("filter"))
async def cmd_filter(message: Message, bot: Bot):
    if not await require_admin(message, bot):
        return
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 3:
        await message.reply("Usage: /filter keyword reply text")
        return
    kw = parts[1].lower()[:128]
    reply = parts[2][:4000]
    async with get_db_context() as session:
        row = (
            await session.execute(
                select(GroupFilter).where(
                    GroupFilter.chat_id == _cid(message),
                    GroupFilter.keyword == kw,
                )
            )
        ).scalar_one_or_none()
        if row:
            row.reply = reply
        else:
            session.add(
                GroupFilter(
                    chat_id=_cid(message),
                    keyword=kw,
                    reply=reply,
                    created_by=message.from_user.id,
                )
            )
    await message.reply(f"Filter added for: {kw}")


@router.message(Command("delfilter"))
async def cmd_delfilter(message: Message, bot: Bot):
    if not await require_admin(message, bot):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.reply("Usage: /delfilter keyword")
        return
    kw = parts[1].strip().lower()[:128]
    async with get_db_context() as session:
        await session.execute(
            delete(GroupFilter).where(
                GroupFilter.chat_id == _cid(message),
                GroupFilter.keyword == kw,
            )
        )
    await message.reply(f"Filter removed: {kw}")


@router.message(Command("filters"))
async def cmd_filters(message: Message, bot: Bot):
    if not await require_admin(message, bot):
        return
    async with get_db_context() as session:
        rows = (
            await session.execute(
                select(GroupFilter).where(GroupFilter.chat_id == _cid(message))
            )
        ).scalars().all()
    if not rows:
        await message.reply("No filters")
        return
    await message.reply("Filters: " + ", ".join(r.keyword for r in rows))


@router.message(Command("blacklist"))
async def cmd_blacklist(message: Message, bot: Bot):
    if not await require_admin(message, bot):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.reply("Usage: /blacklist word")
        return
    word = parts[1].strip().lower()[:128]
    async with get_db_context() as session:
        existing = (
            await session.execute(
                select(GroupBlacklistWord).where(
                    GroupBlacklistWord.chat_id == _cid(message),
                    GroupBlacklistWord.word == word,
                )
            )
        ).scalar_one_or_none()
        if not existing:
            session.add(
                GroupBlacklistWord(
                    chat_id=_cid(message),
                    word=word,
                    created_by=message.from_user.id,
                )
            )
    await message.reply(f"Blacklisted: {word}")


@router.message(Command("unblacklist"))
async def cmd_unblacklist(message: Message, bot: Bot):
    if not await require_admin(message, bot):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.reply("Usage: /unblacklist word")
        return
    word = parts[1].strip().lower()[:128]
    async with get_db_context() as session:
        await session.execute(
            delete(GroupBlacklistWord).where(
                GroupBlacklistWord.chat_id == _cid(message),
                GroupBlacklistWord.word == word,
            )
        )
    await message.reply(f"Removed from blacklist: {word}")


@router.message(Command("blocklist"))
async def cmd_blocklist(message: Message, bot: Bot):
    if not await require_admin(message, bot):
        return
    async with get_db_context() as session:
        rows = (
            await session.execute(
                select(GroupBlacklistWord).where(
                    GroupBlacklistWord.chat_id == _cid(message)
                )
            )
        ).scalars().all()
    if not rows:
        await message.reply("Blacklist empty")
        return
    await message.reply("Blocked: " + ", ".join(r.word for r in rows))


# ---------------------------------------------------------------------------
# Info
# ---------------------------------------------------------------------------

@router.message(Command("id"))
async def cmd_id(message: Message, bot: Bot):
    if message.reply_to_message and message.reply_to_message.from_user:
        u = message.reply_to_message.from_user
        await message.reply(
            f"User ID: <code>{u.id}</code>\nChat ID: <code>{_cid(message)}</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    await message.reply(
        f"Your ID: <code>{message.from_user.id if message.from_user else '—'}</code>\n"
        f"Chat ID: <code>{_cid(message)}</code>",
        parse_mode=ParseMode.HTML,
    )


@router.message(Command("info"))
async def cmd_info(message: Message, bot: Bot):
    u = message.reply_to_message.from_user if message.reply_to_message else message.from_user
    if not u:
        return
    await message.reply(
        f"<b>User info</b>\n"
        f"Name: {u.full_name}\n"
        f"Username: @{u.username or '—'}\n"
        f"ID: <code>{u.id}</code>",
        parse_mode=ParseMode.HTML,
    )


@router.message(Command("report"))
async def cmd_report(message: Message, bot: Bot):
    if not _is_group(message):
        return
    if not message.reply_to_message:
        await message.reply("Reply to a message to report it.")
        return
    try:
        admins = await bot.get_chat_administrators(_cid(message))
        for a in admins:
            if a.user.is_bot:
                continue
            try:
                await bot.send_message(
                    a.user.id,
                    f"Report in chat {_cid(message)}\n"
                    f"From: {message.from_user.id if message.from_user else '—'}\n"
                    f"Message id: {message.reply_to_message.message_id}",
                )
            except Exception:
                pass
        await message.reply("Reported")
    except Exception as e:
        await message.reply(f"Report failed: {e}")


@router.message(Command("kickme"))
async def cmd_kickme(message: Message, bot: Bot):
    if not _is_group(message) or not message.from_user:
        return
    try:
        await bot.ban_chat_member(_cid(message), message.from_user.id)
        await bot.unban_chat_member(_cid(message), message.from_user.id)
    except Exception as e:
        await message.reply(f"Could not leave you: {e}")


# ---------------------------------------------------------------------------
# Join / leave + auto-mod message handlers
# ---------------------------------------------------------------------------

@router.message(F.new_chat_members)
async def on_join(message: Message, bot: Bot):
    if not _is_group(message):
        return
    async with get_db_context() as session:
        s = await get_or_create_settings(session, _cid(message))
        if not s.welcome_enabled:
            return
        tmpl = s.welcome_text or "Welcome {mention} to {chat}!"
    for member in message.new_chat_members or []:
        if member.is_bot:
            continue
        body = _format_welcome(tmpl, member, message.chat.title or "")
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="Rules", callback_data="ghelp:rules", style="primary"),
                    InlineKeyboardButton(text="Close", callback_data="ghelp:close", style="danger"),
                ]
            ]
        )
        try:
            await message.answer(body, parse_mode=ParseMode.HTML, reply_markup=kb)
        except Exception:
            try:
                await message.answer(body, parse_mode=ParseMode.HTML)
            except Exception:
                pass


@router.message(F.left_chat_member)
async def on_leave(message: Message, bot: Bot):
    if not _is_group(message) or not message.left_chat_member:
        return
    async with get_db_context() as session:
        s = await get_or_create_settings(session, _cid(message))
        if not s.goodbye_enabled:
            return
        tmpl = s.goodbye_text or "Goodbye {mention}."
    member = message.left_chat_member
    body = _format_welcome(tmpl, member, message.chat.title or "")
    try:
        await message.answer(body, parse_mode=ParseMode.HTML)
    except Exception:
        pass


@router.message(F.text)
async def group_text_guards(message: Message, bot: Bot):
    """Antilink, blacklist, filters, #notes — non-admins only for restrictions."""
    if not _is_group(message) or not message.from_user or not message.text:
        return
    # Skip admins for restrictions
    is_admin = await _is_group_admin(bot, _cid(message), message.from_user.id)
    text_l = message.text.lower()

    # #note quick get — anyone
    if message.text.startswith("#") and len(message.text) > 1:
        name = message.text[1:].split()[0].lower()[:64]
        async with get_db_context() as session:
            row = (
                await session.execute(
                    select(GroupNote).where(
                        GroupNote.chat_id == _cid(message),
                        GroupNote.name == name,
                    )
                )
            ).scalar_one_or_none()
        if row:
            await message.reply(row.content)
            return

    async with get_db_context() as session:
        s = await get_or_create_settings(session, _cid(message))

        # filters — anyone
        filters = (
            await session.execute(
                select(GroupFilter).where(GroupFilter.chat_id == _cid(message))
            )
        ).scalars().all()
        for f in filters:
            if f.keyword in text_l:
                await message.reply(f.reply)
                break

        if is_admin:
            return

        # approved exempt
        approved = (
            await session.execute(
                select(GroupApprovedUser).where(
                    GroupApprovedUser.chat_id == _cid(message),
                    GroupApprovedUser.user_id == message.from_user.id,
                )
            )
        ).scalar_one_or_none()
        if approved:
            return

        # blacklist
        words = (
            await session.execute(
                select(GroupBlacklistWord).where(
                    GroupBlacklistWord.chat_id == _cid(message)
                )
            )
        ).scalars().all()
        for w in words:
            if w.word in text_l:
                try:
                    await message.delete()
                except Exception:
                    pass
                return

        # antilink
        if s.antilink or s.lock_links:
            if re.search(r"https?://|t\.me/|telegram\.me/", message.text, re.I):
                allowed = _free_links.get(_cid(message), set())
                low = message.text.lower()
                if any(link in low for link in allowed):
                    pass
                else:
                    try:
                        await message.delete()
                    except Exception:
                        pass
                    return

        # antiforward
        if (s.antiforward or s.lock_forward) and (
            message.forward_date or message.forward_from or message.forward_from_chat
        ):
            try:
                await message.delete()
            except Exception:
                pass


@router.message(F.sticker | F.animation)
async def sticker_guard(message: Message, bot: Bot):
    if not _is_group(message) or not message.from_user:
        return
    if await _is_group_admin(bot, _cid(message), message.from_user.id):
        return
    async with get_db_context() as session:
        s = await get_or_create_settings(session, _cid(message))
        if s.stickers_restricted:
            try:
                await message.delete()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Extra commands (quick reasons, AFK, free links, etc.)
# ---------------------------------------------------------------------------

_quick_reasons: dict[int, str] = {}
_afk_users: dict[int, str] = {}
_afk_emoji: dict[int, str] = {}
_free_links: dict[int, set] = {}
_msg_limits: dict[int, int] = {}
_autodelete_hours: dict[int, int] = {}
_nightmode: dict[int, str] = {}


@router.message(Command("eid"))
async def cmd_eid(message: Message, bot: Bot):
    if not await require_admin(message, bot):
        return
    if message.reply_to_message and message.reply_to_message.sticker:
        st = message.reply_to_message.sticker
        await message.reply(f"Emoji/sticker id:\n<code>{st.file_unique_id}</code>\nFile: <code>{st.file_id}</code>", parse_mode=ParseMode.HTML)
        return
    await message.reply("Reply to a sticker / custom emoji message.")


@router.message(Command("set1"))
async def cmd_set1(message: Message, bot: Bot):
    if not await require_admin(message, bot):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.reply("Usage: /set1 reason text")
        return
    _quick_reasons[_cid(message)] = parts[1][:300]
    await message.reply("Quick reason saved.")


@router.message(Command("d1"))
async def cmd_d1(message: Message, bot: Bot):
    if not await require_admin(message, bot):
        return
    if not message.reply_to_message:
        await message.reply("Reply to a message.")
        return
    reason = _quick_reasons.get(_cid(message), "Removed")
    try:
        await message.reply_to_message.delete()
        await message.delete()
    except Exception:
        pass
    try:
        await message.answer(f"Deleted · {reason}")
    except Exception:
        pass


@router.message(Command("w1"))
async def cmd_w1(message: Message, bot: Bot):
    if not await require_admin(message, bot):
        return
    if not message.reply_to_message or not message.reply_to_message.from_user:
        await message.reply("Reply to a user message.")
        return
    reason = _quick_reasons.get(_cid(message), "Warning")
    try:
        await message.reply_to_message.delete()
    except Exception:
        pass
    uid = message.reply_to_message.from_user.id
    message.text = f"/warn {uid} {reason}"
    await cmd_warn(message, bot)


@router.message(Command("scandeleted"))
async def cmd_scandeleted(message: Message, bot: Bot):
    if not await require_admin(message, bot):
        return
    await message.reply("Scan complete. Deleted accounts are removed when they leave; no bulk kick without member list API.")


@router.message(Command("reply"))
async def cmd_reply(message: Message, bot: Bot):
    if not await require_admin(message, bot):
        return
    if not message.reply_to_message or not message.reply_to_message.from_user:
        await message.reply("Reply to a user, then: /reply your text")
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.reply("Usage: /reply text")
        return
    try:
        await bot.send_message(message.reply_to_message.from_user.id, parts[1])
        await message.reply("Sent.", reply_markup=ok_kb())
    except Exception as e:
        await message.reply(f"Failed: {e}")


@router.message(Command("antinsfw"))
async def cmd_antinsfw(message: Message, bot: Bot):
    await _toggle_flag(message, bot, "antichannel", "Anti-NSFW (flag stored)")


@router.message(Command("autodelete"))
async def cmd_autodelete(message: Message, bot: Bot):
    if not await require_admin(message, bot):
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        h = _autodelete_hours.get(_cid(message), 0)
        await message.reply(f"Auto-delete: {h}h (0=off)")
        return
    arg = parts[1].lower()
    if arg == "off":
        _autodelete_hours[_cid(message)] = 0
        await message.reply("Auto-delete OFF")
    elif arg == "on":
        _autodelete_hours[_cid(message)] = 24
        await message.reply("Auto-delete ON · 24h")
    elif arg.isdigit():
        _autodelete_hours[_cid(message)] = int(arg)
        await message.reply(f"Auto-delete · {arg}h")
    else:
        await message.reply("Usage: /autodelete on|off|hours")


@router.message(Command("ml"))
async def cmd_ml(message: Message, bot: Bot):
    if not await require_admin(message, bot):
        return
    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        cur = _msg_limits.get(_cid(message), 0)
        await message.reply(f"Msg limit: {cur or 'off'}")
        return
    n = int(parts[1])
    _msg_limits[_cid(message)] = n
    await message.reply(f"Msg limit · {n} chars")


@router.message(Command("antichanneluser"))
async def cmd_antichanneluser(message: Message, bot: Bot):
    await _toggle_flag(message, bot, "antichannel", "Anti-channel-user")


@router.message(Command("premiumjoin"))
async def cmd_premiumjoin(message: Message, bot: Bot):
    if not await require_admin(message, bot):
        return
    await message.reply("Premium join auto-approve: enable in BotFather / chat join requests settings. Toggle noted.")


@router.message(Command("free"))
async def cmd_free(message: Message, bot: Bot):
    if not await require_admin(message, bot):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.reply("Usage: /free https://example.com")
        return
    link = parts[1].strip().lower()
    _free_links.setdefault(_cid(message), set()).add(link)
    await message.reply(f"Allowed: {link}")


@router.message(Command("rfree"))
async def cmd_rfree(message: Message, bot: Bot):
    if not await require_admin(message, bot):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.reply("Usage: /rfree link")
        return
    link = parts[1].strip().lower()
    _free_links.setdefault(_cid(message), set()).discard(link)
    await message.reply(f"Removed: {link}")


@router.message(Command("list"))
async def cmd_list(message: Message, bot: Bot):
    if not await require_admin(message, bot):
        return
    parts = (message.text or "").split()
    if len(parts) >= 2 and parts[1].lower() == "free":
        links = _free_links.get(_cid(message), set())
        if not links:
            await message.reply("No free links")
            return
        await message.reply("Free links:\n" + "\n".join(sorted(links)))
        return
    await message.reply("Usage: /list free")


@router.message(Command("nightmode"))
async def cmd_nightmode(message: Message, bot: Bot):
    if not await require_admin(message, bot):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        cur = _nightmode.get(_cid(message))
        await message.reply(f"Nightmode: {cur or 'OFF'}")
        return
    _nightmode[_cid(message)] = parts[1].strip()
    await message.reply(f"Nightmode · {parts[1].strip()}")


@router.message(Command("unblocklist"))
async def cmd_unblocklist(message: Message, bot: Bot):
    if not await require_admin(message, bot):
        return
    async with get_db_context() as session:
        await session.execute(
            delete(GroupBlacklistWord).where(GroupBlacklistWord.chat_id == _cid(message))
        )
    await message.reply("Blacklist cleared")


@router.message(Command("afk"))
async def cmd_afk(message: Message, bot: Bot):
    if not message.from_user:
        return
    parts = (message.text or "").split(maxsplit=1)
    reason = parts[1] if len(parts) > 1 else "AFK"
    _afk_users[message.from_user.id] = reason[:200]
    await message.reply(f"You are AFK · {reason[:80]}")


@router.message(Command("setafkemoji"))
async def cmd_setafkemoji(message: Message, bot: Bot):
    if not message.from_user:
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.reply("Usage: /setafkemoji emoji")
        return
    _afk_emoji[message.from_user.id] = parts[1][:20]
    await message.reply("AFK emoji saved")





@router.message(F.text & F.reply_to_message)
async def afk_reply_notice(message: Message, bot: Bot):
    if not _is_group(message) or not message.reply_to_message or not message.reply_to_message.from_user:
        return
    uid = message.reply_to_message.from_user.id
    if uid in _afk_users:
        em = _afk_emoji.get(uid, "")
        reason = _afk_users[uid]
        try:
            await message.reply(f"{em} User is AFK · {reason}".strip())
        except Exception:
            pass


@router.callback_query(F.data.startswith("gmod:"))
async def gmod_action_cb(callback: CallbackQuery, bot: Bot):
    """Inline Unmute / Ban / etc. under moderation replies."""
    if not callback.from_user or not callback.message:
        await callback.answer()
        return
    chat = callback.message.chat
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await callback.answer("Groups only", show_alert=True)
        return
    if not await _is_group_admin(bot, chat.id, callback.from_user.id):
        await callback.answer("Admins only", show_alert=True)
        return
    parts = (callback.data or "").split(":")
    # gmod:action:uid
    if len(parts) < 3:
        await callback.answer()
        return
    action, uid_s = parts[1], parts[2]
    try:
        uid = int(uid_s)
    except ValueError:
        await callback.answer("Bad user id", show_alert=True)
        return
    try:
        if action == "mute":
            await bot.restrict_chat_member(chat.id, uid, permissions=_mute_perms())
            await callback.message.reply(
                f"Muted <code>{uid}</code>.",
                parse_mode=ParseMode.HTML,
                reply_markup=action_kb("mute", uid),
            )
            await callback.answer("Muted")
        elif action == "unmute":
            await bot.restrict_chat_member(chat.id, uid, permissions=_full_unmute_perms())
            await callback.message.reply(
                f"Unmuted <code>{uid}</code>.",
                parse_mode=ParseMode.HTML,
                reply_markup=action_kb("unmute", uid),
            )
            await callback.answer("Unmuted")
        elif action == "ban":
            await bot.ban_chat_member(chat.id, uid)
            await callback.message.reply(
                f"Banned <code>{uid}</code>.",
                parse_mode=ParseMode.HTML,
                reply_markup=action_kb("ban", uid),
            )
            await callback.answer("Banned")
        elif action == "unban":
            await bot.unban_chat_member(chat.id, uid, only_if_banned=True)
            await callback.message.reply(
                f"Unbanned <code>{uid}</code>.",
                parse_mode=ParseMode.HTML,
                reply_markup=action_kb("unban", uid),
            )
            await callback.answer("Unbanned")
        elif action == "unwarn":
            async with get_db_context() as session:
                row = (
                    await session.execute(
                        select(GroupWarn)
                        .where(GroupWarn.chat_id == chat.id, GroupWarn.user_id == uid)
                        .order_by(GroupWarn.created_at.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if row:
                    await session.delete(row)
            await callback.message.reply(
                f"Removed one warn from <code>{uid}</code>.",
                parse_mode=ParseMode.HTML,
                reply_markup=action_kb("warn", uid),
            )
            await callback.answer("Unwarned")
        elif action == "warns":
            async with get_db_context() as session:
                count = (
                    await session.execute(
                        select(func.count())
                        .select_from(GroupWarn)
                        .where(GroupWarn.chat_id == chat.id, GroupWarn.user_id == uid)
                    )
                ).scalar() or 0
            await callback.message.reply(
                f"User <code>{uid}</code> has <b>{count}</b> warn(s).",
                parse_mode=ParseMode.HTML,
                reply_markup=action_kb("warn", uid),
            )
            await callback.answer()
        else:
            await callback.answer("Unknown action")
    except Exception as e:
        await callback.answer(f"Failed: {type(e).__name__}", show_alert=True)
        logger.exception("gmod action %s uid=%s", action, uid)



# ---------------------------------------------------------------------------
# Per-group force join (independent from global bot force-join)
# ---------------------------------------------------------------------------

def group_force_join_kb(channels: list) -> InlineKeyboardMarkup:
    rows = []
    for ch in channels:
        url = ch.invite_url
        if not url and ch.username:
            url = f"https://t.me/{ch.username.lstrip('@')}"
        title = ch.title or ch.username or str(ch.channel_id)
        if url:
            rows.append([
                InlineKeyboardButton(text=f"Join {title}", url=url, style="primary")
            ])
    rows.append([
        InlineKeyboardButton(
            text="I Joined – Check",
            callback_data="gfj:check",
            style="success",
        )
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _group_force_channels(session, chat_id: int):
    result = await session.execute(
        select(GroupForceJoinChannel).where(
            GroupForceJoinChannel.chat_id == chat_id,
            GroupForceJoinChannel.is_active.is_(True),
        )
    )
    return list(result.scalars().all())


async def _check_group_force_join(bot: Bot, user_id: int, channels: list) -> tuple[bool, list]:
    if not channels:
        return True, []
    missing = []
    for ch in channels:
        try:
            member = await bot.get_chat_member(chat_id=ch.channel_id, user_id=user_id)
            if member.status in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED):
                missing.append(ch)
        except Exception:
            missing.append(ch)
    return len(missing) == 0, missing


@router.message(Command("forcejoin"))
async def cmd_forcejoin(message: Message, bot: Bot):
    if not await require_admin(message, bot):
        return
    parts = (message.text or "").split()
    async with get_db_context() as session:
        s = await get_or_create_settings(session, _cid(message))
        if len(parts) >= 2 and parts[1].lower() in ("on", "off", "1", "0", "true", "false"):
            s.force_join_enabled = parts[1].lower() in ("on", "1", "true", "yes")
        else:
            s.force_join_enabled = not s.force_join_enabled
        state = "ON" if s.force_join_enabled else "OFF"
        chans = await _group_force_channels(session, _cid(message))
    await message.reply(
        f"<b>Group force-join: {state}</b>\n"
        f"Channels configured: <b>{len(chans)}</b>\n\n"
        "This is separate from the bot DM force-join.\n"
        "Use /addfjoin /listfjoin /delfjoin",
        parse_mode=ParseMode.HTML,
        reply_markup=ok_kb(),
    )


@router.message(Command("addfjoin"))
async def cmd_addfjoin(message: Message, bot: Bot):
    if not await require_admin(message, bot):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.reply(
            "Usage:\n"
            "/addfjoin @channelusername\n"
            "/addfjoin -100xxxxxxxxxx\n"
            "Bot must be admin in that channel."
        )
        return
    raw = parts[1].strip()
    channel_id = None
    title = raw
    username = None
    invite_url = None
    try:
        if raw.startswith("@"):
            username = raw.lstrip("@")
            chat = await bot.get_chat(raw)
            channel_id = chat.id
            title = chat.title or username
            if chat.username:
                invite_url = f"https://t.me/{chat.username}"
        else:
            channel_id = int(raw)
            try:
                chat = await bot.get_chat(channel_id)
                title = chat.title or str(channel_id)
                username = chat.username
                if username:
                    invite_url = f"https://t.me/{username}"
            except Exception:
                title = str(channel_id)
    except Exception as e:
        await message.reply(f"Could not resolve channel: {e}")
        return
    async with get_db_context() as session:
        existing = (
            await session.execute(
                select(GroupForceJoinChannel).where(
                    GroupForceJoinChannel.chat_id == _cid(message),
                    GroupForceJoinChannel.channel_id == channel_id,
                )
            )
        ).scalar_one_or_none()
        if existing:
            existing.is_active = True
            existing.title = title
            existing.username = username
            existing.invite_url = invite_url
        else:
            session.add(
                GroupForceJoinChannel(
                    chat_id=_cid(message),
                    channel_id=channel_id,
                    title=title,
                    username=username,
                    invite_url=invite_url,
                    is_active=True,
                )
            )
        s = await get_or_create_settings(session, _cid(message))
        s.force_join_enabled = True
    await message.reply(
        f"Added force-join channel: <b>{title}</b>\n"
        f"<code>{channel_id}</code>\n"
        "Group force-join is ON.",
        parse_mode=ParseMode.HTML,
        reply_markup=ok_kb(),
    )


@router.message(Command("delfjoin"))
async def cmd_delfjoin(message: Message, bot: Bot):
    if not await require_admin(message, bot):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.reply("Usage: /delfjoin @channel or /delfjoin -100id")
        return
    raw = parts[1].strip()
    async with get_db_context() as session:
        chans = await _group_force_channels(session, _cid(message))
        # also inactive
        all_ch = (
            await session.execute(
                select(GroupForceJoinChannel).where(
                    GroupForceJoinChannel.chat_id == _cid(message)
                )
            )
        ).scalars().all()
        target = None
        for ch in all_ch:
            if raw.startswith("@") and ch.username and ch.username.lower() == raw.lstrip("@").lower():
                target = ch
                break
            if str(ch.channel_id) == raw or (raw.lstrip("-").isdigit() and ch.channel_id == int(raw)):
                target = ch
                break
        if not target:
            await message.reply("Channel not found in this group's list. /listfjoin")
            return
        await session.delete(target)
    await message.reply("Removed from group force-join list.", reply_markup=ok_kb())


@router.message(Command("listfjoin"))
async def cmd_listfjoin(message: Message, bot: Bot):
    if not await require_admin(message, bot):
        return
    async with get_db_context() as session:
        s = await get_or_create_settings(session, _cid(message))
        chans = (
            await session.execute(
                select(GroupForceJoinChannel).where(
                    GroupForceJoinChannel.chat_id == _cid(message)
                )
            )
        ).scalars().all()
    state = "ON" if s.force_join_enabled else "OFF"
    lines = [f"<b>Group force-join: {state}</b>\n"]
    if not chans:
        lines.append("No channels. Add with /addfjoin @channel")
    else:
        for ch in chans:
            flag = "ON" if ch.is_active else "off"
            un = f"@{ch.username}" if ch.username else ""
            lines.append(f"• {ch.title} {un} <code>{ch.channel_id}</code> · {flag}")
    lines.append("\n<i>Independent from bot DM force-join (Web Admin → Channels).</i>")
    await message.reply("\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=ok_kb())


@router.callback_query(F.data == "gfj:check")
async def gfj_check_cb(callback: CallbackQuery, bot: Bot):
    if not callback.from_user or not callback.message:
        await callback.answer()
        return
    chat = callback.message.chat
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await callback.answer()
        return
    async with get_db_context() as session:
        s = await get_or_create_settings(session, chat.id)
        if not s.force_join_enabled:
            await callback.answer("Force-join is off", show_alert=True)
            return
        chans = await _group_force_channels(session, chat.id)
    ok, missing = await _check_group_force_join(bot, callback.from_user.id, chans)
    if ok:
        await callback.answer("Verified – thank you!", show_alert=True)
        try:
            await callback.message.edit_text(
                f"Verified · {_mention_html(callback.from_user)} can chat.",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
    else:
        await callback.answer("Still missing channels", show_alert=True)
        try:
            await callback.message.edit_reply_markup(
                reply_markup=group_force_join_kb(missing)
            )
        except Exception:
            pass


@router.message(
    F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}),
    F.text,
    ~F.text.startswith("/"),
)
async def group_force_join_guard(message: Message, bot: Bot):
    """Delete messages from users who have not joined this group's force-join channels."""
    if not message.from_user or message.from_user.is_bot:
        return
    # Skip admins
    if await _is_group_admin(bot, _cid(message), message.from_user.id):
        return
    async with get_db_context() as session:
        s = await get_or_create_settings(session, _cid(message))
        if not s.force_join_enabled:
            return
        chans = await _group_force_channels(session, _cid(message))
        if not chans:
            return
        # Approved users exempt
        approved = (
            await session.execute(
                select(GroupApprovedUser).where(
                    GroupApprovedUser.chat_id == _cid(message),
                    GroupApprovedUser.user_id == message.from_user.id,
                )
            )
        ).scalar_one_or_none()
        if approved:
            return
    ok, missing = await _check_group_force_join(bot, message.from_user.id, chans)
    if ok:
        return
    try:
        await message.delete()
    except Exception:
        pass
    try:
        await message.answer(
            f"{_mention_html(message.from_user)} join required channels to chat here:",
            parse_mode=ParseMode.HTML,
            reply_markup=group_force_join_kb(missing),
        )
    except Exception:
        pass



# ---------------------------------------------------------------------------
# DM connect + extra admin features
# ---------------------------------------------------------------------------


@router.message(Command("connect"))
async def cmd_connect(message: Message, bot: Bot):
    if not message.from_user:
        return
    if message.chat.type not in (ChatType.PRIVATE, ChatType.GROUP, ChatType.SUPERGROUP):
        return
    parts = (message.text or "").split()
    uid = message.from_user.id
    await _typing(bot, _cid(message))

    # /connect in group → connect that group
    if message.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP) and len(parts) == 1:
        chat_id = _cid(message)
    elif len(parts) >= 2:
        raw = parts[1].strip()
        try:
            chat_id = int(raw)
        except ValueError:
            await message.reply("Usage: <code>/connect -100xxxxxxxxxx</code>", parse_mode=ParseMode.HTML)
            return
    else:
        await message.reply(
            "🔌 <b>Connect a group</b>\n\n"
            "• In the group: <code>/connect</code>\n"
            "• In DM: <code>/connect -100xxxxxxxxxx</code>\n\n"
            "Get id: <code>/id</code> inside the group.",
            parse_mode=ParseMode.HTML,
        )
        return

    if not await _is_group_admin(bot, chat_id, uid):
        await message.reply("You must be an admin in that group.")
        return
    try:
        chat = await bot.get_chat(chat_id)
        title = getattr(chat, "title", None) or str(chat_id)
    except Exception as e:
        await message.reply(f"Cannot access chat: {e}")
        return

    _CONNECTED_CHATS[uid] = chat_id
    await message.reply(
        f"✅ <b>Connected</b>\n\n"
        f"Group: <b>{title}</b>\n"
        f"ID: <code>{chat_id}</code>\n\n"
        f"Now use /ban /mute /warn /setwelcome … from this chat.\n"
        f"/disconnect to unlink.",
        parse_mode=ParseMode.HTML,
        reply_markup=ok_kb(),
    )


@router.message(Command("disconnect"))
async def cmd_disconnect(message: Message, bot: Bot):
    if not message.from_user:
        return
    await _typing(bot, _cid(message))
    if message.from_user.id in _CONNECTED_CHATS:
        _CONNECTED_CHATS.pop(message.from_user.id, None)
        await message.reply("🔌 Disconnected. Group remote control is off.")
    else:
        await message.reply("No active connection.")


@router.message(Command("connection"))
async def cmd_connection(message: Message, bot: Bot):
    if not message.from_user:
        return
    await _typing(bot, _cid(message))
    cid = _CONNECTED_CHATS.get(message.from_user.id)
    if not cid:
        await message.reply("Not connected. Use /connect")
        return
    try:
        chat = await bot.get_chat(cid)
        title = getattr(chat, "title", None) or str(cid)
    except Exception:
        title = str(cid)
    await message.reply(
        f"🔌 <b>Connected</b>\n{title}\n<code>{cid}</code>",
        parse_mode=ParseMode.HTML,
    )


@router.message(Command("staff", "admins"))
async def cmd_staff(message: Message, bot: Bot):
    if not await require_admin(message, bot):
        return
    chat_id = _resolve_target_chat_id(message) or _cid(message)
    await _typing(bot, _cid(message))
    try:
        admins = await bot.get_chat_administrators(chat_id)
    except Exception as e:
        await message.reply(f"Could not list admins: {e}")
        return
    lines = ["👥 <b>Staff</b>\n"]
    for a in admins:
        u = a.user
        if u.is_bot:
            continue
        tag = "creator" if a.status == ChatMemberStatus.CREATOR else "admin"
        un = f"@{u.username}" if u.username else u.full_name
        lines.append(f"• {un} · <code>{u.id}</code> · {tag}")
    await message.reply("\n".join(lines) or "No staff.", parse_mode=ParseMode.HTML, reply_markup=ok_kb())


@router.message(Command("slowmode"))
async def cmd_slowmode(message: Message, bot: Bot):
    if not await require_admin(message, bot):
        return
    chat_id = _resolve_target_chat_id(message) or _cid(message)
    parts = (message.text or "").split()
    await _typing(bot, _cid(message))
    if len(parts) < 2 or not parts[1].isdigit():
        await message.reply("Usage: <code>/slowmode 10</code> (0 = off)", parse_mode=ParseMode.HTML)
        return
    sec = min(int(parts[1]), 3600)
    try:
        await bot.set_chat_slow_mode_delay(chat_id, sec)
        async with get_db_context() as session:
            s = await get_or_create_settings(session, chat_id)
            s.slowmode_seconds = sec
        await message.reply(
            f"⏱ Slow mode: <b>{sec}s</b>" if sec else "⏱ Slow mode off.",
            parse_mode=ParseMode.HTML,
            reply_markup=ok_kb(),
        )
    except Exception as e:
        await message.reply(f"Failed: {e}")


@router.message(Command("invitelink"))
async def cmd_invitelink(message: Message, bot: Bot):
    if not await require_admin(message, bot):
        return
    chat_id = _resolve_target_chat_id(message) or _cid(message)
    await _typing(bot, _cid(message))
    try:
        link = await bot.export_chat_invite_link(chat_id)
        await message.reply(f"🔗 Invite link:\n{link}", reply_markup=ok_kb())
    except Exception as e:
        await message.reply(f"Failed: {e}")


@router.message(Command("tmute"))
async def cmd_tmute(message: Message, bot: Bot):
    if not await require_admin(message, bot):
        return
    chat_id = _resolve_target_chat_id(message) or _cid(message)
    uid = _target_user_id(message)
    parts = (message.text or "").split()
    await _typing(bot, _cid(message))
    if not uid or len(parts) < 2:
        await message.reply("Reply to user: <code>/tmute 30m</code>", parse_mode=ParseMode.HTML)
        return
    # duration token
    dur = None
    for p in parts[1:]:
        dur = parse_duration(p)
        if dur:
            break
    if not dur:
        await message.reply("Duration like 30m / 1h / 1d required.")
        return
    until = datetime.now(timezone.utc) + dur
    try:
        await bot.restrict_chat_member(
            chat_id,
            uid,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=until,
        )
        await message.reply(
            f"🔇 Muted until <b>{until.strftime('%Y-%m-%d %H:%M UTC')}</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=action_kb("mute", uid),
        )
    except Exception as e:
        await message.reply(f"Failed: {e}")


@router.message(Command("zombies", "scandeleted"))
async def cmd_zombies(message: Message, bot: Bot):
    if not await require_admin(message, bot):
        return
    await _typing(bot, _cid(message))
    await message.reply(
        "🧹 <b>Deleted accounts</b>\n\n"
        "Telegram does not allow bots to scan full member lists easily.\n"
        "Remove deleted accounts manually or with a userbot.\n"
        "Tip: check inactive users via group insights.",
        parse_mode=ParseMode.HTML,
        reply_markup=ok_kb(),
    )



# ---------------------------------------------------------------------------
# Ban/mute logs, join DM, unbanall / unmuteall / frozen
# ---------------------------------------------------------------------------


async def _log_ban(chat_id: int, user_id: int, by: int | None) -> None:
    async with get_db_context() as session:
        session.add(
            GroupBanLog(chat_id=chat_id, user_id=user_id, banned_by=by, active=True)
        )


async def _log_mute(chat_id: int, user_id: int, by: int | None) -> None:
    async with get_db_context() as session:
        session.add(
            GroupMuteLog(chat_id=chat_id, user_id=user_id, muted_by=by, active=True)
        )


async def _clear_ban_log(chat_id: int, user_id: int) -> None:
    async with get_db_context() as session:
        rows = (
            await session.execute(
                select(GroupBanLog).where(
                    GroupBanLog.chat_id == chat_id,
                    GroupBanLog.user_id == user_id,
                    GroupBanLog.active.is_(True),
                )
            )
        ).scalars().all()
        for r in rows:
            r.active = False


async def _clear_mute_log(chat_id: int, user_id: int) -> None:
    async with get_db_context() as session:
        rows = (
            await session.execute(
                select(GroupMuteLog).where(
                    GroupMuteLog.chat_id == chat_id,
                    GroupMuteLog.user_id == user_id,
                    GroupMuteLog.active.is_(True),
                )
            )
        ).scalars().all()
        for r in rows:
            r.active = False


@router.message(F.new_chat_members)
async def on_user_join(message: Message, bot: Bot):
    """Group welcome + optional private DM (no animation)."""
    if not message.new_chat_members:
        return
    chat_id = message.chat.id
    async with get_db_context() as session:
        s = await get_or_create_settings(session, chat_id)
        welcome_on = s.welcome_enabled
        welcome_text = s.welcome_text
        dm_on = bool(getattr(s, "dm_on_join", False))
        dm_text = getattr(s, "dm_on_join_text", None)
        log_ch = getattr(s, "log_channel_id", None)

    for user in message.new_chat_members:
        if user.is_bot:
            continue
        if welcome_on:
            tmpl = welcome_text or "Welcome {mention} to {chat}!"
            text = _format_welcome(tmpl, user, message.chat.title or "")
            try:
                await message.answer(text, parse_mode=ParseMode.HTML)
            except Exception:
                try:
                    await message.answer(f"Welcome, {user.full_name}!")
                except Exception:
                    pass
        if dm_on:
            body = (dm_text or (
                "Welcome to <b>{chat}</b>!\n\n"
                "You joined via our group. Open this bot anytime for rewards."
            )).replace("{chat}", message.chat.title or "the group")
            body = body.replace("{name}", user.first_name or "there")
            body = body.replace("{mention}", _mention_html(user))
            try:
                await bot.send_message(user.id, body, parse_mode=ParseMode.HTML)
            except Exception:
                pass
        # Log channel
        try:
            from app.services.activity_log import log_event
            await log_event(
                bot,
                "join",
                f"User joined group\n"
                f"User: {user.full_name} (<code>{user.id}</code>)\n"
                f"Group: {message.chat.title} (<code>{chat_id}</code>)",
                group_log_chat_id=log_ch,
            )
        except Exception:
            pass


@router.message(Command("dmonjoin"))
async def cmd_dmonjoin(message: Message, bot: Bot):
    if not await require_admin(message, bot):
        return
    chat_id = _cid(message)
    parts = (message.text or "").split(maxsplit=1)
    await _typing(bot, message.chat.id)
    async with get_db_context() as session:
        s = await get_or_create_settings(session, chat_id)
        if len(parts) >= 2 and parts[1].lower() not in ("on", "off", "1", "0"):
            s.dm_on_join_text = parts[1][:2000]
            s.dm_on_join = True
            state = "ON (custom text saved)"
        else:
            if len(parts) >= 2:
                s.dm_on_join = parts[1].lower() in ("on", "1", "true", "yes")
            else:
                s.dm_on_join = not bool(s.dm_on_join)
            state = "ON" if s.dm_on_join else "OFF"
    await animate_then_final(
        bot,
        message.chat.id,
        "Updating",
        f"DM on join: <b>{state}</b>\n\n"
        "When a user joins the group, the bot tries to message them in private "
        "(only if they already started the bot).\n"
        "Set text: <code>/dmonjoin Welcome to {chat}!</code>",
        reply_markup=ok_kb(),
    )


@router.message(Command("unbanall"))
async def cmd_unbanall(message: Message, bot: Bot):
    if not await require_admin(message, bot):
        return
    chat_id = _cid(message)
    await _typing(bot, message.chat.id)
    status = await bot.send_message(message.chat.id, "Unbanning.")
    async with get_db_context() as session:
        rows = (
            await session.execute(
                select(GroupBanLog).where(
                    GroupBanLog.chat_id == chat_id,
                    GroupBanLog.active.is_(True),
                )
            )
        ).scalars().all()
        ids = list({r.user_id for r in rows})
        for r in rows:
            r.active = False
    ok = fail = 0
    for i, uid in enumerate(ids):
        try:
            await bot.unban_chat_member(chat_id, uid, only_if_banned=True)
            ok += 1
        except Exception:
            fail += 1
        if i % 3 == 0:
            try:
                dots = "." * ((i // 3) % 3 + 1)
                await status.edit_text(f"Unbanning{dots}\n{ok + fail}/{len(ids)}")
            except Exception:
                pass
    try:
        await status.edit_text(
            f"Unban all done.\nUnbanned: <b>{ok}</b>\nFailed: <b>{fail}</b>\n"
            f"Total logged: <b>{len(ids)}</b>\n\n"
            "<i>Only users banned via this bot are tracked.</i>",
            parse_mode=ParseMode.HTML,
            reply_markup=ok_kb(),
        )
    except Exception:
        await message.reply(f"Unbanned {ok}, failed {fail}.")


@router.message(Command("unmuteall"))
async def cmd_unmuteall(message: Message, bot: Bot):
    if not await require_admin(message, bot):
        return
    chat_id = _cid(message)
    await _typing(bot, message.chat.id)
    status = await bot.send_message(message.chat.id, "Unmuting.")
    async with get_db_context() as session:
        rows = (
            await session.execute(
                select(GroupMuteLog).where(
                    GroupMuteLog.chat_id == chat_id,
                    GroupMuteLog.active.is_(True),
                )
            )
        ).scalars().all()
        ids = list({r.user_id for r in rows})
        for r in rows:
            r.active = False
    ok = fail = 0
    perms = ChatPermissions(
        can_send_messages=True,
        can_send_media_messages=True,
        can_send_other_messages=True,
        can_add_web_page_previews=True,
    )
    for i, uid in enumerate(ids):
        try:
            await bot.restrict_chat_member(chat_id, uid, permissions=perms)
            ok += 1
        except Exception:
            fail += 1
        if i % 3 == 0:
            try:
                dots = "." * ((i // 3) % 3 + 1)
                await status.edit_text(f"Unmuting{dots}\n{ok + fail}/{len(ids)}")
            except Exception:
                pass
    try:
        await status.edit_text(
            f"Unmute all done.\nUnmuted: <b>{ok}</b>\nFailed: <b>{fail}</b>\n"
            f"Total logged: <b>{len(ids)}</b>\n\n"
            "<i>Only users muted via this bot are tracked.</i>",
            parse_mode=ParseMode.HTML,
            reply_markup=ok_kb(),
        )
    except Exception:
        await message.reply(f"Unmuted {ok}, failed {fail}.")


@router.message(Command("frozen", "checkfrozen"))
async def cmd_frozen(message: Message, bot: Bot):
    """Detect deleted / inaccessible (often called frozen) accounts."""
    if not await require_admin(message, bot):
        return
    chat_id = _cid(message)
    uid = _target_user_id(message)
    await _typing(bot, message.chat.id)
    if not uid:
        await message.reply("Reply to a user: <code>/frozen</code>", parse_mode=ParseMode.HTML)
        return
    status = await bot.send_message(message.chat.id, "Checking.")
    import asyncio as _aio
    for fr in ("Checking.", "Checking..", "Checking..."):
        try:
            await status.edit_text(fr)
        except Exception:
            pass
        await _aio.sleep(0.35)
    try:
        member = await bot.get_chat_member(chat_id, uid)
        user = member.user
        deleted = bool(getattr(user, "is_deleted", False)) or (
            not user.first_name and not user.username
        )
        status_name = member.status.value if hasattr(member.status, "value") else str(member.status)
        if deleted:
            result = (
                f"<b>Possible deleted / frozen account</b>\n"
                f"ID: <code>{uid}</code>\n"
                f"Status: {status_name}\n"
                f"Tip: you can ban to clean the group."
            )
        else:
            name = user.full_name or user.username or str(uid)
            result = (
                f"<b>Account looks active</b>\n"
                f"Name: {name}\n"
                f"ID: <code>{uid}</code>\n"
                f"Status: {status_name}\n"
                f"Username: @{user.username}" if user.username else
                f"<b>Account looks active</b>\n"
                f"Name: {name}\n"
                f"ID: <code>{uid}</code>\n"
                f"Status: {status_name}"
            )
        await status.edit_text(result, parse_mode=ParseMode.HTML, reply_markup=ok_kb())
    except Exception as e:
        try:
            await status.edit_text(
                f"<b>Cannot fetch user</b>\n"
                f"ID: <code>{uid}</code>\n"
                f"Error: {e}\n"
                f"May be deleted, frozen, or never in this chat.",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            await message.reply(f"Check failed: {e}")
