/* FINDMY-FM dashboard client.
 *
 * Design: HTMX handles GET partials (polling + WS-triggered refresh). All
 * mutations go through one delegated click/submit listener (CSP-safe — no inline
 * handlers, survives HTMX swaps). Alpine (CSP build) only toggles modal panels.
 */

// --- helpers ------------------------------------------------------------

function apiHeaders() {
  const h = { "Content-Type": "application/json" };
  if (window.API_KEY) h["X-API-Key"] = window.API_KEY;
  return h;
}

async function api(method, url, body) {
  const res = await fetch(url, {
    method,
    headers: apiHeaders(),
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({}));
    alert("Error: " + (detail.detail || res.status));
    throw new Error(res.status);
  }
  return res.json();
}

function refreshAll() {
  document.body.dispatchEvent(new CustomEvent("refresh"));
}

// Symbol filter for the audit feed lives on row classes (inside the swapped partial), so
// re-apply it after every poll/swap. The category filter is pure CSS on #audit-wrap.
function applyAuditSymbol() {
  const sym = window._auditSym || "";
  document.querySelectorAll(".audit-row").forEach((r) => {
    r.classList.toggle("audit-sym-hidden", sym !== "" && r.dataset.symbol !== sym);
  });
}
document.addEventListener("DOMContentLoaded", () => {
  document.body.addEventListener("htmx:afterSwap", applyAuditSymbol);
});

async function openLadder(url) {
  const res = await fetch(url, { headers: apiHeaders() });
  document.getElementById("ladder-body").innerHTML = await res.text();
  document.getElementById("ladder-modal").style.display = "flex";
}

// --- mutation handlers (event delegation) -------------------------------

