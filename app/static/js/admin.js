/* ============================================================
   QR Claim Admin Panel – Professional SPA
   ============================================================ */

const API = "/api";
let currentPage = "dashboard";
let adminRole = localStorage.getItem("admin_role") || "";
let adminUsername = localStorage.getItem("admin_username") || "";
let offset = 0;
const LIMIT = 30;
let chartInstances = [];

/* ---------- API ---------- */

async function api(path, opts = {}) {
  const headers = { ...(opts.headers || {}) };
  if (!(opts.body instanceof FormData)) {
    headers["Content-Type"] = "application/json";
  }
  const csrf = localStorage.getItem("csrf_token");
  if (csrf) headers["X-CSRF-Token"] = csrf;
  const token = localStorage.getItem("access_token");
  if (token) headers["Authorization"] = "Bearer " + token;

  const res = await fetch(API + path, {
    ...opts,
    headers,
    credentials: "include",
    body:
      opts.body && typeof opts.body === "object" && !(opts.body instanceof FormData)
        ? JSON.stringify(opts.body)
        : opts.body,
  });

  if (res.status === 401) {
    localStorage.clear();
    window.location.href = "/";
    throw new Error("Unauthorized");
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    let msg = data.message || `Error ${res.status}`;
    if (typeof data.detail === "string") msg = data.detail;
    else if (Array.isArray(data.detail)) {
      msg = data.detail.map((d) => d.msg || JSON.stringify(d)).join("; ");
    } else if (data.detail) msg = String(data.detail);
    throw new Error(msg);
  }
  return data;
}

/* ---------- Theme / sound / haptics ---------- */

const PREFS = {
  theme: localStorage.getItem("admin_theme") || "dark",
  sound: localStorage.getItem("admin_sound") !== "0",
};

function applyTheme(theme) {
  PREFS.theme = theme;
  document.documentElement.setAttribute("data-theme", theme);
  localStorage.setItem("admin_theme", theme);
}

function haptic(pattern = 12) {
  try {
    if (navigator.vibrate) navigator.vibrate(pattern);
  } catch (_) {}
}

let _audioCtx = null;
function playTone(freq = 520, ms = 80, type = "sine") {
  if (!PREFS.sound) return;
  try {
    _audioCtx = _audioCtx || new (window.AudioContext || window.webkitAudioContext)();
    const o = _audioCtx.createOscillator();
    const g = _audioCtx.createGain();
    o.type = type;
    o.frequency.value = freq;
    g.gain.value = 0.04;
    o.connect(g);
    g.connect(_audioCtx.destination);
    o.start();
    g.gain.exponentialRampToValueAtTime(0.001, _audioCtx.currentTime + ms / 1000);
    o.stop(_audioCtx.currentTime + ms / 1000);
  } catch (_) {}
}

function fxSuccess() {
  haptic([10, 30, 10]);
  playTone(660, 70);
  setTimeout(() => playTone(880, 90), 70);
}
function fxError() {
  haptic([40, 20, 40]);
  playTone(220, 120, "square");
}
function fxClick() {
  haptic(8);
  playTone(480, 40);
}

/* Prevent double-clicks on action buttons */
function onceButton(btn, fn) {
  if (!btn || btn.disabled) return;
  btn.disabled = true;
  btn.classList.add("is-loading");
  Promise.resolve()
    .then(fn)
    .finally(() => {
      setTimeout(() => {
        btn.disabled = false;
        btn.classList.remove("is-loading");
      }, 600);
    });
}

/* ---------- UI helpers ---------- */

function toast(msg, type = "success") {
  const container = document.getElementById("toastContainer");
  const el = document.createElement("div");
  el.className = `toast ${type} anim-slide`;
  el.innerHTML = `<span>${escapeHtml(msg)}</span>`;
  container.appendChild(el);
  if (type === "error") fxError();
  else if (type === "success") fxSuccess();
  else fxClick();
  setTimeout(() => {
    el.style.opacity = "0";
    el.style.transition = "opacity 0.3s";
    setTimeout(() => el.remove(), 300);
  }, 3500);
}

function cents(c) {
  return "$" + (Number(c) / 100).toFixed(2);
}

function badge(status) {
  const s = (status || "").toLowerCase();
  return `<span class="badge badge-${s}">${status}</span>`;
}


