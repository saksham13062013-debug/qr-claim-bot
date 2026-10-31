"""Editable bot response templates (HTML + custom emoji packs supported).

Admins can set templates via web admin → Bot Messages.
Placeholders: {name}, {reward}, {title}, {reason}, etc.

Custom emoji packs (stars, VIP, checks):
  1. Reply to pack emoji message → /eid → copy ID
  2. Paste: <tg-emoji emoji-id="ID">⭐</tg-emoji>
"""

from __future__ import annotations

import re

from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.settings_service import get_setting, set_setting

# Defaults use Unicode emoji (work for everyone).
# Replace any emoji with pack HTML via Bot Messages for true custom packs.
DEFAULT_TEMPLATES: dict[str, str] = {
    "msg_welcome": (
        "👋 <b>Welcome, {name}!</b>\n\n"
        "{welcome_body}\n\n"
        "⬇️ Use the menu below to get started."
    ),
    "msg_welcome_body": (
        "💎 Claim exclusive one-time QR rewards\n"
        "💰 Track your balance & withdraw easily\n\n"
        "<b>How it works</b>\n"
        "1️⃣ Join required channels (if asked)\n"
        "2️⃣ Tap <b>CLAIM QR</b> on a reward\n"
        "3️⃣ Submit your claim\n"
        "4️⃣ Get paid after approval\n"
        "5️⃣ Withdraw via UPI / Binance\n\n"
        "⚠️ Each QR can be claimed by only <b>ONE</b> user."
    ),
    "msg_force_join": (
        "🔒 <b>Join required channels</b>\n\n"
        "You must join the channel(s) below before using the bot.\n\n"
        "✅ After joining, tap <b>I Joined – Check Again</b>."
    ),
    "msg_force_join_ok": (
        "✅ <b>Verified</b> · Welcome, {name}!\n\n{welcome_body}"
    ),
    "msg_how_to_claim": (
        "📖 <b>How to claim a reward</b>\n\n"
        "1️⃣ When a reward is live, you get a <b>private message</b> from this bot.\n"
        "2️⃣ Tap <b>CLAIM QR</b> (green button).\n"
        "3️⃣ Required channels must already be joined.\n"
        "4️⃣ Review the QR image, then tap <b>Submit Claim</b>.\n"
        "5️⃣ Admin reviews → reward goes to your wallet.\n"
        "6️⃣ Withdraw via <b>UPI</b> or <b>Binance ID</b>.\n\n"
        "⚠️ <i>Each QR = only one claimer.</i>"
    ),
    "msg_balance": (
        "💼 <b>Your wallet</b>\n\n"
        "💵 Balance: <b>{balance}</b>\n"
        "⏳ Pending: <b>{pending}</b>\n"
        "📈 Total earned: <b>{total_earned}</b>"
    ),
    "msg_claim_submitted": (
        "📤 <b>Submitted</b> · {title}\n"
        "💰 Reward: <b>{reward}</b>\n"
        "⏳ Status: Pending review\n"
        "🔔 We will notify you here after approval."
    ),
    "msg_claim_approved": (
        "✅ <b>Approved</b> · {title}\n"
        "💰 Reward: <b>{reward}</b>\n"
        "🎉 Credited to your wallet.\n"
        "➡️ Menu → Balance / Withdraw"
    ),
    "msg_claim_rejected": (
        "❌ <b>Rejected</b> · {title}\n"
        "📝 Reason: {reason}\n"
        "This claim will not be paid.\n"
        "🔄 Wait for the next task to claim again."
    ),
    "msg_claim_hold": (
        "⏸️ <b>On hold</b> · {title}\n"
        "💰 Reward: <b>{reward}</b>\n"
        "🔍 Under extra review.\n"
        "🔔 You will be notified when decided."
    ),
    "msg_already_claimed": (
        "⛔ <b>Already claimed</b>\n\n"
        "🔗 QR: <code>{code}</code>\n"
        "👤 Claimed by: {claimer}"
    ),
    "msg_withdraw_start": (
        "💸 <b>Withdraw</b>\n\n"
        "💵 Balance: <b>{balance}</b>\n"
        "📉 Min: <b>{min_amount}</b> · 📈 Max: <b>{max_amount}</b>\n\n"
        "Choose payout method below ⬇️"
    ),
    "msg_support_start": (
        "🛟 <b>Support</b>\n\n"
        "Describe your issue in one message.\n"
        "An admin will reply here."
    ),
    "msg_group_private_only": (
        "🤖 This bot works in <b>private chat</b> only.\n"
        "Open the bot profile → Start / Message."
    ),
    "msg_new_task_caption": (
        "🆕 <b>New task #{id}</b> · {app}\n"
        "💰 Your reward: <b>{reward}</b>\n"
        "⏰ Expires: {expires}\n"
        "⚡ Claim before opening the payment link."
    ),
}

