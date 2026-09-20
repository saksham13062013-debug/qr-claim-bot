# Railway Deployment Checklist

## Before Deploy

- [ ] Bot created with BotFather
- [ ] Bot token copied
- [ ] GitHub repository created
- [ ] All files committed

## Railway Setup

- [ ] Project created
- [ ] GitHub connected
- [ ] BOT_TOKEN set
- [ ] ADMIN_IDS set
- [ ] WEBAPP_URL set
- [ ] DB_PATH=/data/qr_claim_bot.db
- [ ] /data volume created (1GB+)

## After Deploy

- [ ] Bot responds to /start
- [ ] Admin panel opens
- [ ] Database persists
- [ ] No errors in logs

## Optional

- [ ] GROUP_IDS configured
- [ ] FORCE_JOIN configured
- [ ] Custom domain added

## Troubleshooting

**Bot not starting:**
- Check BOT_TOKEN
- View Railway logs
- Verify /data volume

**Web App not loading:**
- Check WEBAPP_URL (HTTPS)
- Verify BOT_TOKEN
- Clear Telegram cache

**Database errors:**
- Ensure /data volume mounted
- Check DB_PATH

## Support

- Railway docs: docs.railway.app
- Railway Discord: railway.app/discord
