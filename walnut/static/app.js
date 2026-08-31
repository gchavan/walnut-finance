/* Walnut — single-page dashboard. Hash routes, no bundler. Live source: SimpleFIN. */
"use strict";

const view = document.getElementById("view");
let statusCache = null;
let txnWindow = "30d";
let spendPeriod = "this_month";
let spendStart = "";
let spendEnd = "";
let spendCatFocus = "";
let txnQuery = "";
let txnCategory = "";
let catShowAll = false;
let acctOwnerFilter = "all";
let acctVisFilter = "all";
let householdCache = null;
let hhAddingPartner = false;
let hhAddingKid = false;

const ROUTES = ["overview", "transactions", "spending", "retirement", "settings", "recurring"];
const GROUP_LABELS = {
  cash: "Cash",
  cards: "Credit cards",
  investments: "Investments / Robinhood",
  retirement: "Retirement",
  loans: "Loans",
  other: "Other",
};
const GROUP_ORDER = ["cash", "cards", "investments", "retirement", "loans", "other"];
const ACCT_CAT_ORDER = [
  "Checking", "Savings", "Credit card", "Taxable",
  "401(k)", "Roth IRA", "Traditional IRA", "HSA", "Crypto",
  "Mortgage", "Auto loan", "Student loan", "Other",
];
const PROP_KINDS = ["Primary Home", "Investment Rental", "Vacation Home", "Land"];
const LOAN_CATS = ["Mortgage", "Auto loan", "Student loan"];
const SFIN_CREATE = "https://bridge.simplefin.org/simplefin/create";
const SPEND_TAGS = [
  "Dining & Drinks", "Groceries", "Auto & Transport", "Shopping",
  "Entertainment & Rec.", "Health & Wellness", "Personal Care",
  "Bills & Utilities", "Travel & Vacation", "Family Care",
  "Home & Garden", "Medical", "Software & Tech", "Education",
  "Kids", "Charitable Donations", "Fees", "Income",
  "Transfer (not spend)", "Uncategorized",
];

function spendTagOpts(selected) {
  const cur = prettyCat(selected);
  const list = SPEND_TAGS.slice();
  if (cur && !list.includes(cur)) list.unshift(cur);
  return list.map((t) =>
    `<option value="${esc(t)}" ${t === cur ? "selected" : ""}>${esc(t)}</option>`
  ).join("");
}


