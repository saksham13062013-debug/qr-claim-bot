# Railway deployment

Deploy this repository as **3 Railway services** plus **1 PostgreSQL service**.

## 1. PostgreSQL
Create a Railway PostgreSQL service. Railway exposes `DATABASE_URL`.

## 2. API service
Create a service from this GitHub repository.
- Dockerfile path: `apps/api/Dockerfile`
- Expose port: `4000`
- Required vars: `DATABASE_URL`, `JWT_SECRET`, `ADMIN_USERNAME`, `ADMIN_PASSWORD`, `OWNER_TELEGRAM_ID`, `BOT_INTERNAL_KEY`
- Deploy command is provided by the Dockerfile.
- After first deploy, run Prisma schema sync from a Railway shell: `npx prisma db push`.
- Seed only if needed: `npm run db:seed`.

## 3. Bot service
Create another service from the same repository.
- Dockerfile path: `apps/bot/Dockerfile`
- Required vars: `BOT_TOKEN`, `API_URL`, `BOT_INTERNAL_KEY`
- `API_URL` must point to the public API service URL.

## 4. Admin service
Create another service from the same repository.
- Dockerfile path: `apps/admin/Dockerfile`
- Expose port: `3000`
- Required var: `NEXT_PUBLIC_API_URL` pointing to the public API URL.

## Important
- Use HTTPS Railway domains for API and Admin.
- Keep `BOT_TOKEN`, JWT secret, database URL, and internal key in Railway Variables only.
- Set the Telegram webhook to your Bot service if you switch from polling to webhook mode. The current starter uses polling, so no webhook is required.
- Withdrawals are **manual**: you pay the user externally, then click **Mark Paid** in the admin panel.
- Back up PostgreSQL before any Owner reset operation.
