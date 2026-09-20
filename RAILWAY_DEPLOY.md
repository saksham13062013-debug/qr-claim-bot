# Railway Deployment Guide - QR Claim Bot

## Quick Deploy (5 Minutes)

### Step 1: Push to GitHub

```bash
cd qr_claim_bot
git init
git add .
git commit -m "Initial commit"
git branch -M main
git remote add origin https://github.com/yourusername/qr-claim-bot.git
git push -u origin main
```

### Step 2: Deploy to Railway

1. Go to [railway.app](https://railway.app)
2. Click **"New Project"**
3. Select **"Deploy from GitHub repo"**
4. Choose your repository
5. Railway auto-deploys

### Step 3: Add Environment Variables

In Railway dashboard → **Variables**:

**Required:**
```
BOT_TOKEN=your_bot_token_from_botfather
ADMIN_IDS=your_telegram_id
WEBAPP_URL=https://your-project.up.railway.app
DB_PATH=/data/qr_claim_bot.db
```

**Optional:**
```
GROUP_IDS=-1001234567890
FORCE_JOIN_CHANNEL=@channel1
FORCE_JOIN_URL=https://t.me/channel1
USD_TO_INR=85
MIN_WITHDRAWAL=100
```

### Step 4: Add Persistent Volume

1. Railway dashboard → **Volumes**
2. Click **"New Volume"**
3. Mount Path: `/data`
4. Size: 1GB minimum
5. Create

### Step 5: Update WEBAPP_URL

1. Copy your Railway URL from **Settings** → **Domains**
2. Update `WEBAPP_URL` variable
3. Railway auto-redeploys

### Step 6: Test

1. Open Telegram
2. Send `/start` to your bot
3. Bot should respond!

---

## Get Your IDs

### Bot Token
1. Message [@BotFather](https://t.me/BotFather)
2. Send `/newbot`
3. Copy token → `BOT_TOKEN`

### Admin ID
1. Message [@userinfobot](https://t.me/userinfobot)
2. Copy ID → `ADMIN_IDS`

### Group ID
1. Add bot as admin to group
2. Forward message to [@RawDataBot](https://t.me/RawDataBot)
3. Copy `id` → `GROUP_IDS`

---

## Troubleshooting

### Bot doesn't start
- Check logs in Railway dashboard
- Verify `BOT_TOKEN` is correct
- Ensure `/data` volume exists

### Web App doesn't load
- Check `WEBAPP_URL` is HTTPS
- Verify `BOT_TOKEN` is valid
- Clear Telegram cache

### Database errors
- Ensure `/data` volume mounted
- Check `DB_PATH` variable

---

## Railway Pricing

- **Free**: $5 credit/month (enough for small bots)
- **Pro**: $20/month (unlimited)

---

## Update Bot

```bash
git push
```

Railway auto-deploys on push!

---

## Success Checklist

- [ ] Bot responds to /start
- [ ] Admin panel opens
- [ ] Database persists
- [ ] No errors in logs

**Done! Your bot is live! 🎉**