function parseRoute() {
  const raw = ((location.hash || "#/overview").replace(/^#\/?/, "") || "overview").split("?")[0];
  const parts = raw.split("/").filter(Boolean);
  const head = parts[0] || "overview";
  if (head === "categories") {
    return { id: "settings", tab: "categories", canonical: "#/settings/categories" };
  }
  if (head === "accounts") {
    return { id: "settings", tab: "accounts", canonical: "#/settings/accounts" };
  }
  if (head === "settings") {
    const t = parts[1];
    let tab = "accounts";
    if (t === "app") tab = "app";
    else if (t === "rules") tab = "rules";
    else if (t === "categories") tab = "categories";
    else if (t === "accounts") tab = "accounts";
    else if (t === "household") tab = "household";
    else if (t === "properties") tab = "properties";
    let canonical = "#/settings";
    if (tab === "app") canonical = "#/settings/app";
    else if (tab === "rules") canonical = "#/settings/rules";
    else if (tab === "categories") canonical = "#/settings/categories";
    else if (tab === "household") canonical = "#/settings/household";
    else if (tab === "properties") canonical = "#/settings/properties";
    else if (t === "accounts") canonical = "#/settings/accounts";
    return { id: "settings", tab, canonical };
  }
  if (head === "retirement") {
    const t = parts[1];
    const tab = (t === "blueprint") ? "blueprint" : "networth";
    const canonical = tab === "blueprint" ? "#/retirement/blueprint" : "#/retirement";
    return { id: "retirement", tab, canonical };
  }
  const id = ROUTES.includes(head) ? head : "overview";
  return { id, tab: null, canonical: null };
}

function isDemo() {
  try { return localStorage.getItem("walnut-demo") === "1"; } catch (e) { return false; }
}
function setDemo(on) {
  try { localStorage.setItem("walnut-demo", on ? "1" : "0"); } catch (e) {}
  demoScaleReady = false;
}
let demoScale = 0.35;
let demoScaleReady = false;
const DEMO_NW_CAP_CENTS = 120000000; // $1.2M household net worth in demo
function setDemoScaleFromNetWorth(nw) {
  const mag = Math.abs(Number(nw) || 0);
  if (!mag) { demoScale = 0.35; return; }
  demoScale = DEMO_NW_CAP_CENTS / mag;
  demoScaleReady = true;
}
async function ensureDemoScale() {
  if (!isDemo() || demoScaleReady) return;
  try {
    const d = await api("/api/overview");
    const nw = (d.net_worth && d.net_worth.net_worth_cents) || 0;
    setDemoScaleFromNetWorth(nw);
  } catch (e) {
    demoScale = 0.35;
  }
}
function demoAmount(n) {
  if (n == null || Number.isNaN(n)) return n;
  const sign = n < 0 ? -1 : 1;
  return sign * Math.round(Math.abs(n) * demoScale / 100) * 100;
}
function hashStr(s) {
  let h = 2166136261;
  const t = String(s || "");
  for (let i = 0; i < t.length; i++) h = Math.imul(h ^ t.charCodeAt(i), 16777619);
  return h >>> 0;
}
const DEMO_PAYEES = [
  "Corner Cafe", "Market Basket", "City Transit", "North Loop Shop",
  "Streamflix", "Well Clinic", "Power Co", "Harbor Inn", "Playground Co",
  "Software Inc", "School Store", "Gifts", "Bank Fee", "Payroll",
];
function demoPayee(raw) {
  return DEMO_PAYEES[hashStr(raw || "payee") % DEMO_PAYEES.length];
}
function demoAcctName(a) {
  const cat = (a && a.category) || "";
  const base = ({
    Checking: "Checking", Savings: "Savings", "Credit card": "Credit card",
    Taxable: "Brokerage", "401(k)": "401(k)", "Roth IRA": "Roth IRA",
    "Traditional IRA": "IRA", HSA: "HSA", Crypto: "Crypto",
    Mortgage: "Mortgage", "Auto loan": "Auto loan", "Student loan": "Student loan",
    "Primary Home": "Home", "Investment Rental": "Rental",
    "Vacation Home": "Vacation", Land: "Land",
  })[cat] || ((a && a.group_key) === "cards" ? "Credit card" : "Account");
  const letter = String.fromCharCode(65 + (hashStr((a && (a.account_id || a.name || a.account_name)) || base) % 4));
  return base + " " + letter;
}
function demoPropName(p) {
  const kind = (p && p.kind) || "";
  const base = ({
    "Primary Home": "Home",
    "Investment Rental": "Rental",
    "Vacation Home": "Vacation",
    Land: "Land",
  })[kind] || "Home";
  const letter = String.fromCharCode(65 + (hashStr((p && (p.property_id || p.name)) || base) % 4));
  return base + " " + letter;
}
function isManualId(id) {
  return String(id || "").startsWith("manual:");
}

function closeWalnutModal() {
  const back = document.querySelector(".modal-back");
  if (back) back.remove();
  document.removeEventListener("keydown", onWalnutModalKey);
}

function onWalnutModalKey(e) {
  if (e.key === "Escape") closeWalnutModal();
}

function openWalnutModal(title, innerHtml) {
  closeWalnutModal();
  const back = document.createElement("div");
  back.className = "modal-back";
  back.innerHTML = `<div class="modal" role="dialog" aria-modal="true">
    <div class="modal-head">
      <h2>${esc(title)}</h2>
      <button type="button" class="btn ghost" data-modal-close>Close</button>
    </div>
    ${innerHtml}
  </div>`;
  back.addEventListener("click", (e) => {
    if (e.target === back) closeWalnutModal();
  });
  const closeBtn = back.querySelector("[data-modal-close]");
  if (closeBtn) closeBtn.addEventListener("click", closeWalnutModal);
  document.addEventListener("keydown", onWalnutModalKey);
  document.body.appendChild(back);
  const first = back.querySelector("input, select, textarea");
  if (first) first.focus();
  return back;
}

function householdData() {
  return householdCache || { self: null, partner: null, kids: [], owners: [], joint_label: null };
}

async function loadHousehold() {
  householdCache = await api("/api/household");
  return householdCache;
}

function cacheHousehold(hh) {
  if (hh && typeof hh === "object") householdCache = hh;
  return householdData();
}

function initials(name, demoLetter) {
  if (isDemo() && demoLetter) return demoLetter;
  const parts = String(name || "").trim().split(/\s+/).filter(Boolean);
  if (!parts.length) return "?";
  if (parts.length === 1) return parts[0].slice(0, 1).toUpperCase();
  return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
}

function demoOwner(o) {
  if (o === "Joint") return "Joint";
  const hh = householdData();
  if (hh.self && o === hh.self.name) return "Alex";
  if (hh.partner && o === hh.partner.name) return "Sam";
  const kids = hh.kids || [];
  for (let i = 0; i < kids.length; i++) {
    if (kids[i] && kids[i].name === o) {
      return "Kid " + String.fromCharCode(65 + (i % 26));
    }
  }
  return o || "Alex";
}
function acctName(a) {
  if (!a) return "";
  if (isDemo()) return demoAcctName(a);
  return a.display_name || a.name || a.account_name || "";
}
function payeeName(raw) {
  if (isDemo()) return demoPayee(raw || "payee");
  return raw || "—";
}
function ownerName(o) {
  if (isDemo()) return demoOwner(o);
  if (o === "Joint") return householdData().joint_label || "Joint";
  return o || "";
}

function ownerOptionLabel(o) {
  if (o === "Joint") return ownerName("Joint");
  return ownerName(o);
}

function cents(n) {
  if (n == null || Number.isNaN(n)) return "—";
  if (isDemo()) n = demoAmount(n);
  const sign = n < 0 ? "−" : "";
  return sign + "$" + (Math.abs(n) / 100).toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

function signedCents(n) {
  if (n == null) return "—";
  if (n > 0) return "−" + cents(n).replace("−", ""); // money out
  if (n < 0) return "+" + cents(-n);
  return cents(0);
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function prettyCat(s) {
  if (!s) return "Uncategorized";
  if (s.indexOf("_") >= 0 && s === s.toUpperCase()) {
    return s.replace(/_/g, " ").toLowerCase().replace(/\b\w/g, (c) => c.toUpperCase());
  }
  return s;
}

function liveSource(st) {
  st = st || statusCache || {};
  if (st.simplefin_ready) return "simplefin";
  return "simplefin";
}

function liveSourceLabel(st) {
  return "SimpleFIN";
}

async function api(path, opts) {
  const res = await fetch(path, Object.assign({
    headers: { "Accept": "application/json", "Content-Type": "application/json" },
    cache: "no-store",
  }, opts || {}));
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(data.error || res.statusText);
    err.data = data;
    err.status = res.status;
    throw err;
  }
  return data;
}

function setNav(name) {
  document.querySelectorAll("nav a").forEach((a) => {
    a.setAttribute("aria-current", a.dataset.route === name ? "page" : null);
  });
}

function setupBanner(st) {
  if (st.simplefin_ready) return "";
  return `
    <div class="callout setup">
      <h2>Connect SimpleFIN Bridge</h2>
      <p>Walnut’s live bank source is SimpleFIN. Create a setup token, paste it here, then Connect.
        The token is claimed once and the Access URL is stored only in gitignored <code>.env</code>.
        Never commit it.</p>
      <ol>
        <li>Open <a href="${SFIN_CREATE}" target="_blank" rel="noopener">${esc(SFIN_CREATE.replace("https://", ""))}</a>
          and create a setup token.</li>
        <li>Paste the token below. Do not send it anywhere else.</li>
      </ol>
      <textarea id="sfin-token" rows="3" placeholder="Paste setup token" autocomplete="off" spellcheck="false"></textarea>
      <button class="btn" type="button" data-act="claim">Connect</button>
      <p class="note">If Connect fails because the token was already used (403), revoke it on SimpleFIN and create a new one.</p>
    </div>`;
}

function errlistBanner(st) {
  const list = (st && st.errlist) || [];
  if (!list.length) return "";
  const items = list.map((e) => {
    const msg = (e && e.msg) ? e.msg : e;
    const code = (e && e.code) ? ` <code>${esc(e.code)}</code>` : "";
    return `<li>${esc(msg)}${code}</li>`;
  }).join("");
  return `<div class="callout errors"><h2>SimpleFIN notices</h2><ul>${items}</ul></div>`;
}

function coverageLine(cov, st) {
  return "";
  const srcLabel = liveSourceLabel(st);
  if (!cov || !cov.min_date) {
    const floor = (cov && cov.transactions_since) || "~90 days";
    return `<div class="coverage">No transactions held yet. Sync pulls ${esc(srcLabel)} history
      (${esc(String(floor))}).
      This page never pretends a partial window is complete.</div>`;
  }
  const a = cov.min_date;
  const b = cov.max_date;
  const days = Math.round((Date.parse(b) - Date.parse(a)) / 86400000) + 1;
  const defaultPartial = "SimpleFIN typically returns about 90 days; this is what they sent.";
  const partial = cov.partial
    ? ` This is a <strong>partial</strong> picture — ${esc(cov.partial_reason || defaultPartial)}`
    : ` Range is what ${esc(srcLabel)} returned; it is not padded.`;
  const track = (cov.tracking_accounts || 0);
  const onb = (cov.on_budget_accounts || 0);
  return `<div class="coverage">
    Live source: <strong>${esc(srcLabel)}</strong>.
    Holding <strong>${cov.n}</strong> transactions from
    <strong>${esc(a)}</strong> to <strong>${esc(b)}</strong>
    (${days} day${days === 1 ? "" : "s"}).
    ${onb} cash/card account${onb === 1 ? "" : "s"},
    ${track} tracking account${track === 1 ? "" : "s"} (investments / retirement).
    ${partial}
    Transfers are listed but excluded from spending. Pending rows show a badge and are not totaled.
  </div>`;
}

function header(title, sub, extra) {
  const demo = isDemo() ? `<div class="demo-banner" role="status">
      <div>
        <div class="demo-title">DEMO MODE</div>
        <div class="demo-sub">Demo household. Amounts and names are fake.</div>
      </div>
    </div>` : "";
  return demo + `<header class="page">
    <div>
      <h1>${esc(title)}</h1>
      ${sub ? `<div class="sub">${sub}</div>` : ""}
    </div>
    <div class="controls">${extra || ""}</div>
  </header>`;
}

function syncButton(st) {
  const ready = st && st.simplefin_ready;
  const disabled = ready ? "" : "disabled";
  return `<button class="btn" data-act="sync" ${disabled}>Sync</button>`;
}

async function doSync() {
  const btn = view.querySelector('[data-act="sync"]');
  if (btn) { btn.disabled = true; btn.textContent = "Syncing…"; }
  try {
    await api("/api/sync", { method: "POST", body: "{}" });
    await refresh();
  } catch (err) {
    alert((err.data && err.data.error) || err.message);
    if (btn) { btn.disabled = false; btn.textContent = "Sync"; }
  }
}

async function doClaim() {
  const ta = view.querySelector("#sfin-token");
  const token = (ta && ta.value || "").trim();
  if (!token) { alert("Paste a setup token first."); return; }
  const btn = view.querySelector('[data-act="claim"]');
  if (btn) { btn.disabled = true; btn.textContent = "Connecting…"; }
  try {
    await api("/api/simplefin/claim", {
      method: "POST",
      body: JSON.stringify({ token }),
    });
    if (ta) ta.value = "";
    await refresh();
  } catch (err) {
    alert((err.data && err.data.error) || err.message);
    if (btn) { btn.disabled = false; btn.textContent = "Connect"; }
  }
}

function bindChrome() {
  view.querySelectorAll("[data-act]").forEach((el) => {
    el.addEventListener("click", () => {
      if (el.dataset.act === "sync") doSync();
      if (el.dataset.act === "claim") doClaim();
    });
  });
}

function accountBalanceLabel(a) {
  const c = a.current_cents;
  if (c == null) return "—";
  return cents(c);
}
function balanceClass(centsOrAcct) {
  const c = (centsOrAcct && typeof centsOrAcct === "object")
    ? centsOrAcct.current_cents : centsOrAcct;
  return (c != null && c < 0) ? "bal neg" : "bal";
}

function acctVisibility(a) {
  if (a.hidden) return "exclude";
  if (Number(a.on_blueprint ?? 1) === 0) return "hide";
  return a.visibility || "include";
}

function isOnBlueprint(a) {
  return Number(a.on_blueprint ?? 1) !== 0;
}

/* ---------- pages ---------- */

async function renderOverview() {
  const st = statusCache;
  const d = await api("/api/overview");
  const nw = d.net_worth || {};
  const delta = d.this_month_spending_cents - d.last_month_spending_cents;
  const deltaCls = delta > 0 ? "up" : (delta < 0 ? "down" : "");
  const deltaTxt = d.last_month_spending_cents
    ? `${delta >= 0 ? "+" : ""}${cents(delta)} vs last month`
    : "no last-month baseline yet";
  const upcoming = (d.upcoming || []).map((r) =>
    `<div class="acct">
      <div><div class="nm">${esc(payeeName(r.merchant))}</div>
        <div class="meta">${esc(r.frequency)} · next ${esc(r.next_date || "—")}</div></div>
      <div class="bal">${cents(r.average_cents)}</div>
    </div>`
  ).join("") || `<p class="empty">No upcoming recurrences yet. Sync, then the local detector fills this in.</p>`;

  const sub = monthLong(d.this_month) || "";
  view.innerHTML = header("Overview", sub, syncButton(st)) +
    setupBanner(st) +
    `<div class="cards">
      <div class="card"><div class="lbl">Net worth</div>
        <div class="val">${cents(nw.net_worth_cents)}</div>
        <div class="delta">Assets ${cents(nw.assets_cents)} · Liabilities ${cents(nw.liabilities_cents)}</div>
      </div>
      <div class="card"><div class="lbl">Spending this month</div>
        <div class="val">${cents(d.this_month_spending_cents)}</div>
        <div class="delta ${deltaCls}">${esc(deltaTxt)}</div>
      </div>
    </div>
    <div class="panel">
      <h2>Upcoming recurring</h2>
      ${upcoming}
    </div>`;
  bindChrome();
}

function windowSeg(current, which) {
  const chips = [
    ["30d", "30d"], ["90d", "90d"], ["12m", "12m"], ["all", "All"],
  ];
  return `<div class="seg" data-seg="${which}">` +
    chips.map(([v, l]) =>
      `<button type="button" data-w="${v}" aria-pressed="${v === current}">${l}</button>`
    ).join("") + `</div>`;
}

async function renderTransactions() {
  const st = statusCache;
  const params = new URLSearchParams({ window: txnWindow });
  if (txnQuery) params.set("q", txnQuery);
  if (txnCategory) params.set("category", txnCategory);
  const d = await api("/api/transactions?" + params.toString());
  const cats = (d.categories || []).slice();
  if (txnCategory && !cats.includes(txnCategory)) cats.unshift(txnCategory);
  const rows = (d.transactions || []).map((t) => {
    const cls = t.amount_cents > 0 ? "out" : (t.amount_cents < 0 ? "in" : "");
    const badges = [
      t.pending ? `<span class="badge pending">pending</span>` : "",
      t.is_transfer ? `<span class="badge">transfer</span>` : "",
      (!t.is_spending && !t.pending) ? `<span class="badge">not spend</span>` : "",
    ].join(" ");
    return `<tr>
      <td>${esc(t.date)}</td>
      <td>${esc(payeeName(t.payee))}
        <div class="muted">${esc(acctName(t))}${(!isDemo() && t.memo) ? " · " + esc(t.memo) : ""}</div></td>
      <td>${esc(prettyCat(t.category_name))}</td>
      <td>${badges}</td>
      <td class="num ${cls}">${signedCents(t.amount_cents)}</td>
    </tr>`;
  }).join("") || `<tr><td colspan="5" class="empty">No transactions in this window.</td></tr>`;

  view.innerHTML = header("Transactions", "Search, category, window. Robinhood trades hidden. Transfers listed, not spent. Pending = not posted.",
      windowSeg(txnWindow, "txn") +
      `<input type="search" id="q" placeholder="Search payee, memo, account" value="${esc(txnQuery)}">` +
      syncButton(st)) +
    setupBanner(st) +
    coverageLine(d.coverage, st) +
    `<div class="panel">
      <div class="controls" style="margin-bottom:12px">
        <select id="cat">
          <option value="">All categories</option>
          ${cats.map((c) => `<option value="${esc(c)}" ${c === txnCategory ? "selected" : ""}>${esc(prettyCat(c))}</option>`).join("")}
        </select>
      </div>
      <table>
        <thead><tr><th>Date</th><th>Payee</th><th>Category</th><th></th><th class="num">Amount</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
      <p class="note">Showing up to 500. Outflows are red (positive cents = money out). Inflows are green. Robinhood trades hidden (Robinhood and Crypto accounts). Other brokerage / retirement rows show a not-spend badge and are excluded from totals. Assign tags in Settings.</p>
    </div>`;
  bindChrome();
  view.querySelectorAll("[data-seg=txn] button").forEach((b) => {
    b.addEventListener("click", () => { txnWindow = b.dataset.w; refresh(); });
  });
  const q = view.querySelector("#q");
  q.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { txnQuery = q.value.trim(); refresh(); }
  });
  q.addEventListener("change", () => { txnQuery = q.value.trim(); refresh(); });
  view.querySelector("#cat").addEventListener("change", (e) => {
    txnCategory = e.target.value; refresh();
  });
}

function barList(rows, labelKey) {
  const max = Math.max(1, ...rows.map((r) => r.cents || 0));
  return rows.map((r) => {
    const pct = Math.round(100 * (r.cents || 0) / max);
    return `<div class="bar-row">
      <div>${esc(prettyCat(r[labelKey]))}</div>
      <div class="bar-track"><div class="bar-fill" style="width:${pct}%"></div></div>
      <div class="bar-amt">${cents(r.cents)}</div>
    </div>`;
  }).join("") || `<p class="empty">Nothing in this window that counts as spending.</p>`;
}

const CAT_COLORS = {
  "Dining & Drinks": "#e07040",
  "Groceries": "#3d9a62",
  "Auto & Transport": "#4c6fd6",
  "Shopping": "#c45c8a",
  "Entertainment & Rec.": "#8b6cc7",
  "Health & Wellness": "#2a9d8f",
  "Personal Care": "#e09f3e",
  "Bills & Utilities": "#5c7c99",
  "Travel & Vacation": "#2d8a9e",
  "Family Care": "#d4784a",
  "Home & Garden": "#6a994e",
  "Medical": "#d64c5a",
  "Software & Tech": "#3d7ea6",
  "Education": "#6d72c3",
  "Kids": "#e07a7a",
  "Charitable Donations": "#b56576",
  "Fees": "#8a817c",
  "Uncategorized": "#9aa3ad",
  "Checking": "#4c6fd6",
  "Savings": "#3d9a62",
  "Credit card": "#d1444a",
  "Taxable": "#2d8a9e",
  "401(k)": "#8b6cc7",
  "Roth IRA": "#2a9d8f",
  "Traditional IRA": "#6d72c3",
  "HSA": "#3d7ea6",
  "Crypto": "#e07040",
  "Other": "#9aa3ad",
};
const MONTH_SHORT = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

function catColor(name) {
  const key = prettyCat(name);
  if (CAT_COLORS[key]) return CAT_COLORS[key];
  let h = 0;
  const s = String(name || "");
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
  const hue = h % 360;
  return `hsl(${hue}, 42%, 52%)`;
}

function monthLabel(ym) {
  const parts = String(ym || "").split("-");
  const m = Number(parts[1] || 0);
  return MONTH_SHORT[m - 1] || ym;
}

const MONTH_LONG = ["January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December"];

function monthLong(ym) {
  const parts = String(ym || "").split("-");
  const m = Number(parts[1] || 0);
  const y = parts[0] || "";
  if (MONTH_LONG[m - 1] && y) return MONTH_LONG[m - 1] + " " + y;
  return ym || "";
}

function isoToday() {
  const d = new Date();
  const z = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${z(d.getMonth() + 1)}-${z(d.getDate())}`;
}

function isoMonthStart(offset) {
  const d = new Date();
  d.setDate(1);
  d.setMonth(d.getMonth() + (offset || 0));
  const z = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${z(d.getMonth() + 1)}-01`;
}

function lastDayOfYm(ym) {
  const [y, m] = String(ym).split("-").map(Number);
  const d = new Date(y, m, 0);
  const z = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${z(d.getMonth() + 1)}-${z(d.getDate())}`;
}

function changeCell(cur, prev, pct, invert) {
  if (!prev && !cur) return `<td class="num muted">—</td>`;
  if (!prev && cur) {
    const cls = invert ? "in" : "out";
    return `<td class="num ${cls}">New</td>`;
  }
  if (pct == null || Number.isNaN(pct)) return `<td class="num muted">—</td>`;
  if (pct === 0) return `<td class="num muted">0%</td>`;
  const more = pct > 0;
  const cls = invert ? (more ? "in" : "out") : (more ? "out" : "in");
  const sign = more ? "+" : "−";
  return `<td class="num ${cls}">${sign}${Math.abs(Math.round(pct))}%</td>`;
}

function changeDelta(cur, prev, invert) {
  if (!prev && !cur) return { cls: "", text: "no prior period" };
  if (!prev && cur) return { cls: invert ? "down" : "up", text: "new vs prior" };
  const pct = prev ? (100 * (cur - prev) / prev) : 0;
  if (!pct) return { cls: "", text: "same as prior period" };
  const more = pct > 0;
  return {
    cls: invert ? (more ? "down" : "up") : (more ? "up" : "down"),
    text: `${more ? "+" : "−"}${Math.abs(Math.round(pct))}% vs prior period`,
  };
}

function periodSeg(current) {
  const chips = [
    ["last_month", "Last month"],
    ["this_month", "This month"],
    ["custom", "Custom"],
  ];
  const start = spendStart || isoMonthStart(0);
  const end = spendEnd || isoToday();
  const dates = `<span class="custom-dates" ${current === "custom" ? "" : "hidden"}>
      <input type="date" id="sp-start" value="${esc(start)}" aria-label="Start date">
      <span class="muted">to</span>
      <input type="date" id="sp-end" value="${esc(end)}" aria-label="End date">
    </span>`;
  return `<div class="seg" data-seg="sp-period">` +
    chips.map(([v, l]) =>
      `<button type="button" data-p="${v}" aria-pressed="${v === current}">${l}</button>`
    ).join("") + `</div>` + dates;
}

function monthBarsSvg(months, selected) {
  const list = months || [];
  if (!list.length) return `<p class="empty">No monthly spend yet.</p>`;
  const w = 420, h = 168, l = 6, r = 6, t = 10, b = 30;
  const innerW = w - l - r, innerH = h - t - b;
  const max = Math.max(1, ...list.map((m) => m.cents || 0));
  const n = list.length;
  const gap = 12;
  const bw = Math.max(10, (innerW - gap * (n - 1)) / n);
  const parts = list.map((m, i) => {
    const bh = Math.max(m.cents ? 4 : 2, Math.round(innerH * (m.cents || 0) / max));
    const x = l + i * (bw + gap);
    const y = t + innerH - bh;
    const on = m.month === selected;
    const fill = on ? "var(--accent)" : "var(--track)";
    const title = `${esc(monthLabel(m.month))} ${esc(String(m.month).slice(0, 4))} · ${cents(m.cents)}`;
    return `<g class="bar-hit" data-month="${esc(m.month)}" tabindex="0" role="button" aria-label="${title}">
        <title>${title}</title>
        <rect x="${x}" y="${t}" width="${bw}" height="${innerH}" fill="transparent"/>
        <rect class="stem" x="${x}" y="${y}" width="${bw}" height="${bh}" rx="4" fill="${fill}"/>
        <text x="${x + bw / 2}" y="${h - 10}" text-anchor="middle">${esc(monthLabel(m.month))}</text>
      </g>`;
  });
  return `<svg class="spend-svg month-svg" viewBox="0 0 ${w} ${h}" role="img" aria-label="Spend by month">${parts.join("")}</svg>`;
}

function donutSvg(rows, total, focus) {
  const size = 220, cx = 110, cy = 110, radius = 78, sw = 22;
  const parts = [`<circle cx="${cx}" cy="${cy}" r="${radius}" fill="none" stroke="var(--track)" stroke-width="${sw}"/>`];
  if (total) {
    let angle = -90;
    (rows || []).forEach((row) => {
      if (!row.cents) return;
      const sweep = 360 * row.cents / total;
      if (sweep <= 0) return;
      const start = angle * Math.PI / 180;
      const end = (angle + Math.max(sweep - 0.001, 0)) * Math.PI / 180;
      const x0 = cx + radius * Math.cos(start);
      const y0 = cy + radius * Math.sin(start);
      const x1 = cx + radius * Math.cos(end);
      const y1 = cy + radius * Math.sin(end);
      const large = sweep > 180 ? 1 : 0;
      const d = `M ${x0.toFixed(2)} ${y0.toFixed(2)} A ${radius} ${radius} 0 ${large} 1 ${x1.toFixed(2)} ${y1.toFixed(2)}`;
      const pct = (100 * row.cents / total).toFixed(1);
      const on = focus && (row.category || "Uncategorized") === focus;
      parts.push(
        `<path class="donut-slice${on ? " is-on" : ""}" d="${d}" fill="none" stroke="${catColor(row.category)}"
           stroke-width="${on ? 26 : sw}" stroke-linecap="butt" pointer-events="stroke"
           role="button" tabindex="0"
           data-key="${esc(row.category || "Uncategorized")}"
           data-cat="${esc(prettyCat(row.category))}" data-amt="${esc(cents(row.cents))}" data-pct="${pct}"></path>`
      );
      angle += sweep;
    });
  }
  return `<svg class="spend-svg donut-svg" viewBox="0 0 ${size} ${size}">${parts.join("")}</svg>
    <div class="donut-tip" hidden></div>`;
}

function sparkDate(iso) {
  const s = String(iso || "");
  const parts = s.split("-");
  if (parts.length < 3) return s;
  const m = Number(parts[1] || 0);
  const d = Number(parts[2] || 0);
  const mon = MONTH_SHORT[m - 1] || "";
  const y = parts[0] || "";
  const nowY = String(new Date().getFullYear());
  if (mon && d) return y && y !== nowY ? `${mon} ${d}, ${y}` : `${mon} ${d}`;
  return s;
}

function sparkline(values, dates) {
  const pts = [];
  (values || []).forEach((n, i) => {
    if (typeof n === "number" && !Number.isNaN(n)) pts.push({ n, d: (dates || [])[i] || "" });
  });
  if (pts.length < 2) return "";
  const w = 160, h = 36;
  const list = pts.map((p) => p.n);
  let min = Math.min.apply(null, list), max = Math.max.apply(null, list);
  if (min === max) { min -= 1; max += 1; }
  const span = max - min;
  const xy = pts.map((p, i) => {
    const x = 2 + (i / (pts.length - 1)) * (w - 4);
    const y = (h - 2) - ((p.n - min) / span) * (h - 4);
    return { x, y };
  });
  const poly = xy.map((p) => p.x.toFixed(1) + "," + p.y.toFixed(1)).join(" ");
  const up = list[list.length - 1] >= list[0];
  const color = up ? "var(--ok)" : "var(--bad)";
  const series = pts.map((p) => p.n).join(",");
  const dateStr = pts.map((p) => p.d).join(",");
  return `<svg class="nw-spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" role="img"
      data-series="${esc(series)}" data-dates="${esc(dateStr)}" data-min="${min}" data-max="${max}">
    <rect x="0" y="0" width="${w}" height="${h}" fill="transparent"/>
    <polyline fill="none" stroke="${color}" stroke-width="1.8" stroke-linejoin="round" stroke-linecap="round" points="${poly}"/>
    <line class="nw-spark-cursor" x1="0" y1="2" x2="0" y2="${h - 2}" stroke="currentColor" stroke-width="1" opacity="0"/>
    <circle class="nw-spark-dot" r="3.2" fill="${color}" opacity="0"></circle>
  </svg>`;
}

function bindNwSparklines(root) {
  (root || document).querySelectorAll("svg.nw-spark").forEach((svg) => {
    const card = svg.closest(".card");
    const valEl = card && card.querySelector(".val");
    const whenEl = card && card.querySelector(".nw-spark-when");
    const orig = valEl ? valEl.textContent : "";
    const series = (svg.dataset.series || "").split(",").map(Number).filter((n) => !Number.isNaN(n));
    const dates = (svg.dataset.dates || "").split(",");
    const n = series.length;
    if (n < 2) return;
    const w = 160, h = 36;
    let min = Number(svg.dataset.min), max = Number(svg.dataset.max);
    if (!(max > min)) { min = Math.min.apply(null, series); max = Math.max.apply(null, series); if (min === max) { min -= 1; max += 1; } }
    const span = max - min;
    const cursor = svg.querySelector(".nw-spark-cursor");
    const dot = svg.querySelector(".nw-spark-dot");
    const at = (i) => {
      const x = 2 + (i / (n - 1)) * (w - 4);
      const y = (h - 2) - ((series[i] - min) / span) * (h - 4);
      return { x, y };
    };
    const show = (e) => {
      const rect = svg.getBoundingClientRect();
      const t = rect.width ? (e.clientX - rect.left) / rect.width : 1;
      const i = Math.max(0, Math.min(n - 1, Math.round(t * (n - 1))));
      if (valEl) valEl.textContent = cents(series[i]);
      if (whenEl) {
        whenEl.hidden = false;
        whenEl.textContent = sparkDate(dates[i]);
      }
      const p = at(i);
      if (cursor) {
        cursor.setAttribute("opacity", "0.35");
        cursor.setAttribute("x1", p.x.toFixed(1));
        cursor.setAttribute("x2", p.x.toFixed(1));
      }
      if (dot) {
        dot.setAttribute("opacity", "1");
        dot.setAttribute("cx", p.x.toFixed(1));
        dot.setAttribute("cy", p.y.toFixed(1));
      }
    };
    const reset = () => {
      if (valEl) valEl.textContent = orig;
      if (whenEl) { whenEl.hidden = true; whenEl.textContent = ""; }
      if (cursor) cursor.setAttribute("opacity", "0");
      if (dot) dot.setAttribute("opacity", "0");
    };
    svg.addEventListener("pointerenter", show);
    svg.addEventListener("pointermove", show);
    svg.addEventListener("pointerleave", reset);
  });
}

function openSpendCategory(cat) {
  const next = (cat || "Uncategorized").trim() || "Uncategorized";
  spendCatFocus = (spendCatFocus === next) ? "" : next;
  refresh();
}

function selectSpendMonth(ym) {
  const thisM = isoMonthStart(0).slice(0, 7);
  const lastM = isoMonthStart(-1).slice(0, 7);
  if (ym === thisM) {
    spendPeriod = "this_month";
  } else if (ym === lastM) {
    spendPeriod = "last_month";
  } else {
    spendPeriod = "custom";
    spendStart = `${ym}-01`;
    spendEnd = lastDayOfYm(ym);
  }
  refresh();
}

async function renderSpending() {
  const st = statusCache;
  const params = new URLSearchParams({ period: spendPeriod });
  if (spendPeriod === "custom") {
    if (spendStart) params.set("start", spendStart);
    if (spendEnd) params.set("end", spendEnd);
  }
  const d = await api("/api/spending?" + params.toString());
  spendPeriod = d.period || spendPeriod;
  if (d.start) spendStart = d.start;
  if (d.end) spendEnd = d.end;
  const total = d.total_spend_cents || 0;
  const prevTotal = d.prev_total_cents || 0;
  const cats = d.by_category || [];
  const vs = changeDelta(total, prevTotal, false);
  const selected = d.selected_month || "";

  const catRows = cats.map((r) => {
    const color = catColor(r.category);
    const on = spendCatFocus && (r.category || "Uncategorized") === spendCatFocus;
    return `<tr class="cat-hit${on ? " is-on" : ""}" data-key="${esc(r.category || "Uncategorized")}" tabindex="0" role="button">
      <td><span class="cat-dot" style="background:${color}"></span>${esc(prettyCat(r.category))}</td>
      <td class="num">${(r.pct || 0).toFixed(1)}%</td>
      ${changeCell(r.cents, r.prev_cents, r.change_pct, false)}
      <td class="num">${cents(r.cents)}</td>
    </tr>`;
  }).join("") || `<tr><td colspan="4" class="empty">No spending in this period.</td></tr>`;

  const nonSpend = [
    ["Internal Transfers", d.transfer_cents || 0],
    ["Ignored", d.ignored_cents || 0],
    ["Income", d.income_cents || 0],
    ["Tax Deductible", d.tax_deductible_cents || 0],
    ["Reimbursements", d.reimbursements_cents || 0],
  ];
  const nonRows = nonSpend.map(([label, amt]) => {
    const cls = label === "Income" ? "in" : "muted";
    return `<tr>
      <td>${esc(label)}</td>
      <td class="num ${cls}">${cents(amt)}</td>
    </tr>`;
  }).join("");

  const incomeDelta = changeDelta(d.income_cents || 0, d.prev_income_cents || 0, true);
  const billsDelta = changeDelta(d.bills_cents || 0, d.prev_bills_cents || 0, false);
  const spendDelta = vs;

  const freq = (d.frequent || []).map((f) => `
    <div class="acct">
      <div>
        <div class="nm">${esc(payeeName(f.merchant))}</div>
        <div class="meta">${f.n}× · avg ${cents(f.avg_cents)}</div>
      </div>
      <div class="bal">${cents(f.cents)}</div>
    </div>`).join("") || `<p class="empty">No repeat merchants this period.</p>`;

  const largest = (d.largest || []).map((t) => `
    <div class="acct">
      <div>
        <div class="nm">${esc(payeeName(t.payee))}</div>
        <div class="meta">${esc(t.date)} · ${esc(prettyCat(t.category))}</div>
      </div>
      <div class="bal out">${cents(t.amount_cents)}</div>
    </div>`).join("") || `<p class="empty">No purchases this period.</p>`;

  const rangeSub = `${esc(d.start || "")} → ${esc(d.end || "")}. Cash and cards only. Transfers, pending, and brokerage excluded.`;

  let catPanel = `<div class="panel">
          <h2>Categories</h2>
          <table class="spend-cat-table">
            <thead><tr>
              <th>Category</th>
              <th class="num">% Spend</th>
              <th class="num">Change vs prev</th>
              <th class="num">Amount</th>
            </tr></thead>
            <tbody>${catRows}</tbody>
          </table>
        </div>`;
  if (spendCatFocus) {
    const tp = new URLSearchParams({
      window: "all",
      category: spendCatFocus,
      spending_only: "1",
    });
    if (d.start) tp.set("start", d.start);
    if (d.end) tp.set("end", d.end);
    const td = await api("/api/transactions?" + tp.toString());
    const trows = (td.transactions || []).map((t) => {
      const cls = t.amount_cents > 0 ? "out" : (t.amount_cents < 0 ? "in" : "");
      const memo = t.memo ? `<div class="muted">${esc(t.memo)}</div>` : "";
      return `<tr>
        <td>${esc(t.date)}</td>
        <td>${esc(payeeName(t.payee))}${isDemo() ? "" : memo}</td>
        <td>
          <select class="spend-txn-cat" data-txn="${esc(t.transaction_id)}" data-payee="${esc(t.payee || "")}" data-orig="${esc(prettyCat(t.category_name || t.category))}" aria-label="Category">
            ${spendTagOpts(t.category_name || t.category)}
          </select>
        </td>
        <td class="num ${cls}">${signedCents(t.amount_cents)}</td>
      </tr>`;
    }).join("") || `<tr><td colspan="4" class="empty">No spending in ${esc(prettyCat(spendCatFocus))} this period.</td></tr>`;
    catPanel = `<div class="panel">
          <div class="panel-head">
            <h2>${esc(prettyCat(spendCatFocus))}</h2>
            <button class="btn ghost small" type="button" id="spend-cat-back">Categories</button>
          </div>
          <table class="spend-txn-table">
            <thead><tr><th>Date</th><th>Payee</th><th>Category</th><th class="num">Amount</th></tr></thead>
            <tbody>${trows}</tbody>
          </table>
        </div>`;
  }

  view.innerHTML = header("Spending", rangeSub, periodSeg(spendPeriod) + syncButton(st)) +
    setupBanner(st) +
    coverageLine(d.coverage, st) +
    `<div class="spend-layout">
      <div class="spend-main">
        <div class="panel spend-charts">
          <div>
            <h2>Last 6 months</h2>
            ${monthBarsSvg(d.by_month || [], selected)}
          </div>
          <div class="donut-wrap">
            ${donutSvg(cats, total, spendCatFocus)}
            <div class="donut-center">
              <div class="donut-lbl">TOTAL SPEND</div>
              <div class="donut-val">${cents(total)}</div>
              <div class="donut-delta ${vs.cls}">${esc(vs.text)}</div>
            </div>
          </div>
        </div>
        ${catPanel}
        <div class="panel">
          <h2>Not included in spending</h2>
          <table>
            <thead><tr><th>Type</th><th class="num">Amount</th></tr></thead>
            <tbody>${nonRows}</tbody>
          </table>
          <p class="note">Ignored is investment / Robinhood / retirement outflows. Income is cash and card inflows. Tax Deductible and Reimbursements are $0 until tagged in Settings.</p>
        </div>
      </div>
      <aside class="spend-rail">
        <div class="panel">
          <h2>Summary</h2>
          <div class="sum-row">
            <div>
              <div class="lbl">Income</div>
              <div class="delta ${incomeDelta.cls}">${esc(incomeDelta.text)}</div>
            </div>
            <div class="val in">${cents(d.income_cents)}</div>
          </div>
          <div class="sum-row">
            <div>
              <div class="lbl">Bills</div>
              <div class="delta ${billsDelta.cls}">${esc(billsDelta.text)}</div>
            </div>
            <div class="val">${cents(d.bills_cents)}</div>
          </div>
          <div class="sum-row">
            <div>
              <div class="lbl">Spending</div>
              <div class="delta ${spendDelta.cls}">${esc(spendDelta.text)}</div>
            </div>
            <div class="val">${cents(total)}</div>
          </div>
        </div>
        <div class="panel">
          <h2>Frequent spend</h2>
          ${freq}
        </div>
        <div class="panel">
          <h2>Largest purchases</h2>
          ${largest}
        </div>
      </aside>
    </div>
    <p class="note">More spend than the prior period is red; less is green. Uncategorized stays last in the table. Assign tags in Settings.</p>`;
  bindChrome();
  view.querySelectorAll("[data-seg=sp-period] button").forEach((b) => {
    b.addEventListener("click", () => {
      spendPeriod = b.dataset.p;
      if (spendPeriod === "custom" && !spendStart) {
        spendStart = isoMonthStart(0);
        spendEnd = isoToday();
      }
      refresh();
    });
  });
  const startEl = view.querySelector("#sp-start");
  const endEl = view.querySelector("#sp-end");
  const applyCustom = () => {
    spendPeriod = "custom";
    spendStart = (startEl && startEl.value) || spendStart;
    spendEnd = (endEl && endEl.value) || spendEnd;
    refresh();
  };
  if (startEl) startEl.addEventListener("change", applyCustom);
  if (endEl) endEl.addEventListener("change", applyCustom);
  const wrap = view.querySelector(".donut-wrap");
  const tip = view.querySelector(".donut-tip");
  const lbl = view.querySelector(".donut-lbl");
  const val = view.querySelector(".donut-val");
  const delta = view.querySelector(".donut-delta");
  const resetDonut = () => {
    if (lbl) lbl.textContent = "TOTAL SPEND";
    if (val) val.textContent = cents(total);
    if (delta) { delta.className = "donut-delta " + vs.cls; delta.textContent = vs.text; }
    if (tip) tip.hidden = true;
  };
  view.querySelectorAll(".donut-slice").forEach((p) => {
    const show = (e) => {
      if (lbl) lbl.textContent = p.dataset.cat || "";
      if (val) val.textContent = p.dataset.amt || "";
      if (delta) { delta.className = "donut-delta"; delta.textContent = (p.dataset.pct || "") + "% of spend"; }
      if (tip && wrap) {
        tip.hidden = false;
        tip.textContent = (p.dataset.cat || "") + " · " + (p.dataset.amt || "") + " · " + (p.dataset.pct || "") + "%";
        const rect = wrap.getBoundingClientRect();
        tip.style.left = Math.min(rect.width - 12, Math.max(8, e.clientX - rect.left + 12)) + "px";
        tip.style.top = Math.min(rect.height - 12, Math.max(8, e.clientY - rect.top + 12)) + "px";
      }
    };
    p.addEventListener("pointerenter", show);
    p.addEventListener("pointermove", show);
    p.addEventListener("pointerleave", resetDonut);
    p.addEventListener("click", () => openSpendCategory(p.dataset.key));
    p.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openSpendCategory(p.dataset.key); }
    });
  });
  view.querySelectorAll(".cat-hit").forEach((row) => {
    row.addEventListener("click", () => openSpendCategory(row.dataset.key));
    row.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openSpendCategory(row.dataset.key); }
    });
  });
  const back = view.querySelector("#spend-cat-back");
  if (back) back.addEventListener("click", () => { spendCatFocus = ""; refresh(); });
  view.querySelectorAll("select.spend-txn-cat").forEach((sel) => {
    sel.addEventListener("change", async () => {
      const cat = (sel.value || "").trim();
      const orig = sel.dataset.orig || "";
      if (!cat || cat === orig) return;
      sel.disabled = true;
      try {
        const payee = (sel.dataset.payee || "").trim();
        if (payee) {
          await api("/api/categories/apply", {
            method: "POST",
            body: JSON.stringify({ cluster_key: payee, category: cat }),
          });
        } else {
          await api("/api/categories/tag", {
            method: "POST",
            body: JSON.stringify({ transaction_id: sel.dataset.txn, category: cat }),
          });
        }
        await refresh();
      } catch (err) {
        alert((err.data && err.data.error) || err.message);
        sel.value = orig;
        sel.disabled = false;
      }
    });
  });
  view.querySelectorAll(".bar-hit").forEach((g) => {
    g.addEventListener("click", () => selectSpendMonth(g.dataset.month));
    g.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        selectSpendMonth(g.dataset.month);
      }
    });
  });
}

async function renderRecurring() {
  const st = statusCache;
  const d = await api("/api/recurring");
  const rows = (d.recurring || []).map((r) => `
    <tr>
      <td>${esc(payeeName(r.merchant))}</td>
      <td>${esc(r.frequency)}</td>
      <td>${esc(prettyCat(r.category))}</td>
      <td>${esc(r.next_date || "—")}</td>
      <td><span class="badge">${esc(r.status || "")}</span>
        <span class="badge">${esc(r.source || "")}</span>
        ${r.is_subscription ? `<span class="badge pending">subscription</span>` : ""}</td>
      <td class="num">${cents(r.average_cents)}</td>
      <td class="sug-actions">
        <button class="btn ghost" data-sub="${esc(r.stream_id)}" data-on="${r.is_subscription ? "0" : "1"}">
          ${r.is_subscription ? "Unmark" : "Mark subscription"}
        </button>
        <button class="btn ghost" data-ignore-rec="${esc(r.stream_id)}">Ignore</button>
      </td>
    </tr>`).join("") || `<tr><td colspan="7" class="empty">No recurring streams yet. Sync, then the local detector looks for regular payees.</td></tr>`;

  view.innerHTML = header("Recurring", "Local detector (same payee, similar amount, regular interval). Ignore drops a false match so it stays gone after Sync.",
      syncButton(st)) +
    setupBanner(st) +
    `<div class="panel">
      <table>
        <thead><tr><th>Payee</th><th>Cadence</th><th>Category</th><th>Next</th><th>Status</th><th class="num">Amount</th><th></th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;
  bindChrome();
  view.querySelectorAll("[data-sub]").forEach((b) => {
    b.addEventListener("click", async () => {
      await api("/api/recurring/" + encodeURIComponent(b.dataset.sub) + "/subscription", {
        method: "POST",
        body: JSON.stringify({ is_subscription: b.dataset.on === "1" }),
      });
      refresh();
    });
  });
  view.querySelectorAll("[data-ignore-rec]").forEach((b) => {
    b.addEventListener("click", async () => {
      b.disabled = true;
      try {
        await api("/api/recurring/" + encodeURIComponent(b.dataset.ignoreRec) + "/ignore", {
          method: "POST",
          body: "{}",
        });
        await refresh();
      } catch (err) {
        alert((err.data && err.data.error) || err.message);
        b.disabled = false;
      }
    });
  });
}

async function renderSettings() {
  const st = statusCache;
  const parsedTab = parseRoute().tab;
  let tab = (parsedTab === "app" || parsedTab === "rules" || parsedTab === "categories" || parsedTab === "accounts" || parsedTab === "household" || parsedTab === "properties")
    ? parsedTab : "accounts";
  if (tab !== "household") {
    hhAddingPartner = false;
    hhAddingKid = false;
  }

  let catData = { tags: [], clusters: [], rules: [], suggestions: [] };
  let acctData = { accounts: [], owners: [], categories: ACCT_CAT_ORDER.slice() };
  const hh = await loadHousehold();
  if (tab === "accounts" || tab === "properties") {
    acctData = await api("/api/accounts");
    if (acctData.household) cacheHousehold(acctData.household);
    if (!Array.isArray(acctData.properties)) {
      try {
        const pd = await api("/api/properties");
        acctData.properties = pd.properties || [];
        acctData.property_kinds = pd.kinds;
      } catch (e) {
        acctData.properties = [];
      }
    }
  } else if (tab === "categories" || tab === "rules") {
    catData = await api("/api/categories" + (catShowAll ? "?all=1" : ""));
  }
  const tags = catData.tags || [];
  const clusters = catData.clusters || [];
  const rules = catData.rules || [];
  const suggestions = catData.suggestions || [];
  const owners = (acctData.owners && acctData.owners.length)
    ? acctData.owners : (hh.owners || []);
  const acctCats = (acctData.categories && acctData.categories.length)
    ? acctData.categories : ACCT_CAT_ORDER.slice();
  const tagOpts = (selected) => tags.map((t) =>
    `<option value="${esc(t)}" ${t === selected ? "selected" : ""}>${esc(t)}</option>`
  ).join("");

  function canonical() {
    if (tab === "app") return "#/settings/app";
    if (tab === "rules") return "#/settings/rules";
    if (tab === "categories") return "#/settings/categories";
    if (tab === "household") return "#/settings/household";
    if (tab === "properties") return "#/settings/properties";
    if (location.hash === "#/settings/accounts") return "#/settings/accounts";
    return "#/settings";
  }
  function syncHash() {
    const next = canonical();
    if (location.hash !== next) history.replaceState(null, "", next);
  }

  async function patchAccount(id, body) {
    return api("/api/accounts/" + encodeURIComponent(id), {
      method: "PATCH",
      body: JSON.stringify(body),
    });
  }

  function suggestLabel(s) {
    const np = s.new_pattern || "";
    const drops = s.drop_patterns || [];
    if (s.action === "merge") {
      return `Keep ${np}` + (drops.length ? ` · drop ${drops.join(", ")}` : "");
    }
    if (s.action === "broaden") {
      return `${s.pattern} → ${np}`;
    }
    if (s.action === "drop") {
      return `Drop ${s.pattern}` + (np && np !== s.pattern ? ` · keep ${np}` : "");
    }
    return np || s.pattern || "";
  }

  function suggestionsPanel() {
    if (!suggestions.length) return "";
    const rows = suggestions.map((s) => `
      <tr>
        <td>
          <div class="sug-change">${esc(suggestLabel(s))}</div>
          <div class="muted">${esc(s.category || "")}</div>
        </td>
        <td>${esc(s.reason || "")}${s.n_extra ? ` <span class="muted">(+${s.n_extra})</span>` : ""}</td>
        <td class="sug-actions">
          <button class="btn" type="button" data-suggest="${esc(s.id)}">Apply</button>
          <button class="btn ghost" type="button" data-ignore="${esc(s.id)}">Ignore</button>
        </td>
      </tr>`).join("");
    return `<div class="panel">
      <h2>Suggestions</h2>
      <table>
        <thead><tr><th>Change</th><th>Reason</th><th></th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;
  }

  function clusterRows() {
    return clusters.map((c) => {
      const samples = (c.payees || []).map((p) => esc(p)).join(" · ");
      const suggested = c.suggested || "";
      const preset = suggested || c.current || "Uncategorized";
      return `<tr>
        <td>
          <div class="nm">${esc(c.key)}</div>
          <div class="muted">${samples}</div>
        </td>
        <td class="num">${c.n}</td>
        <td class="num">${cents(c.cents)}</td>
        <td>${esc(suggested || "—")}</td>
        <td>
          <div class="apply-row">
            <select data-key="${esc(c.key)}" data-current="${esc(preset)}">${tagOpts(preset)}</select>
            <input type="text" data-custom="${esc(c.key)}" placeholder="Custom tag" autocomplete="off">
          </div>
        </td>
      </tr>`;
    }).join("") || `<tr><td colspan="5" class="empty">No uncategorized spending clusters in cash/cards.</td></tr>`;
  }

  function categoriesBody() {
    return `<div class="panel">
      <h2>Payee clusters ${catShowAll ? "(all spending)" : "(uncategorized spending)"} — ${clusters.length}</h2>
      <label class="chk"><input type="checkbox" id="cat-all" ${catShowAll ? "checked" : ""}> Show all spending payees</label>
      <table>
        <thead><tr><th>Payee</th><th class="num">Count</th><th class="num">Total</th><th>Suggested</th><th>Tag</th></tr></thead>
        <tbody>${clusterRows()}</tbody>
      </table>
    </div>`;
  }

  function rulesBody() {
    const byCat = {};
    rules.forEach((r) => {
      const cat = r.category || "Uncategorized";
      (byCat[cat] = byCat[cat] || []).push(r);
    });
    const catNames = Object.keys(byCat).sort((a, b) => a.localeCompare(b));
    const groups = catNames.map((cat) => {
      const extra = (cat && !tags.includes(cat))
        ? `<option value="${esc(cat)}" selected>${esc(cat)}</option>` : "";
      const rows = byCat[cat].map((r) => `
        <div class="rule-pat">
          <input type="text" data-rule-pat="${esc(r.rule_id)}" data-orig="${esc(r.pattern)}" value="${esc(r.pattern)}" autocomplete="off">
          <select data-rule-cat="${esc(r.rule_id)}" data-orig="${esc(r.category)}" aria-label="Category">${extra}${tagOpts(cat)}</select>
          <span class="badge">${esc(r.source || "")}</span>
        </div>`).join("");
      return `<div class="panel rule-group">
        <h2>${esc(cat)} <span class="muted">${byCat[cat].length}</span></h2>
        ${rows}
        <div class="rule-pat add">
          <input type="text" data-add-pat="${esc(cat)}" placeholder="Add pattern" autocomplete="off">
        </div>
      </div>`;
    }).join("") || `<p class="empty">No rules yet.</p>`;
    return suggestionsPanel() + groups;
  }

  function ownerSeg(current) {
    const chips = [["all", "All"]].concat(owners.map((o) => [o, o === "Joint" ? "Joint" : ownerName(o)]));
    return `<div class="seg" data-seg="acct-owner">` +
      chips.map(([v, l]) =>
        `<button type="button" data-ow="${esc(v)}" aria-pressed="${v === current}">${esc(l)}</button>`
      ).join("") + `</div>`;
  }

  function visSeg(current) {
    const chips = [
      ["all", "All"],
      ["include", "Included"],
      ["exclude", "Excluded"],
      ["blueprint-off", "Off Net worth"],
    ];
    return `<div class="seg" data-seg="acct-vis">` +
      chips.map(([v, l]) =>
        `<button type="button" data-vis="${esc(v)}" aria-pressed="${v === current}">${esc(l)}</button>`
      ).join("") + `</div>`;
  }

  function visMatch(a) {
    if (acctVisFilter === "include") return !a.hidden;
    if (acctVisFilter === "exclude") return !!a.hidden;
    if (acctVisFilter === "blueprint-off") return !isOnBlueprint(a);
    return true;
  }

  function propertyLinkSelect(accountId, selectedId) {
    const props = acctData.properties || [];
    const sid = selectedId || "";
    const opts = `<option value="">None</option>` + props.map((p) =>
      `<option value="${esc(p.property_id)}" ${p.property_id === sid ? "selected" : ""}>${esc(isDemo() ? demoPropName(p) : p.name)}</option>`
    ).join("");
    return `<select data-prop-link="${esc(accountId)}" aria-label="Property">${opts}</select>`;
  }

  function dollarsFromCents(n) {
    if (n == null || Number.isNaN(Number(n))) return "";
    return (Math.abs(Number(n)) / 100).toFixed(2);
  }

  function accountRow(a) {
    const id = a.account_id;
    const hidden = !!a.hidden;
    const onBp = isOnBlueprint(a);
    const bank = a.name || "";
    const display = isDemo() ? acctName(a) : (a.display_name || bank);
    const nicknamed = !isDemo() && !!(display && bank && display !== bank);
    const manual = isManualId(id);
    const loan = a.loan;
    const badges = (hidden ? ' <span class="badge">Excluded</span>' : "") +
      (!onBp ? ' <span class="badge">Off Net worth</span>' : "");
    if (loan || manual) {
      const bits = [];
      if (loan) {
        bits.push("original " + esc(cents(loan.original)));
        if (loan.rate_pct != null) bits.push(esc(Number(loan.rate_pct).toFixed(2)) + "%");
        bits.push("P&amp;I " + esc(cents(loan.principal_cents)) + " / " + esc(cents(loan.interest_cents)));
        if ((a.category || "") === "Mortgage") bits.push("PITI " + esc(cents(loan.piti_cents)));
        if (loan.property_name) bits.push(esc(isDemo() ? demoPropName({ name: loan.property_name, property_id: loan.property_id }) : loan.property_name));
      }
      const delBtn = (!isDemo() && manual)
        ? `<button class="btn ghost small" type="button" data-del-acct="${esc(id)}">Delete</button>` : "";
      const editBtn = isDemo() ? "" :
        `<button class="btn ghost small" type="button" data-edit-acct="${esc(id)}">Edit</button>`;
      const loanCatSet = new Set(LOAN_CATS);
      const extraLoanCat = (a.category && !loanCatSet.has(a.category))
        ? `<option value="${esc(a.category)}" selected>${esc(a.category)}</option>` : "";
      const loanCatOpts = LOAN_CATS.map((c) =>
        `<option value="${esc(c)}" ${c === a.category ? "selected" : ""}>${esc(c)}</option>`
      ).join("");
      return `<div class="acct acct-edit ${hidden || !onBp ? "is-hidden" : ""}">
      <div>
        <div class="nm-row">
          <div class="nm">${esc(display)}</div>
          ${badges}
        </div>
        <div class="meta">${bits.join(" · ")}</div>
        <div class="acct-toggles">
          <label class="chk"><input type="checkbox" data-include-id="${esc(id)}" ${hidden ? "" : "checked"}> Include</label>
          <label class="chk"><input type="checkbox" data-bp-id="${esc(id)}" ${onBp ? "checked" : ""}> Net worth</label>
          ${editBtn}${delBtn}
        </div>
      </div>
      <div class="acct-prefs">
        <select data-cat-id="${esc(id)}" aria-label="Type">${extraLoanCat}${loanCatOpts}</select>
        <select data-owner-id="${esc(id)}" aria-label="Owner">${ownerSelectOpts(a.owner)}</select>
        <div class="${balanceClass(a)}">${accountBalanceLabel(a)}</div>
      </div>
    </div>`;
    }
    const catSet = new Set(acctCats);
    const extraCat = (a.category && !catSet.has(a.category))
      ? `<option value="${esc(a.category)}" selected>${esc(a.category)}</option>` : "";
    const catOpts = acctCats.map((c) =>
      `<option value="${esc(c)}" ${c === a.category ? "selected" : ""}>${esc(c)}</option>`
    ).join("");
    const ownerSet = new Set(owners);
    const extraOwner = (a.owner && !ownerSet.has(a.owner))
      ? `<option value="${esc(a.owner)}" selected>${esc(ownerOptionLabel(a.owner))}</option>` : "";
    const ownerOpts = owners.map((o) =>
      `<option value="${esc(o)}" ${o === a.owner ? "selected" : ""}>${esc(ownerOptionLabel(o))}</option>`
    ).join("");
    const nameInput = isDemo()
      ? `<div class="nm">${esc(display)}</div>`
      : `<input type="text" class="nm-input" data-nick-id="${esc(id)}" data-orig="${esc(display)}" data-bank="${esc(bank)}" value="${esc(display)}" aria-label="Account name" autocomplete="off">`;
    const meta = nicknamed ? esc(bank) : "";
    return `<div class="acct acct-edit ${hidden || !onBp ? "is-hidden" : ""}">
      <div>
        <div class="nm-row">
          ${nameInput}
          ${badges}
        </div>
        <div class="meta">${meta}</div>
        <div class="acct-toggles">
          <label class="chk"><input type="checkbox" data-include-id="${esc(id)}" ${hidden ? "" : "checked"}> Include</label>
          <label class="chk"><input type="checkbox" data-bp-id="${esc(id)}" ${onBp ? "checked" : ""}> Net worth</label>
          ${(a.category || "") === "Mortgage" ? `<label class="chk prop-link">Property ${propertyLinkSelect(id, a.property_id)}</label>` : ""}
        </div>
      </div>
      <div class="acct-prefs">
        <select data-cat-id="${esc(id)}" aria-label="Category">${extraCat}${catOpts}</select>
        <select data-owner-id="${esc(id)}" aria-label="Owner">${extraOwner}${ownerOpts}</select>
        <div class="${balanceClass(a)}">${accountBalanceLabel(a)}</div>
      </div>
    </div>`;
  }

  function accountsBody() {
    const all = acctData.accounts || [];
    const match = (a) => (acctOwnerFilter === "all" || a.owner === acctOwnerFilter) && visMatch(a);
    const open = all.filter((a) => !a.closed && match(a));
    const closed = all.filter((a) => a.closed && match(a));
    const groups = {};
    open.forEach((a) => {
      const k = a.category || "Other";
      (groups[k] = groups[k] || []).push(a);
    });
    const emptyMsg = st.simplefin_ready
      ? "No accounts match this filter."
      : "No accounts. Connect, then Sync.";
    const seen = new Set();
    const catKeys = [];
    ACCT_CAT_ORDER.forEach((k) => { if (groups[k] && groups[k].length) { catKeys.push(k); seen.add(k); } });
    Object.keys(groups).sort((a, b) => a.localeCompare(b)).forEach((k) => {
      if (!seen.has(k) && groups[k].length) catKeys.push(k);
    });
    const body = catKeys.map((k) => {
      const rows = groups[k].map(accountRow).join("");
      return `<div class="panel"><h2>${esc(k)}</h2>${rows}</div>`;
    }).join("") || `<p class="empty">${emptyMsg}</p>`;
    const closedBlock = closed.length
      ? `<div class="panel"><h2>Closed</h2>${closed.map(accountRow).join("")}</div>`
      : "";
    const addAcct = isDemo() ? "" : `<div class="add-menu" id="add-acct-menu">
      <button class="btn" type="button" id="add-acct-btn">Add account</button>
      <div class="add-menu-list" id="add-acct-list" hidden>
        <button type="button" id="open-add-loan">Loan</button>
      </div>
    </div>`;
    return `<div class="controls controls-split" style="margin-bottom:14px"><div class="controls">${ownerSeg(acctOwnerFilter)}${visSeg(acctVisFilter)}</div>${addAcct}</div>` +
      body + closedBlock +
      (householdData().joint_label
        ? `<p class="note">Include is household totals and transactions. Net worth is the family map. Kid-owned accounts default to Include off; Net worth is the family map and stays on unless you turn it off. Edit the account name in place; the bank name stays underneath. Joint accounts are labeled ${esc(householdData().joint_label)}.</p>`
        : `<p class="note">Include is household totals and transactions. Net worth is the family map. Kid-owned accounts default to Include off; Net worth is the family map and stays on unless you turn it off. Edit the account name in place; the bank name stays underneath.</p>`);
  }

  function ownerSelectOpts(selected) {
    const ownerSet = new Set(owners);
    const extra = (selected && !ownerSet.has(selected))
      ? `<option value="${esc(selected)}" selected>${esc(ownerOptionLabel(selected))}</option>` : "";
    return extra + owners.map((o) =>
      `<option value="${esc(o)}" ${o === selected ? "selected" : ""}>${esc(ownerOptionLabel(o))}</option>`
    ).join("");
  }

  function loanFormHtml(a) {
    const loan = (a && a.loan) || {};
    const selected = (a && a.category) || "Mortgage";
    const catOpts = LOAN_CATS.map((c) =>
      `<option value="${esc(c)}" ${c === selected ? "selected" : ""}>${esc(c)}</option>`
    ).join("");
    const props = acctData.properties || [];
    const pid = loan.property_id || "";
    const propOpts = `<option value="">None</option>` + props.map((p) =>
      `<option value="${esc(p.property_id)}" ${p.property_id === pid ? "selected" : ""}>${esc(isDemo() ? demoPropName(p) : p.name)}</option>`
    ).join("");
    const now = new Date();
    const ym = loan.originated_on || (now.getFullYear() + "-" + String(now.getMonth() + 1).padStart(2, "0"));
    const name = a ? (isDemo() ? acctName(a) : (a.display_name || a.name || "")) : "";
    const orig = a ? dollarsFromCents(loan.original) : "";
    const rate = (loan.rate_pct != null && loan.rate_pct !== "") ? String(loan.rate_pct) : "";
    const term = loan.term_years || 30;
    const bal = a ? dollarsFromCents(a.current_cents) : "";
    const submit = a ? "Save" : "Add loan";
    const typeOwner = a ? "" : `<label>Type <select name="category" id="loan-cat">${catOpts}</select></label>
        <label>Owner <select name="owner">${ownerSelectOpts()}</select></label>`;
    return `<form class="add-form" id="loan-form">
        <label${a ? ' class="span-2"' : ""}>Name <input type="text" name="name" required autocomplete="off" value="${esc(name)}"></label>
        ${typeOwner}
        <label>Original amount <input type="number" name="original" step="0.01" min="0" value="${esc(orig)}"></label>
        <label>Origination <input type="month" name="originated_on" value="${esc(ym)}" required></label>
        <label>Interest Rate % <input type="number" name="rate" step="0.01" min="0" value="${esc(rate)}"></label>
        <label>Term years <input type="number" name="term_years" min="1" max="40" value="${esc(String(term))}"></label>
        <label class="span-2">Current balance <input type="number" name="balance" step="0.01" min="0" value="${esc(bal)}"></label>
        <label id="loan-prop-wrap" class="span-2">Property <select name="property_id">${propOpts}</select></label>
        <button class="btn span-2" type="submit">${submit}</button>
      </form>`;
  }

  function bindLoanForm(modal, existing) {
    const form = modal.querySelector("#loan-form");
    if (!form) return;
    const catSel = form.querySelector("#loan-cat");
    const propWrap = modal.querySelector("#loan-prop-wrap");
    function currentCat() {
      if (existing) return existing.category || "";
      return (catSel && catSel.value) || "";
    }
    function syncProp() {
      if (propWrap) propWrap.hidden = currentCat() !== "Mortgage";
    }
    if (catSel) catSel.addEventListener("change", syncProp);
    syncProp();
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const fd = new FormData(form);
      const category = existing
        ? (existing.category || "")
        : String(fd.get("category") || "");
      const body = {
        name: String(fd.get("name") || "").trim(),
        original: fd.get("original") === "" ? 0 : Number(fd.get("original")),
        originated_on: String(fd.get("originated_on") || ""),
        rate: fd.get("rate") === "" ? 0 : Number(fd.get("rate")),
        term_years: Number(fd.get("term_years") || 30),
        balance: fd.get("balance") === "" ? 0 : Number(fd.get("balance")),
      };
      if (!existing) {
        body.category = category;
        body.owner = String(fd.get("owner") || "");
      }
      if (category === "Mortgage") {
        const pid = String(fd.get("property_id") || "").trim();
        body.property_id = pid || null;
      } else if (existing) {
        body.property_id = null;
      }
      if (!existing) body.kind = "loan";
      const btn = form.querySelector("button[type=submit]");
      if (btn) btn.disabled = true;
      try {
        if (existing) {
          await patchAccount(existing.account_id, body);
        } else {
          await api("/api/accounts/manual", { method: "POST", body: JSON.stringify(body) });
        }
        closeWalnutModal();
        await refresh();
      } catch (err) {
        alert((err.data && err.data.error) || err.message);
        if (btn) btn.disabled = false;
      }
    });
  }

  function openAccountEditor(id) {
    const a = (acctData.accounts || []).find((x) => x.account_id === id);
    if (!a || !(a.loan || isManualId(id))) return;
    const modal = openWalnutModal("Edit loan", loanFormHtml(a));
    bindLoanForm(modal, a);
  }

  function bindAccountEditors() {
    view.querySelectorAll("[data-seg=acct-owner] button").forEach((b) => {
      b.addEventListener("click", () => { acctOwnerFilter = b.dataset.ow; refresh(); });
    });
    view.querySelectorAll("[data-seg=acct-vis] button").forEach((b) => {
      b.addEventListener("click", () => { acctVisFilter = b.dataset.vis; refresh(); });
    });
    view.querySelectorAll("[data-owner-id]").forEach((sel) => {
      sel.addEventListener("change", async () => {
        try {
          const owner = sel.value;
          const kidNames = (householdData().kids || []).map((k) => k.name).filter(Boolean);
          const isKid = kidNames.some((n) => n === owner);
          const row = sel.closest(".acct");
          const includeBox = row && row.querySelector("[data-include-id]");
          const included = !!(includeBox && includeBox.checked);
          const body = (isKid && included) ? { owner, hidden: 1 } : { owner };
          await patchAccount(sel.dataset.ownerId, body);
          await refresh();
        } catch (err) {
          alert((err.data && err.data.error) || err.message);
        }
      });
    });
    view.querySelectorAll("[data-cat-id]").forEach((sel) => {
      sel.addEventListener("change", async () => {
        try {
          const body = { category: sel.value };
          if (sel.value !== "Mortgage") body.property_id = null;
          await patchAccount(sel.dataset.catId, body);
          await refresh();
        } catch (err) {
          alert((err.data && err.data.error) || err.message);
        }
      });
    });
    view.querySelectorAll("[data-prop-link]").forEach((sel) => {
      sel.addEventListener("change", async () => {
        try {
          const pid = String(sel.value || "").trim();
          await patchAccount(sel.dataset.propLink, { property_id: pid || null });
          await refresh();
        } catch (err) {
          alert((err.data && err.data.error) || err.message);
        }
      });
    });
    view.querySelectorAll("[data-include-id]").forEach((box) => {
      box.addEventListener("change", async () => {
        try {
          await patchAccount(box.dataset.includeId, { hidden: box.checked ? 0 : 1 });
          await refresh();
        } catch (err) {
          alert((err.data && err.data.error) || err.message);
          box.checked = !box.checked;
        }
      });
    });
    view.querySelectorAll("[data-bp-id]").forEach((box) => {
      box.addEventListener("change", async () => {
        try {
          await patchAccount(box.dataset.bpId, { on_blueprint: box.checked ? 1 : 0 });
          await refresh();
        } catch (err) {
          alert((err.data && err.data.error) || err.message);
          box.checked = !box.checked;
        }
      });
    });
    async function saveName(id, input) {
      const val = (input && input.value || "").trim();
      const orig = (input && input.dataset.orig) || "";
      const bank = (input && input.dataset.bank) || "";
      if (val === orig) return;
      const nickname = (!val || val === bank) ? "" : val;
      try {
        await patchAccount(id, { nickname });
        await refresh();
      } catch (err) {
        alert((err.data && err.data.error) || err.message);
        if (input) input.value = orig;
      }
    }
    view.querySelectorAll("[data-nick-id]").forEach((inp) => {
      inp.addEventListener("blur", () => saveName(inp.dataset.nickId, inp));
      inp.addEventListener("keydown", (e) => {
        if (e.key === "Enter") { e.preventDefault(); inp.blur(); }
      });
    });
    view.querySelectorAll("[data-edit-acct]").forEach((btn) => {
      btn.addEventListener("click", () => openAccountEditor(btn.dataset.editAcct));
    });
    view.querySelectorAll("[data-del-acct]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const id = btn.dataset.delAcct;
        if (!isManualId(id)) return;
        if (!window.confirm("Delete this loan?")) return;
        btn.disabled = true;
        try {
          await api("/api/accounts/" + encodeURIComponent(id), { method: "DELETE" });
          await refresh();
        } catch (err) {
          alert((err.data && err.data.error) || err.message);
          btn.disabled = false;
        }
      });
    });
    const addAcctBtn = view.querySelector("#add-acct-btn");
    const addAcctList = view.querySelector("#add-acct-list");
    if (addAcctBtn && addAcctList) {
      addAcctBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        const willOpen = addAcctList.hidden;
        addAcctList.hidden = !addAcctList.hidden;
        if (willOpen) {
          setTimeout(() => {
            document.addEventListener("click", () => { addAcctList.hidden = true; }, { once: true });
          }, 0);
        }
      });
    }
    const openLoan = view.querySelector("#open-add-loan");
    if (openLoan) {
      openLoan.addEventListener("click", () => {
        if (addAcctList) addAcctList.hidden = true;
        const modal = openWalnutModal("Add loan", loanFormHtml(null));
        bindLoanForm(modal, null);
      });
    }
  }

  function propertyKinds() {
    const k = acctData.property_kinds;
    return (k && k.length) ? k : PROP_KINDS.slice();
  }

  function propertyFormHtml(p) {
    const kinds = propertyKinds();
    const selected = (p && p.kind) || kinds[0] || "";
    const kindSet = new Set(kinds);
    const extraKind = (selected && !kindSet.has(selected))
      ? `<option value="${esc(selected)}" selected>${esc(selected)}</option>` : "";
    const kindOpts = extraKind + kinds.map((k) =>
      `<option value="${esc(k)}" ${k === selected ? "selected" : ""}>${esc(k)}</option>`
    ).join("");
    const name = p ? (isDemo() ? demoPropName(p) : (p.name || "")) : "";
    const val = p ? dollarsFromCents(p.value_cents) : "";
    const tax = p ? dollarsFromCents(p.tax_cents) : "";
    const ins = p ? dollarsFromCents(p.insurance_cents) : "";
    const hoa = p ? dollarsFromCents(p.hoa_cents) : "";
    const submit = p ? "Save" : "Add property";
    return `<form class="add-form" id="prop-form">
        <label>Name <input type="text" name="name" required autocomplete="off" value="${esc(name)}"></label>
        <label>Kind <select name="kind">${kindOpts}</select></label>
        <label>Estimated value <input type="number" name="value" step="0.01" min="0" value="${esc(val)}"></label>
        <label>Owner <select name="owner">${ownerSelectOpts(p && p.owner)}</select></label>
        <label>Monthly tax <input type="number" name="tax" step="0.01" min="0" value="${esc(tax)}"></label>
        <label>Monthly insurance <input type="number" name="insurance" step="0.01" min="0" value="${esc(ins)}"></label>
        <label class="span-2">Monthly HOA <input type="number" name="hoa" step="0.01" min="0" value="${esc(hoa)}"></label>
        <button class="btn span-2" type="submit">${submit}</button>
      </form>`;
  }

  function propertyRow(p) {
    const id = p.property_id;
    const display = isDemo() ? demoPropName(p) : (p.name || "");
    const bits = [];
    if (p.owner) bits.push(esc(ownerName(p.owner)));
    if (Number(p.tax_cents)) bits.push("tax " + esc(cents(p.tax_cents)) + "/mo");
    if (Number(p.insurance_cents)) bits.push("insurance " + esc(cents(p.insurance_cents)) + "/mo");
    if (Number(p.hoa_cents)) bits.push("HOA " + esc(cents(p.hoa_cents)) + "/mo");
    const editBtn = isDemo() ? "" :
      `<button class="btn ghost small" type="button" data-edit-prop="${esc(id)}">Edit</button>`;
    const delBtn = isDemo() ? "" :
      `<button class="btn ghost small" type="button" data-del-prop="${esc(id)}">Delete</button>`;
    return `<div class="acct acct-edit">
      <div>
        <div class="nm-row">
          <div class="nm">${esc(display)}</div>
        </div>
        <div class="meta">${bits.join(" · ")}</div>
      </div>
      <div class="acct-prefs">
        ${editBtn}${delBtn}
        <div class="bal">${cents(p.value_cents)}</div>
      </div>
    </div>`;
  }

  function propertiesBody() {
    const all = acctData.properties || [];
    const kinds = propertyKinds();
    const groups = {};
    all.forEach((p) => {
      const k = p.kind || "Other";
      (groups[k] = groups[k] || []).push(p);
    });
    const catKeys = [];
    kinds.forEach((k) => { if (groups[k] && groups[k].length) catKeys.push(k); });
    Object.keys(groups).forEach((k) => {
      if (!catKeys.includes(k) && groups[k].length) catKeys.push(k);
    });
    const body = catKeys.map((k) => {
      const rows = groups[k].map(propertyRow).join("");
      return `<div class="panel"><h2>${esc(k)}</h2>${rows}</div>`;
    }).join("") || `<p class="empty">No properties yet. Add each home, rental, vacation place, or land so a mortgage can point at it.</p>`;
    const addBtn = isDemo() ? "" : `<div class="controls controls-end" style="margin-bottom:14px"><button class="btn" type="button" id="open-add-prop">Add property</button></div>`;
    return addBtn + body;
  }

  async function patchProperty(id, body) {
    return api("/api/properties/" + encodeURIComponent(id), {
      method: "PATCH",
      body: JSON.stringify(body),
    });
  }

  function bindPropForm(modal, existing) {
    const form = modal.querySelector("#prop-form");
    if (!form) return;
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const fd = new FormData(form);
      const body = {
        name: String(fd.get("name") || "").trim(),
        kind: String(fd.get("kind") || ""),
        value: fd.get("value") === "" ? 0 : Number(fd.get("value")),
        owner: String(fd.get("owner") || ""),
        tax: fd.get("tax") === "" ? 0 : Number(fd.get("tax")),
        insurance: fd.get("insurance") === "" ? 0 : Number(fd.get("insurance")),
        hoa: fd.get("hoa") === "" ? 0 : Number(fd.get("hoa")),
      };
      const btn = form.querySelector("button[type=submit]");
      if (btn) btn.disabled = true;
      try {
        if (existing) {
          await patchProperty(existing.property_id, body);
        } else {
          await api("/api/properties", { method: "POST", body: JSON.stringify(body) });
        }
        closeWalnutModal();
        await refresh();
      } catch (err) {
        alert((err.data && err.data.error) || err.message);
        if (btn) btn.disabled = false;
      }
    });
  }

  function bindPropertyEditors() {
    view.querySelectorAll("[data-edit-prop]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const p = (acctData.properties || []).find((x) => x.property_id === btn.dataset.editProp);
        if (!p) return;
        const modal = openWalnutModal("Edit property", propertyFormHtml(p));
        bindPropForm(modal, p);
      });
    });
    view.querySelectorAll("[data-del-prop]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        if (!window.confirm("Delete this property?")) return;
        btn.disabled = true;
        try {
          await api("/api/properties/" + encodeURIComponent(btn.dataset.delProp), { method: "DELETE" });
          await refresh();
        } catch (err) {
          alert((err.data && err.data.error) || err.message);
          btn.disabled = false;
        }
      });
    });
    const openProp = view.querySelector("#open-add-prop");
    if (openProp) {
      openProp.addEventListener("click", () => {
        const modal = openWalnutModal("Add property", propertyFormHtml(null));
        bindPropForm(modal, null);
      });
    }
  }

  function appBody() {
    const connected = !!(st && st.simplefin_ready);
    return `<div class="panel">
      <h2>Demo mode</h2>
      <label class="chk"><input type="checkbox" id="demo-mode" ${isDemo() ? "checked" : ""}> Redact amounts and names</label>
      <p class="note">For screenshots. Household net worth is scaled to about $1.2M. Names and payees are generic. Categories stay. Stored only in this browser. Turn off here if you need real numbers. The banner will not.</p>
    </div>
    <div class="panel">
      <h2>SimpleFIN setup token</h2>
      <p class="note">${connected
        ? "Connected. Paste a new setup token to replace the stored Access URL. The previous token cannot be reused."
        : "Not connected. Create a setup token, paste it, then Connect."}</p>
      <p><a href="${SFIN_CREATE}" target="_blank" rel="noopener">Create a setup token</a></p>
      <textarea id="sfin-token" rows="3" placeholder="Paste setup token" autocomplete="off" spellcheck="false"></textarea>
      <button class="btn" type="button" data-act="claim">${connected ? "Update token" : "Connect"}</button>
    </div>`;
  }

  function personInput(p, canRemove) {
    if (isDemo()) {
      return `<div class="rule-pat">
        <div class="nm">${esc(ownerName(p.name))}</div>
      </div>`;
    }
    return `<div class="rule-pat">
      <input type="text" data-person-id="${p.id}" data-orig="${esc(p.name)}" value="${esc(p.name)}" autocomplete="off" aria-label="Name">
      ${canRemove ? `<button class="btn ghost" type="button" data-del-person="${p.id}">Remove</button>` : ""}
    </div>`;
  }

  function householdBody() {
    const data = householdData();
    const selfP = data.self;
    const partner = data.partner;
    const kids = data.kids || [];
    const you = selfP
      ? personInput(selfP, false)
      : `<p class="empty">No self person. Sync or reload.</p>`;
    let partnerBlock;
    if (partner) {
      partnerBlock = personInput(partner, true);
    } else if (isDemo()) {
      partnerBlock = `<p class="empty">No partner.</p>`;
    } else if (hhAddingPartner) {
      partnerBlock = `<div class="rule-pat add">
        <input type="text" data-add-role="partner" placeholder="Partner name" autocomplete="off" aria-label="Partner name">
      </div>`;
    } else {
      partnerBlock = `<button class="btn" type="button" data-add-partner>Add partner</button>`;
    }
    const kidRows = kids.map((k) => personInput(k, true)).join("");
    let kidAdd = "";
    if (!isDemo()) {
      if (hhAddingKid) {
        kidAdd = `<div class="rule-pat add">
          <input type="text" data-add-role="kid" placeholder="Kid name" autocomplete="off" aria-label="Kid name">
        </div>`;
      } else {
        kidAdd = `<div class="rule-pat add"><button class="btn" type="button" data-add-kid>Add kid</button></div>`;
      }
    }
    return `<div class="panel">
      <h2>You</h2>
      ${you}
    </div>
    <div class="panel">
      <h2>Partner</h2>
      ${partnerBlock}
    </div>
    <div class="panel">
      <h2>Kids</h2>
      ${kidRows || (isDemo() ? `<p class="empty">No kids.</p>` : "")}
      ${kidAdd}
    </div>
    <p class="note">Joint accounts are labeled Joint (You &amp; Partner). Kids stay off household totals unless Include is on.</p>`;
  }

  function bindHouseholdEditors() {
    async function savePerson(id, input) {
      const val = (input && input.value || "").trim();
      const orig = (input && input.dataset.orig) || "";
      if (val === orig) return;
      if (!val) { if (input) input.value = orig; return; }
      try {
        cacheHousehold(await api("/api/household/people/" + encodeURIComponent(id), {
          method: "PATCH",
          body: JSON.stringify({ name: val }),
        }));
        await refresh();
      } catch (err) {
        alert((err.data && err.data.error) || err.message);
        if (input) input.value = orig;
      }
    }
    view.querySelectorAll("[data-person-id]").forEach((inp) => {
      inp.addEventListener("blur", () => savePerson(inp.dataset.personId, inp));
      inp.addEventListener("keydown", (e) => {
        if (e.key === "Enter") { e.preventDefault(); inp.blur(); }
      });
    });
    view.querySelectorAll("[data-del-person]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        btn.disabled = true;
        try {
          cacheHousehold(await api("/api/household/people/" + encodeURIComponent(btn.dataset.delPerson), {
            method: "DELETE",
          }));
          hhAddingPartner = false;
          hhAddingKid = false;
          await refresh();
        } catch (err) {
          alert((err.data && err.data.error) || err.message);
          btn.disabled = false;
        }
      });
    });
    async function addPerson(role, input) {
      const val = (input && input.value || "").trim();
      if (!val) {
        if (role === "partner") hhAddingPartner = false;
        if (role === "kid") hhAddingKid = false;
        paintBody();
        return;
      }
      if (input) input.disabled = true;
      try {
        cacheHousehold(await api("/api/household/people", {
          method: "POST",
          body: JSON.stringify({ name: val, role }),
        }));
        hhAddingPartner = false;
        hhAddingKid = false;
        await refresh();
      } catch (err) {
        alert((err.data && err.data.error) || err.message);
        if (input) { input.disabled = false; input.focus(); }
      }
    }
    const addPartnerBtn = view.querySelector("[data-add-partner]");
    if (addPartnerBtn) {
      addPartnerBtn.addEventListener("click", () => {
        hhAddingPartner = true;
        paintBody();
      });
    }
    const addKidBtn = view.querySelector("[data-add-kid]");
    if (addKidBtn) {
      addKidBtn.addEventListener("click", () => {
        hhAddingKid = true;
        paintBody();
      });
    }
    view.querySelectorAll("[data-add-role]").forEach((inp) => {
      inp.addEventListener("keydown", (e) => {
        if (e.key === "Enter") { e.preventDefault(); inp.blur(); }
        if (e.key === "Escape") {
          e.preventDefault();
          inp.value = "";
          inp.blur();
        }
      });
      inp.addEventListener("blur", () => addPerson(inp.dataset.addRole, inp));
      inp.focus();
    });
  }

  function paintBody() {
    const body = view.querySelector("#settings-body");
    if (tab === "app") {
      body.innerHTML = appBody();
      bindChrome();
      const box = view.querySelector("#demo-mode");
      if (box) box.addEventListener("change", () => {
        if (!box.checked) {
          const ok = window.confirm("Turn off demo mode? Real names and amounts will show on screen.");
          if (!ok) { box.checked = true; return; }
        }
        setDemo(box.checked);
        refresh();
      });
      return;
    }
    if (tab === "household") {
      body.innerHTML = householdBody();
      bindHouseholdEditors();
      return;
    }
    if (tab === "accounts") {
      body.innerHTML = accountsBody();
      bindAccountEditors();
      return;
    }
    if (tab === "properties") {
      body.innerHTML = propertiesBody();
      bindPropertyEditors();
      return;
    }
    body.innerHTML = tab === "rules" ? rulesBody() : categoriesBody();
    const allBox = view.querySelector("#cat-all");
    if (allBox) {
      allBox.addEventListener("change", () => { catShowAll = allBox.checked; refresh(); });
    }
    async function applyCluster(key, category, ctl) {
      category = (category || "").trim();
      if (!category) return;
      const sel = view.querySelector(`select[data-key="${CSS.escape(key)}"]`);
      if (sel && category === (sel.dataset.current || "").trim()) return;
      if (ctl) ctl.disabled = true;
      try {
        await api("/api/categories/apply", {
          method: "POST",
          body: JSON.stringify({ cluster_key: key, category }),
        });
        await refresh();
      } catch (err) {
        alert((err.data && err.data.error) || err.message);
        if (ctl) ctl.disabled = false;
      }
    }
    view.querySelectorAll("select[data-key]").forEach((sel) => {
      sel.addEventListener("change", () => applyCluster(sel.dataset.key, sel.value, sel));
    });
    view.querySelectorAll("[data-custom]").forEach((inp) => {
      const go = () => applyCluster(inp.dataset.custom, inp.value, inp);
      inp.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); go(); } });
      inp.addEventListener("blur", go);
    });
    view.querySelectorAll("[data-suggest]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        btn.disabled = true;
        try {
          await api("/api/categories/suggest/apply", {
            method: "POST",
            body: JSON.stringify({ id: btn.dataset.suggest }),
          });
          await refresh();
        } catch (err) {
          alert((err.data && err.data.error) || err.message);
          btn.disabled = false;
        }
      });
    });
    view.querySelectorAll("[data-ignore]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        btn.disabled = true;
        try {
          await api("/api/categories/suggest/ignore", {
            method: "POST",
            body: JSON.stringify({ id: btn.dataset.ignore }),
          });
          await refresh();
        } catch (err) {
          alert((err.data && err.data.error) || err.message);
          btn.disabled = false;
        }
      });
    });
    view.querySelectorAll("[data-add-pat]").forEach((inp) => {
      const go = async () => {
        const val = (inp.value || "").trim();
        if (!val) return;
        inp.disabled = true;
        try {
          await api("/api/categories/apply", {
            method: "POST",
            body: JSON.stringify({ pattern: val, category: inp.dataset.addPat }),
          });
          await refresh();
        } catch (err) {
          alert((err.data && err.data.error) || err.message);
          inp.disabled = false;
        }
      };
      inp.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); go(); } });
      inp.addEventListener("blur", go);
    });
    async function patchRule(id, body, ctl) {
      if (ctl) ctl.disabled = true;
      try {
        await api("/api/categories/rules/" + encodeURIComponent(id), {
          method: "PATCH",
          body: JSON.stringify(body),
        });
        await refresh();
      } catch (err) {
        alert((err.data && err.data.error) || err.message);
        if (ctl) ctl.disabled = false;
      }
    }
    view.querySelectorAll("[data-rule-cat]").forEach((sel) => {
      sel.addEventListener("change", () => {
        if (sel.value === sel.dataset.orig) return;
        patchRule(sel.dataset.ruleCat, { category: sel.value }, sel);
      });
    });
    view.querySelectorAll("[data-rule-pat]").forEach((inp) => {
      const go = () => {
        const val = (inp.value || "").trim();
        if (!val || val === inp.dataset.orig) return;
        patchRule(inp.dataset.rulePat, { pattern: val }, inp);
      };
      inp.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); inp.blur(); } });
      inp.addEventListener("blur", go);
    });
  }

  const sub = tab === "app" ? "connection and demo mode"
    : (tab === "household" ? "you, partner, kids"
    : (tab === "properties" ? "register homes and land, then attach a mortgage"
    : (tab === "accounts" ? "nickname, owner, category, include in household" : "payee tags for cash/cards")));
  view.innerHTML = header("Settings", sub, syncButton(st)) +
    (tab === "app" ? "" : setupBanner(st)) +
    `<div class="tabs" id="settings-tabs" role="tablist" aria-label="Settings">
      <button type="button" role="tab" data-t="app" aria-selected="${tab === "app"}">App</button>
      <button type="button" role="tab" data-t="household" aria-selected="${tab === "household"}">Household</button>
      <button type="button" role="tab" data-t="properties" aria-selected="${tab === "properties"}">Properties</button>
      <button type="button" role="tab" data-t="accounts" aria-selected="${tab === "accounts"}">Accounts</button>
      <button type="button" role="tab" data-t="categories" aria-selected="${tab === "categories"}">Categories</button>
      <button type="button" role="tab" data-t="rules" aria-selected="${tab === "rules"}">Rules</button>
    </div>
    <div id="settings-body"></div>`;
  bindChrome();
  view.querySelector("#settings-tabs").addEventListener("click", (e) => {
    const btn = e.target.closest("button");
    if (!btn) return;
    const nextTab = btn.dataset.t;
    let next = "#/settings";
    if (nextTab === "app") next = "#/settings/app";
    else if (nextTab === "household") next = "#/settings/household";
    else if (nextTab === "properties") next = "#/settings/properties";
    else if (nextTab === "rules") next = "#/settings/rules";
    else if (nextTab === "categories") next = "#/settings/categories";
    else if (nextTab === "accounts") next = "#/settings";
    if (location.hash !== next) location.hash = next;
  });
  paintBody();
  syncHash();
}


async function renderRetirement() {
  const st = statusCache;
  const parsed = parseRoute();
  const tab = parsed.tab === "blueprint" ? "blueprint" : "networth";
  const [data, hh] = await Promise.all([api("/api/accounts"), loadHousehold()]);
  const properties = data.properties || [];
  if (!data.properties) {
    try {
      const pd = await api("/api/properties");
      properties.push.apply(properties, pd.properties || []);
    } catch (e) {}
  }
  const accounts = (data.accounts || []).concat(properties.map((p) => ({
    account_id: p.property_id,
    name: p.name,
    display_name: p.name,
    category: p.kind,
    current_cents: p.value_cents || 0,
    owner: p.owner,
    hidden: 0,
    on_blueprint: 1,
    group_key: "other",
    closed: 0,
  })));
  const nw = data.net_worth || {};
  const selfName = (hh.self && hh.self.name) || "";
  const partnerName = (hh.partner && hh.partner.name) || "";
  const kidNames = (hh.kids || []).map((k) => k.name).filter(Boolean);

  function openIncluded() {
    return accounts.filter((a) => !a.closed && !a.hidden);
  }
  function centsPlain(n) {
    if (n == null || Number.isNaN(n)) return "—";
    if (isDemo()) n = demoAmount(n);
    const sign = n < 0 ? "−" : "";
    return sign + "$" + Math.round(Math.abs(n) / 100).toLocaleString("en-US");
  }
  function bpTone(a) {
    const cat = a.category || "";
    if (cat === "Credit card" || a.group_key === "cards" || a.group_key === "loans") return "debt";
    return "bank";
  }
  const BP_CLUSTER_ORDER = [
    "Checking", "Savings", "Credit cards", "Taxable", "Robinhood",
    "401(k)", "Roth IRA", "Traditional IRA", "HSA", "Crypto",
    "Properties", "Other",
  ];
  function bpClusterKey(a) {
    const n = ((a.display_name || "") + " " + (a.name || "")).toLowerCase();
    if (n.includes("robinhood")) return "Robinhood";
    if ((a.category || "") === "Credit card" || a.group_key === "cards") return "Credit cards";
    if (["Primary Home", "Vacation Home", "Investment Rental", "Land"].includes(a.category || "")) return "Properties";
    return a.category || "Other";
  }
  function bpLineName(a, cluster) {
    if (isDemo()) return acctName(a);
    let name = a.display_name || a.name || "";
    if (cluster === "Robinhood") {
      name = name.replace(/^Robinhood\s+/i, "");
      if (name) name = name.charAt(0).toUpperCase() + name.slice(1);
    }
    return name;
  }
  function clusterize(list) {
    const groups = {};
    list.forEach((a) => {
      const k = bpClusterKey(a);
      (groups[k] = groups[k] || []).push(a);
    });
    const keys = [];
    const seen = new Set();
    BP_CLUSTER_ORDER.forEach((k) => { if (groups[k] && groups[k].length) { keys.push(k); seen.add(k); } });
    Object.keys(groups).sort().forEach((k) => { if (!seen.has(k)) keys.push(k); });
    return keys.map((k) => ({ key: k, items: sortAccts(groups[k]) }));
  }
  function clusterAmount(items) {
    const n = sumCents(items);
    return cents(n);
  }
  function isPropRow(a) {
    return String(a.account_id || "").startsWith("prop:");
  }
  function closingCostCents(a) {
    const v = Number(a.current_cents || 0);
    if (v <= 0) return 0;
    return Math.round(v * 0.07);
  }
  function closingLabel(n) {
    if (!n) return "";
    return "−" + cents(n).replace(/^−/, "");
  }
  function closingTip(n) {
    return "Sale typically costs about 7% (" + closingLabel(n) + "). Not subtracted from net worth.";
  }
  function wrapTip(inner, tip) {
    return `<span class="tip-wrap" tabindex="0">${inner}<span class="tip" role="tooltip">${esc(tip)}</span></span>`;
  }
  function balCell(a) {
    const main = `<div class="${balanceClass(a)}">${esc(accountBalanceLabel(a))}</div>`;
    if (!isPropRow(a)) return main;
    const c = closingCostCents(a);
    if (!c) return main;
    return wrapTip(main, closingTip(c));
  }
  function clusterBal(items) {
    const main = `<div class="bal">${esc(clusterAmount(items))}</div>`;
    if (!items.some(isPropRow)) return main;
    const c = items.reduce((n, a) => n + closingCostCents(a), 0);
    if (!c) return main;
    return wrapTip(main, closingTip(c));
  }
  function bpClusterCard(cluster) {
    const items = cluster.items;
    const tone = items.every((a) => bpTone(a) === "debt") ? "debt" : "bank";
    const allEx = items.length && items.every((a) => a.hidden);
    if (items.length === 1) {
      const a = items[0];
      return `<div class="bp-cluster solo tone-${bpTone(a)}${a.hidden ? " is-hidden" : ""}">
        <div class="bp-cluster-head">
          <div class="nm">${esc(acctName(a))}</div>
          ${balCell(a)}
        </div>
      </div>`;
    }
    const rows = items.map((a) => {
      const excluded = !!a.hidden;
      return `<div class="bp-line${excluded ? " is-hidden" : ""}">
        <div class="nm">${esc(bpLineName(a, cluster.key))}</div>
        ${balCell(a)}
      </div>`;
    }).join("");
    return `<div class="bp-cluster tone-${tone}${allEx ? " is-hidden" : ""}">
      <div class="bp-cluster-head">
        <div class="nm">${esc(cluster.key)}</div>
        ${clusterBal(items)}
      </div>
      ${rows}
    </div>`;
  }
  function sortAccts(list) {
    const rank = (a) => {
      const i = ACCT_CAT_ORDER.indexOf(a.category);
      return i < 0 ? 99 : i;
    };
    return list.slice().sort((a, b) => {
      const d = rank(a) - rank(b);
      if (d) return d;
      return String(a.display_name || a.name || "").localeCompare(String(b.display_name || b.name || ""));
    });
  }

  function skipSavings(a) {
    return (a.category || "") !== "Savings";
  }
  function sumCents(list) {
    return list.reduce((n, a) => n + (a.current_cents || 0), 0);
  }
  function ownerLabel(o) {
    return ownerName(o);
  }

  function netWorthBody() {
    const open = openIncluded().filter(skipSavings);
    const isLiab = (a) => bpTone(a) === "debt" || (a.current_cents || 0) < 0;
    const assetList = open.filter((a) => !isLiab(a));
    const liabList = open.filter(isLiab);
    const assets = sumCents(assetList);
    const liabs = sumCents(liabList);
    const total = assets + liabs;
    function typeTotalLabel(n) {
      return cents(n);
    }
    function sideBody(list, empty) {
      const clusters = clusterize(list);
      if (!clusters.length) return `<p class="empty">${empty}</p>`;
      if (clusters.length === 1) {
        const c = clusters[0];
        const tone = c.items.every((a) => bpTone(a) === "debt") ? "debt" : "bank";
        const lines = c.items.map((a) => `
          <div class="bp-line">
            <div class="nm">${esc(bpLineName(a, c.key))}</div>
            ${balCell(a)}
          </div>`).join("");
        return `<div class="bp-cluster tone-${tone} nw-flat">${lines}</div>`;
      }
      return clusters.map(bpClusterCard).join("");
    }
    function sidePanel(title, list, empty) {
      const clusters = clusterize(list);
      const tot = typeTotalLabel(sumCents(list));
      return `<div class="panel">
        <div class="nw-panel-head">
          <h2>${esc(title)}</h2>
          <div class="${balanceClass(sumCents(list))}">${tot}</div>
        </div>
        ${sideBody(list, empty)}
      </div>`;
    }
    const ownerTot = {};
    open.forEach((a) => {
      const k = a.owner || selfName;
      ownerTot[k] = (ownerTot[k] || 0) + (a.current_cents || 0);
    });
    const hist = data.net_worth_history || {};
    const cardOwners = [];
    if (selfName) cardOwners.push(selfName);
    if (partnerName) cardOwners.push(partnerName);
    if (partnerName) cardOwners.push("Joint");
    const ownerCards = cardOwners.map((o) => {
      return `<div class="card"><div class="lbl">${esc(ownerName(o))}</div>
        <div class="val">${cents(ownerTot[o] || 0)}</div>
        <div class="nw-spark-when" hidden></div>
        ${sparkline(hist[o], hist.dates)}</div>`;
    }).join("");
    return `<div class="cards nw-top">
      <div class="card">
        <div class="lbl">Household</div>
        <div class="val">${cents(total)}</div>
        <div class="nw-spark-when" hidden></div>
        ${sparkline(hist.household, hist.dates)}
      </div>
      ${ownerCards}
    </div>
    <div class="nw-split">
      ${sidePanel("Assets", assetList, "No assets.")}
      ${sidePanel("Liabilities", liabList, "No liabilities.")}
    </div>
`;
  }

  function blueprintBody() {
    const open = accounts.filter((a) => !a.closed && isOnBlueprint(a));
    const selfAccts = sortAccts(open.filter((a) => a.owner === selfName));
    const partnerAccts = sortAccts(open.filter((a) => partnerName && a.owner === partnerName));
    const joint = sortAccts(open.filter((a) => a.owner === "Joint"));
    const kidSet = new Set(kidNames);
    const kids = sortAccts(open.filter((a) => kidSet.has(a.owner)));
    const nwList = open.filter((a) => !a.hidden && skipSavings(a));
    const total = sumCents(nwList);
    function col(personHtml, list) {
      const body = clusterize(list).map(bpClusterCard).join("") || `<p class="empty">None</p>`;
      return `<div class="bp-col">${personHtml}${body}</div>`;
    }
    function personHead(person, demoLetter) {
      if (!person) return "";
      return `<div class="bp-person"><div class="bp-avatar">${esc(initials(person.name, demoLetter))}</div><div class="nm">${esc(ownerName(person.name))}</div></div>`;
    }
    const kidsBlock = kids.length
      ? `<div class="bp-kids-lbl">Kids (excluded)</div>${clusterize(kids).map(bpClusterCard).join("")}`
      : "";
    let mid = "";
    if (hh.partner) {
      const selfInit = initials(selfName, "A");
      const partInit = initials(partnerName, "S");
      const meta = isDemo() ? "Alex & Sam" : (selfName && partnerName ? (selfName + " & " + partnerName) : "");
      mid = `<div class="bp-col">
          <div class="bp-person">
            <div class="bp-avatars">
              <div class="bp-avatar">${esc(selfInit)}</div>
              <div class="bp-avatar">${esc(partInit)}</div>
            </div>
            <div class="nm">Jointly held</div>
            <div class="meta">${esc(meta)}</div>
          </div>
          ${clusterize(joint).map(bpClusterCard).join("") || `<p class="empty">No joint accounts</p>`}
          ${kidsBlock}
        </div>`;
    } else if (kids.length) {
      mid = `<div class="bp-col">${kidsBlock}</div>`;
    }
    const nCols = (hh.partner ? 3 : (kids.length ? 2 : 1));
    const partnerCol = hh.partner ? col(personHead(hh.partner, "S"), partnerAccts) : "";
    return `<div class="blueprint">
      <div class="bp-nw">Net Worth: ${esc(centsPlain(total))}</div>
      <div class="bp-legend">
        <span><i class="tone-bank"></i> Bank &amp; investments</span>
        <span><i class="tone-debt"></i> Cards &amp; loans</span>
      </div>
      <div class="bp-cols" style="grid-template-columns:repeat(${nCols},1fr)">
        ${col(personHead(hh.self, "A"), selfAccts)}
        ${mid}
        ${partnerCol}
      </div>
    </div>`;
  }

  view.innerHTML = header("Retirement", tab === "blueprint" ? "household map" : "net worth by type", syncButton(st)) +
    setupBanner(st) +
    `<div class="tabs" id="ret-tabs" role="tablist" aria-label="Retirement">
      <button type="button" role="tab" data-t="networth" aria-selected="${tab === "networth"}">Net worth</button>
      <button type="button" role="tab" data-t="blueprint" aria-selected="${tab === "blueprint"}">Blueprint</button>
    </div>
    <div id="ret-body">${tab === "blueprint" ? blueprintBody() : netWorthBody()}</div>`;
  bindChrome();
  bindNwSparklines(view);
  view.querySelector("#ret-tabs").addEventListener("click", (e) => {
    const btn = e.target.closest("button");
    if (!btn) return;
    const next = btn.dataset.t === "blueprint" ? "#/retirement/blueprint" : "#/retirement";
    if (location.hash !== next) location.hash = next;
  });
}

const PAGES = {
  overview: renderOverview,
  transactions: renderTransactions,
  spending: renderSpending,
  retirement: renderRetirement,
  settings: renderSettings,
  recurring: renderRecurring,
};

async function refresh() {
  closeWalnutModal();
  const parsed = parseRoute();
  if (parsed.canonical && location.hash !== parsed.canonical) {
    history.replaceState(null, "", parsed.canonical);
  }
  const name = parsed.id;
  setNav(name);
  view.classList.toggle("spend", name === "spending");
  view.classList.toggle("blueprint", name === "retirement");
  try {
    statusCache = await api("/api/status");
    await ensureDemoScale();
    await PAGES[name]();
  } catch (err) {
    view.innerHTML = `<div class="callout setup"><h2>Could not load</h2><p>${esc(err.message)}</p></div>`;
  }
}

window.addEventListener("hashchange", refresh);
if (!location.hash) location.hash = "#/overview";
refresh();
