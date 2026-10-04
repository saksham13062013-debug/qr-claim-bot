import fs from "node:fs";
import path from "node:path";
import Database from "better-sqlite3";

const dataDir = process.env.DATA_DIR || "./data";
fs.mkdirSync(dataDir, { recursive: true });
const db = new Database(path.join(dataDir, "bot.sqlite"));
db.pragma("journal_mode = WAL");
db.pragma("foreign_keys = ON");

db.exec(`
CREATE TABLE IF NOT EXISTS users (
  telegram_id TEXT PRIMARY KEY,
  username TEXT,
  first_name TEXT,
  blocked INTEGER NOT NULL DEFAULT 0,
  joined_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  last_seen TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  referred_by TEXT REFERENCES users(telegram_id),
  team_rate REAL NOT NULL DEFAULT 0 CHECK(team_rate >= 0)
);
CREATE TABLE IF NOT EXISTS campaigns (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  description TEXT NOT NULL,
  link_url TEXT NOT NULL,
  qr_path TEXT NOT NULL,
  reward REAL NOT NULL CHECK(reward >= 0),
  status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','closed')),
  winner_telegram_id TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS claims (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  campaign_id INTEGER NOT NULL REFERENCES campaigns(id),
  telegram_id TEXT NOT NULL REFERENCES users(telegram_id),
  status TEXT NOT NULL DEFAULT 'claimed' CHECK(status IN ('claimed','pending','approved','rejected')),
  proof_text TEXT,
  proof_file_id TEXT,
  reward REAL NOT NULL DEFAULT 0,
  inviter_telegram_id TEXT REFERENCES users(telegram_id),
  inviter_commission REAL NOT NULL DEFAULT 0,
  friend_reward REAL NOT NULL DEFAULT 0,
  claimed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  submitted_at TEXT,
  reviewed_at TEXT,
  reviewed_by TEXT,
  UNIQUE(campaign_id, telegram_id)
);
CREATE TABLE IF NOT EXISTS wallet_ledger (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  telegram_id TEXT NOT NULL REFERENCES users(telegram_id),
  claim_id INTEGER REFERENCES claims(id),
  amount REAL NOT NULL,
  note TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  details TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_claims_status ON claims(status);
CREATE INDEX IF NOT EXISTS idx_users_referred_by ON users(referred_by);
CREATE INDEX IF NOT EXISTS idx_users_joined ON users(joined_at);
CREATE TABLE IF NOT EXISTS proof_sessions (
  telegram_id TEXT PRIMARY KEY REFERENCES users(telegram_id),
  campaign_id INTEGER NOT NULL REFERENCES campaigns(id),
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
`);

const ensureColumn = (table, column, definition) => {
  const columns = db.prepare(`PRAGMA table_info(${table})`).all().map(c => c.name);
  if (!columns.includes(column)) db.exec(`ALTER TABLE ${table} ADD COLUMN ${column} ${definition}`);
};
ensureColumn("users", "referred_by", "TEXT REFERENCES users(telegram_id)");
ensureColumn("users", "team_rate", "REAL NOT NULL DEFAULT 0");
ensureColumn("claims", "inviter_telegram_id", "TEXT REFERENCES users(telegram_id)");
ensureColumn("claims", "inviter_commission", "REAL NOT NULL DEFAULT 0");
ensureColumn("claims", "friend_reward", "REAL NOT NULL DEFAULT 0");

export function logAudit(actor, action, details = "") {
  db.prepare("INSERT INTO audit_log(actor, action, details) VALUES (?, ?, ?)")
    .run(String(actor), action, String(details).slice(0, 1000));
}
export function getWalletBalance(telegramId) {
  return db.prepare("SELECT COALESCE(SUM(amount),0) AS balance FROM wallet_ledger WHERE telegram_id = ?")
    .get(String(telegramId)).balance;
}
export default db;
