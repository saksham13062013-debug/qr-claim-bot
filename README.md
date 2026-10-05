# QR Claim Bot — Complete production-oriented build

## Features implemented
- Telegram bot with typing indicators and force-join verification.
- QR create/post/repost lifecycle and one-user-per-QR atomic claim.
- QR reclaim/revoke so a claimed QR can be made available again.
- USD reward approval/rejection with automatic success/failure notifications.
- Wallet ledger and withdrawal request/review/reversal flow.
- Owner/Super Admin/Manager/Moderator RBAC; Owner-only reset; Owner-only admin promotion/demotion.
- Dashboard, users, QR, claims, withdrawals, admin and audit-log API endpoints.
- PostgreSQL + Prisma schema and seed script.
- Next.js dark admin dashboard starter.

## Run locally
1. `cp .env.example .env` and set real secrets.
2. `docker compose up -d`
3. `npm install`
4. `npm run db:generate`
5. `npm run db:push`
6. `npm run db:seed`
7. `npm run dev`

API http://localhost:4000 · Admin http://localhost:3000

## Important production integrations
The core reward/claim/withdrawal state machine is implemented. A real money payout provider is intentionally an adapter boundary: connect your compliant provider before paying users. Telegram webhook mode, Redis/BullMQ retries, HTTPS, secrets manager, backups, monitoring and rate limits should be enabled for production deployment.


## Manual withdrawals
Withdrawals are intentionally manual. No payout gateway is used. An admin sends the money externally, then clicks **Mark Paid** (`PAID`) in the admin/API review action. Rejecting a withdrawal atomically restores the reserved wallet balance. The bot sends the user a paid/rejected notification.

For production, run a dedicated notification worker and connect Telegram webhook/HTTPS before launch.
