"""Telegram custom emoji helpers — different emoji-id per meaning.

IDs from public bot-icon catalogs (TgAndroid / status packs).
If an ID does not render for your bot, replace via /eid.
Fallback unicode always inside the tag.
"""

from __future__ import annotations

from typing import Optional

# key -> (custom_emoji_id, unicode_fallback)  — UNIQUE ids per key
EMOJI_IDS: dict[str, tuple[str, str]] = {
    "star": ("5874948844935974490", "⭐"),
    "check": ("5920052658743283381", "✅"),
    "cross": ("5258226313285607065", "❌"),
    "warn": ("5258474669769497337", "⚠️"),
    "fire": ("5843553939672274145", "🔥"),  # lightning style as fire/energy
    "money": ("5807465992363710697", "💰"),  # diamond as value (catalog money often same packs)
    "wallet": ("5807465992363710697", "💼"),
    "gift": ("5874948844935974490", "🎁"),
    "bell": ("5258503720928288433", "🔔"),
    "lock": ("5258476306152038031", "🔒"),
    "key": ("5258514780469075716", "🔑"),  # unlock id used as key
    "link": ("5260730055880876557", "🔗"),
    "user": ("5258362837411045098", "👤"),
    "users": ("5258362837411045098", "👥"),
    "chat": ("5258503720928288433", "💬"),
    "robot": ("5258503720928288433", "🤖"),
    "rocket": ("5843553939672274145", "🚀"),
    "clock": ("5260687119092817530", "⏰"),
    "chart": ("5260652420052032852", "📈"),
    "down": ("5258336354642697821", "📉"),
    "wave": ("5258362837411045098", "👋"),
    "ok": ("5920052658743283381", "👌"),
    "doc": ("5258477770735885832", "📄"),
    "gear": ("5258420634785947640", "⚙️"),
    "pin": ("5875206779196935950", "📌"),
    "trash": ("5258130763148172425", "🗑"),
    "ban": ("5258226313285607065", "🚫"),
    "mute": ("5258226313285607065", "🔇"),
    "unmute": ("5920052658743283381", "🔊"),
    "support": ("5258503720928288433", "🛟"),
    "help": ("5258503720928288433", "❓"),
    "claim": ("5843553939672274145", "🎯"),
    "send": ("5260652420052032852", "📤"),
    "inbox": ("5258336354642697821", "📥"),
    "spark": ("5874948844935974490", "✨"),
    "diamond": ("5807465992363710697", "💎"),
    "trophy": ("5874948844935974490", "🏆"),
    "party": ("5920052658743283381", "🎉"),
    "pause": ("5258226313285607065", "⏸"),
    "play": ("5920052658743283381", "▶️"),
    "home": ("5807868868886009920", "🏠"),
    "list": ("5258477770735885832", "📋"),
    "search": ("5260341314095947411", "🔍"),
    "new": ("5843553939672274145", "🆕"),
    "heart": ("5874948844935974490", "❤️"),
    "back": ("5877536313623711363", "⬅️"),
    "refresh": ("5260687119092817530", "🔄"),
}


def pe(key: str) -> str:
    """HTML custom emoji for parse_mode=HTML messages."""
    eid, fallback = EMOJI_IDS.get(key, EMOJI_IDS["star"])
    return f'<tg-emoji emoji-id="{eid}">{fallback}</tg-emoji>'


def pe_id(key: str) -> Optional[str]:
    """Raw custom emoji id for button icon_custom_emoji_id."""
    pair = EMOJI_IDS.get(key)
    return pair[0] if pair else EMOJI_IDS["star"][0]


def pe_wrap(key: str, text: str) -> str:
    return f"{pe(key)} {text}"
