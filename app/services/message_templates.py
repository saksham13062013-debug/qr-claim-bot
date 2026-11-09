"""Editable bot response templates (HTML + different custom emoji ids)."""

from __future__ import annotations

import re
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.settings_service import get_setting, set_setting

DEFAULT_TEMPLATES: dict[str, str] = {
    'msg_welcome': '<tg-emoji emoji-id="5258362837411045098">👋</tg-emoji> <b>Welcome, {name}!</b>\n\n{welcome_body}\n\n<tg-emoji emoji-id="5874948844935974490">✨</tg-emoji> Use the menu below to get started.',
    'msg_welcome_body': '<tg-emoji emoji-id="5807465992363710697">💎</tg-emoji> Claim exclusive one-time QR rewards\n<tg-emoji emoji-id="5807465992363710697">💰</tg-emoji> Track your balance and withdraw easily\n\n<b>How it works</b>\n<tg-emoji emoji-id="5258476306152038031">🔒</tg-emoji> 1. Join required channels (if asked)\n<tg-emoji emoji-id="5843553939672274145">🎯</tg-emoji> 2. Tap <b>CLAIM QR</b> on a reward\n<tg-emoji emoji-id="5260652420052032852">📤</tg-emoji> 3. Submit your claim\n<tg-emoji emoji-id="5920052658743283381">✅</tg-emoji> 4. Get paid after approval\n<tg-emoji emoji-id="5807465992363710697">💼</tg-emoji> 5. Withdraw via UPI / Binance\n\n<tg-emoji emoji-id="5258474669769497337">⚠️</tg-emoji> Each QR can be claimed by only <b>ONE</b> user.',
    'msg_force_join': '<tg-emoji emoji-id="5258476306152038031">🔒</tg-emoji> <b>Join required channels</b>\n\nYou must join the channel(s) below before using the bot.\n\n<tg-emoji emoji-id="5920052658743283381">✅</tg-emoji> After joining, tap <b>I Joined – Check Again</b>.',
    'msg_force_join_ok': '<tg-emoji emoji-id="5920052658743283381">✅</tg-emoji> <b>Verified</b> · Welcome, {name}!\n\n{welcome_body}',
    'msg_how_to_claim': '<tg-emoji emoji-id="5258503720928288433">❓</tg-emoji> <b>How to claim a reward</b>\n\n1. When a reward is live, you get a <b>private message</b> from this bot.\n2. Tap <b>CLAIM QR</b>.\n3. Required channels must already be joined.\n4. Review the QR image, then tap <b>Submit Claim</b>.\n5. Admin reviews — reward goes to your wallet.\n6. Withdraw via <b>UPI</b> or <b>Binance ID</b>.\n\n<tg-emoji emoji-id="5258474669769497337">⚠️</tg-emoji> <i>Each QR can be claimed by only one user.</i>',
    'msg_balance': '<tg-emoji emoji-id="5807465992363710697">💼</tg-emoji> <b>Your wallet</b>\n\n<tg-emoji emoji-id="5807465992363710697">💰</tg-emoji> Balance: <b>{balance}</b>\n<tg-emoji emoji-id="5260687119092817530">⏰</tg-emoji> Pending: <b>{pending}</b>\n<tg-emoji emoji-id="5260652420052032852">📈</tg-emoji> Total earned: <b>{total_earned}</b>',
    'msg_claim_submitted': '<tg-emoji emoji-id="5260652420052032852">📤</tg-emoji> <b>Submitted</b> · {title}\n<tg-emoji emoji-id="5807465992363710697">💰</tg-emoji> Reward: <b>{reward}</b>\n<tg-emoji emoji-id="5260687119092817530">⏰</tg-emoji> Status: Pending review\n<tg-emoji emoji-id="5258503720928288433">🔔</tg-emoji> We will notify you here after approval.',
    'msg_claim_approved': '<tg-emoji emoji-id="5920052658743283381">✅</tg-emoji> <b>Approved</b> · {title}\n<tg-emoji emoji-id="5807465992363710697">💰</tg-emoji> Reward: <b>{reward}</b>\n<tg-emoji emoji-id="5920052658743283381">🎉</tg-emoji> Credited to your wallet.\n<tg-emoji emoji-id="5807465992363710697">💼</tg-emoji> Menu → Balance / Withdraw',
    'msg_claim_rejected': '<tg-emoji emoji-id="5258226313285607065">❌</tg-emoji> <b>Rejected</b> · {title}\n<tg-emoji emoji-id="5258477770735885832">📄</tg-emoji> Reason: {reason}\nThis claim will not be paid.\n<tg-emoji emoji-id="5258503720928288433">🔔</tg-emoji> Wait for the next task to claim again.',
    'msg_claim_hold': '<tg-emoji emoji-id="5258226313285607065">⏸</tg-emoji> <b>On hold</b> · {title}\n<tg-emoji emoji-id="5807465992363710697">💰</tg-emoji> Reward: <b>{reward}</b>\n<tg-emoji emoji-id="5260341314095947411">🔍</tg-emoji> Under extra review.\n<tg-emoji emoji-id="5258503720928288433">🔔</tg-emoji> You will be notified when decided.',
    'msg_already_claimed': '<tg-emoji emoji-id="5258226313285607065">🚫</tg-emoji> <b>Already claimed</b>\n\n<tg-emoji emoji-id="5260730055880876557">🔗</tg-emoji> QR: <code>{code}</code>\n<tg-emoji emoji-id="5258362837411045098">👤</tg-emoji> Claimed by: {claimer}',
    'msg_withdraw_start': '<tg-emoji emoji-id="5807465992363710697">💰</tg-emoji> <b>Withdraw</b>\n\n<tg-emoji emoji-id="5807465992363710697">💼</tg-emoji> Balance: <b>{balance}</b>\n<tg-emoji emoji-id="5258336354642697821">📉</tg-emoji> Min: <b>{min_amount}</b> · <tg-emoji emoji-id="5260652420052032852">📈</tg-emoji> Max: <b>{max_amount}</b>\n\nChoose payout method below.',
    'msg_support_start': '<tg-emoji emoji-id="5258503720928288433">🛟</tg-emoji> <b>Support</b>\n\nDescribe your issue in one message.\nAn admin will reply here.',
    'msg_group_private_only': '<tg-emoji emoji-id="5258503720928288433">🤖</tg-emoji> This bot works in <b>private chat</b> only.\nOpen the bot profile → Start / Message.',
    'msg_new_task_caption': '<tg-emoji emoji-id="5843553939672274145">🆕</tg-emoji> <b>New task #{id}</b> · {app}\n<tg-emoji emoji-id="5807465992363710697">💰</tg-emoji> Your reward: <b>{reward}</b>\n<tg-emoji emoji-id="5260687119092817530">⏰</tg-emoji> Expires: {expires}\n<tg-emoji emoji-id="5843553939672274145">🔥</tg-emoji> Claim before opening the payment link.',
}

TEMPLATE_LABELS: dict[str, str] = {
    'msg_welcome': 'Welcome (header + body wrapper)',
    'msg_welcome_body': 'Welcome body text',
    'msg_force_join': 'Force-join prompt',
    'msg_force_join_ok': 'Force-join success',
    'msg_how_to_claim': 'How to claim / Help',
    'msg_balance': 'Balance / wallet',
    'msg_claim_submitted': 'Claim submitted',
    'msg_claim_approved': 'Claim approved',
    'msg_claim_rejected': 'Claim rejected',
    'msg_claim_hold': 'Claim on hold',
    'msg_already_claimed': 'QR already claimed',
    'msg_withdraw_start': 'Withdraw start',
    'msg_support_start': 'Support prompt',
    'msg_group_private_only': 'Group private-only notice',
    'msg_new_task_caption': 'New task notification caption',
}


def sanitize_template_html(value: str) -> str:
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