const actions = {
  async approve(id) {
    await api("POST", `/api/pending/approve/${id}`);
    refreshAll();
  },
  async reject(id) {
    const reason = prompt("Reject reason?", "") ?? "";
    await api("POST", `/api/pending/reject/${id}`, { reason });
    refreshAll();
  },
  async kssStart(id) {
    await api("POST", `/api/kss/sessions/${id}/start`);
    refreshAll();
  },
  async kssStop(id) {
    await api("POST", `/api/kss/sessions/${id}/stop`);
    refreshAll();
  },
  async kssDelete(id) {
    if (!confirm("Delete session " + id + "?")) return;
    await api("DELETE", `/api/kss/sessions/${id}`);
    refreshAll();
  },
  async kssCheckTp(id) {
    const r = await api("POST", `/api/kss/sessions/${id}/check-tp`);
    alert(r.tp_deferred
      ? "TP đạt theo avg session nhưng DƯỚI giá vốn tổng + 2× phí — đã HOÃN (K-2), tránh chốt lời mà lỗ."
      : (r.tp_triggered ? "TP đạt — đã đưa lệnh bán vào hàng chờ." : "Chưa đạt TP."));
    refreshAll();
  },
  async scan() {
    await api("POST", "/api/scan");
    refreshAll();
  },
  async toggleAuto() {
    const state = await api("GET", "/api/autotrade");
    if (!state.auto_trade &&
        !confirm("Enable FULL-AUTO trading? Qualifying sessions will be auto-approved.")) return;
    await api("POST", "/api/autotrade", { enabled: !state.auto_trade });
    refreshAll();
  },
  async approveAll() {
    if (!confirm("Approve and execute ALL pending orders?")) return;
    await api("POST", "/api/pending/approve-all");
    refreshAll();
  },
  async rejectAll() {
    if (!confirm("Reject ALL pending orders?")) return;
    await api("POST", "/api/pending/reject-all", { reason: "bulk reject" });
    refreshAll();
  },
  async toggleAutoApprove() {
    const s = await api("GET", "/api/autoapprove");
    if (!s.enabled &&
        !confirm("Enable auto-approval rule? Small KSS orders will be approved automatically.")) return;
    await api("POST", "/api/autoapprove", { enabled: !s.enabled });
    refreshAll();
  },
  async setAutoApproveMax() {
    const inp = document.getElementById("aa-max-input");
    const v = num(inp && inp.value);
    if (v == null || v <= 0) { alert("Enter a positive max notional (USD)."); return; }
    // Preserve the current enabled flag; only change the threshold.
    const s = await api("GET", "/api/autoapprove");
    await api("POST", "/api/autoapprove", { enabled: s.enabled, max_notional: v });
    refreshAll();
  },
  async toggleScheduler() {
    const state = await api("GET", "/api/scheduler");
    if (!state.enabled &&
        !confirm("Start the background scheduler? It will scan & manage sessions on an interval.")) return;
    await api("POST", "/api/scheduler", { enabled: !state.enabled });
    refreshAll();
  },
  async toggleFullAuto() {
    const state = await api("GET", "/api/full-auto");
    if (!state.full_auto &&
        !confirm("Enable FULL-AUTO master switch? This starts the scheduler and enables auto-trade + auto-approve.")) return;
    if (state.full_auto &&
        !confirm("Disable FULL-AUTO? This will stop the scheduler and disable autonomous trading.")) return;
    await api("POST", "/api/full-auto", { enabled: !state.full_auto });
    refreshAll();
  },
  async toggleOpus() {
    const s = await api("GET", "/api/opus");
    if (!s.mode &&
        !confirm("Enable OPUS orchestrator mode? Opus will orchestrate trades on its own capital envelope (paper).")) return;
    await api("POST", "/api/opus", { enabled: !s.mode });
    refreshAll();
  },
  async toggleGrok() {
    const s = await api("GET", "/api/opus");
    await api("POST", "/api/grok", { enabled: !s.grok_enabled });
    if (!s.grok_enabled && !s.grok_active)
      alert("Đã bật Grok. Cần thêm XAI_API_KEY vào .env để Grok thật sự tham gia đồng thuận.");
    refreshAll();
  },
  async toggleGrokScanner() {
    const s = await api("GET", "/api/automation");
    await api("POST", "/api/grok-scanner", { enabled: !s.grok_scanner });
    if (!s.grok_scanner)
      alert("Đã bật Grok scanner. Cần XAI_API_KEY trong .env để Grok thực sự duyệt ứng viên.");
    refreshAll();
  },
  async toggleOpusShadow() {
    const s = await api("GET", "/api/opus");
    // shadow ON → confirm before letting Opus place (paper) orders.
    if (s.shadow &&
        !confirm("Turn OFF shadow? Opus will then PLACE paper orders (still inside the sandbox + caps).")) return;
    await api("POST", "/api/opus/shadow", { enabled: !s.shadow });
    refreshAll();
  },
  async viewLadder(id) {
    await openLadder(`/partials/ladder?session=${id}`);
  },
  async closePosition(sym) {
    if (!confirm(`Đóng TOÀN BỘ vị thế ${sym} (bán market) và dừng session KSS của coin này?`)) return;
    const r = await api("POST", "/api/positions/close", { symbol: sym });
    alert(r.closed ? `Đã bán ${sym}: ${r.qty} (PnL $${(r.realized || 0).toFixed(2)})` : "Không có vị thế để đóng.");
    refreshAll();
  },
  async viewLadderSymbol(sym) {
    await openLadder(`/partials/ladder?symbol=${encodeURIComponent(sym)}`);
  },
  closeLadder() {
    document.getElementById("ladder-modal").style.display = "none";
  },
  auditFilter(mode) {
    const w = document.getElementById("audit-wrap");
    if (w) w.className = "af-" + (mode || "important");
    document.querySelectorAll("[data-action='auditFilter']").forEach((b) =>
      b.classList.toggle("ghost", b.dataset.id !== mode));
    applyAuditSymbol();
  },
  auditFilterSymbol(sym) {
    window._auditSym = sym || "";
    const f = document.getElementById("audit-sym-filter");
    if (f) { f.style.display = ""; document.getElementById("audit-sym-label").textContent = sym; }
    applyAuditSymbol();
  },
  auditClearSymbol() {
    window._auditSym = "";
    const f = document.getElementById("audit-sym-filter");
    if (f) f.style.display = "none";
    applyAuditSymbol();
  },
  async resetBreaker() {
    if (!confirm("Manually reset the circuit-breaker? The system will resume trading.")) return;
    await api("POST", "/api/breaker/reset");
    refreshAll();
  },
  async toggleGuardian() {
    const state = await api("GET", "/api/guardian");
    if (!state.enabled &&
        !confirm("Enable AI Guardian? It will veto orders that fail its risk checks.")) return;
    if (state.enabled &&
        !confirm("Disable AI Guardian? Orders will no longer be screened by the Guardian.")) return;
    await api("POST", "/api/guardian", { enabled: !state.enabled });
    refreshAll();
  },
  async toggleTelegram() {
    const state = await api("GET", "/api/telegram");
    if (!state.enabled &&
        !confirm("Enable Telegram poller? The bot will receive and relay trade alerts.")) return;
    if (state.enabled &&
        !confirm("Disable Telegram poller?")) return;
    await api("POST", "/api/telegram", { enabled: !state.enabled });
    refreshAll();
  },
  async telegramTest() {
    const r = await api("POST", "/api/telegram/test");
    alert(r.sent ? "Test alert sent successfully." : "Test alert failed — check Telegram config.");
  },
  async toggleHyperopt() {
    const state = await api("GET", "/api/hyperopt");
    if (!state.enabled &&
        !confirm("Enable Hyperopt? The system will tune KSS parameters using Optuna.")) return;
    if (state.enabled &&
        !confirm("Disable Hyperopt? Parameter tuning will stop.")) return;
    await api("POST", "/api/hyperopt", { enabled: !state.enabled });
    refreshAll();
  },
  async toggleMl() {
    const state = await api("GET", "/api/ml");
    if (!state.enabled &&
        !confirm("Enable ML? A model will be trained to predict entry quality.")) return;
    if (state.enabled &&
        !confirm("Disable ML? Model-based filtering will be turned off.")) return;
    await api("POST", "/api/ml", { enabled: !state.enabled });
    refreshAll();
  },
  async hyperoptRun() {
    const btn = document.querySelector("[data-action='hyperoptRun']");
    if (btn) btn.disabled = true;
    try {
      const r = await api("POST", "/api/hyperopt/run");
      const n = Array.isArray(r) ? r.length : (r.count ?? "?");
      alert("Hyperopt complete — " + n + " symbol(s) tuned.");
      loadParams();
    } finally {
      if (btn) btn.disabled = false;
    }
  },
  async mlRetrain() {
    const btn = document.querySelector("[data-action='mlRetrain']");
    if (btn) btn.disabled = true;
    try {
      const r = await api("POST", "/api/ml/retrain");
      if (r && r.model) {
        alert("Model trained: v" + r.model.version + " · metric " + r.model.metric + " · " + r.model.n_samples + " samples.");
      } else {
        alert("Retrain returned no model — not enough data yet.");
      }
      loadParams();
    } finally {
      if (btn) btn.disabled = false;
    }
  },
};

