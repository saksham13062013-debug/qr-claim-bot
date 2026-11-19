"""Editable bot response templates (HTML + custom emoji ids)."""

from __future__ import annotations

import re
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.premium_emoji import pe
from app.services.settings_service import get_setting, set_setting

DEFAULT_TEMPLATES: dict[str, str] = {
    "msg_welcome": (
        f"{pe('wave')} <b>Welcome, {{name}}!</b>\n\n"
        f"{{welcome_body}}\n\n"
        f"{pe('spark')} Use the menu below to get started."
    ),
    "msg_welcome_body": (
        f"{pe('diamond')} Claim exclusive one-time QR rewards\n"
        f"{pe('money')} Track your balance and withdraw easily\n\n"
        f"<b>How it works</b>\n"
        f"{pe('lock')} 1. Join required channels (if asked)\n"
        f"{pe('claim')} 2. Tap <b>CLAIM QR</b> on a reward\n"
        f"{pe('send')} 3. Submit your claim\n"
        f"{pe('check')} 4. Get paid after approval\n"
        f"{pe('wallet')} 5. Withdraw via UPI / Binance\n\n"
        f"{pe('warn')} Each QR can be claimed by only <b>ONE</b> user."
    ),
    "msg_force_join": (
        f"{pe('lock')} <b>Join required channels</b>\n\n"
        f"You must join the channel(s) below before using the bot.\n\n"
        f"{pe('check')} After joining, tap <b>I Joined – Check Again</b>."
    ),
    "msg_force_join_ok": (
        f"{pe('check')} <b>Verified</b> · Welcome, {{name}}!\n\n{{welcome_body}}"
    ),
    "msg_how_to_claim": (
        f"{pe('help')} <b>How to claim a reward</b>\n\n"
        "1. When a reward is live, you get a <b>private message</b> from this bot.\n"
        "2. Tap <b>CLAIM QR</b>.\n"
        "3. Required channels must already be joined.\n"
        "4. Review the QR image, then tap <b>Submit Claim</b>.\n"
        "5. Admin reviews — reward goes to your wallet.\n"
        "6. Withdraw via <b>UPI</b> or <b>Binance ID</b>.\n\n"
        f"{pe('warn')} <i>Each QR can be claimed by only one user.</i>"
    ),
    "msg_balance": (
        f"{pe('wallet')} <b>Your wallet</b>\n\n"
        f"{pe('money')} Balance: <b>{{balance}}</b>\n"
        f"{pe('clock')} Pending: <b>{{pending}}</b>\n"
        f"{pe('chart')} Total earned: <b>{{total_earned}}</b>"
    ),
    "msg_claim_submitted": (
        f"{pe('send')} <b>Submitted</b> · {{title}}\n"
        f"{pe('money')} Reward: <b>{{reward}}</b>\n"
        f"{pe('clock')} Status: Pending review\n"
        f"{pe('bell')} We will notify you here after approval."
    ),
    "msg_claim_approved": (
        f"{pe('check')} <b>Approved</b> · {{title}}\n"
        f"{pe('money')} Reward: <b>{{reward}}</b>\n"
        f"{pe('party')} Credited to your wallet.\n"
        f"{pe('wallet')} Menu → Balance / Withdraw"
    ),
    "msg_claim_rejected": (
        f"{pe('cross')} <b>Rejected</b> · {{title}}\n"
        f"{pe('doc')} Reason: {{reason}}\n"
        "This claim will not be paid.\n"
        f"{pe('bell')} Wait for the next task to claim again."
    ),
    "msg_claim_hold": (
        f"{pe('pause')} <b>On hold</b> · {{title}}\n"
        f"{pe('money')} Reward: <b>{{reward}}</b>\n"
        f"{pe('search')} Under extra review.\n"
        f"{pe('bell')} You will be notified when decided."
    ),
    "msg_already_claimed": (
        f"{pe('ban')} <b>Already claimed</b>\n\n"
        f"{pe('link')} QR: <code>{{code}}</code>\n"
        f"{pe('user')} Claimed by: {{claimer}}"
    ),
    "msg_withdraw_start": (
        f"{pe('money')} <b>Withdraw</b>\n\n"
        f"{pe('wallet')} Balance: <b>{{balance}}</b>\n"
        f"{pe('down')} Min: <b>{{min_amount}}</b> · {pe('chart')} Max: <b>{{max_amount}}</b>\n\n"
        "Choose payout method below."
    ),
    "msg_support_start": (
        f"{pe('support')} <b>Support</b>\n\n"
        "Describe your issue in one message.\n"
        "An admin will reply here."
    ),
    "msg_group_private_only": (
        f"{pe('robot')} This bot works in <b>private chat</b> only.\n"
        "Open the bot profile → Start / Message."
    ),
    "msg_new_task_caption": (
        f"{pe('new')} <b>New task #{{id}}</b> · {{app}}\n"
        f"{pe('money')} Your reward: <b>{{reward}}</b>\n"
        f"{pe('clock')} Expires: {{expires}}\n"
        f"{pe('fire')} Claim before opening the payment link."
    ),
}

