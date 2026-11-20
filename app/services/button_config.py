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
    "how_to_claim": {"text": "How to Claim", "style": "primary", "icon": "5258503720928288433"},
    "balance": {"text": "Balance", "style": "primary", "icon": "5807465992363710697"},
    "my_claims": {"text": "My Claims", "style": "primary", "icon": "5258477770735885832"},
    "withdraw": {"text": "Withdraw", "style": "success", "icon": "5807465992363710697"},
    "history": {"text": "History", "style": "primary", "icon": "5260687119092817530"},
    "support": {"text": "Support", "style": "danger", "icon": "5258503720928288433"},
    "help": {"text": "Help", "style": "primary", "icon": "5258503720928288433"},
    "claim_qr": {"text": "CLAIM QR", "style": "success", "icon": "5843553939672274145"},
    "submit_claim": {"text": "Submit Claim", "style": "success", "icon": "5920052658743283381"},
    "cancel": {"text": "Cancel", "style": "danger", "icon": "5258226313285607065"},
    "check_join": {"text": "I Joined – Check Again", "style": "success", "icon": "5920052658743283381"},
    "upi": {"text": "UPI ID", "style": "success", "icon": "5807465992363710697"},
    "binance": {"text": "Binance ID", "style": "success", "icon": "5807465992363710697"},
    "view_balance": {"text": "View Balance", "style": "primary", "icon": "5807465992363710697"},
    "home": {"text": "Home", "style": "primary", "icon": "5807868868886009920"},
    "open_link": {"text": "Open Link", "style": "primary", "icon": "5260730055880876557"},
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
    "home": "Home (inline)",
    "open_link": "Open Link (inline)",
}


async def _raw_store(session: AsyncSession) -> dict[str, Any]:
    raw = await get_setting(session, SETTING_KEY)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


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
