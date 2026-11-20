"""Global activity logging to Telegram LOG_CHANNEL_ID (+ optional group log)."""

from __future__ import annotations

import html
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from aiogram import Bot
from aiogram.enums import ParseMode

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


def _esc(v: Any) -> str:
    return html.escape(str(v if v is not None else ""))


async def log_event(
    bot: Optional[Bot],
    event_type: str,
    text: str,
    *,
    group_log_chat_id: Optional[int] = None,
) -> None:
    if not bot:
        try:
            from app.bot import get_bot
            bot = get_bot()
        except Exception:
            bot = None
    if not bot:
        return

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    body = (
        f"<b>LOG</b> · <code>{_esc(event_type)}</code>\n"
        f"<i>{stamp}</i>\n\n"
        f"{text}"
    )[:4000]

    targets: list[int] = []
    global_id = getattr(settings, "log_channel_id", None)
    if global_id:
        try:
            targets.append(int(global_id))
        except Exception:
            pass
    if group_log_chat_id:
        try:
            targets.append(int(group_log_chat_id))
        except Exception:
            pass

    for cid in dict.fromkeys(targets):
        try:
            await bot.send_message(cid, body, parse_mode=ParseMode.HTML)
        except Exception as exc:
            logger.warning("log_event failed chat=%s: %s", cid, exc)


async def log_user(
    event_type: str,
    *,
    telegram_id: int | None = None,
    username: str | None = None,
    name: str | None = None,
    detail: str = "",
    bot: Optional[Bot] = None,
) -> None:
    uname = f"@{username}" if username else "—"
    lines = [
        f"User: <b>{_esc(name or '—')}</b> ({_esc(uname)})",
        f"ID: <code>{telegram_id or '—'}</code>",
    ]
    if detail:
        lines.append(detail)
    await log_event(bot, event_type, "\n".join(lines))


async def log_claim(
    event_type: str,
    *,
    telegram_id: int | None = None,
    username: str | None = None,
    code: str = "",
    reward: str = "",
    title: str = "",
    reason: str = "",
    bot: Optional[Bot] = None,
) -> None:
    uname = f"@{username}" if username else "—"
    lines = [
        f"User: {_esc(uname)} (<code>{telegram_id or '—'}</code>)",
        f"QR: <code>{_esc(code)}</code>",
    ]
    if title:
        lines.append(f"Title: {_esc(title)}")
    if reward:
        lines.append(f"Reward: <b>{_esc(reward)}</b>")
    if reason:
        lines.append(f"Reason: {_esc(reason)}")
    await log_event(bot, event_type, "\n".join(lines))


async def log_withdraw(
    event_type: str,
    *,
    telegram_id: int | None = None,
    username: str | None = None,
    amount: str = "",
    method: str = "",
    details: str = "",
    reason: str = "",
    bot: Optional[Bot] = None,
) -> None:
    uname = f"@{username}" if username else "—"
    lines = [
        f"User: {_esc(uname)} (<code>{telegram_id or '—'}</code>)",
        f"Amount: <b>{_esc(amount)}</b>",
        f"Method: {_esc(method or '—')}",
    ]
    if details:
        lines.append(f"Details: <code>{_esc(details)}</code>")
    if reason:
        lines.append(f"Reason: {_esc(reason)}")
    await log_event(bot, event_type, "\n".join(lines))
