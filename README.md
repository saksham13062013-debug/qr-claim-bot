# QR Claim Bot v2 — Railway

## Features
- Private user menu: Wallet, Rewards, Withdraw, Transactions, Profile, Help
- Green-style primary CLAIM QR action label
- Group never receives the QR image; QR is sent by DM
- Atomic one-user QR claim locking
- Shows claimed-by user in group
- Upload proof and admin Approve / Reject buttons
- Approved reward is credited automatically
- Withdrawal: Pending -> Processing -> Paid
- Withdrawal Reject refunds balance
- User notifications for processing/paid/rejected
- Admin Telegram panel
- Admin Mini App dashboard
- QR list, proof list, withdrawal list and dashboard statistics
- Force channel join
- UPI/USDT method toggles

## Railway variables
BOT_TOKEN=your_new_bot_token
ADMIN_IDS=123456789
GROUP_ID=-1001234567890
WEBAPP_URL=https://qr-claim-bot-production.up.railway.app
SUPPORT_ID=@itsvanshu
FORCE_JOIN_CHANNEL=@yourchannel
FORCE_JOIN_URL=https://t.me/yourchannel
ENABLE_UPI=1
ENABLE_USDT=1
MIN_WITHDRAWAL=50
DB_PATH=qr_claim_bot.db

## Start command
python bot.py

## Important
Telegram Bot API does not allow arbitrary button background colors. Emoji/labels can distinguish actions, but actual inline button color is controlled by Telegram.

For the Mini App, WEBAPP_URL must be HTTPS and point to this same Railway service.
The Railway service must expose the PORT environment variable; the app reads PORT automatically.

Admin:
Send /admin in the bot private chat. Then tap 📊 DASHBOARD.
