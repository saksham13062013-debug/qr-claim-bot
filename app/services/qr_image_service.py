"""Generate QR code images (PNG bytes) for Telegram and admin use."""

from __future__ import annotations

import io
from typing import Optional

import qrcode
from qrcode.constants import ERROR_CORRECT_M


def generate_qr_png_bytes(
    data: str,
    *,
    box_size: int = 10,
    border: int = 2,
) -> bytes:
    """Return PNG bytes for a QR code encoding `data`."""
    qr = qrcode.QRCode(
        version=None,
        error_correction=ERROR_CORRECT_M,
        box_size=box_size,
        border=border,
    )
    qr.add_data(data)
    qr.make(fit=True)

    buf = io.BytesIO()
    try:
        # Prefer PIL if available (qrcode[pil])
        img = qr.make_image(fill_color="black", back_color="white")
        img.save(buf, format="PNG")
    except Exception:
        from qrcode.image.pure import PyPNGImage

        img = qr.make_image(image_factory=PyPNGImage)
        img.save(buf)
    return buf.getvalue()


def claim_payload(code: str, bot_username: Optional[str] = None) -> str:
    """
    Data encoded inside the visual QR.
    Prefer deep-link so scanning opens the bot; fallback to plain code.
    """
    if bot_username:
        uname = bot_username.lstrip("@")
        return f"https://t.me/{uname}?start=claim_{code}"
    return code
