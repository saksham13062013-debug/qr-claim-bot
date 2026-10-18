"""Group management commands — Telegram group admins only."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from aiogram import Bot, F, Router
from aiogram.enums import ChatMemberStatus, ChatType, ParseMode
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
    GroupBlacklistWord,
    GroupChatSettings,
    GroupFilter,
    GroupNote,
    GroupWarn,
)
from sqlalchemy import delete, func, select

logger = logging.getLogger(__name__)
settings = get_settings()
router = Router(name="group_mod")

HELP_SECTIONS: dict[str, str] = {
    "home": (
        "<b>Group Tools</b>\n"
        "<i>Admins only</i>\n\n"
        "Choose a category below."
    ),
    "welcome": (
        "<b>Welcome</b>\n\n"
        "<code>/setwelcome</code> text\n"
        "<code>/welcome</code> on/off\n"
        "<code>/setgoodbye</code> text\n"
        "<code>/goodbye</code> on/off\n"
        "<code>/resetwelcome</code>\n"
        "<code>/eid</code> sticker id\n\n"
        "Placeholders: {name} {id} {chat}"
    ),
    "rules": (
        "<b>Rules</b>\n\n"
        "<code>/setrules</code> text\n"
        "<code>/rules</code> show"
    ),
    "warns": (
        "<b>Warns</b>\n\n"
        "<code>/warn</code> user\n"
        "<code>/unwarn</code>\n"
        "<code>/resetwarns</code>\n"
        "<code>/warns</code> check\n\n"
        "Limit reached → auto ban"
    ),
    "mod": (
        "<b>Moderation</b>\n\n"
        "<code>/ban</code> <code>/unban</code> <code>/kick</code>\n"
        "<code>/tban 1h</code> temp ban\n"
        "<code>/mute</code> <code>/unmute</code>\n"
        "<code>/dban</code> <code>/dmute</code> <code>/dwarn</code>\n"
        "<code>/ns</code> stickers off · <code>/as</code> on\n"
        "<code>/kickme</code>"
    ),
    "pins": (
        "<b>Pins &amp; cleanup</b>\n\n"
        "<code>/pin</code> <code>/unpin</code> <code>/unpinall</code>\n"
        "<code>/del</code> delete message\n"
        "<code>/set1</code> save reason\n"
        "<code>/d1</code> <code>/w1</code> del / del+warn\n"
        "<code>/purge</code> from reply to now"
    ),
    "admin": (
        "<b>Admin</b>\n\n"
        "<code>/promote</code> <code>/demote</code>\n"
        "<code>/approve</code> <code>/disapprove</code>\n"
        "<code>/approved</code> <code>/admins</code>\n"
        "<code>/slowmode</code> seconds\n"
        "<code>/setlog</code> <code>/unsetlog</code>\n"
        "<code>/send</code> <code>/reply</code> DM"
    ),
    "automod": (
        "<b>Auto-mod</b>\n\n"
        "<code>/antilink</code> <code>/antiforward</code>\n"
        "<code>/antiflood</code> <code>/captcha</code>\n"
        "<code>/antichannel</code> <code>/antinsfw</code>\n"
        "<code>/autodelete</code> <code>/ml</code>\n"
        "<code>/lock</code> <code>/unlock</code>\n"
        "<code>/free</code> <code>/rfree</code> <code>/list free</code>\n"
        "<code>/nightmode</code>"
    ),
    "notes": (
        "<b>Notes · Filters · List</b>\n\n"
        "<code>/save</code> <code>/get</code> <code>/notes</code>\n"
        "<code>#name</code> quick note\n"
        "<code>/filter</code> <code>/filters</code>\n"
        "<code>/blacklist</code> <code>/blocklist</code>\n"
        "<code>/afk</code> <code>/setafkemoji</code>"
    ),
    "info": (
        "<b>Info</b>\n\n"
        "<code>/id</code> ids\n"
        "<code>/info</code> user\n"
        "<code>/report</code> to admins\n"
        "<code>/grouphelp</code> this menu"
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
            [_btn("Auto-mod", "ghelp:automod", "success"), _btn("Notes", "ghelp:notes", "success")],
            [_btn("Info", "ghelp:info", "primary"), _btn("Close", "ghelp:close", "danger")],
        ]
    else:
        rows = [
            [
                _btn("« Menu", "ghelp:home", "primary"),
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


async def require_admin(message: Message, bot: Bot) -> bool:
    if not _is_group(message):
        await message.reply("This command only works in groups.")
        return False
    if not message.from_user:
        return False
    if not await _is_group_admin(bot, message.chat.id, message.from_user.id):
        await message.reply("Only group admins can use this command.")
        return False
    return True


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
    await message.reply(
        HELP_SECTIONS["home"],
        parse_mode=ParseMode.HTML,
        reply_markup=help_keyboard("home"),
    )


@router.callback_query(F.data.startswith("ghelp:"))
async def ghelp_callback(callback: CallbackQuery, bot: Bot):
    if not callback.message or not callback.from_user:
        await callback.answer()
        return
    # Admin-only even for buttons
    if callback.message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await callback.answer("Groups only", show_alert=True)
        return
    if not await _is_group_admin(bot, callback.message.chat.id, callback.from_user.id):
        await callback.answer("Admins only", show_alert=True)
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
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.reply("Usage: /setwelcome text\nUse {mention} {name} {id} {chat}")
        return
    async with get_db_context() as session:
        s = await get_or_create_settings(session, message.chat.id)
        s.welcome_text = parts[1][:4000]
        s.welcome_enabled = True
    await message.reply("Welcome saved · ON", reply_markup=ok_kb())


@router.message(Command("welcome"))
async def cmd_welcome_toggle(message: Message, bot: Bot):
    if not await require_admin(message, bot):
        return
    async with get_db_context() as session:
        s = await get_or_create_settings(session, message.chat.id)
        s.welcome_enabled = not s.welcome_enabled
        state = "ON" if s.welcome_enabled else "OFF"
    await message.reply(f"Welcome messages: <b>{state}</b>", parse_mode=ParseMode.HTML)


@router.message(Command("setgoodbye"))
async def cmd_setgoodbye(message: Message, bot: Bot):
    if not await require_admin(message, bot):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.reply("Usage: /setgoodbye Your goodbye text")
        return
    async with get_db_context() as session:
        s = await get_or_create_settings(session, message.chat.id)
        s.goodbye_text = parts[1][:4000]
        s.goodbye_enabled = True
    await message.reply("Goodbye saved · ON")


@router.message(Command("goodbye"))
async def cmd_goodbye_toggle(message: Message, bot: Bot):
    if not await require_admin(message, bot):
        return
    async with get_db_context() as session:
        s = await get_or_create_settings(session, message.chat.id)
        s.goodbye_enabled = not s.goodbye_enabled
        state = "ON" if s.goodbye_enabled else "OFF"
    await message.reply(f"Goodbye messages: <b>{state}</b>", parse_mode=ParseMode.HTML)


@router.message(Command("resetwelcome"))
async def cmd_resetwelcome(message: Message, bot: Bot):
    if not await require_admin(message, bot):
        return
    async with get_db_context() as session:
        s = await get_or_create_settings(session, message.chat.id)
        s.welcome_text = None
    await message.reply("Welcome reset")


@router.message(Command("setrules"))
async def cmd_setrules(message: Message, bot: Bot):
    if not await require_admin(message, bot):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.reply("Usage: /setrules Your rules text")
        return
    async with get_db_context() as session:
        s = await get_or_create_settings(session, message.chat.id)
        s.rules_text = parts[1][:8000]
    await message.reply("Rules saved", reply_markup=ok_kb())


@router.message(Command("rules"))
async def cmd_rules(message: Message, bot: Bot):
    # Members can view rules
    if not _is_group(message):
        return
    async with get_db_context() as session:
        s = await get_or_create_settings(session, message.chat.id)
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
                chat_id=message.chat.id,
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
                    GroupWarn.chat_id == message.chat.id,
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
                    GroupWarn.chat_id == message.chat.id,
                    GroupWarn.user_id == uid,
                )
            )
        ).scalar() or 0
        s = await get_or_create_settings(session, message.chat.id)
        limit = s.warn_limit or 3
    await message.reply(
        f"Warned <code>{uid}</code> ({count}/{limit})\nReason: {reason}",
        parse_mode=ParseMode.HTML,
        reply_markup=mod_result_kb(),
    )
    if count >= limit:
        try:
            await bot.ban_chat_member(message.chat.id, uid)
            await message.reply(f"Warn limit reached. User <code>{uid}</code> banned.", parse_mode=ParseMode.HTML)
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
                    GroupWarn.chat_id == message.chat.id,
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
                GroupWarn.chat_id == message.chat.id,
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
                    GroupWarn.chat_id == message.chat.id,
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
        await bot.ban_chat_member(message.chat.id, uid)
        await message.reply(f"Banned {_mention_html(message.reply_to_message.from_user) if message.reply_to_message and message.reply_to_message.from_user else uid}.\nReason: {_reason(message)}", parse_mode=ParseMode.HTML, reply_markup=mod_result_kb())
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
        await bot.unban_chat_member(message.chat.id, uid, only_if_banned=True)
        await message.reply(f"Unbanned <code>{uid}</code>.", parse_mode=ParseMode.HTML, reply_markup=mod_result_kb())
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
        await bot.ban_chat_member(message.chat.id, uid)
        await bot.unban_chat_member(message.chat.id, uid)
        await message.reply(f"Kicked <code>{uid}</code>.", parse_mode=ParseMode.HTML, reply_markup=mod_result_kb())
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
        await bot.ban_chat_member(message.chat.id, uid, until_date=until)
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
        await bot.restrict_chat_member(message.chat.id, uid, permissions=perms)
        await message.reply(f"Muted <code>{uid}</code>.", parse_mode=ParseMode.HTML, reply_markup=mod_result_kb())
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
    perms = ChatPermissions(
        can_send_messages=True,
        can_send_media_messages=True,
        can_send_other_messages=True,
        can_add_web_page_previews=True,
    )
    try:
        await bot.restrict_chat_member(message.chat.id, uid, permissions=perms)
        await message.reply(f"Unmuted <code>{uid}</code>.", parse_mode=ParseMode.HTML, reply_markup=mod_result_kb())
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
        await bot.ban_chat_member(message.chat.id, uid)
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
            message.chat.id, uid, permissions=ChatPermissions(can_send_messages=False)
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
        s = await get_or_create_settings(session, message.chat.id)
        s.stickers_restricted = True
    await message.reply("Stickers restricted")


@router.message(Command("as"))
async def cmd_as(message: Message, bot: Bot):
    if not await require_admin(message, bot):
        return
    async with get_db_context() as session:
        s = await get_or_create_settings(session, message.chat.id)
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
        await bot.pin_chat_message(message.chat.id, message.reply_to_message.message_id)
        await message.reply("Pinned", reply_markup=ok_kb())
    except Exception as e:
        await message.reply(f"Pin failed: {e}")


@router.message(Command("unpin"))
async def cmd_unpin(message: Message, bot: Bot):
    if not await require_admin(message, bot):
        return
    try:
        await bot.unpin_chat_message(message.chat.id)
        await message.reply("Unpinned", reply_markup=ok_kb())
    except Exception as e:
        await message.reply(f"Unpin failed: {e}")


@router.message(Command("unpinall"))
async def cmd_unpinall(message: Message, bot: Bot):
    if not await require_admin(message, bot):
        return
    try:
        await bot.unpin_all_chat_messages(message.chat.id)
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
            await bot.delete_message(message.chat.id, mid)
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
            message.chat.id,
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
            message.chat.id,
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
                    GroupApprovedUser.chat_id == message.chat.id,
                    GroupApprovedUser.user_id == uid,
                )
            )
        ).scalar_one_or_none()
        if not existing:
            session.add(
                GroupApprovedUser(
                    chat_id=message.chat.id,
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
                GroupApprovedUser.chat_id == message.chat.id,
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
                select(GroupApprovedUser).where(GroupApprovedUser.chat_id == message.chat.id)
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
        admins = await bot.get_chat_administrators(message.chat.id)
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
        await bot.set_chat_slow_mode_delay(message.chat.id, secs)
        async with get_db_context() as session:
            s = await get_or_create_settings(session, message.chat.id)
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
        s = await get_or_create_settings(session, message.chat.id)
        s.log_channel_id = cid
    await message.reply(f"Log channel set to <code>{cid}</code>.", parse_mode=ParseMode.HTML)


@router.message(Command("unsetlog"))
async def cmd_unsetlog(message: Message, bot: Bot):
    if not await require_admin(message, bot):
        return
    async with get_db_context() as session:
        s = await get_or_create_settings(session, message.chat.id)
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
        s = await get_or_create_settings(session, message.chat.id)
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
        s = await get_or_create_settings(session, message.chat.id)
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
        s = await get_or_create_settings(session, message.chat.id)
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
                    GroupNote.chat_id == message.chat.id,
                    GroupNote.name == name,
                )
            )
        ).scalar_one_or_none()
        if row:
            row.content = content
        else:
            session.add(
                GroupNote(
                    chat_id=message.chat.id,
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
                    GroupNote.chat_id == message.chat.id,
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
                GroupNote.chat_id == message.chat.id,
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
                select(GroupNote).where(GroupNote.chat_id == message.chat.id)
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
                    GroupFilter.chat_id == message.chat.id,
                    GroupFilter.keyword == kw,
                )
            )
        ).scalar_one_or_none()
        if row:
            row.reply = reply
        else:
            session.add(
                GroupFilter(
                    chat_id=message.chat.id,
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
                GroupFilter.chat_id == message.chat.id,
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
                select(GroupFilter).where(GroupFilter.chat_id == message.chat.id)
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
                    GroupBlacklistWord.chat_id == message.chat.id,
                    GroupBlacklistWord.word == word,
                )
            )
        ).scalar_one_or_none()
        if not existing:
            session.add(
                GroupBlacklistWord(
                    chat_id=message.chat.id,
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
                GroupBlacklistWord.chat_id == message.chat.id,
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
                    GroupBlacklistWord.chat_id == message.chat.id
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
            f"User ID: <code>{u.id}</code>\nChat ID: <code>{message.chat.id}</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    await message.reply(
        f"Your ID: <code>{message.from_user.id if message.from_user else '—'}</code>\n"
        f"Chat ID: <code>{message.chat.id}</code>",
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
        admins = await bot.get_chat_administrators(message.chat.id)
        for a in admins:
            if a.user.is_bot:
                continue
            try:
                await bot.send_message(
                    a.user.id,
                    f"Report in chat {message.chat.id}\n"
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
        await bot.ban_chat_member(message.chat.id, message.from_user.id)
        await bot.unban_chat_member(message.chat.id, message.from_user.id)
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
        s = await get_or_create_settings(session, message.chat.id)
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
        s = await get_or_create_settings(session, message.chat.id)
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
    is_admin = await _is_group_admin(bot, message.chat.id, message.from_user.id)
    text_l = message.text.lower()

    # #note quick get — anyone
    if message.text.startswith("#") and len(message.text) > 1:
        name = message.text[1:].split()[0].lower()[:64]
        async with get_db_context() as session:
            row = (
                await session.execute(
                    select(GroupNote).where(
                        GroupNote.chat_id == message.chat.id,
                        GroupNote.name == name,
                    )
                )
            ).scalar_one_or_none()
        if row:
            await message.reply(row.content)
            return

    async with get_db_context() as session:
        s = await get_or_create_settings(session, message.chat.id)

        # filters — anyone
        filters = (
            await session.execute(
                select(GroupFilter).where(GroupFilter.chat_id == message.chat.id)
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
                    GroupApprovedUser.chat_id == message.chat.id,
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
                    GroupBlacklistWord.chat_id == message.chat.id
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
                allowed = _free_links.get(message.chat.id, set())
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
    if await _is_group_admin(bot, message.chat.id, message.from_user.id):
        return
    async with get_db_context() as session:
        s = await get_or_create_settings(session, message.chat.id)
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
    _quick_reasons[message.chat.id] = parts[1][:300]
    await message.reply("Quick reason saved.")


@router.message(Command("d1"))
async def cmd_d1(message: Message, bot: Bot):
    if not await require_admin(message, bot):
        return
    if not message.reply_to_message:
        await message.reply("Reply to a message.")
        return
    reason = _quick_reasons.get(message.chat.id, "Removed")
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
    reason = _quick_reasons.get(message.chat.id, "Warning")
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
        h = _autodelete_hours.get(message.chat.id, 0)
        await message.reply(f"Auto-delete: {h}h (0=off)")
        return
    arg = parts[1].lower()
    if arg == "off":
        _autodelete_hours[message.chat.id] = 0
        await message.reply("Auto-delete OFF")
    elif arg == "on":
        _autodelete_hours[message.chat.id] = 24
        await message.reply("Auto-delete ON · 24h")
    elif arg.isdigit():
        _autodelete_hours[message.chat.id] = int(arg)
        await message.reply(f"Auto-delete · {arg}h")
    else:
        await message.reply("Usage: /autodelete on|off|hours")


@router.message(Command("ml"))
async def cmd_ml(message: Message, bot: Bot):
    if not await require_admin(message, bot):
        return
    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        cur = _msg_limits.get(message.chat.id, 0)
        await message.reply(f"Msg limit: {cur or 'off'}")
        return
    n = int(parts[1])
    _msg_limits[message.chat.id] = n
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
    _free_links.setdefault(message.chat.id, set()).add(link)
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
    _free_links.setdefault(message.chat.id, set()).discard(link)
    await message.reply(f"Removed: {link}")


@router.message(Command("list"))
async def cmd_list(message: Message, bot: Bot):
    if not await require_admin(message, bot):
        return
    parts = (message.text or "").split()
    if len(parts) >= 2 and parts[1].lower() == "free":
        links = _free_links.get(message.chat.id, set())
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
        cur = _nightmode.get(message.chat.id)
        await message.reply(f"Nightmode: {cur or 'OFF'}")
        return
    _nightmode[message.chat.id] = parts[1].strip()
    await message.reply(f"Nightmode · {parts[1].strip()}")


@router.message(Command("unblocklist"))
async def cmd_unblocklist(message: Message, bot: Bot):
    if not await require_admin(message, bot):
        return
    async with get_db_context() as session:
        await session.execute(
            delete(GroupBlacklistWord).where(GroupBlacklistWord.chat_id == message.chat.id)
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
