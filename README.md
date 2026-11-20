# QR Claim & Rewards Bot

Production-ready Telegram bot + secure web Admin Panel for one-time QR reward claims, wallet management, and manual withdrawals.

## Features

- **Telegram Bot** (aiogram 3.x) – `/start`, balance, wallet, claims, withdrawals, force-join
- **Single-use QR codes** with cryptographically secure codes and atomic claiming (`SELECT … FOR UPDATE`)
- **Claim approval workflow** – pending → approve / reject / hold with idempotent wallet credit
- **Wallet** – balances stored as integer cents (USD)
- **Manual withdrawals** – request → admin mark paid / reject (with refund) / hold
- **Force-join channels** – configurable required Telegram channels
- **Admin Panel** – responsive dashboard, users, QRs, claims, withdrawals, channels, settings, audit log
- **RBAC** – OWNER / SUPER_ADMIN / MANAGER / MODERATOR with server-side enforcement
- **Audit logging** for every sensitive action
- **Owner-only destructive resets** with confirmation text
- **Railway-ready** Docker deployment

## Architecture

```
app/
  main.py          # FastAPI app + lifespan (starts bot once)
  bot.py           # aiogram 3 handlers + single polling lifecycle
  api.py           # Admin REST API
  config.py        # Pydantic Settings (env vars)
  db.py            # Async SQLAlchemy engine/session
  models.py        # All ORM models
  security.py      # Passwords, JWT, RBAC, QR generation
  services/        # Business logic (claim, wallet, QR, …)
  templates/       # Admin HTML
  static/          # CSS / JS
```

## Local Setup

### 1. Prerequisites

- Python 3.12+
- PostgreSQL 14+
- A Telegram bot token from [@BotFather](https://t.me/BotFather)

### 2. Clone & install

```bash
cd qr-claim-bot
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 3. PostgreSQL

```bash
createdb qrclaim
# or via psql:
# CREATE DATABASE qrclaim;
```

### 4. Environment

```bash
cp .env.example .env
# Edit .env – fill in all required values
```

Required variables:

| Variable | Description |
|----------|-------------|
| `DATABASE_URL` | `postgresql+asyncpg://user:pass@host:5432/qrclaim` |
| `BOT_TOKEN` | From BotFather |
| `OWNER_TELEGRAM_ID` | Your Telegram numeric ID |
| `ADMIN_USERNAME` | Initial owner login |
| `ADMIN_PASSWORD` | Initial owner password (≥8 chars) |
| `SECRET_KEY` | ≥32 random chars |
| `JWT_SECRET` | ≥32 random chars |
| `CSRF_SECRET` | ≥32 random chars |

### 5. Run

```bash
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

On startup the app will:

1. Create database tables
2. Bootstrap the OWNER admin from env credentials
3. Seed default settings
4. Authenticate the Telegram bot, clear webhooks, and start **one** polling instance

### 6. Admin login

Open http://localhost:8000 and sign in with `ADMIN_USERNAME` / `ADMIN_PASSWORD`.

## Telegram Bot Setup

1. Create a bot with @BotFather → copy the token into `BOT_TOKEN`.
2. Optionally set `BOT_USERNAME`.
3. Add the bot as an **administrator** to any force-join channels (so membership checks work).
4. Get channel IDs (forward a message to @userinfobot or use the admin panel after adding).

## Force Join Setup

1. In Admin Panel → **Channels** → Add Channel.
2. Enter the numeric channel ID (e.g. `-1001234567890`), title, optional username and invite link.
3. Mark as Active + Required.
4. Ensure `Force Join Enabled` is on in Settings.

## Railway Deployment

1. Create a new Railway project.
2. Add a **PostgreSQL** plugin – Railway injects `DATABASE_URL`.
3. Connect your Git repo (or deploy from Dockerfile).
4. Set all environment variables from `.env.example` (except `DATABASE_URL` which Railway provides – just ensure the driver is `postgresql+asyncpg://…`; the app normalizes `postgres://` automatically).
5. Deploy. The Dockerfile exposes `$PORT` and runs uvicorn.

Health check: `GET /health`

### Important: single bot instance

Railway should run **exactly one** replica of this service. Multiple replicas will cause Telegram `Conflict` errors because only one process may poll.

## Database

Tables are created automatically on startup via SQLAlchemy `create_all`. For production schema evolution you can add Alembic later; the current models include all constraints, indexes, and checks.

Money is always stored as **integer cents**. Never use floating-point columns for balances or rewards.

## Security Notes

- All secrets come from environment variables – never hard-coded.
- Passwords hashed with bcrypt.
- JWT in HttpOnly cookies (+ Bearer support).
- Server-side RBAC on every admin endpoint.
- Atomic QR claiming with `SELECT … FOR UPDATE` + unique constraint on claims.
- Idempotent claim approval and withdrawal pay/reject.
- Audit log for sensitive actions; secrets never logged.
- Rate limiting available via slowapi on login.
- Stack traces never returned to clients.

## Troubleshooting

| Issue | Fix |
|-------|-----|
| `Conflict: terminated by other getUpdates` | Ensure only one process is polling. Scale to 1 replica. |
| Bot doesn't respond | Check `BOT_TOKEN`, look at logs for `BOT AUTH OK`. |
| Membership check fails | Bot must be admin in the channel. |
| Admin login fails | Verify `ADMIN_USERNAME`/`ADMIN_PASSWORD` and that bootstrap ran. |
| DB connection errors | Confirm `DATABASE_URL` uses `postgresql+asyncpg://`. |

## Tests

```bash
pytest tests/ -v
```

Unit tests cover password hashing, JWT, QR generation uniqueness, RBAC matrix, and cents arithmetic. Integration tests for concurrent claims require a running Postgres instance (`TEST_DATABASE_URL`).

## License

Proprietary – all rights reserved.
