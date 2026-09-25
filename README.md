# QR Work Bot Pro

A production-style starter for a legitimate Telegram task/QR workflow.

## Included
- Telegram bot
- Separate web admin dashboard
- Admin login/session
- Dashboard statistics
- Create tasks
- Generate QR images from authorized payloads
- Open/claimed/pending/expired task management
- User list and status
- Wallet/credits view
- Task history
- Cancel tasks
- Bot + dashboard use the same SQLite database

## Run
```bash
pip install -r requirements.txt
cp .env.example .env
```

Set:
- `BOT_TOKEN`
- `ADMIN_IDS`
- `ADMIN_USERNAME`
- `ADMIN_PASSWORD`

Run bot:
```bash
python bot.py
```

Run admin panel:
```bash
python admin.py
```

Open:
`http://127.0.0.1:8080`

For public deployment, put the dashboard behind HTTPS and a proper reverse proxy. Change the default admin password.

## Important
The starter uses internal credits/points. It does not claim that a payment occurred based only on a QR scan or a button click. If you later add real payments, use an authorized payment provider and server-side verification.
