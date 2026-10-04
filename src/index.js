import "dotenv/config";
import fs from "node:fs";
import path from "node:path";
import crypto from "node:crypto";
import express from "express";
import session from "express-session";
import SQLiteStoreFactory from "connect-sqlite3";
import rateLimit from "express-rate-limit";
import helmet from "helmet";
import multer from "multer";
import bcrypt from "bcryptjs";
import { Bot, InlineKeyboard, InputFile } from "grammy";
import db, { getWalletBalance, logAudit } from "./db.js";

const required = ["BOT_TOKEN", "ADMIN_TELEGRAM_IDS", "ADMIN_USERNAME", "ADMIN_PASSWORD_HASH", "SESSION_SECRET"];
for (const key of required) {
  if (!process.env[key]) {
    console.error(`Missing required environment variable: ${key}`);
    process.exit(1);
  }
}
if (process.env.SESSION_SECRET.length < 32) {
  console.error("SESSION_SECRET must be at least 32 characters.");
  process.exit(1);
}
const port = Number(process.env.PORT || 3000);
const baseUrl = process.env.BASE_URL || "";
const adminIds = new Set(process.env.ADMIN_TELEGRAM_IDS.split(",").map(x => x.trim()).filter(Boolean));
const dataDir = process.env.DATA_DIR || "./data";
const uploadDir = process.env.UPLOAD_DIR || "./uploads";
fs.mkdirSync(uploadDir, { recursive: true });

const bot = new Bot(process.env.BOT_TOKEN);
const app = express();
const SQLiteStore = SQLiteStoreFactory(session);
app.disable("x-powered-by");
app.set("trust proxy", 1);
app.use(helmet({ contentSecurityPolicy: {
  directives: {
    defaultSrc: ["'self'"],
    styleSrc: ["'self'", "'unsafe-inline'"],
    imgSrc: ["'self'", "data:"],
    scriptSrc: ["'self'"],
    objectSrc: ["'none'"],
    frameAncestors: ["'none'"]
  }
}}));
app.use(express.urlencoded({ extended: false, limit: "32kb" }));
app.use(express.json({ limit: "32kb" }));
app.use("/uploads", express.static(uploadDir, { fallthrough: false, dotfiles: "deny", index: false }));
app.use(session({
  store: new SQLiteStore({ db: "sessions.sqlite", dir: dataDir, concurrentDB: true }),
  name: "rewardbot.sid",
  secret: process.env.SESSION_SECRET,
  resave: false,
  saveUninitialized: false,
  cookie: { httpOnly: true, sameSite: "strict", secure: process.env.NODE_ENV === "production", maxAge: 8 * 60 * 60 * 1000 }
}));

const loginLimiter = rateLimit({ windowMs: 15 * 60 * 1000, limit: 8, standardHeaders: true, legacyHeaders: false });
const upload = multer({
  storage: multer.diskStorage({
    destination: (_req, _file, cb) => cb(null, uploadDir),
    filename: (_req, file, cb) => cb(null, `${Date.now()}-${crypto.randomBytes(8).toString("hex")}${path.extname(file.originalname).toLowerCase()}`)
  }),
  limits: { fileSize: 5 * 1024 * 1024, files: 1 },
  fileFilter: (_req, file, cb) => {
    const allowed = new Set(["image/png", "image/jpeg", "image/webp"]);
    cb(allowed.has(file.mimetype) ? null : new Error("QR file must be PNG, JPG or WEBP."), allowed.has(file.mimetype));
  }
});

