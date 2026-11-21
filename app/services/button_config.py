"""Configurable user-bot button labels + optional custom emoji icons."""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.settings_service import get_setting, set_setting

SETTING_KEY = "bot_button_config"

_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001F9FF"
    "\U00002600-\U000027BF"
    "\U0001FA00-\U0001FAFF"
    "\U0000200D"
    "\U0000FE0F"
    "]+",
    flags=re.UNICODE,
)


def strip_button_emoji(text: str) -> str:
    if not text:
        return text or ""
    cleaned = _EMOJI_RE.sub("", text)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" ·-|")
    return cleaned.strip() or text.strip()


DEFAULT_BUTTONS: dict[str, dict[str, Any]] = {
    # Reply keyboard (main menu)
    "how_to_claim": {"text": "How to Claim", "style": "primary", "icon": "5258503720928288433"},
    "balance": {"text": "Balance", "style": "primary", "icon": "5807465992363710697"},
    "my_claims": {"text": "My Claims", "style": "primary", "icon": "5258477770735885832"},
    "withdraw": {"text": "Withdraw", "style": "success", "icon": "5807465992363710697"},
    "history": {"text": "History", "style": "primary", "icon": "5260687119092817530"},
    "support": {"text": "Support", "style": "danger", "icon": "5258503720928288433"},
    "help": {"text": "Help", "style": "primary", "icon": "5258503720928288433"},
    # Inline — claim flow
    "claim_qr": {"text": "CLAIM QR", "style": "success", "icon": "5843553939672274145"},
    "submit_claim": {"text": "Submit Claim", "style": "success", "icon": "5920052658743283381"},
    "cancel": {"text": "Cancel", "style": "danger", "icon": "5258226313285607065"},
    "open_link": {"text": "Open Link", "style": "primary", "icon": "5260730055880876557"},
    "check_join": {"text": "I Joined – Check Again", "style": "success", "icon": "5920052658743283381"},
    # Inline — payout
    "upi": {"text": "UPI ID", "style": "success", "icon": "5807465992363710697"},
    "binance": {"text": "Binance ID", "style": "success", "icon": "5807465992363710697"},
    "view_balance": {"text": "View Balance", "style": "primary", "icon": "5807465992363710697"},
    # Inline — navigation
    "home": {"text": "Home", "style": "primary", "icon": "5807868868886009920"},
    "open_claimer_profile": {"text": "Open claimer profile", "style": "primary", "icon": "5258362837411045098"},
    # Tools
    "open_mini_app": {"text": "Open Mini App", "style": "primary", "icon": "5260730055880876557"},
    "open_admin_panel": {"text": "Open Admin Panel", "style": "primary", "icon": "5258420634785947640"},
    "login_page": {"text": "Login page", "style": "primary", "icon": "5258514780469075716"},
    "open_scanner": {"text": "Open Scanner", "style": "primary", "icon": "5260341314095947411"},
    # Group help related (labels for consistency)
    "group_help": {"text": "Group Help", "style": "primary", "icon": "5258503720928288433"},
    "unmute": {"text": "Unmute", "style": "success", "icon": "5920052658743283381"},
    "unban": {"text": "Unban", "style": "success", "icon": "5920052658743283381"},
    "unlock": {"text": "Unlock", "style": "success", "icon": "5920052658743283381"},
    "unwarn": {"text": "Remove Warn", "style": "success", "icon": "5920052658743283381"},
    "rules": {"text": "Rules", "style": "primary", "icon": "5258477770735885832"},
    "admins_list": {"text": "Admins", "style": "primary", "icon": "5258362837411045098"},
}

BUTTON_CATEGORIES: dict[str, str] = {
    "how_to_claim": "Reply menu",
    "balance": "Reply menu",
    "my_claims": "Reply menu",
    "withdraw": "Reply menu",
    "history": "Reply menu",
    "support": "Reply menu",
    "help": "Reply menu",
    "claim_qr": "Inline · Claim",
    "submit_claim": "Inline · Claim",
    "cancel": "Inline · Claim",
    "open_link": "Inline · Claim",
    "check_join": "Inline · Force join",
    "upi": "Inline · Withdraw",
    "binance": "Inline · Withdraw",
    "view_balance": "Inline · Withdraw",
    "home": "Inline · Nav",
    "open_claimer_profile": "Inline · Nav",
    "open_mini_app": "Inline · Tools",
    "open_admin_panel": "Inline · Tools",
    "login_page": "Inline · Tools",
    "open_scanner": "Inline · Tools",
    "group_help": "Group",
    "unmute": "Group",
    "unban": "Group",
    "unlock": "Group",
    "unwarn": "Group",
    "rules": "Group",
    "admins_list": "Group",
}

