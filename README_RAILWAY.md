# Railway deployment

## Recommended architecture
Deploy this project as **two Railway services** from the same repository:

### 1. Web service
Start command:
`python admin.py`

Railway automatically provides `PORT`; the patched `admin.py` uses it.

### 2. Worker service
Start command:
`python bot.py`

Both services must receive the same environment variables and must use the same persistent database storage if you keep SQLite.

## Environment variables

Set these on BOTH services:

BOT_TOKEN=your BotFather token
ADMIN_IDS=your Telegram numeric ID
ADMIN_USERNAME=your admin username
ADMIN_PASSWORD=use-a-long-random-password
DB_PATH=/data/qr_work.db
CURRENCY=POINTS
TASK_TTL_MINUTES=30
OFFLINE_AFTER_MINUTES=30

For the web service you can optionally set:
ADMIN_HOST=0.0.0.0

## Persistent SQLite storage

If using SQLite, attach a Railway persistent volume and mount it at:
`/data`

Then keep:
`DB_PATH=/data/qr_work.db`

Without persistent storage, a redeploy/restart can lose the database.

## Public admin URL

After deploying the web service, generate a Railway public domain for that service. Open:
`https://YOUR-RAILWAY-DOMAIN/`

## Security

- Never put BOT_TOKEN in source code or GitHub.
- Use a strong ADMIN_PASSWORD.
- Use HTTPS/public domain only for the web dashboard.
- Restrict admin access further before production.
- Real-money transactions should be verified server-side through an authorized payment provider; QR display alone is not payment verification.

## Message Style Manager
Open Admin Panel → Message Style. Choose Modern, Minimal, Premium, or edit the task message sections. Supported variables: `{task_id}`, `{title}`, `{reward}`, `{currency}`, `{expires}`.