const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({ "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;" }[c]));
const money = value => Number(value || 0).toFixed(2);
const layout = (title, body, req) => `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>${esc(title)}</title><style>
:root{color-scheme:dark;--bg:#0b1020;--panel:#141c30;--line:#27334d;--text:#e9eefb;--muted:#a7b3ca;--accent:#72a7ff;--green:#66d6a5;--red:#ff8585}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 system-ui,-apple-system,sans-serif}header{border-bottom:1px solid var(--line);padding:16px 22px;display:flex;justify-content:space-between;align-items:center;gap:12px}main{max-width:1180px;margin:24px auto;padding:0 16px}.brand{font-weight:750;font-size:18px}a{color:var(--accent)}.nav{display:flex;gap:14px;flex-wrap:wrap}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px}.card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:16px;margin-bottom:14px}.stat{font-size:28px;font-weight:750}.muted{color:var(--muted)}label{display:block;margin:12px 0 5px;color:var(--muted)}input,textarea,select{width:100%;background:#0b1224;border:1px solid var(--line);border-radius:8px;padding:10px;color:var(--text);font:inherit}textarea{min-height:85px}button,.button{border:0;border-radius:8px;padding:9px 13px;background:var(--accent);color:#081326;font-weight:700;cursor:pointer;text-decoration:none;display:inline-block;margin:3px 2px}button.danger{background:var(--red)}button.good{background:var(--green)}table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:10px;border-bottom:1px solid var(--line);vertical-align:top}th{color:var(--muted)}.tablewrap{overflow:auto}.notice{background:#1c2a45;border-radius:9px;padding:10px;margin:10px 0}.error{color:var(--red)}.success{color:var(--green)}small{color:var(--muted)}code{overflow-wrap:anywhere}@media(max-width:650px){header{align-items:flex-start;flex-direction:column}th,td{padding:7px;font-size:13px}}
</style></head><body><header><div class="brand">🎁 Reward Utility Bot <small>Admin</small></div>${req?.session?.admin ? `<nav class="nav"><a href="/">Overview</a><a href="/campaigns">Campaigns</a><a href="/claims">Verification queue</a><a href="/users">Users</a><a href="/audit">Audit log</a><form method="post" action="/logout" style="display:inline">${csrf(req)}<button>Log out</button></form></nav>` : ""}</header><main>${body}</main></body></html>`;
function requireAdmin(req, res, next) { if (!req.session?.admin) return res.redirect("/login"); next(); }
function flashPage(req, title, message, back = "/") {
  return res.send(layout(title, `<section class="card"><h2>${esc(title)}</h2><p>${esc(message)}</p><a class="button" href="${esc(back)}">Continue</a></section>`, req));
}
function validHttpUrl(raw) {
  try { const u = new URL(raw); return ["https:", "http:"].includes(u.protocol) && !u.username && !u.password; } catch { return false; }
}
function userUpsert(from) {
  db.prepare(`INSERT INTO users(telegram_id,username,first_name,last_seen) VALUES(?,?,?,CURRENT_TIMESTAMP)
    ON CONFLICT(telegram_id) DO UPDATE SET username=excluded.username,first_name=excluded.first_name,last_seen=CURRENT_TIMESTAMP`)
    .run(String(from.id), from.username || "", from.first_name || "");
}
function userIsAdmin(id) { return adminIds.has(String(id)); }
function activeClaimForUser(id) {
  return db.prepare("SELECT c.*,cl.status AS claim_status FROM campaigns c JOIN claims cl ON cl.campaign_id=c.id WHERE cl.telegram_id=? AND cl.status IN ('claimed','pending') ORDER BY cl.claimed_at DESC LIMIT 1").get(String(id));
}
function fmtUser(id) {
  const u = db.prepare("SELECT username,first_name FROM users WHERE telegram_id=?").get(String(id));
  return u ? `${u.first_name || "User"}${u.username ? " @" + u.username : ""} (ID ${id})` : `ID ${id}`;
}

bot.command("start", async ctx => {
  userUpsert(ctx.from);
  const refArg = String(ctx.match || "").trim();
  const refMatch = refArg.match(/^ref_(\d+)$/);
  if (refMatch && refMatch[1] !== String(ctx.from.id)) {
    const referrer = db.prepare("SELECT telegram_id FROM users WHERE telegram_id=?").get(refMatch[1]);
    if (referrer) db.prepare("UPDATE users SET referred_by=? WHERE telegram_id=? AND referred_by IS NULL").run(refMatch[1], String(ctx.from.id));
  }
  const active = db.prepare("SELECT id,title,reward FROM campaigns WHERE status='active' ORDER BY id DESC LIMIT 1").get();
  const keyboard = new InlineKeyboard().text("💰 Wallet", "wallet").text("📦 Orders", "myclaims").row().text("👥 Team", "team").text("🎯 Campaigns", "campaigns");
  if (active) keyboard.row().text("🎁 View available campaign", `campaign:${active.id}`);
  await ctx.reply(
    `Welcome, ${ctx.from.first_name || "there"}!\n\nThis bot offers promotional tasks with manual admin verification.\n\nCommands:\n/wallet — check reward balance\n/claims — view your claim status\n/team — invite link and team stats\n/teamrate 70 — set friend reward rate\n/help — help\n\nSafety: Never submit passwords, OTPs, recovery codes, or banking credentials for a reward.`,
    { reply_markup: keyboard }
  );
});
bot.command("help", ctx => ctx.reply("Commands:\n/start — start the bot\n/wallet — reward balance\n/claims — claim history\n/team — team invite link and stats\n/teamrate 70 — set friend reward rate\n/id — your Telegram ID\n\nClaim a campaign using its button, then submit proof through this bot. Only submit non-sensitive proof."));
bot.command("id", ctx => ctx.reply(`Your Telegram ID: ${ctx.from.id}`));

