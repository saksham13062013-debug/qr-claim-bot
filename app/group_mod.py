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
        "<b>Group Management</b>\n"
        "<i>Admin-only · groups/supergroups</i>\n\n"
        "Tap a category below for commands.\n"
        "Green = safe / info · Blue = tools · Red = moderation"
    ),
    "welcome": (
        "<b>Welcome &amp; Goodbye</b>\n\n"
        "/setwelcome — Set welcome message\n"
        "/welcome — Toggle welcome on/off\n"
        "/setgoodbye — Set goodbye message\n"
        "/goodbye — Toggle goodbye on/off\n"
        "/resetwelcome — Reset welcome text\n\n"
        "Placeholders: <code>{name}</code> <code>{id}</code> <code>{chat}</code>"
    ),
    "rules": (
        "<b>Rules</b>\n\n"
        "/setrules — Set group rules\n"
        "/rules — Show rules (members can use)"
    ),
    "warns": (
        "<b>Warn system</b>\n\n"
        "/warn — Warn a user (reply or ID)\n"
        "/unwarn — Remove one warning\n"
        "/resetwarns — Clear all warnings\n"
        "/warns — Check warnings\n\n"
        "At warn limit the user is banned automatically."
    ),
    "mod": (
        "<b>Moderation</b>\n\n"
        "/ban /unban /kick\n"
        "/tban 1h — temporary ban\n"
        "/mute /unmute\n"
        "/dban /dmute /dwarn — delete + action\n"
        "/ns — restrict stickers &amp; GIFs\n"
        "/as — allow stickers &amp; GIFs"
    ),
    "pins": (
        "<b>Pins &amp; cleanup</b>\n\n"
        "/pin — Pin replied message\n"
        "/unpin — Unpin latest\n"
        "/unpinall — Unpin all\n"
        "/del — Delete replied message\n"
        "/purge — Delete from reply to now"
    ),
    "admin": (
        "<b>Admin tools</b>\n\n"
        "/promote /demote\n"
        "/approve /disapprove /approved\n"
        "/admins — List admins\n"
        "/slowmode [seconds]\n"
        "/setlog /unsetlog\n"
        "/send &lt;user_id&gt; &lt;text&gt;"
    ),
    "automod": (
        "<b>Auto-mod &amp; locks</b>\n\n"
        "/antilink /antiforward /antiflood\n"
        "/captcha /antichannel\n"
        "/lock links|forward\n"
        "/unlock links|forward"
    ),
    "notes": (
        "<b>Notes · Filters · Blacklist</b>\n\n"
        "/save /get /delnote /notes\n"
        "#name — quick note\n"
        "/filter /delfilter /filters\n"
        "/blacklist /unblacklist /blocklist"
    ),
    "info": (
        "<b>Info</b>\n\n"
        "/id — User or chat ID\n"
        "/info — User details\n"
        "/report — Report to admins (reply)\n"
        "/kickme — Leave the group\n"
        "/grouphelp — This menu"
    ),
}


