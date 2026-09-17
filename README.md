# QR Claim Bot – Admin Build

## Railway variables
- `BOT_TOKEN` – BotFather token
- `ADMIN_IDS` – comma-separated Telegram admin IDs
- `GROUP_ID` – group/supergroup ID where claim post is sent
- `WEBAPP_URL` – Railway public HTTPS URL
- `LOG_CHANNEL_ID` – channel ID for logs (bot must be admin there)
- `DB_PATH=/data/qr_claim_bot.db` when using a Railway Volume mounted at `/data`
- `MIN_WITHDRAWAL=50`
- `ENABLE_UPI=1`
- `ENABLE_USDT=1`
- `SUPPORT_ID=@your_support`

Force join can be configured from the Admin Web App. Public channels use `@username`; private channels use `-100...` plus their invite URL.

## Admin commands
`/admin` `/addqr` `/postqr` `/addbalance USER_ID AMOUNT [REASON]` `/message USER_ID MESSAGE` `/ban USER_ID` `/unban USER_ID` `/broadcast MESSAGE` `/addadmin USER_ID` `/deladmin USER_ID`

QR claim messages display the claimant's **name only**. A button containing the name can be tapped to show the claimant's Telegram ID. No @username is displayed in the group claim message.
