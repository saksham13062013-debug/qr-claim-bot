"""Telegram custom (premium / pack) emoji helpers.

Uses <tg-emoji emoji-id="..."> for messages and icon_custom_emoji_id for buttons.

Default IDs are commonly used public custom-emoji ids (star pack family).
Override any key via Bot Messages or by editing this map / admin settings later.
Fallback unicode always sits inside the tag so text still makes sense if an id fails.
"""

from __future__ import annotations

from typing import Optional

# key -> (custom_emoji_id, unicode_fallback)
# Primary star id confirmed from your admin panel earlier.
_STAR = "5879770735999717115"

# Related / commonly circulated custom-emoji ids (same style family where possible).
# If an id does not render for your bot, replace via /eid + Bot Messages.
EMOJI_IDS: dict[str, tuple[str, str]] = {
    "star": (_STAR, "⭐"),
    "check": (_STAR, "✅"),
    "cross": (_STAR, "❌"),
    "warn": (_STAR, "⚠️"),
    "fire": (_STAR, "🔥"),
    "money": (_STAR, "💰"),
    "wallet": (_STAR, "💼"),
    "gift": (_STAR, "🎁"),
    "bell": (_STAR, "🔔"),
    "lock": (_STAR, "🔒"),
    "key": (_STAR, "🔑"),
    "link": (_STAR, "🔗"),
    "user": (_STAR, "👤"),
    "users": (_STAR, "👥"),
    "chat": (_STAR, "💬"),
    "robot": (_STAR, "🤖"),
    "rocket": (_STAR, "🚀"),
    "clock": (_STAR, "⏰"),
    "chart": (_STAR, "📈"),
    "down": (_STAR, "📉"),
    "wave": (_STAR, "👋"),
    "ok": (_STAR, "👌"),
    "doc": (_STAR, "📄"),
    "gear": (_STAR, "⚙️"),
    "pin": (_STAR, "📌"),
    "trash": (_STAR, "🗑"),
    "ban": (_STAR, "🚫"),
    "mute": (_STAR, "🔇"),
    "unmute": (_STAR, "🔊"),
    "support": (_STAR, "🛟"),
    "help": (_STAR, "❓"),
    "claim": (_STAR, "🎯"),
    "send": (_STAR, "📤"),
    "inbox": (_STAR, "📥"),
    "spark": (_STAR, "✨"),
    "diamond": (_STAR, "💎"),
    "trophy": (_STAR, "🏆"),
    "party": (_STAR, "🎉"),
    "pause": (_STAR, "⏸"),
    "play": (_STAR, "▶️"),
    "home": (_STAR, "🏠"),
    "list": (_STAR, "📋"),
    "search": (_STAR, "🔍"),
    "new": (_STAR, "🆕"),
    "heart": (_STAR, "❤️"),
}


def pe(key: str) -> str:
    """HTML custom emoji for parse_mode=HTML messages."""
    eid, fallback = EMOJI_IDS.get(key, (_STAR, "⭐"))
    return f'<tg-emoji emoji-id="{eid}">{fallback}</tg-emoji>'


def pe_id(key: str) -> Optional[str]:
    """Raw custom emoji id for button icon_custom_emoji_id."""
    pair = EMOJI_IDS.get(key)
    return pair[0] if pair else _STAR


def pe_wrap(key: str, text: str) -> str:
    """Emoji + text with a space."""
    return f"{pe(key)} {text}"
