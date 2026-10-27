"""Editable bot response templates (HTML + custom emoji packs supported).

Admins can set templates via web admin or Telegram /admin.
Use placeholders like {name}, {reward}, {title}, {reason}.
Custom emoji: paste pack emoji in Telegram admin, or HTML:
  <tg-emoji emoji-id=\"ID\">⭐</tg-emoji>
"""

from __future__ import annotations

from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.settings_service import get_setting, set_setting

# All keys admins can edit. Values are HTML (parse_mode=HTML).
DEFAULT_TEMPLATES: dict[str, str] = {
    "msg_welcome": (
        "<b>Welcome, {name}!</b>\n\n"
        "{welcome_body}\n\n"
        "Use the menu below or the buttons to get started."
    ),
    "msg_welcome_body": (
        "Claim exclusive one-time QR rewards, track your balance, and request withdrawals.\n\n"
        "How it works:\n"
        "1. Join required channels (if asked)\n"
        "2. Tap CLAIM QR on a reward post\n"
        "3. Submit your claim and wait for approval\n"
        "4. Get rewarded in your wallet\n"
        "5. Withdraw when you reach the minimum\n\n"
        "Each QR can be claimed by only ONE user."
    ),
    "msg_force_join": (
        "<b>Join required channels to continue</b>\n\n"
        "You must join the channel(s) below before using the bot.\n"
        "After joining, tap <b>I Joined – Check Again</b>."
    ),
    "msg_force_join_ok": (
        "<b>Verified</b> · Welcome, {name}!\n\n{welcome_body}"
    ),
    "msg_how_to_claim": (
        "<b>How to claim a reward</b>\n\n"
        "1. When a reward is live, you get a <b>private message</b> from this bot.\n"
        "2. Tap <b>CLAIM QR</b> (green).\n"
        "3. Required channels must already be joined (checked at /start).\n"
        "4. Review the QR image, then tap <b>Submit Claim</b>.\n"
        "5. An admin reviews your claim. Once approved, the reward is credited to your wallet.\n"
        "6. Withdraw via <b>UPI</b> or <b>Binance ID</b> when you reach the minimum.\n\n"
        "<i>Each QR can be claimed by only one user.</i>"
    ),
    "msg_balance": (
        "<b>Your wallet</b>\n\n"
        "Balance: <b>{balance}</b>\n"
        "Pending: <b>{pending}</b>\n"
        "Total earned: <b>{total_earned}</b>"
    ),
    "msg_claim_submitted": (
        "<b>Submitted</b> · {title}\n"
        "Reward: <b>{reward}</b>\n"
        "Status: Pending review\n"
        "We will notify you here after approval."
    ),
    "msg_claim_approved": (
        "<b>Approved</b> · {title}\n"
        "Reward: <b>{reward}</b>\n"
        "Credited to your wallet.\n"
        "Menu → Balance / Withdraw"
    ),
    "msg_claim_rejected": (
        "<b>Rejected</b> · {title}\n"
        "Reason: {reason}\n"
        "This claim will not be paid.\n"
        "Wait for the next task to claim again."
    ),
    "msg_claim_hold": (
        "<b>On hold</b> · {title}\n"
        "Reward: <b>{reward}</b>\n"
        "Under extra review.\n"
        "You will be notified when decided."
    ),
    "msg_already_claimed": (
        "<b>Already claimed</b>\n\n"
        "QR: <code>{code}</code>\n"
        "Claimed by: {claimer}"
    ),
    "msg_withdraw_start": (
        "<b>Withdraw</b>\n\n"
        "Balance: <b>{balance}</b>\n"
        "Min: <b>{min_amount}</b> · Max: <b>{max_amount}</b>\n\n"
        "Choose payout method:"
    ),
    "msg_support_start": (
        "Describe your issue in one message. An admin will reply here."
    ),
    "msg_group_private_only": (
        "This bot works in <b>private chat</b> only.\n"
        "Open the bot profile → Start / Message."
    ),
    "msg_new_task_caption": (
        "New task #{id} · {app}\n"
        "Your reward: {reward}\n"
        "Expires: {expires}\n"
        "Claim before opening the payment link."
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


def render_template(template: str, **kwargs: Any) -> str:
    """Safe format: missing keys become empty string."""
    class _Safe(dict):
        def __missing__(self, key: str) -> str:
            return "{" + key + "}"

    try:
        return template.format_map(_Safe(**{k: ("" if v is None else v) for k, v in kwargs.items()}))
    except Exception:
        return template


async def get_template(session: AsyncSession, key: str) -> str:
    if key not in DEFAULT_TEMPLATES:
        return ""
    val = await get_setting(session, key)
    if val is None or not str(val).strip():
        return DEFAULT_TEMPLATES[key]
    return str(val)


async def get_rendered(
    session: AsyncSession, key: str, **kwargs: Any
) -> str:
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
                "value": current if current is not None else default,
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
    # Store empty to mean default — or delete. We store default explicitly cleared via empty + get falls back
    await set_setting(session, key, "", admin_id=admin_id)