document.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-action]");
  if (!btn) return;
  const fn = actions[btn.dataset.action];
  if (fn) {
    e.preventDefault();
    fn(btn.dataset.id).catch(() => {});
  }
});

// --- Tab navigation (plain JS, CSP-safe — Alpine only handles modals) ----
function showTab(name) {
  document.querySelectorAll("[data-tab]").forEach((b) => {
    b.classList.toggle("active", b.dataset.tab === name);
  });
  document.querySelectorAll("[data-tab-panel]").forEach((p) => {
    p.style.display = p.dataset.tabPanel === name ? "" : "none";
  });
}
document.addEventListener("click", (e) => {
  const tabBtn = e.target.closest("[data-tab]");
  if (tabBtn) showTab(tabBtn.dataset.tab);
});
document.addEventListener("DOMContentLoaded", () => showTab("overview"));

// Close the ladder modal when clicking the dark backdrop (outside the box).
document.addEventListener("click", (e) => {
  const m = document.getElementById("ladder-modal");
  if (m && e.target === m) m.style.display = "none";
});

// --- forms --------------------------------------------------------------

function num(v) {
  return v === "" || v == null ? null : Number(v);
}

document.addEventListener("submit", async (e) => {
  const form = e.target;
  if (form.id === "order-form") {
    e.preventDefault();
    const f = new FormData(form);
    await api("POST", "/api/orders", {
      symbol: f.get("symbol"),
      side: f.get("side"),
      quantity: num(f.get("quantity")),
      price: Number(f.get("price") || 0),
      order_type: f.get("order_type") || "LIMIT",
    });
    form.reset();
    refreshAll();
  } else if (form.id === "kss-form") {
    e.preventDefault();
    const f = new FormData(form);
    await api("POST", "/api/kss/sessions", {
      symbol: f.get("symbol"),
      entry_price: Number(f.get("entry_price")),
      distance_pct: Number(f.get("distance_pct")),
      max_waves: Number(f.get("max_waves")),
      isolated_fund: Number(f.get("isolated_fund")),
      tp_pct: Number(f.get("tp_pct")),
    });
    refreshAll();
  } else if (form.id === "kss-settings-form") {
    e.preventDefault();
    const f = new FormData(form);
    await api("POST", "/api/kss-settings", {
      scan_distance_pct: num(f.get("scan_distance_pct")),
      scan_tp_pct: num(f.get("scan_tp_pct")),
      scan_max_waves: num(f.get("scan_max_waves")),
      scan_fund: num(f.get("scan_fund")),
      sl_pct: num(f.get("sl_pct")),
      trailing_pct: num(f.get("trailing_pct")),
      deadline_days: num(f.get("deadline_days")),
      max_concurrent_sessions: num(f.get("max_concurrent_sessions")),
      max_deployed_pct: num(f.get("max_deployed_pct")),
      loss_streak_block_k: num(f.get("loss_streak_block_k")),
      loss_streak_window_days: num(f.get("loss_streak_window_days")),
    });
    alert("Đã lưu cấu hình KSS — áp dụng cho session mới.");
    refreshAll();
  } else if (form.id === "preview-form") {
    e.preventDefault();
    const f = new FormData(form);
    const r = await api("POST", "/api/kss/preview", {
      symbol: f.get("symbol") || "BTC",
      entry_price: Number(f.get("entry_price")),
      distance_pct: Number(f.get("distance_pct")),
      max_waves: Number(f.get("max_waves")),
      isolated_fund: Number(f.get("isolated_fund")),
      tp_pct: Number(f.get("tp_pct")),
    });
    renderPreview(r);
  }
});