TEMPLATE_LABELS: dict[str, str] = {
    "msg_welcome": "Welcome (header)",
    "msg_welcome_body": "Welcome body",
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
    """Normalize tg-emoji tags so Telegram always parses them.

    Rules:
    - Tag must be ONE line: <tg-emoji emoji-id="123">X</tg-emoji>
    - emoji-id must be digits in double quotes
    - No spaces inside tag name (tg-emoji)
    """
    if not value:
        return value or ""
    value = value.replace("\r\n", "\n").replace("\r", "\n")

    # Unescape common accidental HTML entities from bad pastes
    value = (
        value.replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#34;", '"')
        .replace("&amp;", "&")
    )

    # Fix broken multi-line opening tags
    def _fix_open(m: re.Match) -> str:
        inner = re.sub(r"\s+", " ", m.group(1)).strip()
        # pull id
        id_m = re.search(r"emoji\s*-?\s*id\s*=\s*[\"']?(\d+)[\"']?", inner, re.I)
        if not id_m:
            id_m = re.search(r"(\d{10,})", inner)
        if not id_m:
            return m.group(0)
        eid = id_m.group(1)
        return f'<tg-emoji emoji-id="{eid}">'

    value = re.sub(
        r"<\s*tg\s*-?\s*emoji([^>]*)>",
        _fix_open,
        value,
        flags=re.I | re.S,
    )
    value = re.sub(
        r"<\s*/\s*tg\s*-?\s*emoji\s*>",
        "</tg-emoji>",
        value,
        flags=re.I | re.S,
    )

    # Standalone patterns people paste: emoji-id=123 or id:123 with fallback
    # Convert [eid:123456:✅] shortcode if used
    value = re.sub(
        r"\[eid:(\d+):([^\]]+)\]",
        lambda m: f'<tg-emoji emoji-id="{m.group(1)}">{m.group(2)}</tg-emoji>',
        value,
    )

    # Ensure space after closing tag before letters
    value = re.sub(r"(</tg-emoji>)(\w)", r"\1 \2", value, flags=re.I)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def render_template(template: str, **kwargs: Any) -> str:
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
        if current is not None and str(current).strip():
            value = sanitize_template_html(str(current))
            is_default = False
        else:
            value = default
            is_default = True
        items.append(
            {
                "key": key,
                "label": TEMPLATE_LABELS.get(key, key),
                "value": value,
                "is_default": is_default,
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


async def reset_all_templates(session: AsyncSession, admin_id: Optional[int] = None) -> int:
    n = 0
    for key in DEFAULT_TEMPLATES:
        await set_setting(session, key, "", admin_id=admin_id)
        n += 1
    return n
