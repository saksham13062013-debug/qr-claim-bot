# QR Claim Bot — Full Admin v12

## Features
- QR rewards and wallet in USD
- INR withdrawals via Binance ID / UID
- Atomic QR claim; QR image stays private in DM
- Proof upload + admin approval/rejection
- Withdrawal Pending/Processing/Paid/Rejected
- User search, ban/unban, USD balance add/deduct
- Multiple Force Join channels
- Broadcast + direct user message
- Referral, leaderboard, daily bonus, gift codes
- Full admin dashboard: Dashboard, QRs, Claims, Proofs, Withdrawals, Users, Admins, Force Join, Broadcast, Gift Codes, Logs, Analytics
- Telegram log channel support
- Persistent SQLite; Railway Volume recommended

## Railway
Set `BOT_TOKEN`, `ADMIN_IDS`, `GROUP_ID`, and `WEBAPP_URL=https://YOUR-RAILWAY-DOMAIN`.
Recommended: attach a Volume at `/data` and set `DB_PATH=/data/qr_claim_bot.db`.
Set `USD_TO_INR=90` and `MIN_WITHDRAWAL=50` as needed.

**Do not replace/delete an existing production database.** This ZIP intentionally does not include a database file.


### Multiple QR groups
Set `GROUP_IDS` to a comma-separated list of Telegram group IDs/usernames, e.g. `-1001111111111,-1002222222222` or `@group1,@group2`. A posted QR is sent to every configured group. After claiming, every post changes to the claimant; after proof review, every post changes to 🟢 QR SUCCESS or 🔴 QR FAILED. `GROUP_ID` remains supported as a single-group fallback.


## v14 button + multi-group fixes
- Set GROUP_IDS to comma-separated Telegram group IDs.
- QR is posted to every configured group and tracked in qr_posts.
- Claim updates every group post.
- Proof approval/rejection updates every group with 🟢 QR SUCCESS / 🔴 QR FAILED.
- Callback errors are caught and logged instead of silently breaking button presses.