async function sendTeamSummary(ctx) {
  userUpsert(ctx.from);
  const me = await bot.api.getMe();
  const id = String(ctx.from.id);
  const profile = db.prepare("SELECT team_rate FROM users WHERE telegram_id=?").get(id);
  const members = db.prepare("SELECT COUNT(*) AS n FROM users WHERE referred_by=?").get(id).n;
  const approvedTasks = db.prepare("SELECT COUNT(*) AS n FROM claims WHERE inviter_telegram_id=? AND status='approved'").get(id).n;
  const commission = db.prepare("SELECT COALESCE(SUM(amount),0) AS n FROM wallet_ledger WHERE telegram_id=? AND note LIKE 'Team commission%'").get(id).n;
  const link = `https://t.me/${me.username}?start=ref_${id}`;
  await ctx.reply(`👥 My Team\n\nInvite link:\n${link}\n\nYour friend reward rate: ${money(profile?.team_rate || 0)} units per task\nTeam members: ${members}\nApproved team tasks: ${approvedTasks}\nTotal team commission: ${money(commission)} units\n\nSet a friend's reward rate with /teamrate 70 (example: 70 units). The rate cannot exceed the campaign reward. The remaining amount is your commission after their task is approved.`, { reply_markup: new InlineKeyboard().text("💰 Wallet", "wallet").text("📦 Orders", "myclaims") });
}
bot.command("team", sendTeamSummary);
bot.callbackQuery("team", async ctx => { await ctx.answerCallbackQuery(); await sendTeamSummary(ctx); });
bot.command("teamrate", async ctx => {
  userUpsert(ctx.from);
  const raw = String(ctx.match || "").trim();
  if (!raw || !/^\d+(?:\.\d{1,2})?$/.test(raw)) return ctx.reply("Usage: /teamrate 70\nSet the reward amount (in reward units) your invited friends receive per approved task.");
  const rate = Number(raw);
  if (!Number.isFinite(rate) || rate < 0 || rate > 100000000) return ctx.reply("Please enter a valid non-negative reward rate.");
  db.prepare("UPDATE users SET team_rate=? WHERE telegram_id=?").run(rate, String(ctx.from.id));
  logAudit(String(ctx.from.id), "team_rate_updated", `rate=${rate}`);
  await ctx.reply(`✅ Your team friend rate is now ${money(rate)} units per approved task. If a campaign reward is lower, the friend receives only the campaign amount and your commission is zero for that task. Use /team to view your invite link and team stats.`);
});
bot.command("wallet", async ctx => {
  userUpsert(ctx.from);
  const balance = getWalletBalance(ctx.from.id);
  await ctx.reply(`👛 Wallet balance: ${money(balance)} reward units\n\nRewards are credited after an admin approves your verification.`);
});
bot.command("claims", async ctx => {
  userUpsert(ctx.from);
  const rows = db.prepare(`SELECT c.title,cl.status,cl.reward,cl.claimed_at FROM claims cl JOIN campaigns c ON c.id=cl.campaign_id WHERE cl.telegram_id=? ORDER BY cl.claimed_at DESC LIMIT 10`).all(String(ctx.from.id));
  if (!rows.length) return ctx.reply("You haven't claimed any campaign yet.");
  await ctx.reply("📋 Your recent claims:\n\n" + rows.map(r => `• ${r.title}\n  Status: ${r.status}\n  Reward: ${money(r.reward)}\n  Claimed: ${r.claimed_at}`).join("\n\n"));
});