def help_keyboard(section: str = "home") -> InlineKeyboardMarkup:
    if section == "home":
        rows = [
            [
                InlineKeyboardButton(text="Welcome", callback_data="ghelp:welcome", style="success"),
                InlineKeyboardButton(text="Rules", callback_data="ghelp:rules", style="primary"),
            ],
            [
                InlineKeyboardButton(text="Warns", callback_data="ghelp:warns", style="danger"),
                InlineKeyboardButton(text="Moderation", callback_data="ghelp:mod", style="danger"),
            ],
            [
                InlineKeyboardButton(text="Pins & Clean", callback_data="ghelp:pins", style="primary"),
                InlineKeyboardButton(text="Admin", callback_data="ghelp:admin", style="primary"),
            ],
            [
                InlineKeyboardButton(text="Auto-mod", callback_data="ghelp:automod", style="success"),
                InlineKeyboardButton(text="Notes & Filters", callback_data="ghelp:notes", style="success"),
            ],
            [
                InlineKeyboardButton(text="Info", callback_data="ghelp:info", style="primary"),
                InlineKeyboardButton(text="Close", callback_data="ghelp:close", style="danger"),
            ],
        ]
    else:
        rows = [
            [
                InlineKeyboardButton(text="Back", callback_data="ghelp:home", style="primary"),
                InlineKeyboardButton(text="Close", callback_data="ghelp:close", style="danger"),
            ]
        ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


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
                await callback.message.edit_text("Closed.", reply_markup=None)
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
        await message.reply("Usage: /setwelcome Your welcome text\nUse {name} {id} {chat}")
        return
    async with get_db_context() as session:
        s = await get_or_create_settings(session, message.chat.id)
        s.welcome_text = parts[1][:4000]
        s.welcome_enabled = True
    await message.reply("Welcome message saved and enabled.")


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
    await message.reply("Goodbye message saved and enabled.")


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
    await message.reply("Welcome text reset to default.")


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
    await message.reply("Rules saved.")


@router.message(Command("rules"))
async def cmd_rules(message: Message, bot: Bot):
    # Members can view rules
    if not _is_group(message):
        return
    async with get_db_context() as session:
        s = await get_or_create_settings(session, message.chat.id)
        text = s.rules_text or "No rules set yet."
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
        f"User <code>{uid}</code> warned ({count}/{limit}).\nReason: {reason}",
        parse_mode=ParseMode.HTML,
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
            await message.reply("No warnings to remove.")
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
        await message.reply(f"Banned <code>{uid}</code>.\nReason: {_reason(message)}", parse_mode=ParseMode.HTML)
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
        await message.reply(f"Unbanned <code>{uid}</code>.", parse_mode=ParseMode.HTML)
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
        await message.reply(f"Kicked <code>{uid}</code>.", parse_mode=ParseMode.HTML)
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
        await message.reply(f"Muted <code>{uid}</code>.", parse_mode=ParseMode.HTML)
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
        await message.reply(f"Unmuted <code>{uid}</code>.", parse_mode=ParseMode.HTML)
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
    await message.reply("Stickers/GIFs restricted (bot will delete them when possible).")


@router.message(Command("as"))
async def cmd_as(message: Message, bot: Bot):
    if not await require_admin(message, bot):
        return
    async with get_db_context() as session:
        s = await get_or_create_settings(session, message.chat.id)
        s.stickers_restricted = False
    await message.reply("Stickers/GIFs allowed again.")


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
        await message.reply("Pinned.")
    except Exception as e:
        await message.reply(f"Pin failed: {e}")


@router.message(Command("unpin"))
async def cmd_unpin(message: Message, bot: Bot):
    if not await require_admin(message, bot):
        return
    try:
        await bot.unpin_chat_message(message.chat.id)
        await message.reply("Unpinned latest.")
    except Exception as e:
        await message.reply(f"Unpin failed: {e}")


@router.message(Command("unpinall"))
async def cmd_unpinall(message: Message, bot: Bot):
    if not await require_admin(message, bot):
        return
    try:
        await bot.unpin_all_chat_messages(message.chat.id)
        await message.reply("All pins removed.")
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
        await message.reply("No approved users.")
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
    await message.reply("Log channel removed.")


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
        await message.reply("Message sent.")
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
        await message.reply("No notes.")
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
        await message.reply("No filters.")
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
        await message.reply("Blacklist empty.")
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
        await message.reply("Reported to admins.")
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
        tmpl = s.welcome_text or "Welcome {name} to {chat}!"
    for m in message.new_chat_members or []:
        text = (
            tmpl.replace("{name}", m.full_name or str(m.id))
            .replace("{id}", str(m.id))
            .replace("{chat}", message.chat.title or "")
        )
        try:
            await message.answer(text)
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
        tmpl = s.goodbye_text or "Goodbye {name}."
    m = message.left_chat_member
    text = (
        tmpl.replace("{name}", m.full_name or str(m.id))
        .replace("{id}", str(m.id))
        .replace("{chat}", message.chat.title or "")
    )
    try:
        await message.answer(text)
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
