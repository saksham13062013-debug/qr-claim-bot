# Advanced Telegram Reward & Verification Bot

A Node.js Telegram bot for legitimate promotions: admins publish a campaign with a QR image, destination link, task description and reward amount; registered users receive a notification; the first successful claim gets the link and QR; the claimant submits non-sensitive proof; an admin approves/rejects it; approved rewards are credited to the user's in-bot wallet.

## Important behavior

- **One winner per campaign:** SQLite enforces a unique claim per campaign, and the winner is chosen in a transaction.
- **Manual verification:** proof can be a short text or Telegram photo. Admin reviews it in the dashboard.
- **Reward wallet:** approval writes a ledger entry; `/wallet` shows the total.
- **No real-money withdrawals:** wallet units are internal points. If you add payouts later, add proper payment compliance and fraud controls.
- **Privacy/safety:** only request proof of task completion. Never ask for passwords, OTPs, recovery codes, private keys, payment credentials, or account access.
- Users need to press `/start` before Telegram allows the bot to message them.
- Telegram may throttle broadcasts; current implementation spaces messages by 35 ms. For large audiences, use a queue and respect Telegram rate limits.

## Requirements

- Node.js 20+
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- A cloud host with persistent disk storage (recommended) and HTTPS for the admin dashboard

## Project structure

```text
advanced-telegram-reward-bot/
├── src/
│   ├── db.js
│   └── index.js
├── data/                 # SQLite database (persistent volume recommended)
├── uploads/              # uploaded QR images (persistent volume recommended)
├── .env.example
├── .gitignore
├── Dockerfile
├── render.yaml
├── package.json
└── README.md
```

## Local setup

1. Install Node.js 20 or newer.
2. Create a bot with [@BotFather](https://t.me/BotFather) and copy the token.
3. Copy `.env.example` to `.env`.
4. Set `BOT_TOKEN`, your numeric `ADMIN_TELEGRAM_IDS`, `ADMIN_USERNAME`, `SESSION_SECRET`, and `ADMIN_PASSWORD_HASH`.

Generate a session secret:

```bash
node -e "console.log(require('crypto').randomBytes(48).toString('hex'))"
```

Generate a bcrypt password hash (replace the example password with a strong unique password):

```bash
node -e "const b=require('bcryptjs'); console.log(b.hashSync('REPLACE_WITH_A_LONG_UNIQUE_PASSWORD', 12))"
```

Do not put the plain dashboard password in the repository. Add `.env` to secret storage and never commit it.

5. Install and start:

```bash
npm install
npm start
```

6. Open `http://localhost:3000/login` and sign in with `ADMIN_USERNAME` and the password used to create `ADMIN_PASSWORD_HASH`.
7. Send `/start` to your Telegram bot. The numeric Telegram ID must be listed in `ADMIN_TELEGRAM_IDS` only if you want that person to be a bot-level admin; the web panel uses the separate username/password.

## Admin workflow

1. Open **Campaigns** in the web panel.
2. Enter a title, task description, destination URL, reward points, and upload a QR image (PNG/JPG/WEBP, maximum 5 MB).
3. Create the campaign. The bot broadcasts it to users who have started the bot and are not blocked from broadcasts.
4. The first person to press **Claim reward** gets the campaign QR and link. Each campaign has one winner.
5. The winner completes the legitimate task and taps **Submit for verification**, then sends a text/photo as proof.
6. In **Verification queue**, review proof and press **Approve & credit** or **Reject**.
7. Approved reward points appear in the user's `/wallet`.

## Cloud deployment (Render example)

1. Push this project to a private Git repository.
2. In Render, create a new Blueprint from the repository and review `render.yaml`.
3. Configure all `sync: false` environment variables in the dashboard:
   - `BOT_TOKEN`
   - `ADMIN_TELEGRAM_IDS`
   - `BASE_URL` (the HTTPS URL assigned to your service)
   - `ADMIN_USERNAME`
   - `ADMIN_PASSWORD_HASH`
   - `SESSION_SECRET`
4. Keep the persistent disk mounted at `/var/data`. The database and QR uploads must survive redeploys.
5. Deploy, then visit `https://your-service-url/login`.
6. Confirm `https://your-service-url/health` returns `{"ok":true,"service":"telegram-reward-bot"}`.
7. Test with a private campaign and a second Telegram account before public launch.

## Security checklist

- Use HTTPS. Do not expose the dashboard over plain HTTP on the public internet.
- Use a long, unique dashboard password and a randomly generated `SESSION_SECRET`.
- Keep `.env`, SQLite databases, and uploaded QR files private; do not commit them.
- Restrict `ADMIN_TELEGRAM_IDS` to trusted IDs.
- Keep dependencies updated and monitor server logs.
- Back up `/var/data` securely.
- Review destination URLs before sending them to users. The application accepts HTTP(S) links; it does not certify the destination as safe.
- Do not use campaigns to collect credentials or deceive users. Clearly disclose the task, eligibility, reward amount, and verification criteria.
- For high traffic, move broadcasts into a durable queue and consider PostgreSQL plus a managed session store.

## Notes / limitations

- QR images are uploaded by the admin to the server; they are not scanned or validated for their destination.
- Proof photos are represented in the dashboard by their Telegram file ID; the bot does not download users' images onto the server. If you need image review inside the dashboard, add a secure Telegram file proxy with access controls.
- Reward amounts are stored as decimal numeric values for simplicity. For real-money balances, use integer minor units and a regulated payment provider instead.
- This is a runnable starter implementation, not a substitute for a security review before handling real users or money.


## Team referral commission

Users can open **Team** or run `/team` to get a personal Telegram deep link. New users who start through that link are attributed to the inviter once. A team leader sets the invited friend's reward with `/teamrate 70`. On a campaign worth 100 units, the referred user is set to receive 70 units and the inviter earns 30 units only after the referred user's proof is approved. The rate is snapshotted when the claim is created; changing it later does not alter existing claims. Campaigns now allow each registered user to claim once, rather than being limited to a single global winner.

Wallet balances include both task rewards and team commissions; the ledger records them separately. Team commission is credited in the same database transaction as the approval.