TEMPLATE_LABELS: dict[str, str] = {
    "msg_welcome": "Welcome (header + body wrapper)",
    "msg_welcome_body": "Welcome body text",
    "msg_force_join": "Force-join prompt",
    "msg_force_join_ok": "Force-join success",
    "msg_how_to_claim": "How to claim / Help",
    "msg_balance": "Balance / wallet",
    "msg_claim_submitted": "Claim submitted",
    "msg_claim_approved": "Claim approved",
    "msg_claim_rejected": "Claim rejected",
    "msg_claim_hold": "Claim on hold",
    "msg_already_claimed": "QR already claimed",
    "msg_withdraw_start": "Withdraw start",
    "msg_support_start": "Support prompt",
    "msg_group_private_only": "Group private-only notice",
    "msg_new_task_caption": "New task notification caption",
}



def sanitize_template_html(value: str) -> str:
    """Fix admin paste issues so Telegram accepts custom emoji HTML."""
    if not value:
        return value or ""
    value = value.replace("\r\n", "\n").replace("\r", "\n")

    def _join_open(m: re.Match) -> str:
        inner = re.sub(r"\s+", " ", m.group(1))
        return "<tg-emoji " + inner.strip() + ">"

    value = re.sub(
        r"<\s*tg\s*-\s*emoji\s*([^>]*)>",
        _join_open,
        value,
        flags=re.I | re.S,
    )
    value = re.sub(
        r"<\s*/\s*tg\s*-\s*emoji\s*>",
        "</tg-emoji>",
        value,
        flags=re.I | re.S,
    )
    value = re.sub(
        r'emoji\s*-\s*id\s*=\s*["\']?(\d+)["\']?',
        lambda m: 'emoji-id="%s"' % m.group(1),
        value,
        flags=re.I,
    )
    value = re.sub(r"(</tg-emoji>)(\w)", r"\1 \2", value, flags=re.I)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()

def render_template(template: str, **kwargs: Any) -> str:
    """Safe format: unknown keys left as {key}."""

    class _Safe(dict):
        def __missing__(self, key: str) -> str:
            return "{" + key + "}"

    try:
        return template.format_map(
            _Safe(**{k: ("" if v is None else v) for k, v in kwargs.items()})
        )
    except Exception:
        return template


async def get_template(session: AsyncSession, key: str) -> str:
    if key not in DEFAULT_TEMPLATES:
        return ""
    val = await get_setting(session, key)
    if val is None or not str(val).strip():
        return DEFAULT_TEMPLATES[key]
    return sanitize_template_html(str(val))


async def get_rendered(session: AsyncSession, key: str, **kwargs: Any) -> str:
    tmpl = await get_template(session, key)
    return render_template(tmpl, **kwargs)


async def list_templates(session: AsyncSession) -> list[dict[str, str]]:
    items = []
    for key, default in DEFAULT_TEMPLATES.items():
        current = await get_setting(session, key)
        items.append(
            {
                "key": key,
                "label": TEMPLATE_LABELS.get(key, key),
                "value": current if current is not None and str(current).strip() else default,
                "is_default": current is None or not str(current).strip(),
                "default": default,
            }
        )
    return items


async def save_template(
    session: AsyncSession,
    key: str,
    value: str,
    admin_id: Optional[int] = None,
) -> None:
    if key not in DEFAULT_TEMPLATES:
        raise ValueError("UNKNOWN_TEMPLATE")
    value = sanitize_template_html(value)
    await set_setting(
        session,
        key,
        value[:8000],
        admin_id=admin_id,
        description=TEMPLATE_LABELS.get(key, key),
    )


async def reset_template(
    session: AsyncSession, key: str, admin_id: Optional[int] = None
) -> None:
    if key not in DEFAULT_TEMPLATES:
        raise ValueError("UNKNOWN_TEMPLATE")
    await set_setting(session, key, "", admin_id=admin_id)