bot.callbackQuery("wallet", async ctx => {
  userUpsert(ctx.from);
  await ctx.answerCallbackQuery();
  await ctx.reply(`👛 Wallet balance: ${money(getWalletBalance(ctx.from.id))} reward units`);
});
bot.callbackQuery("myclaims", async ctx => {
  userUpsert(ctx.from);
  await ctx.answerCallbackQuery();
  const rows = db.prepare("SELECT c.title,cl.status,cl.reward,cl.inviter_commission FROM claims cl JOIN campaigns c ON c.id=cl.campaign_id WHERE cl.telegram_id=? ORDER BY cl.claimed_at DESC LIMIT 10").all(String(ctx.from.id));
  await ctx.reply(rows.length ? rows.map(r => `${r.title} — ${r.status} — ${money(r.reward)} units`).join("\n") : "No claims yet.");
});
bot.callbackQuery(/^campaign:(\d+)$/, async ctx => {
  const campaign = db.prepare("SELECT * FROM campaigns WHERE id=? AND status='active'").get(Number(ctx.match[1]));
  await ctx.answerCallbackQuery();
  if (!campaign) return ctx.reply("Sorry, this campaign has already been claimed or closed.");
  const keyboard = new InlineKeyboard().text("🎁 Claim reward", `claim:${campaign.id}`);
  await ctx.reply(`🎯 ${campaign.title}\n\n${campaign.description}\n\nReward: ${money(campaign.reward)} units\n\nEach user can claim this campaign once. Your reward is credited after verification.`, { reply_markup: keyboard });
});
bot.callbackQuery(/^claim:(\d+)$/, async ctx => {
  userUpsert(ctx.from);
  const campaignId = Number(ctx.match[1]);
  const claimTx = db.transaction(() => {
    const campaign = db.prepare("SELECT * FROM campaigns WHERE id=? AND status='active'").get(campaignId);
    if (!campaign) return { ok: false, reason: "This campaign is no longer active." };
    const existing = db.prepare("SELECT id FROM claims WHERE campaign_id=? AND telegram_id=?").get(campaignId, String(ctx.from.id));
    if (existing) return { ok: false, reason: "You have already claimed this campaign." };
    const user = db.prepare("SELECT referred_by FROM users WHERE telegram_id=?").get(String(ctx.from.id));
    const inviter = user?.referred_by && user.referred_by !== String(ctx.from.id) ? user.referred_by : null;
    const inviterProfile = inviter ? db.prepare("SELECT team_rate FROM users WHERE telegram_id=?").get(inviter) : null;
    const friendReward = inviter ? Math.min(Number(inviterProfile?.team_rate || 0), Number(campaign.reward)) : Number(campaign.reward);
    const commission = inviter ? Math.max(0, Number(campaign.reward) - friendReward) : 0;
    const result = db.prepare("INSERT INTO claims(campaign_id,telegram_id,status,reward,inviter_telegram_id,inviter_commission,friend_reward) VALUES(?,?,'claimed',?,?,?,?)").run(campaignId, String(ctx.from.id), friendReward, inviter, commission, friendReward);
    db.prepare("UPDATE campaigns SET winner_telegram_id=COALESCE(winner_telegram_id,?) WHERE id=?").run(String(ctx.from.id), campaignId);
    return { ok: true, campaign, claimId: Number(result.lastInsertRowid) };
  });
  const result = claimTx();
  await ctx.answerCallbackQuery({ text: result.ok ? "Claim successful!" : "Claim unavailable", show_alert: !result.ok });
  if (!result.ok) return;
  logAudit(String(ctx.from.id), "campaign_claimed", `campaign=${campaignId}; claim=${result.claimId}`);
  const c = result.campaign;
  const keyboard = new InlineKeyboard().text("✅ Submit for verification", `submit:${c.id}`);
  const photoPath = path.resolve(c.qr_path);
  const claimRow = db.prepare("SELECT reward FROM claims WHERE id=?").get(result.claimId);
  const caption = `🎉 Claim successful!\n\n${c.title}\nYour task reward: ${money(claimRow?.reward ?? c.reward)} units\n\nLink: ${c.link_url}\n\nNext, complete the task and press “Submit for verification”. Send only non-sensitive proof. Never send passwords, OTPs, recovery codes, or financial credentials.`;
  try {
    if (fs.existsSync(photoPath)) await ctx.replyWithPhoto(new InputFile(photoPath), { caption, reply_markup: keyboard });
    else await ctx.reply(`${caption}\n\nQR image: ${baseUrl}${c.qr_path}`, { reply_markup: keyboard });
  } catch {
    await ctx.reply(`${caption}\n\nQR image: ${baseUrl}${c.qr_path}`, { reply_markup: keyboard });
  }
});
bot.callbackQuery(/^submit:(\d+)$/, async ctx => {
  const campaignId = Number(ctx.match[1]);
  const claim = db.prepare("SELECT * FROM claims WHERE campaign_id=? AND telegram_id=?").get(campaignId, String(ctx.from.id));
  if (!claim) {
    await ctx.answerCallbackQuery({ text: "No claim found.", show_alert: true });
    return;
  }
  if (["pending","approved"].includes(claim.status)) {
    await ctx.answerCallbackQuery({ text: `Your claim status is ${claim.status}.`, show_alert: true });
    return;
  }
  if (claim.status === "rejected") {
    await ctx.answerCallbackQuery({ text: "This claim was rejected.", show_alert: true });
    return;
  }
  db.prepare("UPDATE claims SET status='pending',submitted_at=CURRENT_TIMESTAMP WHERE id=?").run(claim.id);
  await ctx.answerCallbackQuery({ text: "Verification started" });
  await ctx.reply("📩 Send your proof as a photo or a short text message now. Do not send passwords, OTPs, recovery codes, private keys, or banking/payment credentials. Your proof will be reviewed by the campaign admin. Send /cancel to stop.");
  // Store submission state in DB, so it survives process restarts.
  db.prepare("INSERT OR REPLACE INTO audit_log(actor,action,details) VALUES(?,?,?)")
    .run(String(ctx.from.id), "verification_prompted", `campaign=${campaignId}`);
  db.prepare("UPDATE claims SET status='claimed' WHERE id=? AND status='pending' AND proof_text IS NULL AND proof_file_id IS NULL").run(claim.id);
  db.prepare("UPDATE users SET last_seen=CURRENT_TIMESTAMP WHERE telegram_id=?").run(String(ctx.from.id));
  db.prepare("INSERT OR REPLACE INTO audit_log(actor,action,details) VALUES(?,?,?)")
    .run(String(ctx.from.id), "awaiting_proof", `campaign=${campaignId}; claim=${claim.id}`);
  db.prepare("UPDATE claims SET status='pending' WHERE id=?").run(claim.id);
  // Awaiting proof is tracked in a dedicated table.
  db.prepare("INSERT OR REPLACE INTO proof_sessions(telegram_id,campaign_id) VALUES(?,?)").run(String(ctx.from.id), campaignId);
});
bot.command("cancel", async ctx => {
  db.prepare("DELETE FROM proof_sessions WHERE telegram_id=?").run(String(ctx.from.id));
  await ctx.reply("Proof submission cancelled. Your claim remains saved.");
});
bot.on("message", async ctx => {
  if (!ctx.from || (!ctx.message.text && !ctx.message.photo && !ctx.message.document)) return;
  userUpsert(ctx.from);
  const pending = db.prepare("SELECT campaign_id FROM proof_sessions WHERE telegram_id=?").get(String(ctx.from.id));
  if (!pending) return;
  const claim = db.prepare("SELECT * FROM claims WHERE campaign_id=? AND telegram_id=?").get(pending.campaign_id, String(ctx.from.id));
  if (!claim || claim.status !== "pending") {
    db.prepare("DELETE FROM proof_sessions WHERE telegram_id=?").run(String(ctx.from.id));
    return;
  }
  const text = ctx.message.text ? ctx.message.text.slice(0, 3000) : (ctx.message.caption || "").slice(0, 1000);
  const photos = ctx.message.photo;
  const photoFileId = photos?.length ? photos[photos.length - 1].file_id : null;
  if (!text && !photoFileId && !ctx.message.document) return;
  db.prepare("UPDATE claims SET proof_text=?,proof_file_id=?,submitted_at=CURRENT_TIMESTAMP,status='pending' WHERE id=?")
    .run(text || (ctx.message.document ? `Document submitted: ${ctx.message.document.file_name || "file"}` : ""), photoFileId, claim.id);
  db.prepare("DELETE FROM proof_sessions WHERE telegram_id=?").run(String(ctx.from.id));
  await ctx.reply("✅ Proof received. Your submission is waiting for admin verification. Use /claims to check status.");
  const campaign = db.prepare("SELECT title FROM campaigns WHERE id=?").get(pending.campaign_id);
  const adminCaption = `🧾 Verification submission\nCampaign: ${campaign?.title || pending.campaign_id}\nClaim ID: ${claim.id}\nUser: ${ctx.from.first_name || "User"} (@${ctx.from.username || "no-username"})\nTelegram ID: ${ctx.from.id}\n\nReview and approve/reject in the web admin dashboard.`;
  for (const adminId of adminIds) {
    try {
      if (photoFileId) await bot.api.sendPhoto(adminId, photoFileId, { caption: adminCaption });
      else await bot.api.sendMessage(adminId, `${adminCaption}\n\nProof: ${text || "Document submitted"}`);
    } catch (e) {
      console.error(`Could not notify admin ${adminId} about claim ${claim.id}:`, e?.message || e);
    }
  }
  logAudit(String(ctx.from.id), "proof_submitted", `campaign=${pending.campaign_id}; claim=${claim.id}`);
});
bot.catch(err => console.error("Telegram bot error:", err));

