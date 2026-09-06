# QR Claim Bot

Python Telegram bot implementing the QR Claim user flow.

## Features
- Blue-themed user navigation labels
- Group-only CLAIM QR action; QR image is sent to the claimant in DM
- QR reward amount configured per QR
- Proof upload and single submission
- Admin review: approve/reject
- Wallet, rewards, transactions and withdrawals
- UPI/USDT methods controlled by admin
- Minimum withdrawal configurable
- SQLite persistence

## Setup

1. Create a Telegram bot with BotFather and copy the token.
2. Copy `.env.example` to `.env` and set `BOT_TOKEN` and `ADMIN_IDS`.
3. Install:
   `pip install -r requirements.txt`
4. Run:
   `python bot.py`

## Admin commands
- `/admin` - admin panel
- `/addqr` - guided QR creation
- `/setminwithdraw 50`
- `/methods` - view enabled withdrawal methods
- `/enablemethod upi`
- `/disablemethod upi`
- `/withdrawals` - pending withdrawals
- `/claims` - pending claims
- Owner-only **Manage Admins**: add/remove admins and view admin list
- Owner-only **Bot Statistics** panel

## Important
Do not commit `.env` or your bot token to GitHub.
The Telegram Bot API does not allow arbitrary button background colors. The requested blue/green/red hierarchy is represented through labels, emoji and Telegram's native button UI. CLAIM QR is kept as the green primary-action concept; normal navigation uses the blue-themed labels/UI.

## Force Channel Join

Admin panel includes Force Channel Join. Add public `@channelusername` channels, enable/disable the gate, and users must join all configured channels before `/start` access and QR claiming. The bot must be an administrator in each required channel so `get_chat_member` can verify membership.

Withdrawal admin flow: PENDING → PROCESSING → PAID, or PENDING/PROCESSING → REJECTED with automatic refund. Includes total/pending/processing/paid/rejected counts and paid totals.

### Withdrawal Notifications
Users automatically receive Telegram DM notifications when a withdrawal changes to **Processing**, **Paid**, or **Rejected/refunded**. Notifications include withdrawal ID, amount, and method.

## Admin Dashboard Mini App
A mobile-friendly admin dashboard is included in `dashboard/index.html` with a FastAPI backend in `dashboard_server.py`. It shows live user/claim/withdrawal statistics and supports the withdrawal status flow PENDING → PROCESSING → PAID or REJECTED. For production, protect `/api/admin/*` with Telegram Mini App `initData` verification before exposing it publicly.