function escapeHtml(str) {
  if (!str) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

/** Clickable Telegram profile link */
function tgUserLink(username, telegramId, firstName, lastName) {
  const name = [firstName, lastName].filter(Boolean).join(" ").trim()
    || (username ? "@" + username : null)
    || (telegramId ? "User " + telegramId : "—");
  if (!telegramId && !username) return escapeHtml(name);
  const href = username
    ? `https://t.me/${encodeURIComponent(username.replace(/^@/, ""))}`
    : `tg://user?id=${telegramId}`;
  const sub = username ? `@${escapeHtml(username.replace(/^@/, ""))}` : (telegramId ? `ID ${telegramId}` : "");
  return `<a class="tg-link" href="${href}" target="_blank" rel="noopener" title="Open Telegram profile">${escapeHtml(name)}</a>${sub ? `<div class="muted-xs">${sub}</div>` : ""}`;
}

function copyText(text) {
  navigator.clipboard.writeText(text).then(() => toast("Copied: " + text)).catch(() => toast("Copy failed", "error"));
}

function downloadCsv(filename, rows) {
  const csv = rows.map(r => r.map(c => {
    const s = String(c ?? "");
    return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  }).join(",")).join("\n");
  const blob = new Blob([csv], { type: "text/csv;charset=utf-8;" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  a.click();
  URL.revokeObjectURL(a.href);
}


function fmtDate(iso) {
  if (!iso) return "—";
  try {
    const d = new Date(iso);
    return d.toLocaleString(undefined, {
      year: "numeric", month: "short", day: "numeric",
      hour: "2-digit", minute: "2-digit",
    });
  } catch {
    return iso.slice(0, 16);
  }
}

function emptyState(_icon, text) {
  return `<div class="empty-state anim-fade"><p>${text}</p></div>`;
}

/* ---------- Modal ---------- */

function openModal(title, bodyHtml, footerHtml) {
  document.getElementById("modalTitle").textContent = title;
  document.getElementById("modalBody").innerHTML = bodyHtml;
  document.getElementById("modalFooter").innerHTML = footerHtml || "";
  document.getElementById("modal").classList.remove("hidden");
  document.body.style.overflow = "hidden";
}

function closeModal() {
  document.getElementById("modal").classList.add("hidden");
  document.body.style.overflow = "";
}

document.getElementById("modalClose").onclick = closeModal;
document.getElementById("modal").addEventListener("click", (e) => {
  if (e.target.id === "modal") closeModal();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeModal();
});

/* ---------- Sidebar ---------- */

const sidebar = document.getElementById("sidebar");
const overlay = document.getElementById("sidebarOverlay");

function openSidebar() {
  sidebar.classList.add("open");
  overlay.classList.add("open");
}
function closeSidebar() {
  sidebar.classList.remove("open");
  overlay.classList.remove("open");
}

document.getElementById("sidebarToggle").onclick = openSidebar;
document.getElementById("sidebarClose").onclick = closeSidebar;
overlay.onclick = closeSidebar;

document.querySelectorAll(".nav-item").forEach((el) => {
  el.addEventListener("click", (e) => {
    e.preventDefault();
    navigate(el.dataset.page);
    closeSidebar();
  });
});

document.getElementById("logoutBtn").onclick = async () => {
  try { await api("/auth/logout", { method: "POST" }); } catch {}
  localStorage.clear();
  window.location.href = "/";
};

/* ---------- Navigation ---------- */

const PAGE_META = {
  dashboard: { title: "Dashboard", sub: "Overview of your reward system" },
  users: { title: "Users", sub: "Manage Telegram users & balances" },
  qrs: { title: "QR Codes", sub: "Create, post and manage reward codes" },
  claims: { title: "Claims", sub: "Review and approve reward claims" },
  withdrawals: { title: "Withdrawals", sub: "Process payout requests" },
  channels: { title: "Channels", sub: "Force-join channel requirements" },
  campaigns: { title: "Campaigns", sub: "Campaign performance overview" },
  tickets: { title: "Support Tickets", sub: "User support messages" },
  broadcast: { title: "Broadcast", sub: "Send a message to all bot users" },
  settings: { title: "Settings", sub: "Bot configuration & defaults" },
  audit: { title: "Audit Log", sub: "Security and action history" },
  admins: { title: "Admins & Roles", sub: "Manage admin accounts and permissions" },
  reset: { title: "Reset", sub: "Owner-only destructive operations" },
};

function navigate(page) {
  currentPage = page;
  offset = 0;
  destroyCharts();
  document.querySelectorAll(".nav-item").forEach((n) =>
    n.classList.toggle("active", n.dataset.page === page)
  );
  const meta = PAGE_META[page] || { title: page, sub: "" };
  document.getElementById("pageTitle").textContent = meta.title;
  document.getElementById("pageSub").textContent = meta.sub;
  document.getElementById("topbarActions").innerHTML = "";
  loadPage(page);
}

async function loadPage(page) {
  const content = document.getElementById("content");
  content.innerHTML = `
    <div class="skeleton-page">
      <div class="skeleton skeleton-card"></div>
      <div class="skeleton skeleton-card"></div>
      <div class="skeleton skeleton-card"></div>
      <div class="skeleton skeleton-table"></div>
    </div>`;
  try {
    const handlers = {
      dashboard: renderDashboard,
      users: renderUsers,
      qrs: renderQRs,
      claims: renderClaims,
      withdrawals: renderWithdrawals,
      channels: renderChannels,
      campaigns: renderCampaigns,
      tickets: renderTickets,
      broadcast: renderBroadcast,
      settings: renderSettings,
      admins: renderAdmins,
      audit: renderAudit,
      reset: renderReset,
    };
    const fn = handlers[page];
    if (!fn) {
      content.innerHTML = `<div class="error-banner">Unknown page: ${escapeHtml(page)}</div>`;
      return;
    }
    await fn();
  } catch (err) {
    content.innerHTML = `<div class="error-banner">${escapeHtml(err.message || String(err))}</div>`;
  }
}

/* ---------- Charts ---------- */

function destroyCharts() {
  chartInstances.forEach((c) => c.destroy());
  chartInstances = [];
}

function makeChart(id, rows, key, color) {
  const ctx = document.getElementById(id);
  if (!ctx || typeof Chart === "undefined") return;
  const chart = new Chart(ctx, {
    type: "line",
    data: {
      labels: rows.map((r) => r.date),
      datasets: [{
        data: rows.map((r) => r[key]),
        borderColor: color,
        backgroundColor: color + "22",
        fill: true,
        tension: 0.35,
        pointRadius: 2,
        pointHoverRadius: 5,
        borderWidth: 2,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: true,
      plugins: { legend: { display: false } },
      scales: {
        x: {
          ticks: { color: "#64748b", maxTicksLimit: 7, font: { size: 11 } },
          grid: { color: "#1c2538" },
        },
        y: {
          ticks: { color: "#64748b", font: { size: 11 } },
          grid: { color: "#1c2538" },
          beginAtZero: true,
        },
      },
    },
  });
  chartInstances.push(chart);
}

/* ---------- Dashboard ---------- */

async function renderDashboard() {

  const data = await api("/admin/dashboard?days=30");
  const s = data.stats;

  // Update nav badges
  const bc = document.getElementById("badgeClaims");
  const bw = document.getElementById("badgeWd");
  if (s.pending_claims > 0) {
    bc.textContent = s.pending_claims;
    bc.classList.remove("hidden");
  } else bc.classList.add("hidden");
  if (s.pending_withdrawals > 0) {
    bw.textContent = s.pending_withdrawals;
    bw.classList.remove("hidden");
  } else bw.classList.add("hidden");

  document.getElementById("content").innerHTML = `
    <div class="cards anim-fade">
      <div class="stat-card">
        <div class="stat-icon">👥</div>
        <div class="stat-label">Total Users</div>
        <div class="stat-value">${s.total_users.toLocaleString()}</div>
      </div>
      <div class="stat-card">
        <div class="stat-icon">🎟️</div>
        <div class="stat-label">Total QR Codes</div>
        <div class="stat-value">${s.total_qr_codes.toLocaleString()}</div>
      </div>
      <div class="stat-card">
        <div class="stat-icon">🟢</div>
        <div class="stat-label">Available QR</div>
        <div class="stat-value">${s.available_qr.toLocaleString()}</div>
      </div>
      <div class="stat-card">
        <div class="stat-icon">⏳</div>
        <div class="stat-label">Pending Claims</div>
        <div class="stat-value">${s.pending_claims.toLocaleString()}</div>
      </div>
      <div class="stat-card">
        <div class="stat-icon">✅</div>
        <div class="stat-label">Approved</div>
        <div class="stat-value">${s.approved_claims.toLocaleString()}</div>
      </div>
      <div class="stat-card">
        <div class="stat-icon">❌</div>
        <div class="stat-label">Rejected</div>
        <div class="stat-value">${s.rejected_claims.toLocaleString()}</div>
      </div>
      <div class="stat-card">
        <div class="stat-icon">💰</div>
        <div class="stat-label">Total Rewards</div>
        <div class="stat-value">${cents(s.total_rewards_cents)}</div>
      </div>
      <div class="stat-card">
        <div class="stat-icon">💸</div>
        <div class="stat-label">Pending Withdrawals</div>
        <div class="stat-value">${s.pending_withdrawals.toLocaleString()}</div>
      </div>
      <div class="stat-card">
        <div class="stat-icon">💵</div>
        <div class="stat-label">Total Paid Out</div>
        <div class="stat-value">${cents(s.total_paid_cents)}</div>
      </div>
    </div>
    <div class="charts">
      <div class="chart-card"><h4>Claims per day</h4><canvas id="chartClaims" height="180"></canvas></div>
      <div class="chart-card"><h4>Rewards per day (cents)</h4><canvas id="chartRewards" height="180"></canvas></div>
      <div class="chart-card"><h4>New users per day</h4><canvas id="chartUsers" height="180"></canvas></div>
      <div class="chart-card"><h4>Withdrawals per day</h4><canvas id="chartWd" height="180"></canvas></div>
    </div>
  `;
  const c = data.charts;
  makeChart("chartClaims", c.claims_per_day, "count", "#3b82f6");
  makeChart("chartRewards", c.rewards_per_day, "cents", "#22c55e");
  makeChart("chartUsers", c.new_users, "count", "#f59e0b");
  makeChart("chartWd", c.withdrawals, "count", "#ef4444");
}

/* ---------- Users ---------- */

async function renderUsers(q = "") {
  document.getElementById("topbarActions").innerHTML = `
    <input type="search" id="userSearch" placeholder="Search ID / username…" value="${escapeHtml(q)}" style="width:210px" />
  `;
  document.getElementById("userSearch").onkeydown = (e) => {
    if (e.key === "Enter") { offset = 0; renderUsers(e.target.value); }
  };

  const data = await api(`/admin/users?offset=${offset}&limit=${LIMIT}&q=${encodeURIComponent(q)}`);
  const rows = data.items.map((u) => `
    <tr>
      <td>${u.id}</td>
      <td><code>${u.telegram_id}</code></td>
      <td>@${escapeHtml(u.username || "—")}</td>
      <td>${escapeHtml((u.first_name || "") + " " + (u.last_name || "")).trim() || "—"}</td>
      <td>${badge(u.status)}</td>
      <td><strong>${cents(u.balance_cents)}</strong></td>
      <td>${cents(u.total_earned_cents)}</td>
      <td>
        <div class="btn-group">
          <button class="btn btn-sm btn-outline" onclick="viewUser(${u.id})">View</button>
          <button class="btn btn-sm btn-primary" onclick="showSendDM(${u.telegram_id}, '${escapeHtml(u.username || "")}')">DM</button>
          ${u.status === "ACTIVE"
            ? `<button class="btn btn-sm btn-danger" onclick="toggleUser(${u.id},'disable')">Disable</button>`
            : `<button class="btn btn-sm btn-success" onclick="toggleUser(${u.id},'enable')">Enable</button>`}
        </div>
      </td>
    </tr>
  `).join("");

  document.getElementById("content").innerHTML = `
    <div class="toolbar">
      <button class="btn btn-primary btn-sm" onclick="showSendDM()">Send DM to user</button>
      <div class="spacer"></div>
      <span class="muted" style="font-size:0.85rem">${data.total} total</span>
    </div>
    <div class="table-wrap"><table>
      <thead><tr>
        <th>ID</th><th>Telegram</th><th>Username</th><th>Name</th>
        <th>Status</th><th>Balance</th><th>Earned</th><th>Actions</th>
      </tr></thead>
      <tbody>${rows || `<tr><td colspan="8">${emptyState("👥", "No users found")}</td></tr>`}</tbody>
    </table></div>
    ${pagination(data.total)}
  `;
}

function showSendDM(telegramId = "", username = "") {
  openModal("Send DM to user", `
    <div class="form-group">
      <label>Telegram ID *</label>
      <input type="number" id="dmTgId" value="${telegramId || ""}" placeholder="e.g. 123456789" />
    </div>
    <div class="form-group">
      <label>Message (HTML: &lt;b&gt;, &lt;code&gt;)</label>
      <textarea id="dmMessage" rows="5" placeholder="Your message to the user…"></textarea>
    </div>
    <div class="form-group">
      <label>Photo (optional)</label>
      <input type="file" id="dmPhoto" accept="image/*" />
    </div>
    ${username ? `<p class="muted">User: @${escapeHtml(username)}</p>` : ""}
  `, `
    <button class="btn btn-outline" onclick="closeModal()">Cancel</button>
    <button class="btn btn-primary" onclick="doSendDM()">Send DM</button>
  `);
}

async function doSendDM() {
  const telegram_id = parseInt(document.getElementById("dmTgId").value, 10);
  const message = document.getElementById("dmMessage").value.trim();
  const fileInput = document.getElementById("dmPhoto");
  const file = fileInput && fileInput.files && fileInput.files[0];
  if (!telegram_id) {
    toast("Telegram ID required", "error");
    return;
  }
  if (!message && !file) {
    toast("Message or photo required", "error");
    return;
  }
  try {
    if (file) {
      const fd = new FormData();
      fd.append("telegram_id", String(telegram_id));
      fd.append("message", message || " ");
      fd.append("photo", file);
      await api("/admin/dm", { method: "POST", body: fd });
    } else {
      await api("/admin/dm", { method: "POST", body: { telegram_id, message } });
    }
    toast(file ? "DM sent with photo" : "DM sent");
    closeModal();
  } catch (e) { toast(e.message, "error"); }
}

async function viewUser(id) {
  const u = await api(`/admin/users/${id}`);
  openModal(`User #${u.id}`, `
    <div class="detail-grid">
      <div class="detail-item"><label>Telegram ID</label><span><code>${u.telegram_id}</code></span></div>
      <div class="detail-item"><label>Username</label><span>@${escapeHtml(u.username || "—")}</span></div>
      <div class="detail-item"><label>Name</label><span>${escapeHtml((u.first_name||"")+" "+(u.last_name||"")).trim() || "—"}</span></div>
      <div class="detail-item"><label>Status</label><span>${badge(u.status)}</span></div>
      <div class="detail-item"><label>Balance</label><span><strong>${cents(u.balance_cents)}</strong></span></div>
      <div class="detail-item"><label>Total Earned</label><span>${cents(u.total_earned_cents)}</span></div>
    </div>
    <hr class="divider" />
    <div class="form-group">
      <label>Adjust Balance (USD, can be negative)</label>
      <input type="number" id="adjAmount" step="0.01" placeholder="e.g. 1.50 or -0.50" />
    </div>
    <div class="form-group">
      <label>Reason (required)</label>
      <input type="text" id="adjReason" placeholder="Reason for adjustment" />
    </div>
  `, `
    <button class="btn btn-outline" onclick="closeModal()">Close</button>
    <button class="btn btn-primary" onclick="showSendDM(${u.telegram_id}, '${escapeHtml(u.username || "")}')">Send DM</button>
    <button class="btn btn-primary" onclick="doAdjust(${id})">Adjust Balance</button>
  `);
}

async function doAdjust(id) {
  const amount = parseFloat(document.getElementById("adjAmount").value);
  const reason = document.getElementById("adjReason").value.trim();
  if (isNaN(amount) || !reason) { toast("Amount and reason required", "error"); return; }
  try {
    await api(`/admin/users/${id}/adjust-balance`, { method: "POST", body: { amount_usd: amount, reason } });
    toast("Balance adjusted");
    closeModal();
    renderUsers();
  } catch (e) { toast(e.message, "error"); }
}

async function toggleUser(id, action) {
  if (!confirm(`Are you sure you want to ${action} this user?`)) return;
  try {
    await api(`/admin/users/${id}/${action}`, { method: "POST" });
    toast(`User ${action}d`);
    renderUsers();
  } catch (e) { toast(e.message, "error"); }
}

/* ---------- QR Codes ---------- */

async function renderQRs(status = "") {
  document.getElementById("topbarActions").innerHTML = `
    <button class="btn btn-outline btn-sm" onclick="exportQRs()">⬇ Export CSV</button>
    <button class="btn btn-primary btn-sm" onclick="showCreateQR()">+ Create QR</button>
  `;
  const data = await api(`/admin/qrs?offset=${offset}&limit=${LIMIT}${status ? "&status=" + status : ""}`);

  const rows = data.items.map((q) => `
    <tr>
      <td><code class="copyable" onclick="copyText('${escapeHtml(q.code)}')" title="Click to copy">${escapeHtml(q.code)}</code> <button class="btn btn-sm btn-outline" onclick="window.open('/api/admin/qrs/${q.id}/image','_blank')" title="Download QR image">🖼</button></td>
      <td><strong>${cents(q.reward_cents)}</strong></td>
      <td>${badge(q.status)}</td>
      <td>${escapeHtml(q.app_name || "—")}</td>
      <td>${escapeHtml(q.campaign_name || "—")}</td>
      <td>${escapeHtml(q.created_by_username || "—")}</td>
      <td>${fmtDate(q.created_at)}</td>
      <td>${fmtDate(q.expires_at)}</td>
      <td>${q.claimed_by_user_id
        ? tgUserLink(q.claimed_by_username, q.claimed_by_telegram_id, q.claimed_by_first_name, q.claimed_by_last_name)
        : "—"}</td>
      <td>
        <div class="btn-group">
          <button class="btn btn-sm btn-outline" onclick="uploadQRImage(${q.id})" title="Upload your QR image">${q.has_image ? "Change Img" : "Upload"}</button>
          <button class="btn btn-sm btn-outline" onclick="cloneQR(${q.id})">Clone</button>
          ${["AVAILABLE","RECLAIMED","POSTED"].includes(q.status)
            ? `<button class="btn btn-sm btn-primary" onclick="postQR(${q.id})" title="DM all users">Notify All</button>
               <button class="btn btn-sm btn-outline" onclick="notifyOneUser(${q.id})" title="DM one user">Notify One</button>` : ""}
          ${!["DISABLED","EXPIRED"].includes(q.status)
            ? `<button class="btn btn-sm btn-warning" onclick="reclaimQR(${q.id})">Reclaim</button>` : ""}
          ${q.status !== "DISABLED"
            ? `<button class="btn btn-sm btn-danger" onclick="disableQR(${q.id})">Disable</button>` : ""}
        </div>
      </td>
    </tr>
  `).join("");

  document.getElementById("content").innerHTML = `
    <div class="toolbar">
      <select id="qrFilter" onchange="offset=0;renderQRs(this.value)">
        <option value="">All statuses</option>
        ${["AVAILABLE","POSTED","PENDING","APPROVED","CLAIMED","REJECTED","EXPIRED","RECLAIMED","DISABLED"]
          .map((s) => `<option value="${s}" ${s===status?"selected":""}>${s}</option>`).join("")}
      </select>
      <div class="spacer"></div>
      <span style="color:var(--muted);font-size:0.85rem">${data.total} total · live</span>
    </div>
    <div class="table-wrap"><table>
      <thead><tr>
        <th>Code</th><th>Reward</th><th>Status</th><th>App</th><th>Campaign</th>
        <th>Created By</th><th>Created</th><th>Expires</th><th>Claimed By</th><th>Actions</th>
      </tr></thead>
      <tbody>${rows || `<tr><td colspan="10">${emptyState("QR", "No QR codes yet")}</td></tr>`}</tbody>
    </table></div>
    ${pagination(data.total)}
  `;
}

function showCreateQR() {
  openModal("Create QR Codes", `
    <div class="form-row">
      <div class="form-group"><label>Reward amount (USD) *</label><input type="number" id="qrReward" step="0.01" min="0.01" value="0.50" required /></div>
      <div class="form-group"><label>Quantity *</label><input type="number" id="qrQty" min="1" max="500" value="1" required /></div>
    </div>
    <div class="form-row">
      <div class="form-group"><label>QR app name *</label><input type="text" id="qrApp" placeholder="Which app this QR belongs to (e.g. PhonePe)" required /></div>
      <div class="form-group"><label>Expiry (minutes)</label><input type="number" id="qrExpiryMin" min="1" placeholder="e.g. 60, 1440 (optional)" /></div>
    </div>
    <div class="form-row">
      <div class="form-group"><label>Campaign name</label><input type="text" id="qrCampaign" placeholder="Optional" /></div>
      <div class="form-group"><label>Description</label><input type="text" id="qrDesc" placeholder="Optional notes" /></div>
    </div>
    <p class="muted" style="font-size:0.85rem">QR app name = which app this QR belongs to. Shown to users as a label only (not scan instructions).</p>
  `, `
    <button class="btn btn-outline" onclick="closeModal()">Cancel</button>
    <button class="btn btn-primary" onclick="doCreateQR()">Create</button>
  `);
}

async function doCreateQR() {
  const reward = parseFloat(document.getElementById("qrReward").value);
  const qty = parseInt(document.getElementById("qrQty").value, 10);
  const appName = (document.getElementById("qrApp").value || "").trim();
  if (!reward || reward <= 0) { toast("Enter a valid reward amount", "error"); return; }
  if (!qty || qty < 1) { toast("Enter quantity", "error"); return; }
  if (!appName) { toast("Enter the app name for this QR", "error"); return; }
  const body = {
    reward_usd: reward,
    quantity: qty,
    expiry_minutes: parseInt(document.getElementById("qrExpiryMin").value, 10) || null,
    campaign_name: document.getElementById("qrCampaign").value || null,
    app_name: appName,
    description: document.getElementById("qrDesc").value || null,
  };
  try {
    const res = await api("/admin/qrs", { method: "POST", body });
    toast(`Created ${res.created} QR code(s)`);
    closeModal();
    renderQRs();
  } catch (e) { toast(e.message, "error"); }
}

async function postQR(id) {
  if (!confirm("Send this QR reward to ALL active users via private DM?\n\nChannel/group will NOT receive the message.")) return;
  try {
    const res = await api(`/admin/qrs/${id}/post`, { method: "POST", body: {} });
    toast(res.message || "Users notified via DM");
    renderQRs();
  } catch (e) { toast(e.message, "error"); }
}

function notifyOneUser(qrId) {
  openModal("Notify one user", `
    <p class="muted">Send this QR claim button to a single user via private DM.</p>
    <div class="form-group">
      <label>Telegram ID *</label>
      <input type="number" id="notifyTgId" placeholder="e.g. 123456789" />
    </div>
  `, `
    <button class="btn btn-outline" onclick="closeModal()">Cancel</button>
    <button class="btn btn-primary" onclick="doNotifyOneUser(${qrId})">Send</button>
  `);
}

async function doNotifyOneUser(qrId) {
  const telegram_id = parseInt(document.getElementById("notifyTgId").value, 10);
  if (!telegram_id) { toast("Telegram ID required", "error"); return; }
  try {
    const res = await api(`/admin/qrs/${qrId}/notify-user`, {
      method: "POST",
      body: { telegram_id },
    });
    toast(res.message || "DM sent");
    closeModal();
    renderQRs();
  } catch (e) { toast(e.message, "error"); }
}

async function reclaimQR(id) {
  if (!confirm("Reclaim this QR? Any pending claim will be cancelled.")) return;
  try {
    await api(`/admin/qrs/${id}/reclaim`, { method: "POST" });
    toast("QR reclaimed");
    renderQRs();
  } catch (e) { toast(e.message, "error"); }
}

async function disableQR(id) {
  if (!confirm("Disable this QR permanently?")) return;
  try {
    await api(`/admin/qrs/${id}/disable`, { method: "POST" });
    toast("QR disabled");
    renderQRs();
  } catch (e) { toast(e.message, "error"); }
}

/* ---------- Claims ---------- */

async function renderClaims(status = "PENDING") {
  document.getElementById("topbarActions").innerHTML = `
    <button class="btn btn-outline btn-sm" onclick="exportClaims()">⬇ Export CSV</button>
  `;
  const data = await api(`/admin/claims?offset=${offset}&limit=${LIMIT}${status ? "&status=" + status : ""}`);
  const rows = data.items.map((c) => `
    <tr>
      <td>${c.id}</td>
      <td><code>${escapeHtml(c.qr_code || "—")}</code></td>
      <td>${tgUserLink(c.username, c.telegram_id, c.first_name, c.last_name)}</td>
      <td><strong>${cents(c.reward_cents)}</strong></td>
      <td>${badge(c.status)}</td>
      <td>${fmtDate(c.created_at)}</td>
      <td>
        <div class="btn-group">
          ${c.status === "PENDING" || c.status === "HELD" ? `
            <button class="btn btn-sm btn-success" onclick="approveClaim(${c.id})">Approve</button>
            <button class="btn btn-sm btn-danger" onclick="rejectClaim(${c.id})">Reject</button>
            <button class="btn btn-sm btn-warning" onclick="holdClaim(${c.id})">Hold</button>
          ` : "—"}
        </div>
      </td>
    </tr>
  `).join("");

  document.getElementById("content").innerHTML = `
    <div class="toolbar">
      <select onchange="offset=0;renderClaims(this.value)">
        ${["","PENDING","APPROVED","REJECTED","HELD"]
          .map((s) => `<option value="${s}" ${s===status?"selected":""}>${s || "All"}</option>`).join("")}
      </select>
      <div class="spacer"></div>
      <span style="color:var(--muted);font-size:0.85rem">${data.total} total</span>
    </div>
    <div class="table-wrap"><table>
      <thead><tr>
        <th>ID</th><th>QR</th><th>User</th><th>Reward</th><th>Status</th><th>Created</th><th>Actions</th>
      </tr></thead>
      <tbody>${rows || `<tr><td colspan="7">${emptyState("📌", "No claims found")}</td></tr>`}</tbody>
    </table></div>
    ${pagination(data.total)}
  `;
}

async function approveClaim(id) {
  if (!confirm("Approve this claim and credit the user's wallet?")) return;
  try {
    await api(`/admin/claims/${id}/approve`, { method: "POST" });
    toast("Claim approved – wallet credited");
    renderClaims();
  } catch (e) { toast(e.message, "error"); }
}

function rejectClaim(id) {
  openModal("Reject Claim", `
    <div class="form-group">
      <label>Reason (required)</label>
      <textarea id="rejectReason" placeholder="e.g. Invalid submission, spam, etc."></textarea>
    </div>
    <div class="form-group">
      <label>Photo (optional)</label>
      <input type="file" id="rejectPhoto" accept="image/png,image/jpeg,image/webp,image/gif" />
      <p class="muted" style="font-size:0.8rem;margin:0.35rem 0 0">Sent to the user with the rejection message.</p>
    </div>
  `, `
    <button class="btn btn-outline" onclick="closeModal()">Cancel</button>
    <button class="btn btn-danger" onclick="doRejectClaim(${id})">Reject</button>
  `);
}

async function doRejectClaim(id) {
  const reason = document.getElementById("rejectReason").value.trim();
  if (!reason) { toast("Reason required", "error"); return; }
  const fileInput = document.getElementById("rejectPhoto");
  const file = fileInput && fileInput.files && fileInput.files[0];
  try {
    if (file) {
      const fd = new FormData();
      fd.append("reason", reason);
      fd.append("photo", file);
      await api(`/admin/claims/${id}/reject`, { method: "POST", body: fd });
    } else {
      await api(`/admin/claims/${id}/reject`, { method: "POST", body: { reason } });
    }
    toast(file ? "Claim rejected (photo sent)" : "Claim rejected");
    closeModal();
    renderClaims();
  } catch (e) { toast(e.message, "error"); }
}

async function holdClaim(id) {
  try {
    await api(`/admin/claims/${id}/hold`, { method: "POST" });
    toast("Claim held");
    renderClaims();
  } catch (e) { toast(e.message, "error"); }
}

/* ---------- Withdrawals ---------- */

async function renderWithdrawals(status = "PENDING") {
  document.getElementById("topbarActions").innerHTML = `
    <button class="btn btn-outline btn-sm" onclick="exportWithdrawals()">⬇ Export CSV</button>
  `;

  const data = await api(`/admin/withdrawals?offset=${offset}&limit=${LIMIT}${status ? "&status=" + status : ""}`);
  const rows = data.items.map((w) => `
    <tr>
      <td>${w.id}</td>
      <td>${tgUserLink(w.username, w.telegram_id, w.first_name, w.last_name)}</td>
      <td><strong>${cents(w.amount_cents)}</strong></td>
      <td>${escapeHtml(w.payout_method || "—")}<div class="muted-xs">${escapeHtml(w.payout_details || "")}</div></td>
      <td>${badge(w.status)}</td>
      <td>${fmtDate(w.created_at)}</td>
      <td>
        <div class="btn-group">
          ${w.status === "PENDING" || w.status === "HELD" ? `
            <button class="btn btn-sm btn-success" onclick="payWd(${w.id})">Mark Paid</button>
            <button class="btn btn-sm btn-danger" onclick="rejectWd(${w.id})">Reject</button>
            <button class="btn btn-sm btn-warning" onclick="holdWd(${w.id})">Hold</button>
          ` : "—"}
        </div>
      </td>
    </tr>
  `).join("");

  document.getElementById("content").innerHTML = `
    <div class="toolbar">
      <select onchange="offset=0;renderWithdrawals(this.value)">
        ${["","PENDING","PAID","REJECTED","HELD"]
          .map((s) => `<option value="${s}" ${s===status?"selected":""}>${s || "All"}</option>`).join("")}
      </select>
      <div class="spacer"></div>
      <span style="color:var(--muted);font-size:0.85rem">${data.total} total</span>
    </div>
    <div class="table-wrap"><table>
      <thead><tr>
        <th>ID</th><th>User</th><th>Amount</th><th>Payout</th><th>Status</th><th>Created</th><th>Actions</th>
      </tr></thead>
      <tbody>${rows || `<tr><td colspan="7">${emptyState("💸", "No withdrawals")}</td></tr>`}</tbody>
    </table></div>
    ${pagination(data.total)}
  `;
}

async function payWd(id) {
  const ref = prompt("Payment reference / transaction ID (optional).\nConfirm you paid the user via their UPI or Binance ID:");
  if (ref === null) return;
  try {
    await api(`/admin/withdrawals/${id}/pay`, { method: "POST", body: { payment_reference: ref || null } });
    toast("Marked as paid — user will be notified");
    renderWithdrawals();
  } catch (e) { toast(e.message, "error"); }
}

function rejectWd(id) {
  openModal("Reject Withdrawal", `
    <p style="color:var(--text-secondary);font-size:0.875rem;margin-bottom:1rem">
      The reserved amount will be refunded to the user's balance.
    </p>
    <div class="form-group">
      <label>Reason (required)</label>
      <textarea id="wdRejectReason" placeholder="Reason for rejection"></textarea>
    </div>
  `, `
    <button class="btn btn-outline" onclick="closeModal()">Cancel</button>
    <button class="btn btn-danger" onclick="doRejectWd(${id})">Reject &amp; Refund</button>
  `);
}

async function doRejectWd(id) {
  const reason = document.getElementById("wdRejectReason").value.trim();
  if (!reason) { toast("Reason required", "error"); return; }
  try {
    await api(`/admin/withdrawals/${id}/reject`, { method: "POST", body: { reason } });
    toast("Withdrawal rejected & refunded");
    closeModal();
    renderWithdrawals();
  } catch (e) { toast(e.message, "error"); }
}

async function holdWd(id) {
  try {
    await api(`/admin/withdrawals/${id}/hold`, { method: "POST" });
    toast("Withdrawal held");
    renderWithdrawals();
  } catch (e) { toast(e.message, "error"); }
}

/* ---------- Channels ---------- */

async function renderChannels() {
  document.getElementById("topbarActions").innerHTML = `
    <button class="btn btn-primary btn-sm" onclick="showAddChannel()">+ Add Channel</button>
  `;
  const data = await api("/admin/channels");
  const rows = data.items.map((c) => `
    <tr>
      <td>${c.id}</td>
      <td><code>${c.channel_id}</code></td>
      <td>${escapeHtml(c.title)}</td>
      <td>@${escapeHtml(c.username || "—")}</td>
      <td>${c.is_active ? badge("ACTIVE") : badge("DISABLED")}</td>
      <td>${c.is_required ? "Yes" : "No"}</td>
      <td>
        <div class="btn-group">
          <button class="btn btn-sm btn-outline" onclick='editChannel(${JSON.stringify(c).replace(/'/g, "&#39;")})'>Edit</button>
          <button class="btn btn-sm btn-danger" onclick="deleteChannel(${c.id})">Delete</button>
        </div>
      </td>
    </tr>
  `).join("");

  document.getElementById("content").innerHTML = `
    <p class="muted" style="margin-bottom:0.75rem;font-size:0.85rem">
      Bot must be admin in the channel. Channel ID is negative for supergroups (e.g. -1001234567890).
    </p>
    <div class="table-wrap"><table>
      <thead><tr>
        <th>ID</th><th>Channel ID</th><th>Title</th><th>Username</th>
        <th>Active</th><th>Required</th><th>Actions</th>
      </tr></thead>
      <tbody>${rows || `<tr><td colspan="7">${emptyState("Channels", "No force-join channels")}</td></tr>`}</tbody>
    </table></div>
  `;
}

async function deleteChannel(id) {
  if (!confirm("Remove this force-join channel?")) return;
  try {
    await api(`/admin/channels/${id}`, { method: "DELETE" });
    toast("Channel removed");
    renderChannels();
  } catch (e) { toast(e.message, "error"); }
}

function showAddChannel() {
  openModal("Add Force-Join Channel", `
    <div class="form-group"><label>Channel ID (numeric, e.g. -100123…)</label><input type="number" id="chId" /></div>
    <div class="form-group"><label>Title</label><input type="text" id="chTitle" /></div>
    <div class="form-row">
      <div class="form-group"><label>Username (without @)</label><input type="text" id="chUser" /></div>
      <div class="form-group"><label>Invite URL</label><input type="url" id="chUrl" placeholder="https://t.me/…" /></div>
    </div>
    <div class="form-group">
      <label class="form-check"><input type="checkbox" id="chReq" checked /> Required</label>
    </div>
    <div class="form-group">
      <label class="form-check"><input type="checkbox" id="chActive" checked /> Active</label>
    </div>
  `, `
    <button class="btn btn-outline" onclick="closeModal()">Cancel</button>
    <button class="btn btn-primary" onclick="doAddChannel()">Add Channel</button>
  `);
}

async function doAddChannel() {
  try {
    await api("/admin/channels", {
      method: "POST",
      body: {
        channel_id: parseInt(document.getElementById("chId").value, 10),
        title: document.getElementById("chTitle").value,
        username: document.getElementById("chUser").value || null,
        invite_url: document.getElementById("chUrl").value || null,
        is_required: document.getElementById("chReq").checked,
        is_active: document.getElementById("chActive").checked,
      },
    });
    toast("Channel added");
    closeModal();
    renderChannels();
  } catch (e) { toast(e.message, "error"); }
}

function editChannel(c) {
  openModal("Edit Channel", `
    <div class="form-group"><label>Title</label><input type="text" id="chTitle" value="${escapeHtml(c.title)}" /></div>
    <div class="form-row">
      <div class="form-group"><label>Username</label><input type="text" id="chUser" value="${escapeHtml(c.username || "")}" /></div>
      <div class="form-group"><label>Invite URL</label><input type="url" id="chUrl" value="${escapeHtml(c.invite_url || "")}" /></div>
    </div>
    <div class="form-group">
      <label class="form-check"><input type="checkbox" id="chReq" ${c.is_required?"checked":""} /> Required</label>
    </div>
    <div class="form-group">
      <label class="form-check"><input type="checkbox" id="chActive" ${c.is_active?"checked":""} /> Active</label>
    </div>
  `, `
    <button class="btn btn-outline" onclick="closeModal()">Cancel</button>
    <button class="btn btn-primary" onclick="doEditChannel(${c.id})">Save Changes</button>
  `);
}

async function doEditChannel(id) {
  try {
    await api(`/admin/channels/${id}`, {
      method: "PATCH",
      body: {
        title: document.getElementById("chTitle").value,
        username: document.getElementById("chUser").value || null,
        invite_url: document.getElementById("chUrl").value || null,
        is_required: document.getElementById("chReq").checked,
        is_active: document.getElementById("chActive").checked,
      },
    });
    toast("Channel updated");
    closeModal();
    renderChannels();
  } catch (e) { toast(e.message, "error"); }
}

/* ---------- Settings ---------- */

async function renderSettings() {
  const s = await api("/admin/settings");
  document.getElementById("content").innerHTML = `
    <form id="settingsForm">
      <div class="settings-grid">
        <div class="form-group">
          <label>Default Reward (USD)</label>
          <input type="number" step="0.01" id="sReward" value="${(s.default_reward_cents/100).toFixed(2)}" />
        </div>
        <div class="form-group">
          <label>QR Default Expiry (hours)</label>
          <input type="number" id="sExpiry" value="${s.qr_default_expiry_hours}" />
        </div>
        <div class="form-group">
          <label>Min Withdrawal (USD)</label>
          <input type="number" step="0.01" id="sMin" value="${(s.min_withdrawal_cents/100).toFixed(2)}" />
        </div>
        <div class="form-group">
          <label>Max Withdrawal (USD)</label>
          <input type="number" step="0.01" id="sMax" value="${(s.max_withdrawal_cents/100).toFixed(2)}" />
        </div>
        <div class="form-group">
          <label class="form-check"><input type="checkbox" id="sForce" ${s.force_join_enabled?"checked":""} /> Force Join Enabled</label>
        </div>
        <div class="form-group">
          <label class="form-check"><input type="checkbox" id="sApproval" ${s.claim_approval_required?"checked":""} /> Claim Approval Required</label>
        </div>
        <div class="form-group">
          <label class="form-check"><input type="checkbox" id="sMaint" ${s.maintenance_mode?"checked":""} /> Maintenance Mode</label>
        </div>
        <div class="form-group">
          <label class="form-check"><input type="checkbox" id="sAutoApprove" ${s.auto_approve_enabled?"checked":""} /> Auto-approve claims</label>
        </div>
        <div class="form-group">
          <label>Auto-approve max (USD)</label>
          <input type="number" step="0.01" id="sAutoMax" value="${Number(s.auto_approve_max_usd||0).toFixed(2)}" />
        </div>
        <div class="form-group">
          <label class="form-check"><input type="checkbox" id="sClaims" ${s.claims_enabled!==false?"checked":""} /> Claims enabled</label>
        </div>
        <div class="form-group">
          <label class="form-check"><input type="checkbox" id="sWdEnable" ${s.withdrawals_enabled!==false?"checked":""} /> Withdrawals enabled</label>
        </div>
        <div class="form-group">
          <label class="form-check"><input type="checkbox" id="sDmNotify" ${s.dm_notify_on_post!==false?"checked":""} /> DM all users on Notify</label>
        </div>
        <div class="form-group full">
          <label>Bot Welcome Message</label>
          <textarea id="sWelcome" rows="4">${escapeHtml(s.bot_welcome_message || "")}</textarea>
        </div>
        <div class="full">
          <button type="submit" class="btn btn-primary">Save Settings</button>
        </div>
      </div>
    </form>
  `;
  document.getElementById("settingsForm").onsubmit = async (e) => {
    e.preventDefault();
    try {
      await api("/admin/settings", {
        method: "PUT",
        body: {
          default_reward_usd: parseFloat(document.getElementById("sReward").value),
          min_withdrawal_usd: parseFloat(document.getElementById("sMin").value),
          max_withdrawal_usd: parseFloat(document.getElementById("sMax").value),
          qr_default_expiry_hours: parseInt(document.getElementById("sExpiry").value, 10),
          force_join_enabled: document.getElementById("sForce").checked,
          claim_approval_required: document.getElementById("sApproval").checked,
          maintenance_mode: document.getElementById("sMaint").checked,
          auto_approve_enabled: document.getElementById("sAutoApprove").checked,
          auto_approve_max_usd: parseFloat(document.getElementById("sAutoMax").value),
          claims_enabled: document.getElementById("sClaims").checked,
          withdrawals_enabled: document.getElementById("sWdEnable").checked,
          dm_notify_on_post: document.getElementById("sDmNotify").checked,
          bot_welcome_message: document.getElementById("sWelcome").value,
        },
      });
      toast("Settings saved");
    } catch (err) { toast(err.message, "error"); }
  };

  // Change password card
  const wrap = document.getElementById("content") || document.getElementById("content");
  if (wrap) {
    const card = document.createElement("div");
    card.className = "card";
    card.style.cssText = "margin-top:1.5rem;max-width:480px";
    card.innerHTML = `
      <h3 style="margin-top:0">🔒 Change Password</h3>
      <div class="form-group"><label>Current password</label><input type="password" id="curPass" autocomplete="current-password"></div>
      <div class="form-group"><label>New password (min 8)</label><input type="password" id="newPass" minlength="8" autocomplete="new-password"></div>
      <button type="button" class="btn btn-outline" onclick="doChangePassword()">Update Password</button>
    `;
    wrap.appendChild(card);
  }
}

/* ---------- Audit ---------- */

async function renderAudit() {
  const data = await api(`/admin/audit-logs?offset=${offset}&limit=${LIMIT}`);
  const rows = data.items.map((a) => `
    <tr>
      <td>${a.id}</td>
      <td><code style="font-size:0.75rem">${escapeHtml(a.action)}</code></td>
      <td>${a.actor_admin_id || a.actor_user_id || "—"}</td>
      <td>${escapeHtml(a.target_type || "—")} ${a.target_id ? "#" + a.target_id : ""}</td>
      <td>${escapeHtml(a.ip_address || "—")}</td>
      <td>${fmtDate(a.created_at)}</td>
    </tr>
  `).join("");

  document.getElementById("content").innerHTML = `
    <div class="table-wrap"><table>
      <thead><tr>
        <th>ID</th><th>Action</th><th>Actor</th><th>Target</th><th>IP</th><th>Time</th>
      </tr></thead>
      <tbody>${rows || `<tr><td colspan="6">${emptyState("📋", "No audit logs")}</td></tr>`}</tbody>
    </table></div>
    ${pagination(data.total)}
  `;
}

/* ---------- Reset ---------- */

async function renderReset() {
  if (adminRole !== "OWNER") {
    document.getElementById("content").innerHTML =
      `<div class="error-banner">Only the OWNER role can perform destructive resets.</div>`;
    return;
  }
  document.getElementById("content").innerHTML = `
    <div class="danger-zone">
      <h3>⚠ Reset All Activities</h3>
      <p>
        Deletes all claims, transactions, withdrawals and notifications.
        Resets user balances and QR claim states. This cannot be undone.
        Type exactly: <code>RESET ALL ACTIVITIES</code>
      </p>
      <div class="form-group"><input type="text" id="resetActText" placeholder="RESET ALL ACTIVITIES" /></div>
      <button class="btn btn-danger" onclick="doReset('activity')">Execute Reset</button>
    </div>
    <div class="danger-zone">
      <h3>⚠ Reset All QR Data</h3>
      <p>
        Permanently deletes all QR codes and related claims.
        Type exactly: <code>RESET ALL QR DATA</code>
      </p>
      <div class="form-group"><input type="text" id="resetQrText" placeholder="RESET ALL QR DATA" /></div>
      <button class="btn btn-danger" onclick="doReset('qr')">Execute Reset</button>
    </div>
  `;
}

async function doReset(type) {
  const text = type === "activity"
    ? document.getElementById("resetActText").value
    : document.getElementById("resetQrText").value;
  if (!confirm("This action is IRREVERSIBLE. Continue?")) return;
  try {
    const path = type === "activity" ? "/admin/reset/activity" : "/admin/reset/qr-data";
    const res = await api(path, { method: "POST", body: { confirmation_text: text } });
    toast(`Reset complete`);
    renderReset();
  } catch (e) { toast(e.message, "error"); }
}

/* ---------- Pagination ---------- */

function pagination(total) {
  const pages = Math.ceil(total / LIMIT) || 1;
  if (pages <= 1) return "";
  const cur = Math.floor(offset / LIMIT) + 1;
  return `
    <div class="pagination">
      <button class="btn btn-sm btn-outline" ${cur <= 1 ? "disabled" : ""}
        onclick="offset=Math.max(0,offset-${LIMIT});loadPage(currentPage)">← Prev</button>
      <span>Page ${cur} of ${pages} · ${total} total</span>
      <button class="btn btn-sm btn-outline" ${cur >= pages ? "disabled" : ""}
        onclick="offset+=${LIMIT};loadPage(currentPage)">Next →</button>
    </div>
  `;
}

/* ---------- Init ---------- */

(async function init() {
  try {
    const me = await api("/auth/me");
    adminRole = me.role;
    adminUsername = me.username;
    localStorage.setItem("admin_role", me.role);
    localStorage.setItem("admin_username", me.username);

    document.getElementById("adminName").textContent = me.username;
    document.getElementById("adminRole").textContent = me.role;
    document.getElementById("adminAvatar").textContent =
      (me.username || "A").charAt(0).toUpperCase();

    applyTheme(PREFS.theme);
    navigate("dashboard");

    const themeBtn = document.getElementById("themeToggle");
    const soundBtn = document.getElementById("soundToggle");
    if (themeBtn) {
      themeBtn.textContent = PREFS.theme === "dark" ? "Light" : "Dark";
      themeBtn.onclick = () => {
        const next = PREFS.theme === "dark" ? "light" : "dark";
        applyTheme(next);
        themeBtn.textContent = next === "dark" ? "Light" : "Dark";
        fxClick();
      };
    }
    if (soundBtn) {
      soundBtn.textContent = PREFS.sound ? "Sound on" : "Sound off";
      soundBtn.onclick = () => {
        PREFS.sound = !PREFS.sound;
        localStorage.setItem("admin_sound", PREFS.sound ? "1" : "0");
        soundBtn.textContent = PREFS.sound ? "Sound on" : "Sound off";
        fxClick();
      };
    }

    // Click feedback on primary actions
    document.addEventListener(
      "click",
      (e) => {
        const t = e.target.closest(".btn, .nav-item");
        if (t) fxClick();
      },
      true
    );

    // Pause auto-refresh while user is interacting
    let lastInteract = Date.now();
    const markInteract = () => { lastInteract = Date.now(); };
    document.addEventListener("click", markInteract, true);
    document.addEventListener("keydown", markInteract, true);
    document.addEventListener("scroll", markInteract, true);
    document.addEventListener("touchstart", markInteract, true);

    setInterval(() => {
      const modal = document.getElementById("modal");
      if (modal && !modal.classList.contains("hidden")) return;
      if (Date.now() - lastInteract < 8000) return;
      if (!currentPage) return;
      softRefresh();
    }, 15000);
  } catch {
    window.location.href = "/";
  }
})();

let _refreshing = false;
async function softRefresh() {
  if (_refreshing) return;
  // Only soft-refresh list pages (not forms)
  if (["broadcast", "settings", "reset", "admins"].includes(currentPage)) return;
  // Dashboard: only update badge numbers, full re-render is disruptive
  if (currentPage === "dashboard") {
    try {
      const data = await api("/admin/stats");
      const s = data.stats || data;
      const bc = document.getElementById("badgeClaims");
      const bw = document.getElementById("badgeWd");
      if (bc && s.pending_claims != null) {
        if (s.pending_claims > 0) {
          bc.textContent = s.pending_claims;
          bc.classList.remove("hidden");
        } else bc.classList.add("hidden");
      }
      if (bw && s.pending_withdrawals != null) {
        if (s.pending_withdrawals > 0) {
          bw.textContent = s.pending_withdrawals;
          bw.classList.remove("hidden");
        } else bw.classList.add("hidden");
      }
    } catch (_) {}
    return;
  }
  _refreshing = true;
  try {
    const handlers = {
      users: () => renderUsers(document.getElementById("userSearch")?.value || ""),
      qrs: () => renderQRs(document.getElementById("qrFilter")?.value || ""),
      claims: () => renderClaims(
        document.querySelector("#content select")?.value || "PENDING"
      ),
      withdrawals: renderWithdrawals,
      channels: renderChannels,
      campaigns: renderCampaigns,
      tickets: renderTickets,
      audit: renderAudit,
    };
    const fn = handlers[currentPage];
    if (fn) await fn();
  } catch (_) {
    /* ignore */
  } finally {
    _refreshing = false;
  }
}


/* ---------- Broadcast ---------- */

let _broadcastBusy = false;

async function renderBroadcast() {
  document.getElementById("topbarActions").innerHTML = "";
  document.getElementById("content").innerHTML = `
    <div class="card anim-fade" style="max-width:640px">
      <h3 style="margin-top:0">Broadcast Message</h3>
      <p class="muted">Text and optional photo to all users. Button locks until finished.</p>
      <div class="form-group">
        <label>Message (HTML: &lt;b&gt; &lt;i&gt; &lt;code&gt;)</label>
        <textarea id="broadcastMsg" rows="6" placeholder="Hello! New rewards are live..."></textarea>
      </div>
      <div class="form-group">
        <label>Photo (optional)</label>
        <input type="file" id="broadcastPhoto" accept="image/png,image/jpeg,image/webp,image/gif" />
      </div>
      <div class="form-group">
        <label>Video (optional)</label>
        <input type="file" id="broadcastVideo" accept="video/mp4,video/webm,video/quicktime" />
        <p class="muted" style="font-size:0.8rem;margin:0.35rem 0 0">Photo max 8MB · Video max 200MB. Video preferred if both set.</p>
      </div>
      <div class="form-group">
        <label>Buttons (optional)</label>
        <input type="text" id="broadcastButtons" placeholder="Open Bot|https://t.me/yourbot, Channel|https://t.me/channel" />
        <p class="muted" style="font-size:0.8rem;margin:0.35rem 0 0">Format: Label|url, Label2|url2 — URL buttons under the message.</p>
      </div>
      <label class="check-row"><input type="checkbox" id="broadcastActive" checked> Only active users</label>
      <div style="margin-top:1rem;display:flex;gap:0.5rem;flex-wrap:wrap">
        <button class="btn btn-primary" id="broadcastBtn" onclick="doBroadcast()">Send Broadcast</button>
        <button class="btn btn-danger" onclick="deleteLastBroadcast()">Delete last broadcast</button>
      </div>
      <p class="muted" style="font-size:0.8rem;margin-top:0.75rem">Delete works only for broadcasts sent after this update (message IDs are saved). Telegram may block deletes older than ~48 hours.</p>
    </div>
    <div class="card anim-fade" style="max-width:640px;margin-top:1rem">
      <h3 style="margin-top:0">Post to channel / group</h3>
      <p class="muted">Bot must be admin in that channel. Use chat id (-100…) or @username.</p>
      <div class="form-group">
        <label>Channel / group chat_id</label>
        <input type="text" id="broadcastChannelChat" placeholder="-100123… or @mychannel" />
      </div>
      <div class="form-group">
        <label>Message</label>
        <textarea id="broadcastChannelMsg" rows="3" placeholder="Message for the channel"></textarea>
      </div>
      <div class="form-group">
        <label>Photo (optional)</label>
        <input type="file" id="broadcastChannelPhoto" accept="image/*" />
      </div>
      <div class="form-group">
        <label>Video (optional)</label>
        <input type="file" id="broadcastChannelVideo" accept="video/*" />
      </div>
      <div class="form-group">
        <label>Buttons (optional)</label>
        <input type="text" id="broadcastChannelButtons" placeholder="Claim|https://t.me/bot, Channel|https://t.me/x" />
      </div>
      <button class="btn btn-success" onclick="doChannelBroadcast()">Post to channel</button>
    </div>
    <div class="card anim-fade" style="max-width:640px;margin-top:1rem" id="broadcastHistory">
      <h3 style="margin-top:0">Recent broadcasts</h3>
      <div id="broadcastHistoryBody" class="muted">Loading…</div>
    </div>
  `;
  loadBroadcastHistory();
}

async function loadBroadcastHistory() {
  const el = document.getElementById("broadcastHistoryBody");
  if (!el) return;
  try {
    const data = await api("/admin/broadcasts");
    if (!data.items || !data.items.length) {
      el.innerHTML = "No broadcasts yet.";
      return;
    }
    el.innerHTML = `<div class="table-wrap"><table class="table"><thead><tr>
      <th>ID</th><th>Preview</th><th>Sent</th><th>When</th><th></th>
    </tr></thead><tbody>
      ${data.items.map(b => `<tr>
        <td>#${b.id}</td>
        <td>${escapeHtml((b.preview || "").slice(0, 40))}${b.has_video ? " [video]" : b.has_photo ? " [photo]" : ""}</td>
        <td>${b.sent_count}/${b.sent_count + b.failed_count}</td>
        <td>${fmtDate(b.created_at)}</td>
        <td>${b.deleted ? "<span class=\"muted\">Deleted</span>" :
          `<button class="btn btn-sm btn-danger" onclick="deleteBroadcast(${b.id})">Delete</button>`}</td>
      </tr>`).join("")}
    </tbody></table></div>`;
  } catch (e) {
    el.innerHTML = escapeHtml(e.message);
  }
}

async function deleteBroadcast(id) {
  if (!confirm("Delete this broadcast from all user chats?")) return;
  try {
    const res = await api(`/admin/broadcasts/${id}/delete`, { method: "POST" });
    toast(`Deleted: ${res.deleted}, Failed: ${res.failed}`);
    loadBroadcastHistory();
  } catch (e) { toast(e.message, "error"); }
}

async function deleteLastBroadcast() {
  if (!confirm("Delete the LAST broadcast from all user chats?")) return;
  try {
    const res = await api("/admin/broadcasts/delete-last", { method: "POST" });
    toast(`Deleted: ${res.deleted}, Failed: ${res.failed}`);
    loadBroadcastHistory();
  } catch (e) { toast(e.message, "error"); }
}

async function doBroadcast() {
  if (_broadcastBusy) {
    toast("Broadcast already running", "error");
    return;
  }
  const message = document.getElementById("broadcastMsg").value.trim();
  const fileInput = document.getElementById("broadcastPhoto");
  const videoInput = document.getElementById("broadcastVideo");
  const file = fileInput && fileInput.files && fileInput.files[0];
  const video = videoInput && videoInput.files && videoInput.files[0];
  const buttons = (document.getElementById("broadcastButtons") || {}).value || "";
  if (!message && !file && !video) { toast("Message, photo, or video required", "error"); return; }
  if (!confirm("Send broadcast to all matching users?")) return;
  const btn = document.getElementById("broadcastBtn");
  _broadcastBusy = true;
  if (btn) {
    btn.disabled = true;
    btn.textContent = "Sending…";
    btn.classList.add("is-loading");
  }
  fxClick();
  try {
    const onlyActive = document.getElementById("broadcastActive").checked;
    let res;
    if (file || video || buttons.trim()) {
      const fd = new FormData();
      fd.append("message", message || " ");
      fd.append("only_active", onlyActive ? "true" : "false");
      if (buttons.trim()) fd.append("buttons", buttons.trim());
      if (file) fd.append("photo", file);
      if (video) fd.append("video", video);
      res = await api("/admin/broadcast", { method: "POST", body: fd });
    } else {
      res = await api("/admin/broadcast", {
        method: "POST",
        body: { message, only_active: onlyActive, buttons: buttons.trim() || undefined },
      });
    }
    const notes = [];
    if (res.has_photo) notes.push("photo");
    if (res.has_video) notes.push("video");
    if (res.has_buttons) notes.push("buttons");
    const extra = notes.length ? ` (${notes.join("+")})` : "";
    toast(`Sent: ${res.sent}, Failed: ${res.failed}, Total: ${res.total}${extra}`);
  } catch (e) {
    toast(e.message, "error");
  } finally {
    _broadcastBusy = false;
    if (btn) {
      btn.disabled = false;
      btn.textContent = "Send Broadcast";
      btn.classList.remove("is-loading");
    }
  }
}

async function doChangePassword() {
  const cur = document.getElementById("curPass").value;
  const nw = document.getElementById("newPass").value;
  if (!cur || !nw || nw.length < 8) { toast("New password min 8 chars", "error"); return; }
  try {
    await api("/admin/change-password", { method: "POST", body: { current_password: cur, new_password: nw } });
    toast("Password changed");
    document.getElementById("curPass").value = "";
    document.getElementById("newPass").value = "";
  } catch (e) { toast(e.message, "error"); }
}

function exportClaims() {
  window.open("/api/admin/export/claims", "_blank");
}
function exportQRs() {
  window.open("/api/admin/export/qrs", "_blank");
}
function exportWithdrawals() {
  window.open("/api/admin/export/withdrawals", "_blank");
}


async function uploadQRImage(qrId) {
  const input = document.createElement("input");
  input.type = "file";
  input.accept = "image/png,image/jpeg,image/webp,image/gif,.png,.jpg,.jpeg,.webp";
  input.onchange = async () => {
    const file = input.files && input.files[0];
    if (!file) return;
    if (file.size > 5 * 1024 * 1024) {
      toast("Max 5MB", "error");
      return;
    }
    const fd = new FormData();
    fd.append("file", file);
    try {
      const csrf = localStorage.getItem("csrf_token");
      const token = localStorage.getItem("access_token");
      const headers = {};
      if (csrf) headers["X-CSRF-Token"] = csrf;
      if (token) headers["Authorization"] = "Bearer " + token;
      const res = await fetch(API + `/admin/qrs/${qrId}/upload-image`, {
        method: "POST",
        credentials: "include",
        headers,
        body: fd,
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || data.message || "Upload failed");
      toast("QR image uploaded");
      renderQRs();
    } catch (e) {
      toast(e.message, "error");
    }
  };
  input.click();
}


async function renderCampaigns() {
  document.getElementById("topbarActions").innerHTML = "";
  const data = await api("/admin/campaigns");
  const rows = (data.items || []).map(c => `
    <tr>
      <td>${escapeHtml(c.campaign)}</td>
      <td>${c.total_qrs}</td>
      <td>${c.available}</td>
      <td>${c.claimed}</td>
      <td><strong>${cents(c.total_reward_cents)}</strong></td>
    </tr>`).join("");
  document.getElementById("content").innerHTML = `
    <div class="table-wrap"><table>
      <thead><tr><th>Campaign</th><th>Total QR</th><th>Available</th><th>Claimed</th><th>Total Rewards</th></tr></thead>
      <tbody>${rows || `<tr><td colspan="5">${emptyState("📊","No campaigns")}</td></tr>`}</tbody>
    </table></div>`;
}

async function renderTickets(status = "OPEN") {
  document.getElementById("topbarActions").innerHTML = "";
  const data = await api(`/admin/tickets?offset=${offset}&limit=${LIMIT}${status ? "&status="+status : ""}`);
  const rows = (data.items || []).map(t => `
    <tr>
      <td>${t.id}</td>
      <td>${tgUserLink(t.username, t.telegram_id, t.first_name, null)}</td>
      <td>${escapeHtml(t.subject)}</td>
      <td>${badge(t.status)}</td>
      <td>${fmtDate(t.created_at)}</td>
      <td><button class="btn btn-sm btn-primary" onclick='replyTicket(${t.id})'>Reply</button></td>
    </tr>`).join("");
  document.getElementById("content").innerHTML = `
    <div class="toolbar">
      <select onchange="offset=0;renderTickets(this.value)">
        ${["OPEN","IN_PROGRESS","CLOSED",""].map(s => `<option value="${s}" ${s===status?"selected":""}>${s||"All"}</option>`).join("")}
      </select>
    </div>
    <div class="table-wrap"><table>
      <thead><tr><th>ID</th><th>User</th><th>Subject</th><th>Status</th><th>Created</th><th></th></tr></thead>
      <tbody>${rows || `<tr><td colspan="6">${emptyState("🆘","No tickets")}</td></tr>`}</tbody>
    </table></div>
    ${pagination(data.total)}`;
}

function replyTicket(id) {
  openModal("Reply to Ticket #"+id, `
    <div class="form-group"><label>Reply</label><textarea id="ticketReply" rows="4"></textarea></div>
    <label class="check-row"><input type="checkbox" id="ticketClose" checked> Close ticket</label>
  `, `
    <button class="btn btn-outline" onclick="closeModal()">Cancel</button>
    <button class="btn btn-primary" onclick="doReplyTicket(${id})">Send</button>
  `);
}

async function doReplyTicket(id) {
  const reply = document.getElementById("ticketReply").value.trim();
  if (!reply) { toast("Reply required", "error"); return; }
  try {
    await api(`/admin/tickets/${id}/reply`, { method: "POST", body: {
      reply, close: document.getElementById("ticketClose").checked
    }});
    toast("Reply sent");
    closeModal();
    renderTickets();
  } catch (e) { toast(e.message, "error"); }
}

async function cloneQR(id) {
  const q = prompt("How many copies?", "1");
  if (!q) return;
  try {
    const res = await api(`/admin/qrs/${id}/clone?quantity=${encodeURIComponent(q)}`, { method: "POST" });
    toast(`Cloned ${res.created} QR(s)`);
    renderQRs();
  } catch (e) { toast(e.message, "error"); }
}

async function scheduleQR(id) {
  openModal("Schedule Post", `
    <div class="form-group"><label>Channel / Chat ID</label><input id="schedChat" placeholder="-100..."></div>
    <div class="form-group"><label>When (local)</label><input type="datetime-local" id="schedWhen"></div>
  `, `
    <button class="btn btn-outline" onclick="closeModal()">Cancel</button>
    <button class="btn btn-primary" onclick="doScheduleQR(${id})">Schedule</button>
  `);
}

async function doScheduleQR(id) {
  const chat_id = document.getElementById("schedChat").value.trim();
  const local = document.getElementById("schedWhen").value;
  if (!chat_id || !local) { toast("Chat ID and time required", "error"); return; }
  const scheduled_at = new Date(local).toISOString();
  try {
    await api(`/admin/qrs/${id}/schedule`, { method: "POST", body: { chat_id, scheduled_at } });
    toast("Scheduled");
    closeModal();
  } catch (e) { toast(e.message, "error"); }
}

/* ---------- Admins & Roles ---------- */

async function renderAdmins() {
  document.getElementById("topbarActions").innerHTML = `
    <button class="btn btn-primary btn-sm" onclick="showCreateAdmin()">+ Add Admin</button>
  `;
  try {
    const data = await api("/admin/admins");
    const rows = (data.items || []).map((a) => `
      <tr>
        <td>${a.id}</td>
        <td><strong>${escapeHtml(a.username)}</strong></td>
        <td>${badge(a.role)}</td>
        <td>${escapeHtml(a.display_name || "—")}</td>
        <td>${a.is_active ? badge("ACTIVE") : badge("DISABLED")}</td>
        <td>${fmtDate(a.last_login_at)}</td>
        <td>
          ${a.role !== "OWNER" ? `
            <button class="btn btn-sm btn-outline" onclick='editAdmin(${JSON.stringify(a).replace(/'/g, "&#39;")})'>Edit</button>
          ` : "—"}
        </td>
      </tr>`).join("");
    document.getElementById("content").innerHTML = `
      <div class="table-wrap"><table>
        <thead><tr>
          <th>ID</th><th>Username</th><th>Role</th><th>Name</th><th>Status</th><th>Last login</th><th></th>
        </tr></thead>
        <tbody>${rows || `<tr><td colspan="7">${emptyState("🔐", "No admins")}</td></tr>`}</tbody>
      </table></div>
      <p class="muted" style="margin-top:1rem;font-size:0.85rem">
        Roles: OWNER (full) · SUPER_ADMIN · MANAGER · MODERATOR. Only OWNER can manage admins.
      </p>`;
  } catch (e) {
    document.getElementById("content").innerHTML =
      `<div class="error-banner">${escapeHtml(e.message)}. Owner access required.</div>`;
  }
}

function showCreateAdmin() {
  openModal("Add Admin", `
    <div class="form-group"><label>Username</label><input id="admUser" /></div>
    <div class="form-group"><label>Password (min 8)</label><input type="password" id="admPass" /></div>
    <div class="form-group"><label>Role</label>
      <select id="admRole">
        <option value="MODERATOR">MODERATOR</option>
        <option value="MANAGER">MANAGER</option>
        <option value="SUPER_ADMIN">SUPER_ADMIN</option>
      </select>
    </div>
    <div class="form-group"><label>Display name</label><input id="admName" /></div>
  `, `
    <button class="btn btn-outline" onclick="closeModal()">Cancel</button>
    <button class="btn btn-primary" onclick="doCreateAdmin()">Create</button>
  `);
}

async function doCreateAdmin() {
  const body = {
    username: document.getElementById("admUser").value.trim(),
    password: document.getElementById("admPass").value,
    role: document.getElementById("admRole").value,
    display_name: document.getElementById("admName").value.trim() || null,
  };
  if (!body.username || body.password.length < 8) {
    toast("Username and password (8+) required", "error");
    return;
  }
  try {
    await api("/admin/admins", { method: "POST", body });
    toast("Admin created");
    closeModal();
    renderAdmins();
  } catch (e) { toast(e.message, "error"); }
}

function editAdmin(a) {
  openModal("Edit Admin #" + a.id, `
    <div class="form-group"><label>Role</label>
      <select id="editRole">
        ${["MODERATOR","MANAGER","SUPER_ADMIN"].map(r =>
          `<option value="${r}" ${a.role===r?"selected":""}>${r}</option>`).join("")}
      </select>
    </div>
    <div class="form-group"><label class="form-check">
      <input type="checkbox" id="editActive" ${a.is_active?"checked":""}/> Active
    </label></div>
    <div class="form-group"><label>New password (optional)</label>
      <input type="password" id="editPass" placeholder="Leave blank to keep"/></div>
  `, `
    <button class="btn btn-outline" onclick="closeModal()">Cancel</button>
    <button class="btn btn-primary" onclick="doEditAdmin(${a.id})">Save</button>
  `);
}

async function doEditAdmin(id) {
  const body = {
    role: document.getElementById("editRole").value,
    is_active: document.getElementById("editActive").checked,
  };
  const pass = document.getElementById("editPass").value;
  if (pass) body.password = pass;
  try {
    await api(`/admin/admins/${id}`, { method: "PATCH", body });
    toast("Admin updated");
    closeModal();
    renderAdmins();
  } catch (e) { toast(e.message, "error"); }
}

/* ---------- Chrome browser notifications ---------- */

let _notifClaimId = parseInt(localStorage.getItem("notif_claim_id") || "0", 10);
let _notifWdId = parseInt(localStorage.getItem("notif_wd_id") || "0", 10);

function browserNotify(title, body) {
  if (!("Notification" in window)) return;
  if (Notification.permission === "granted") {
    try {
      new Notification(title, { body, icon: "/static/favicon.ico" });
    } catch (_) {}
  }
}

async function ensureNotifyPermission() {
  if (!("Notification" in window)) return;
  if (Notification.permission === "default") {
    try { await Notification.requestPermission(); } catch (_) {}
  }
}

async function pollAdminEvents() {
  try {
    const data = await api(
      `/admin/events/poll?since_claim_id=${_notifClaimId}&since_withdrawal_id=${_notifWdId}`
    );
    for (const c of data.claims || []) {
      browserNotify(
        "New QR claim",
        `#${c.id} ${c.qr_code || ""} — $${(c.reward_cents / 100).toFixed(2)} from @${c.username || c.telegram_id}`
      );
    }
    for (const w of data.withdrawals || []) {
      browserNotify(
        "New withdrawal",
        `#${w.id} $${(w.amount_cents / 100).toFixed(2)} via ${w.payout_method || "—"} (${w.payout_details || ""})`
      );
    }
    if (data.max_claim_id != null) {
      _notifClaimId = data.max_claim_id;
      localStorage.setItem("notif_claim_id", String(_notifClaimId));
    }
    if (data.max_withdrawal_id != null) {
      _notifWdId = data.max_withdrawal_id;
      localStorage.setItem("notif_wd_id", String(_notifWdId));
    }
  } catch (_) {
    /* ignore when logged out */
  }
}

/* DASH_REFRESH + event poll */
setInterval(() => {
  if (typeof currentPage !== "undefined" && currentPage === "dashboard") {
    renderDashboard().catch(() => {});
  }
}, 60000);

ensureNotifyPermission();
setInterval(pollAdminEvents, 20000);
setTimeout(pollAdminEvents, 3000);


async function doChannelBroadcast() {
  const chat_id = (document.getElementById("broadcastChannelChat") || {}).value.trim();
  const message = (document.getElementById("broadcastChannelMsg") || {}).value.trim();
  const buttons = (document.getElementById("broadcastChannelButtons") || {}).value.trim();
  const photo = (document.getElementById("broadcastChannelPhoto") || {}).files?.[0];
  const video = (document.getElementById("broadcastChannelVideo") || {}).files?.[0];
  if (!chat_id) { toast("chat_id required", "error"); return; }
  if (!message && !photo && !video) { toast("Message or media required", "error"); return; }
  if (!confirm("Post to channel " + chat_id + "?")) return;
  try {
    const fd = new FormData();
    fd.append("chat_id", chat_id);
    fd.append("message", message || " ");
    if (buttons) fd.append("buttons", buttons);
    if (photo) fd.append("photo", photo);
    if (video) fd.append("video", video);
    const res = await api("/admin/broadcast-channel", { method: "POST", body: fd });
    toast(res.message || "Posted");
  } catch (e) { toast(e.message, "error"); }
}