db.exec(`CREATE TABLE IF NOT EXISTS proof_sessions (
  telegram_id TEXT PRIMARY KEY REFERENCES users(telegram_id),
  campaign_id INTEGER NOT NULL REFERENCES campaigns(id),
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)`);

app.get("/health", (_req, res) => res.json({ ok: true, service: "telegram-reward-bot" }));
app.get("/login", (req, res) => {
  if (req.session.admin) return res.redirect("/");
  res.send(layout("Admin login", `<section class="card" style="max-width:430px;margin:60px auto"><h1>Admin login</h1><p class="muted">Sign in to manage campaigns and verification.</p>${req.query.error ? '<p class="error">Invalid credentials.</p>' : ''}<form method="post" action="/login"><label>Username</label><input name="username" required autocomplete="username" maxlength="100"><label>Password</label><input name="password" type="password" required autocomplete="current-password" maxlength="200"><button style="width:100%;margin-top:16px">Sign in</button></form></section>`, req));
});
app.post("/login", loginLimiter, async (req, res) => {
  const usernameOk = typeof req.body.username === "string" && crypto.timingSafeEqual(
    Buffer.from(req.body.username.padEnd(256).slice(0,256)),
    Buffer.from(String(process.env.ADMIN_USERNAME).padEnd(256).slice(0,256))
  );
  const passwordOk = typeof req.body.password === "string" && await bcrypt.compare(req.body.password, process.env.ADMIN_PASSWORD_HASH);
  if (!usernameOk || !passwordOk) {
    logAudit("web", "login_failed", `ip=${req.ip}`);
    return res.redirect("/login?error=1");
  }
  req.session.regenerate(err => {
    if (err) return res.status(500).send("Could not start session.");
    req.session.admin = true;
    req.session.csrf = crypto.randomBytes(24).toString("hex");
    logAudit("web", "login_success", `ip=${req.ip}`);
    res.redirect("/");
  });
});
function csrfIsValid(req) {
  const token = req.body?._csrf;
  if (typeof token !== "string" || typeof req.session?.csrf !== "string") return false;
  const a = Buffer.from(token), b = Buffer.from(req.session.csrf);
  return a.length === b.length && crypto.timingSafeEqual(a, b);
}
app.use((req, res, next) => {
  if (!req.session?.admin || ["GET", "HEAD"].includes(req.method)) return next();
  // Multipart form fields are parsed by multer in the specific upload route.
  if (req.is("multipart/form-data")) return next();
  if (!csrfIsValid(req)) return res.status(403).send("Invalid CSRF token. Refresh the page and try again.");
  next();
});
function csrf(req) { return `<input type="hidden" name="_csrf" value="${esc(req.session.csrf || "")}">`; }
app.post("/logout", requireAdmin, (req, res) => req.session.destroy(() => res.redirect("/login")));
app.get("/", requireAdmin, (req, res) => {
  const users = db.prepare("SELECT COUNT(*) n FROM users").get().n;
  const active = db.prepare("SELECT COUNT(*) n FROM campaigns WHERE status='active'").get().n;
  const pending = db.prepare("SELECT COUNT(*) n FROM claims WHERE status='pending'").get().n;
  const paid = db.prepare("SELECT COALESCE(SUM(amount),0) n FROM wallet_ledger WHERE amount>0").get().n;
  const recent = db.prepare(`SELECT cl.id,cl.status,cl.claimed_at,c.title,cl.telegram_id FROM claims cl JOIN campaigns c ON c.id=cl.campaign_id ORDER BY cl.id DESC LIMIT 8`).all();
  res.send(layout("Overview", `<h1>Dashboard</h1><div class="grid">
    <div class="card"><div class="muted">Registered users</div><div class="stat">${users}</div></div>
    <div class="card"><div class="muted">Available campaigns</div><div class="stat">${active}</div></div>
    <div class="card"><div class="muted">Pending verification</div><div class="stat">${pending}</div></div>
    <div class="card"><div class="muted">Total rewards credited</div><div class="stat">${money(paid)}</div></div></div>
    <section class="card"><h2>Broadcast announcement</h2><p class="muted">Send a message to registered users. Telegram delivery limits apply.</p><form method="post" action="/broadcast">${csrf(req)}<label>Message</label><textarea name="message" required maxlength="3500" placeholder="Your announcement..."></textarea><button>Send broadcast</button></form></section>
    <section class="card"><h2>Recent claims</h2><div class="tablewrap"><table><thead><tr><th>ID</th><th>Campaign</th><th>User</th><th>Status</th><th>Claimed</th></tr></thead><tbody>${recent.map(r=>`<tr><td>${r.id}</td><td>${esc(r.title)}</td><td>${esc(fmtUser(r.telegram_id))}</td><td>${esc(r.status)}</td><td>${esc(r.claimed_at)}</td></tr>`).join("") || '<tr><td colspan="5">No claims yet.</td></tr>'}</tbody></table></div><p><a href="/claims">Open verification queue →</a></p></section>`, req));
});
app.get("/campaigns", requireAdmin, (req, res) => {
  const rows = db.prepare("SELECT * FROM campaigns ORDER BY id DESC").all();
  res.send(layout("Campaigns", `<h1>Campaigns</h1><section class="card"><h2>Create campaign</h2><form method="post" action="/campaigns" enctype="multipart/form-data">${csrf(req)}
    <label>Campaign title</label><input name="title" required maxlength="100">
    <label>Description / task instructions</label><textarea name="description" required maxlength="1500"></textarea>
    <label>Destination link (HTTP/HTTPS only)</label><input type="url" name="link_url" required maxlength="1000" placeholder="https://example.com/task">
    <label>QR image (PNG/JPG/WEBP, max 5 MB)</label><input type="file" name="qr" accept="image/png,image/jpeg,image/webp" required>
    <label>Reward amount (non-negative number; wallet credits after approval)</label><input type="number" name="reward" min="0" max="100000000" step="0.01" required value="10">
    <p class="muted">First-claim-wins: one user can claim each campaign. Verify that your link and task are legitimate before publishing.</p><button>Create and notify users</button></form></section>
    <section class="card"><h2>All campaigns</h2><div class="tablewrap"><table><thead><tr><th>ID</th><th>Campaign</th><th>Reward</th><th>Status</th><th>Winner</th><th>Created</th><th>Action</th></tr></thead><tbody>${rows.map(c=>`<tr><td>${c.id}</td><td>${esc(c.title)}<br><small>${esc(c.link_url)}</small><br><img src="${esc(c.qr_path)}" alt="QR" style="max-width:80px;max-height:80px"></td><td>${money(c.reward)}</td><td>${esc(c.status)}</td><td>${esc(c.winner_telegram_id || "—")}</td><td>${esc(c.created_at)}</td><td>${c.status==="active" ? `<form method="post" action="/campaigns/${c.id}/close">${csrf(req)}<button class="danger">Close</button></form>` : "—"}</td></tr>`).join("") || '<tr><td colspan="7">No campaigns created.</td></tr>'}</tbody></table></div></section>`, req));
});
app.post("/campaigns", requireAdmin, (req, res) => {
  upload.single("qr")(req, res, async err => {
    if (err) return flashPage(req, "Upload failed", err.message, "/campaigns");
    if (!csrfIsValid(req)) {
      if (req.file) fs.unlink(req.file.path, () => {});
      return res.status(403).send("Invalid CSRF token. Refresh the page and try again.");
    }
    const { title, description, link_url, reward } = req.body;
    const amount = Number(reward);
    if (!title?.trim() || !description?.trim() || !validHttpUrl(link_url) || !Number.isFinite(amount) || amount < 0 || amount > 100000000 || !req.file) {
      if (req.file) fs.unlink(req.file.path, () => {});
      return flashPage(req, "Invalid campaign", "Check title, description, HTTP(S) link, QR image and reward amount.", "/campaigns");
    }
    const qrPath = `/uploads/${path.basename(req.file.path)}`;
    const result = db.prepare("INSERT INTO campaigns(title,description,link_url,qr_path,reward) VALUES(?,?,?,?,?)")
      .run(title.trim(), description.trim(), link_url, qrPath, amount);
    logAudit("web", "campaign_created", `campaign=${result.lastInsertRowid}; title=${title.trim()}`);
    const users = db.prepare("SELECT telegram_id FROM users WHERE blocked=0").all();
    let sent = 0, failed = 0;
    const keyboard = new InlineKeyboard().text("🎁 View & claim", `campaign:${result.lastInsertRowid}`);
    for (const user of users) {
      try {
        await bot.api.sendMessage(user.telegram_id, `🎁 NEW REWARD CAMPAIGN\n\n${title.trim()}\n\n${description.trim()}\n\nReward: ${money(amount)} units\n\nFirst valid claim wins. Tap below to see details.`, { reply_markup: keyboard });
        sent++;
      } catch (e) {
        failed++;
        if (e?.error_code === 403) db.prepare("UPDATE users SET blocked=1 WHERE telegram_id=?").run(user.telegram_id);
      }
      await new Promise(resolve => setTimeout(resolve, 35));
    }
    logAudit("web", "campaign_broadcast", `campaign=${result.lastInsertRowid}; sent=${sent}; failed=${failed}`);
    return flashPage(req, "Campaign created", `Campaign #${result.lastInsertRowid} created. Notification sent to ${sent} users; ${failed} delivery failures.`, "/campaigns");
  });
});
app.post("/campaigns/:id/close", requireAdmin, (req, res) => {
  db.prepare("UPDATE campaigns SET status='closed' WHERE id=?").run(Number(req.params.id));
  logAudit("web", "campaign_closed", `campaign=${req.params.id}`);
  res.redirect("/campaigns");
});
app.get("/claims", requireAdmin, (req, res) => {
  const rows = db.prepare(`SELECT cl.*,c.title,u.username,u.first_name FROM claims cl JOIN campaigns c ON c.id=cl.campaign_id JOIN users u ON u.telegram_id=cl.telegram_id ORDER BY CASE cl.status WHEN 'pending' THEN 0 ELSE 1 END, cl.id DESC LIMIT 250`).all();
  res.send(layout("Verification queue", `<h1>Verification queue</h1><p class="muted">Review proof manually. Approve only after confirming the task was completed. Do not request passwords, OTPs, recovery codes, or financial credentials.</p><section class="card"><div class="tablewrap"><table><thead><tr><th>Claim</th><th>User</th><th>Campaign</th><th>Proof</th><th>Status</th><th>Review</th></tr></thead><tbody>${rows.map(r=>`<tr><td>#${r.id}<br><small>${esc(r.submitted_at || r.claimed_at)}</small></td><td>${esc(r.first_name || "User")}<br>@${esc(r.username || "—")}<br><small>${esc(r.telegram_id)}</small></td><td>${esc(r.title)}<br>Reward: ${money(r.reward)}</td><td>${esc(r.proof_text || "—")}${r.proof_file_id ? `<br><small>Telegram photo file ID: ${esc(r.proof_file_id)}</small>` : ""}</td><td>${esc(r.status)}</td><td>${r.status==="pending" ? `<form method="post" action="/claims/${r.id}/review">${csrf(req)}<button name="decision" value="approve" class="good">Approve & credit</button><button name="decision" value="reject" class="danger">Reject</button><label>Admin note (optional)</label><input name="note" maxlength="300"></form>` : "Reviewed"}</td></tr>`).join("") || '<tr><td colspan="6">No claims yet.</td></tr>'}</tbody></table></div></section>`, req));
});
app.post("/claims/:id/review", requireAdmin, async (req, res) => {
  const claimId = Number(req.params.id);
  const decision = req.body.decision;
  const note = String(req.body.note || "").slice(0, 300);
  if (!["approve", "reject"].includes(decision)) return res.status(400).send("Invalid decision.");
  const outcome = db.transaction(() => {
    const claim = db.prepare("SELECT cl.*,c.title FROM claims cl JOIN campaigns c ON c.id=cl.campaign_id WHERE cl.id=?").get(claimId);
    if (!claim) return { error: "Claim not found." };
    if (claim.status !== "pending") return { error: "This claim has already been reviewed." };
    const status = decision === "approve" ? "approved" : "rejected";
    db.prepare("UPDATE claims SET status=?,reviewed_at=CURRENT_TIMESTAMP,reviewed_by=?,proof_text=CASE WHEN ?='' THEN proof_text ELSE COALESCE(proof_text,'') || char(10) || '[Admin note] ' || ? END WHERE id=?")
      .run(status, req.session.admin ? "web-admin" : "unknown", note, note, claimId);
    if (decision === "approve") {
      db.prepare("INSERT INTO wallet_ledger(telegram_id,claim_id,amount,note) VALUES(?,?,?,?)")
        .run(claim.telegram_id, claim.id, claim.reward, `Reward: ${claim.title}`);
      if (claim.inviter_telegram_id && claim.inviter_commission > 0) {
        db.prepare("INSERT INTO wallet_ledger(telegram_id,claim_id,amount,note) VALUES(?,?,?,?)")
          .run(claim.inviter_telegram_id, claim.id, claim.inviter_commission, `Team commission for ${claim.title}`);
      }
    }
    logAudit("web", `claim_${decision}`, `claim=${claimId}; user=${claim.telegram_id}; reward=${decision==="approve"?claim.reward:0}; note=${note}`);
    return { claim, status };
  });
  const result = outcome();
  if (result.error) return flashPage(req, "Could not review claim", result.error, "/claims");
  try {
    await bot.api.sendMessage(result.claim.telegram_id, decision === "approve"
      ? `🎉 Your submission for “${result.claim.title}” has been approved!\n${money(result.claim.reward)} reward units have been added to your wallet.\nUse /wallet to check your balance.`
      : `Your submission for “${result.claim.title}” was not approved.${note ? ` Admin note: ${note}` : ""}\nIf you believe this is a mistake, contact the campaign administrator.`);
  } catch {}
  res.redirect("/claims");
});
app.get("/users", requireAdmin, (req, res) => {
  const rows = db.prepare(`SELECT u.*,COALESCE((SELECT SUM(amount) FROM wallet_ledger l WHERE l.telegram_id=u.telegram_id),0) balance,(SELECT COUNT(*) FROM claims c WHERE c.telegram_id=u.telegram_id) claims_count FROM users u ORDER BY u.joined_at DESC LIMIT 300`).all();
  res.send(layout("Users", `<h1>Users</h1><p class="muted">Latest 300 registered users.</p><section class="card"><div class="tablewrap"><table><thead><tr><th>User</th><th>Joined</th><th>Last seen</th><th>Claims</th><th>Wallet</th><th>State</th><th>Action</th></tr></thead><tbody>${rows.map(u=>`<tr><td>${esc(u.first_name || "User")}<br>@${esc(u.username || "—")}<br><small>${esc(u.telegram_id)}</small></td><td>${esc(u.joined_at)}</td><td>${esc(u.last_seen)}</td><td>${u.claims_count}</td><td>${money(u.balance)}</td><td>${u.blocked ? "Blocked from broadcasts" : "Active"}</td><td><form method="post" action="/users/${encodeURIComponent(u.telegram_id)}/toggle">${csrf(req)}<button>${u.blocked ? "Unblock" : "Block broadcasts"}</button></form></td></tr>`).join("") || '<tr><td colspan="7">No users yet.</td></tr>'}</tbody></table></div></section>`, req));
});
app.post("/users/:id/toggle", requireAdmin, (req, res) => {
  const id = String(req.params.id);
  const user = db.prepare("SELECT blocked FROM users WHERE telegram_id=?").get(id);
  if (user) {
    db.prepare("UPDATE users SET blocked=? WHERE telegram_id=?").run(user.blocked ? 0 : 1, id);
    logAudit("web", "user_broadcast_block_toggle", `user=${id}; blocked=${user.blocked ? 0 : 1}`);
  }
  res.redirect("/users");
});
app.post("/broadcast", requireAdmin, async (req, res) => {
  const message = String(req.body.message || "").trim();
  if (!message || message.length > 3500) return flashPage(req, "Invalid broadcast", "Message must be between 1 and 3500 characters.", "/");
  const users = db.prepare("SELECT telegram_id FROM users WHERE blocked=0").all();
  let sent = 0, failed = 0;
  for (const u of users) {
    try { await bot.api.sendMessage(u.telegram_id, `📢 ADMIN ANNOUNCEMENT\n\n${message}`); sent++; }
    catch (e) { failed++; if (e?.error_code === 403) db.prepare("UPDATE users SET blocked=1 WHERE telegram_id=?").run(u.telegram_id); }
    await new Promise(resolve => setTimeout(resolve, 35));
  }
  logAudit("web", "broadcast_sent", `sent=${sent}; failed=${failed}`);
  flashPage(req, "Broadcast complete", `Sent: ${sent}. Failed: ${failed}.`, "/");
});
app.get("/audit", requireAdmin, (req, res) => {
  const rows = db.prepare("SELECT * FROM audit_log ORDER BY id DESC LIMIT 200").all();
  res.send(layout("Audit log", `<h1>Audit log</h1><section class="card"><div class="tablewrap"><table><thead><tr><th>Time</th><th>Actor</th><th>Action</th><th>Details</th></tr></thead><tbody>${rows.map(r=>`<tr><td>${esc(r.created_at)}</td><td>${esc(r.actor)}</td><td>${esc(r.action)}</td><td>${esc(r.details)}</td></tr>`).join("")}</tbody></table></div></section>`, req));
});
app.use((err, req, res, _next) => {
  console.error("Web error:", err?.message || err);
  if (res.headersSent) return;
  res.status(500).send(layout("Error", `<section class="card"><h2>Something went wrong</h2><p>Request could not be completed. Check server logs.</p><a href="/">Dashboard</a></section>`, req));
});

const server = app.listen(port, "0.0.0.0", () => console.log(`Admin dashboard listening on port ${port}`));
bot.start({ onStart: info => console.log(`Telegram bot @${info.username} started`) }).catch(err => {
  console.error("Bot failed to start:", err);
  process.exitCode = 1;
});
async function shutdown(signal) {
  console.log(`Received ${signal}; shutting down...`);
  bot.stop();
  server.close(() => process.exit(0));
  setTimeout(() => process.exit(1), 10000).unref();
}
process.once("SIGINT", () => shutdown("SIGINT"));
process.once("SIGTERM", () => shutdown("SIGTERM"));