BUTTON_LABELS_HUMAN: dict[str, str] = {
    "how_to_claim": "How to Claim (reply menu)",
    "balance": "Balance (reply + inline)",
    "my_claims": "My Claims (reply + inline)",
    "withdraw": "Withdraw (reply + inline)",
    "history": "History (reply + inline)",
    "support": "Support (reply menu)",
    "help": "Help (reply + inline)",
    "claim_qr": "CLAIM QR (inline)",
    "submit_claim": "Submit Claim (inline)",
    "cancel": "Cancel (inline)",
    "open_link": "Open Link (inline after claim)",
    "check_join": "I Joined – Check Again (inline)",
    "upi": "UPI ID (inline)",
    "binance": "Binance ID (inline)",
    "view_balance": "View Balance (inline)",
    "home": "Home (inline nav)",
    "open_claimer_profile": "Open claimer profile (inline)",
    "open_mini_app": "Open Mini App",
    "open_admin_panel": "Open Admin Panel",
    "login_page": "Login page",
    "open_scanner": "Open Scanner",
    "group_help": "Group Help",
    "unmute": "Unmute",
    "unban": "Unban",
    "unlock": "Unlock",
    "unwarn": "Remove Warn",
    "rules": "Rules",
    "admins_list": "Admins",
}


async def get_all_buttons(session: AsyncSession) -> dict[str, dict[str, Any]]:
    data = await _raw_store(session)
    out: dict[str, dict[str, Any]] = {}
    for key, default in DEFAULT_BUTTONS.items():
        cur = data.get(key) if isinstance(data.get(key), dict) else {}
        text = strip_button_emoji(str(cur.get("text") or default["text"])[:64])
        style = cur.get("style") or default["style"]
        # Respect explicit null icon in store; only fall back if key missing
        if "icon" in cur:
            icon = cur.get("icon") or None
        else:
            icon = default.get("icon")
        out[key] = {"text": text, "style": style, "icon": icon}
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
    data = await _raw_store(session)
    entry = dict(data.get(key) or {})
    entry["text"] = strip_button_emoji((text or DEFAULT_BUTTONS[key]["text"])[:64])
    if style:
        entry["style"] = style
    # Always set icon key when provided (including empty → clear to default on read)
    if icon is not None:
        icon_clean = str(icon).strip()
        entry["icon"] = icon_clean if icon_clean else None
    data[key] = entry
    await set_setting(
        session,
        SETTING_KEY,
        json.dumps(data, ensure_ascii=False),
        admin_id=admin_id,
        description="User bot button labels and custom emoji icons",
    )
    # Return merged view
    return (await get_all_buttons(session))[key]


async def save_all_buttons(
    session: AsyncSession,
    items: list[dict[str, Any]],
    admin_id: Optional[int] = None,
) -> dict[str, dict[str, Any]]:
    data = await _raw_store(session)
    for it in items:
        key = it.get("key")
        if key not in DEFAULT_BUTTONS:
            continue
        entry = dict(data.get(key) or {})
        if "text" in it and it["text"] is not None:
            entry["text"] = strip_button_emoji(str(it["text"])[:64])
        if it.get("style"):
            entry["style"] = it["style"]
        if "icon" in it:
            ic = it.get("icon")
            entry["icon"] = str(ic).strip() if ic else None
        data[key] = entry
    await set_setting(
        session,
        SETTING_KEY,
        json.dumps(data, ensure_ascii=False),
        admin_id=admin_id,
        description="User bot button labels and custom emoji icons",
    )
    return await get_all_buttons(session)


async def reset_buttons(session: AsyncSession, admin_id: Optional[int] = None) -> None:
    await set_setting(session, SETTING_KEY, "{}", admin_id=admin_id)


def extract_custom_emoji_id(message) -> Optional[str]:
    entities = list(getattr(message, "entities", None) or []) + list(
        getattr(message, "caption_entities", None) or []
    )
    for ent in entities:
        if getattr(ent, "type", None) == "custom_emoji" or str(getattr(ent, "type", "")) == "custom_emoji":
            cid = getattr(ent, "custom_emoji_id", None)
            if cid:
                return str(cid)
    return None
