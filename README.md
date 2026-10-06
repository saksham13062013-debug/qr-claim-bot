# QR Claim Bot — Python Edition

A single-service Python rewrite for Railway + Supabase PostgreSQL.

## Includes
- FastAPI API + admin dashboard
- aiogram Telegram bot
- PostgreSQL via SQLAlchemy/asyncpg
- Atomic one-user-per-QR claim using row locking
- QR create/post/reclaim
- Reward approval/rejection and wallet ledger
- Manual withdrawals with paid/rejected flow
- Force join + membership check
- Owner/Super Admin/Manager/Moderator role checks
- Audit logs and health endpoint
- USD rewards

## Local
```bash
cp .env.example .env
# edit .env
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 4000
```
Open `/` for the admin panel.

## Railway
1. Create a Railway project from this GitHub repo.
2. Add environment variables from `.env.example`.
3. Set `DATABASE_URL` to the Supabase PostgreSQL URL, using the `postgresql+asyncpg://` driver.
4. Railway detects the Dockerfile or uses the railway.json start command.
5. No Prisma is required.

## Important
Set a strong `JWT_SECRET` and `ADMIN_PASSWORD`. Never commit `.env` or bot tokens.
The admin payout workflow is manual: mark a withdrawal PAID only after sending money externally.