// ##,###.## money formatting; qty keeps crypto precision.
const money = (v) =>
  Number(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const qtyFmt = (v) =>
  Number(v).toLocaleString(undefined, { minimumFractionDigits: 0, maximumFractionDigits: 6 });

function renderPreview(r) {
  const el = document.getElementById("preview-output");
  if (!el) return;
  const rows = r.waves
    .map(
      (w) =>
        `<tr><td>${w.wave_num}</td><td>${money(w.target_price)}</td><td>${qtyFmt(w.quantity)}</td>` +
        `<td>${money(w.avg_price_after)}</td><td>${money(w.tp_price_after)}</td></tr>`
    )
    .join("");
  el.innerHTML =
    `<p>Total cost ≈ <b>$${money(r.total_cost)}</b> · range ${r.price_range_pct}% · ` +
    `final avg $${money(r.final_avg_price)}</p>` +
    `<table class="tbl"><thead><tr><th>#</th><th>Price</th><th>Qty</th>` +
    `<th>Avg after</th><th>TP after</th></tr></thead><tbody>${rows}</tbody></table>`;
}

// --- WebSocket live refresh --------------------------------------------

function connectWs() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const sock = new WebSocket(`${proto}://${location.host}/ws`);
  sock.onmessage = (m) => {
    try {
      if (JSON.parse(m.data).event === "refresh") refreshAll();
    } catch (_) {}
  };
  sock.onclose = () => setTimeout(connectWs, 5000); // auto-reconnect
}
connectWs();

// --- Phase C: params panel (client-side fetch — no /partials/params route) ----
// All other panels use hx-get to /partials/* HTML routes. The params panel is
// the sole exception: no server route exists and Python cannot be edited, so
// we fetch /api/params + /api/ml JSON here and render rows in JS on load.

function renderMlStatus(data) {
  const el = document.getElementById("ml-status");
  if (!el) return;
  const m = data && data.model;
  if (!m) {
    el.textContent = "ML model: none trained yet.";
    return;
  }
  el.innerHTML =
    "ML model: <b>" + (m.id || "—") + "</b>" +
    " · v" + (m.version || "?") +
    " · metric <b>" + (m.metric ?? "—") + "</b>" +
    " · " + (m.n_samples ?? "?") + " samples" +
    " · trained " + (m.trained_at ? m.trained_at.slice(0, 19).replace("T", " ") : "—");
}

function renderParamsRows(rows) {
  const tbody = document.getElementById("params-tbody");
  if (!tbody) return;
  if (!rows || !rows.length) {
    tbody.innerHTML = "<tr><td colspan='7' class='muted'>No tuned params yet — run Hyperopt first.</td></tr>";
    return;
  }
  tbody.innerHTML = rows.map((r) =>
    "<tr>" +
    "<td>" + r.symbol + "</td>" +
    "<td>" + (r.distance_pct != null ? r.distance_pct.toFixed(2) : "—") + "</td>" +
    "<td>" + (r.tp_pct != null ? r.tp_pct.toFixed(2) : "—") + "</td>" +
    "<td>" + (r.max_waves ?? "—") + "</td>" +
    "<td>" + (r.score != null ? r.score.toFixed(4) : "—") + "</td>" +
    "<td>" + (r.trials ?? "—") + "</td>" +
    "<td class='muted'>" + (r.updated_at ? r.updated_at.slice(0, 16).replace("T", " ") : "—") + "</td>" +
    "</tr>"
  ).join("");
}

async function loadParams() {
  try {
    const [rows, mlData] = await Promise.all([
      api("GET", "/api/params"),
      api("GET", "/api/ml"),
    ]);
    renderParamsRows(rows);
    renderMlStatus(mlData);
  } catch (_) {
    // errors already alerted by api()
  }
}

// Initialise on first load; also refresh when the global refresh event fires.
document.addEventListener("DOMContentLoaded", loadParams);
document.body.addEventListener("refresh", loadParams);

// --- Alpine (CSP build): modal visibility only -------------------------

document.addEventListener("alpine:init", () => {
  // eslint-disable-next-line no-undef
  Alpine.data("ui", () => ({
    orderOpen: false,
    kssOpen: false,
    previewOpen: false,
    toggleOrder() { this.orderOpen = !this.orderOpen; },
    toggleKss() { this.kssOpen = !this.kssOpen; },
    togglePreview() { this.previewOpen = !this.previewOpen; },
  }));
});
