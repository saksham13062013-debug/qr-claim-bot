# QR Reward Bot

A Telegram bot + web admin dashboard for running app/task reward campaigns:
admins post a QR/task to configured groups, the first eligible user claims it
in DM, completes the real task, marks it complete, an admin verifies it, and
the reward is credited to the user's in-bot balance. Users withdraw their
balance to a Binance UID.

**No money ever flows from a user to the bot or an admin.** The only payment
direction is admin → user, when a withdrawal is marked PAID. This is
intentional: it is what separates a legitimate reward/offer bot from a
"pay first, admin approves later" scheme.

## Tech stack

- Python 3.11+, `python-telegram-bot` 21.x
- FastAPI + Telegram WebApp for the admin dashboard
- SQLite (via `sqlite3`, WAL mode) — swap `DB_PATH` for a mounted volume in production
- Single process: bot polling runs on a background thread, FastAPI/uvicorn on the main thread

## Project layout

```
bot.py            Telegram bot handlers (user + admin commands, callbacks)
admin_api.py       FastAPI admin API + dashboard auth (Telegram WebApp initData)
database.py        SQLite schema + all atomic read/write operations
config.py           Environment variable loading
main.py             Entrypoint — runs the bot and the API together
dashboard/index.html  Admin dashboard (vanilla HTML/CSS/JS, no build step)
requirements.txt
Procfile
.env.example
```

## Environment variables

Copy `.env.example` to `.env` and fill in:

| Variable | Description |
|---|---|
| `BOT_TOKEN` | Your bot's token from @BotFather |
| `WEBAPP_URL` | Public HTTPS URL this service is deployed at (used by `/admin`) |
| `DB_PATH` | SQLite file path. On Railway, point this at a mounted volume, e.g. `/data/qr_reward_bot.db` |
| `ADMIN_IDS` | Comma-separated Telegram user IDs. The first one becomes the protected owner |
| `GROUP_IDS` | Comma-separated group chat IDs to broadcast tasks to |
| `USD_TO_INR` | Conversion rate used for withdrawal requests |
| `MIN_WITHDRAWAL_USD` | Minimum withdrawal amount in USD |
| `FORCE_JOIN_CHANNEL` / `FORCE_JOIN_URL` | Comma-separated, same order/length. Leave blank to disable |
| `FORCE_JOIN_STRICT` | Minimum number of required channels a user must have joined |
| `SUPPORT_USERNAME` | Shown in /help |

## Running locally

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then edit it
python main.py
```

The dashboard is served at `http://localhost:8000/` — but it only unlocks once
opened through Telegram's WebApp button (`/admin` in the bot), because it
authenticates every request using Telegram's signed `initData`, not a
password.

## Deploying to Railway

1. Push this project to a repo and create a new Railway service from it.
2. Add a volume and set `DB_PATH=/data/qr_reward_bot.db` so the database
   survives redeploys.
3. Set all variables from `.env.example` in Railway → Variables.
4. Set `WEBAPP_URL` to the Railway-generated public URL once it's assigned.
5. Railway will run `Procfile`'s `web: python main.py`, which starts both the
   bot and the dashboard API on `$PORT`.

## Bot commands

**Everyone:** `/start`, `/help`
**Admins only:** `/admin` (opens dashboard), `/addqr` (create a task, conversational), `/postqr <task_id>` (broadcast a task to groups), `/stats`

## How claiming works (concurrency safety)

`database.claim_task()` runs a single `UPDATE tasks SET status='CLAIMED', ... WHERE task_id=? AND status='AVAILABLE'`
inside an immediate-mode SQLite transaction. If two users tap CLAIM at the same
moment, only one `UPDATE` can match `status='AVAILABLE'` — the second gets
`rowcount == 0` and is told the task is no longer available. The same pattern
protects order verification and withdrawal state changes from double-processing.

## Security notes

- `BOT_TOKEN` is never sent to the frontend, logged, or exposed via any API route.
- Every `/api/admin/*` route requires a valid, freshly-signed Telegram WebApp
  `initData` string (HMAC-verified server-side against `BOT_TOKEN`) belonging
  to a Telegram ID present in the `admins` table.
- The last remaining admin can never be removed, from the dashboard or the bot.

## What this bot intentionally does not do

Per the brief: no referral system, no leaderboard, no gift codes, no daily
bonus, no "transaction history" screen (replaced by Order History), and no
step where a user is asked to pay or send funds before receiving a reward.
