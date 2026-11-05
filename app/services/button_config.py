"""Configurable user-bot button labels + optional custom emoji icons.

Admins set labels from Telegram /admin → Button labels.
Send a message that includes pack/custom emoji; the first custom emoji
on the message is stored as icon_custom_emoji_id (Bot API).
"""

from __future__ import annotations

import json
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.settings_service import get_setting, set_setting

SETTING_KEY = "bot_button_config"

# key -> default label, style, icon_custom_emoji_id
DEFAULT_BUTTONS: dict[str, dict[str, Any]] = {
    "how_to_claim": {"text": "📖 How to Claim", "style": "primary", "icon": "5258503720928288433"},
    "balance": {"text": "💼 Balance", "style": "primary", "icon": "5807465992363710697"},
    "my_claims": {"text": "📋 My Claims", "style": "primary", "icon": "5258477770735885832"},
    "withdraw": {"text": "💸 Withdraw", "style": "success", "icon": "5807465992363710697"},
    "history": {"text": "📜 History", "style": "primary", "icon": "5260687119092817530"},
    "support": {"text": "🆘 Support", "style": "danger", "icon": "5258503720928288433"},
    "help": {"text": "❓ Help", "style": "primary", "icon": "5258503720928288433"},
    "claim_qr": {"text": "✅ CLAIM QR", "style": "success", "icon": "5843553939672274145"},
    "submit_claim": {"text": "📤 Submit Claim", "style": "success", "icon": "5920052658743283381"},
    "cancel": {"text": "❌ Cancel", "style": "danger", "icon": "5258226313285607065"},
    "check_join": {"text": "✅ I Joined – Check Again", "style": "success", "icon": "5920052658743283381"},
    "upi": {"text": "🇮🇳 UPI ID", "style": "success", "icon": "5807465992363710697"},
    "binance": {"text": "🟡 Binance ID", "style": "success", "icon": "5807465992363710697"},
    "view_balance": {"text": "💼 View Balance", "style": "primary", "icon": "5807465992363710697"},
}

BUTTON_LABELS_HUMAN: dict[str, str] = {
    "how_to_claim": "How to Claim (menu)",
    "balance": "Balance (menu)",
    "my_claims": "My Claims (menu)",
    "withdraw": "Withdraw (menu)",
    "history": "History (menu)",
    "support": "Support (menu)",
    "help": "Help (menu)",
    "claim_qr": "CLAIM QR (inline)",
    "submit_claim": "Submit Claim (inline)",
    "cancel": "Cancel (inline)",
    "check_join": "Force-join check (inline)",
    "upi": "UPI payout (inline)",
    "binance": "Binance payout (inline)",
    "view_balance": "View Balance (inline)",
}


async def get_all_buttons(session: AsyncSession) -> dict[str, dict[str, Any]]:
    raw = await get_setting(session, SETTING_KEY)
    data: dict[str, Any] = {}
    if raw:
        try:
            data = json.loads(raw)
        except Exception:
            data = {}
    out: dict[str, dict[str, Any]] = {}
    for key, default in DEFAULT_BUTTONS.items():
        cur = data.get(key) or {}
        out[key] = {
            "text": (cur.get("text") or default["text"])[:64],
            "style": cur.get("style") or default["style"],
            "icon": cur.get("icon") or default.get("icon"),
        }
    return out


async def get_button(session: AsyncSession, key: str) -> dict[str, Any]:
    all_b = await get_all_buttons(session)
    return all_b.get(key, DEFAULT_BUTTONS.get(key, {"text": key, "style": "primary", "icon": None}))


async def save_button(
    session: AsyncSession,
    key: str,
    *,
    text: str,
    icon: Optional[str] = None,
    style: Optional[str] = None,
    admin_id: Optional[int] = None,
) -> dict[str, Any]:
    if key not in DEFAULT_BUTTONS:
        raise ValueError("UNKNOWN_BUTTON")
    all_b = await get_all_buttons(session)
    entry = all_b[key]
    entry["text"] = (text or entry["text"])[:64]
    if icon is not None:
        entry["icon"] = icon or None
    if style:
        entry["style"] = style
    all_b[key] = entry
    await set_setting(
        session,
        SETTING_KEY,
        json.dumps(all_b, ensure_ascii=False),
        admin_id=admin_id,
        description="User bot button labels and custom emoji icons",
    )
    return entry


async def reset_buttons(session: AsyncSession, admin_id: Optional[int] = None) -> None:
    await set_setting(session, SETTING_KEY, "{}", admin_id=admin_id)


def extract_custom_emoji_id(message) -> Optional[str]:
    """First custom_emoji entity id from text or caption."""
    ents = list(getattr(message, "entities", None) or []) + list(
        getattr(message, "caption_entities", None) or []
    )
    for e in ents:
        et = getattr(e, "type", None)
        et_val = et.value if hasattr(et, "value") else str(et)
        if "custom_emoji" in et_val.lower():
            cid = getattr(e, "custom_emoji_id", None)
            if cid:
                return str(cid)
    return None


def plain_text_without_command(message, command: Optional[str] = None) -> str:
    text = (message.text or message.caption or "").strip()
    if command and text.lower().startswith(command.lower()):
        parts = text.split(maxsplit=1)
        return parts[1].strip() if len(parts) > 1 else ""
    return text
