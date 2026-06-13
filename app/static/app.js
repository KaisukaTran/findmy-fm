/* FINDMY-FM dashboard client.
 *
 * Design: HTMX handles GET partials (polling + WS-triggered refresh). All
 * mutations go through one delegated click/submit listener (CSP-safe — no inline
 * handlers, survives HTMX swaps). Alpine (CSP build) only toggles modal panels.
 */

// --- helpers ------------------------------------------------------------

function esc(s) {
  return String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

// --- U2: API key management (sessionStorage, not localStorage) ----------
// Initialise from sessionStorage on load so a page refresh within the same
// browser session does not re-prompt.
window.API_KEY = sessionStorage.getItem("api_key") || "";

// --- Toast notifications (P3) -------------------------------------------
// Container is injected once from JS — CSP-safe, no inline style or script.
// Usage: toast(msg, 'info'|'success'|'error')  auto-dismisses after 4 s.

let _toastContainer = null;

function _ensureToastContainer() {
  if (_toastContainer) return _toastContainer;
  const root = document.body || document.documentElement;
  const el = document.createElement("div");
  el.id = "toast-container";
  el.setAttribute("aria-live", "polite");
  el.setAttribute("aria-atomic", "false");
  root.appendChild(el);
  _toastContainer = el;
  return el;
}

function toast(msg, kind) {
  const container = _ensureToastContainer();
  const item = document.createElement("div");
  item.className = "toast toast-" + (kind || "info");
  item.textContent = msg;
  container.appendChild(item);
  // Trigger CSS enter animation on next frame.
  requestAnimationFrame(() => item.classList.add("toast-in"));
  setTimeout(() => {
    item.classList.remove("toast-in");
    item.classList.add("toast-out");
    item.addEventListener("transitionend", () => item.remove(), { once: true });
    // Fallback remove if transition never fires.
    setTimeout(() => item.remove(), 600);
  }, 4000);
}

// --- Connection chip (P3) -----------------------------------------------
// Drives the #conn-chip element in status.html (re-injected on every poll).
// States: conn-live  conn-error  conn-reconnecting

function setChip(state) {
  const chip = document.getElementById("conn-chip");
  if (!chip) return;
  chip.className = "conn-chip conn-" + state;
  chip.title = state === "live" ? "Kết nối tốt"
             : state === "error" ? "Lỗi tải dữ liệu"
             : "Đang kết nối lại…";
}

// --- Modal helpers (P3) -------------------------------------------------

function closeModal(prop) {
  try {
    const alpine = document.body._x_dataStack && document.body._x_dataStack[0];
    if (alpine && prop in alpine) { alpine[prop] = false; return; }
    // Fallback: Alpine.$data via public API (Alpine 3).
    const data = window.Alpine && Alpine.$data(document.body);
    if (data && prop in data) data[prop] = false;
  } catch (_) {}
}

function apiHeaders() {
  const h = { "Content-Type": "application/json" };
  if (window.API_KEY) h["X-API-Key"] = window.API_KEY;
  return h;
}

// U2: On the first 401, prompt once for the API key, store in sessionStorage,
// and retry the failed call once.  Subsequent 401s (wrong key) surface as errors.
let _promptingKey = false;

async function api(method, url, body) {
  const doFetch = () => fetch(url, {
    method,
    headers: apiHeaders(),
    body: body ? JSON.stringify(body) : undefined,
  });

  let res = await doFetch();

  if (res.status === 401 && !_promptingKey) {
    _promptingKey = true;
    const entered = window.prompt("Nhập API key để tiếp tục:", "") ?? "";
    if (entered) {
      window.API_KEY = entered;
      sessionStorage.setItem("api_key", entered);
      _updateKeyIndicator();
      res = await doFetch();  // single retry with new key
    }
    // Reset only after the retry resolves so the re-entrancy guard covers the
    // full prompt+retry duration (security review: avoid a premature re-prompt).
    _promptingKey = false;
  }

  if (!res.ok) {
    const detail = await res.json().catch(() => ({}));
    toast("Lỗi: " + (detail.detail || res.status), "error");
    throw new Error(res.status);
  }
  return res.json();
}

// U2: Update the key indicator in the header (re-rendered on every status poll,
// so we call this whenever the key changes to keep it in sync immediately).
function _updateKeyIndicator() {
  const ind = document.getElementById("key-indicator");
  if (!ind) return;
  if (window.API_KEY) {
    ind.textContent = "🔑";
    ind.title = "API key đã nạp — bấm để đăng xuất khóa";
    ind.classList.add("key-loaded");
  } else {
    ind.textContent = "";
    ind.title = "";
    ind.classList.remove("key-loaded");
  }
}

// --- Scoped refresh helpers (P2) ----------------------------------------
// Fire a targeted custom event on document.body so only the relevant partials
// re-fetch.  refreshAll() remains for WS pushes (fires every scoped event).

function fireRefresh(scope) {
  document.body.dispatchEvent(new CustomEvent(scope));
}

function refreshStatus()  { fireRefresh("refresh-status"); }
function refreshTrading() { fireRefresh("refresh-trading"); }
function refreshScanner() { fireRefresh("refresh-scanner"); }
function refreshOpus()    { fireRefresh("refresh-opus"); }
function refreshLosses()  { fireRefresh("refresh-losses"); }
function refreshAudit()   { fireRefresh("refresh-audit"); }

function refreshAll() {
  // Used by WS push — refetch everything.
  ["refresh-status","refresh-trading","refresh-scanner",
   "refresh-opus","refresh-losses","refresh-audit"]
    .forEach((ev) => document.body.dispatchEvent(new CustomEvent(ev)));
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
  // U2: sync key indicator on load.
  _updateKeyIndicator();

  document.body.addEventListener("htmx:afterSwap", applyAuditSymbol);

  // P3: connection chip — htmx request lifecycle.
  // htmx:afterRequest fires for every completed request (success or error).
  document.body.addEventListener("htmx:afterRequest", (e) => {
    if (e.detail && e.detail.successful) setChip("live");
  });
  document.body.addEventListener("htmx:responseError", () => setChip("error"));
  document.body.addEventListener("htmx:sendError", () => setChip("error"));

  // P3: Esc closes any open Alpine modal.
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    closeModal("orderOpen");
    closeModal("kssOpen");
    closeModal("previewOpen");
    // Also close the plain JS ladder modal.
    const m = document.getElementById("ladder-modal");
    if (m && !m.classList.contains("hidden")) m.classList.add("hidden");
  });
});

async function openLadder(url) {
  const res = await fetch(url, { headers: apiHeaders() });
  document.getElementById("ladder-body").innerHTML = await res.text();
  const m = document.getElementById("ladder-modal");
  m.classList.remove("hidden");
}

// --- mutation handlers (event delegation) -------------------------------

const actions = {
  async approve(id) {
    await api("POST", `/api/pending/approve/${id}`);
    refreshTrading(); refreshStatus();
  },
  async reject(id) {
    const reason = prompt("Lý do từ chối?", "") ?? "";
    await api("POST", `/api/pending/reject/${id}`, { reason });
    refreshTrading(); refreshStatus();
  },
  async kssStart(id) {
    await api("POST", `/api/kss/sessions/${id}/start`);
    refreshTrading(); refreshStatus();
  },
  async kssStop(id) {
    await api("POST", `/api/kss/sessions/${id}/stop`);
    refreshTrading(); refreshStatus();
  },
  async kssDelete(id) {
    if (!confirm("Xóa phiên " + id + "?")) return;
    await api("DELETE", `/api/kss/sessions/${id}`);
    refreshTrading(); refreshStatus();
  },
  async kssCheckTp(id) {
    const r = await api("POST", `/api/kss/sessions/${id}/check-tp`);
    toast(r.tp_deferred
      ? "TP đạt theo avg session nhưng DƯỚI giá vốn tổng + 2× phí — đã HOÃN (K-2), tránh chốt lời mà lỗ."
      : (r.tp_triggered ? "TP đạt — đã đưa lệnh bán vào hàng chờ." : "Chưa đạt TP."),
      r.tp_triggered ? "success" : "info");
    refreshTrading(); refreshStatus();
  },
  async kssDcaNext(id) {
    if (!confirm("Đặt lệnh DCA sóng tiếp theo cho session " + id + "?")) return;
    const r = await api("POST", `/api/kss/sessions/${id}/dca-next`);
    toast(`Đã đưa sóng ${r.wave_num} vào hàng chờ: LIMIT BUY ${r.quantity} @ ${r.price}.`, "success");
    refreshTrading(); refreshStatus();
  },
  async scan() {
    await api("POST", "/api/scan");
    refreshScanner(); refreshStatus();
  },
  async toggleAuto(desired) {
    const enable = desired === "on";
    if (enable &&
        !confirm("Bật giao dịch FULL-AUTO? Các phiên đủ điều kiện sẽ tự duyệt.")) return;
    await api("POST", "/api/autotrade", { enabled: enable });
    refreshStatus();
  },
  async approveAll() {
    if (!confirm("Duyệt và thực thi TẤT CẢ lệnh chờ?")) return;
    await api("POST", "/api/pending/approve-all");
    refreshTrading(); refreshStatus();
  },
  async rejectAll() {
    if (!confirm("Từ chối TẤT CẢ lệnh chờ?")) return;
    await api("POST", "/api/pending/reject-all", { reason: "bulk reject" });
    refreshTrading(); refreshStatus();
  },
  async toggleAutoApprove(desired) {
    const enable = desired === "on";
    if (enable &&
        !confirm("Bật quy tắc tự duyệt? Lệnh KSS nhỏ sẽ tự động được duyệt.")) return;
    await api("POST", "/api/autoapprove", { enabled: enable });
    refreshStatus();
  },
  async setAutoApproveMax() {
    const inp = document.getElementById("aa-max-input");
    const v = num(inp && inp.value);
    if (v == null || v <= 0) { toast("Nhập giá trị max notional dương (USD).", "error"); return; }
    // Preserve the current enabled flag; only change the threshold.
    const s = await api("GET", "/api/autoapprove");
    await api("POST", "/api/autoapprove", { enabled: s.enabled, max_notional: v });
    refreshTrading(); refreshStatus();
  },
  async toggleScheduler(desired) {
    const enable = desired === "on";
    if (enable &&
        !confirm("Khởi chạy scheduler nền? Nó sẽ quét & quản lý phiên theo chu kỳ.")) return;
    await api("POST", "/api/scheduler", { enabled: enable });
    refreshStatus();
  },
  async toggleFullAuto(desired) {
    const enable = desired === "on";
    if (enable &&
        !confirm("Bật công tắc chính FULL-AUTO? Điều này sẽ khởi chạy scheduler và bật auto-trade + auto-approve.")) return;
    if (!enable &&
        !confirm("Tắt FULL-AUTO? Điều này sẽ dừng scheduler và vô hiệu hóa giao dịch tự động.")) return;
    await api("POST", "/api/full-auto", { enabled: enable });
    refreshStatus();
  },
  async toggleOpus(desired) {
    const enable = desired === "on";
    if (enable &&
        !confirm("Bật chế độ OPUS orchestrator? Opus sẽ điều phối giao dịch trên vốn riêng (giấy).")) return;
    await api("POST", "/api/opus", { enabled: enable });
    refreshStatus(); refreshOpus();
  },
  async toggleGrok(desired) {
    const enable = desired === "on";
    await api("POST", "/api/grok", { enabled: enable });
    if (enable)
      toast("Đã bật Grok. Cần thêm XAI_API_KEY vào .env để Grok thật sự tham gia đồng thuận.", "info");
    refreshStatus();
  },
  async toggleGrokScanner(desired) {
    const enable = desired === "on";
    await api("POST", "/api/grok-scanner", { enabled: enable });
    if (enable)
      toast("Đã bật Grok scanner. Cần XAI_API_KEY trong .env để Grok thực sự duyệt ứng viên.", "info");
    refreshStatus(); refreshScanner();
  },
  async toggleTaLib(desired) {
    const enable = desired === "on";
    await api("POST", "/api/ta-source", { source: "lib", enabled: enable });
    if (enable)
      toast("Đã bật overlay pandas-ta. Cần `pip install pandas-ta`; thiếu thì tự lùi về chỉ báo pure-Python.", "info");
    refreshStatus(); refreshScanner();
  },
  async toggleTaExternal(desired) {
    const enable = desired === "on";
    await api("POST", "/api/ta-source", { source: "external", enabled: enable });
    if (enable)
      toast("Đã bật nguồn TA ngoài (taapi.io). Cần TAAPI_API_KEY trong .env; hiện là STUB cho tới khi nối provider.", "info");
    refreshStatus(); refreshScanner();
  },
  async toggleOpusShadow(desired) {
    const enable = desired === "on";
    // disabling shadow (enable=false) → confirm before letting Opus place (paper) orders.
    if (!enable &&
        !confirm("Turn OFF shadow? Opus will then PLACE paper orders (still inside the sandbox + caps).")) return;
    await api("POST", "/api/opus/shadow", { enabled: enable });
    refreshStatus(); refreshOpus();
  },
  async viewLadder(id) {
    await openLadder(`/partials/ladder?session=${id}`);
  },
  async closePosition(sym) {
    if (!confirm(`Đóng TOÀN BỘ vị thế ${sym} (bán market) và dừng session KSS của coin này?`)) return;
    const r = await api("POST", "/api/positions/close", { symbol: sym });
    toast(r.closed ? `Đã bán ${sym}: ${r.qty} (PnL $${(r.realized || 0).toFixed(2)})` : "Không có vị thế để đóng.",
      r.closed ? "success" : "info");
    refreshTrading(); refreshLosses(); refreshStatus();
  },
  async viewLadderSymbol(sym) {
    await openLadder(`/partials/ladder?symbol=${encodeURIComponent(sym)}`);
  },
  closeLadder() {
    document.getElementById("ladder-modal").classList.add("hidden");
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
    if (f) { f.classList.remove("hidden"); document.getElementById("audit-sym-label").textContent = sym; }
    applyAuditSymbol();
  },
  auditClearSymbol() {
    window._auditSym = "";
    const f = document.getElementById("audit-sym-filter");
    if (f) f.classList.add("hidden");
    applyAuditSymbol();
  },
  clearKey() {
    window.API_KEY = "";
    sessionStorage.removeItem("api_key");
    _updateKeyIndicator();
    location.reload();
  },
  async resetBreaker() {
    if (!confirm("Khôi phục breaker thủ công? Hệ thống sẽ tiếp tục giao dịch.")) return;
    await api("POST", "/api/breaker/reset");
    refreshStatus();
  },
  async toggleGuardian(desired) {
    const enable = desired === "on";
    if (enable &&
        !confirm("Bật AI Guardian? Nó sẽ phủ quyết lệnh không qua kiểm tra rủi ro.")) return;
    if (!enable &&
        !confirm("Tắt AI Guardian? Lệnh sẽ không còn được Guardian kiểm tra.")) return;
    await api("POST", "/api/guardian", { enabled: enable });
    refreshStatus();
  },
  async toggleTelegram(desired) {
    const enable = desired === "on";
    if (enable &&
        !confirm("Bật Telegram poller? Bot sẽ nhận và chuyển tiếp cảnh báo giao dịch.")) return;
    if (!enable &&
        !confirm("Tắt Telegram poller?")) return;
    await api("POST", "/api/telegram", { enabled: enable });
    refreshStatus();
  },
  async telegramTest() {
    const r = await api("POST", "/api/telegram/test");
    toast(r.sent ? "Đã gửi cảnh báo kiểm tra thành công." : "Cảnh báo kiểm tra thất bại — kiểm tra cấu hình Telegram.",
      r.sent ? "success" : "error");
  },
  async toggleHyperopt(desired) {
    const enable = desired === "on";
    if (enable &&
        !confirm("Bật Hyperopt? Hệ thống sẽ điều chỉnh tham số KSS bằng Optuna.")) return;
    if (!enable &&
        !confirm("Tắt Hyperopt? Điều chỉnh tham số sẽ dừng.")) return;
    await api("POST", "/api/hyperopt", { enabled: enable });
    refreshStatus();
  },
  async toggleMl(desired) {
    const enable = desired === "on";
    if (enable &&
        !confirm("Bật ML? Một mô hình sẽ được huấn luyện để dự đoán chất lượng mở vị.")) return;
    if (!enable &&
        !confirm("Tắt ML? Lọc dựa trên mô hình sẽ bị tắt.")) return;
    await api("POST", "/api/ml", { enabled: enable });
    refreshStatus();
  },
};

document.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-action]");
  if (!btn) return;
  const fn = actions[btn.dataset.action];
  if (fn) {
    e.preventDefault();
    Promise.resolve(fn(btn.dataset.id)).catch(() => {});
  }
});

// --- Tab navigation (plain JS, CSP-safe — Alpine only handles modals) ----

// tabActive(el): returns true when the element's tab panel is active.
// Used in hx-trigger conditional-polling guards: every Ns [tabActive(this)].
// Degrades to true (always-poll) if data-active is absent (no JS or missing attr).
function tabActive(el) {
  try {
    const panel = el.closest("[data-tab-panel]");
    if (!panel) return true; // outside any panel — always poll
    return panel.dataset.active === "true";
  } catch (_) {
    return true; // degrade: poll as before
  }
}

function showTab(name) {
  document.querySelectorAll("[data-tab]").forEach((b) => {
    const isActive = b.dataset.tab === name;
    b.classList.toggle("active", isActive);
    b.setAttribute("aria-selected", isActive ? "true" : "false");
  });
  document.querySelectorAll("[data-tab-panel]").forEach((p) => {
    const isActive = p.dataset.tabPanel === name;
    p.style.display = isActive ? "" : "none";
    p.dataset.active = isActive ? "true" : "false";
    if (isActive) {
      // Fire tab-shown so inactive panels that just became active trigger their
      // first poll immediately rather than waiting up to Ns for the interval.
      try { htmx.trigger(p, "tab-shown"); } catch (_) {}
    }
  });
  // U8: persist active tab in location.hash
  try { location.hash = name; } catch (_) {}
}

document.addEventListener("click", (e) => {
  const tabBtn = e.target.closest("[data-tab]");
  if (tabBtn) showTab(tabBtn.dataset.tab);
});
document.addEventListener("DOMContentLoaded", () => {
  // U8: restore tab from hash on load; fallback to overview.
  const hash = location.hash.replace("#", "").trim();
  const valid = ["overview", "trading", "opus", "losses", "strategy", "logs"];
  showTab(valid.includes(hash) ? hash : "overview");
});

// Close the ladder modal when clicking the dark backdrop (outside the box).
document.addEventListener("click", (e) => {
  const m = document.getElementById("ladder-modal");
  if (m && e.target === m) m.classList.add("hidden");
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
    closeModal("orderOpen");
    toast("Đã thêm lệnh vào hàng chờ.", "success");
    refreshTrading(); refreshStatus();
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
    form.reset();
    closeModal("kssOpen");
    toast("Đã tạo phiên KSS mới.", "success");
    refreshTrading(); refreshStatus();
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
      min_expectancy_pct: num(f.get("min_expectancy_pct")),
      min_win_rate: num(f.get("min_win_rate")),
      min_confidence: num(f.get("min_confidence")),
    });
    toast("Đã lưu cấu hình KSS — áp dụng cho phiên mới.", "success");
    refreshTrading(); refreshStatus();
  } else if (form.id === "grok-fail-mode-form") {
    e.preventDefault();
    const f = new FormData(form);
    await api("POST", "/api/kss-settings", { grok_scanner_fail_mode: f.get("grok_scanner_fail_mode") });
    toast("Đã lưu chế độ lỗi Grok.", "success");
  } else if (form.id === "consensus-weights-form") {
    e.preventDefault();
    const f = new FormData(form);
    await api("POST", "/api/consensus-weights", {
      trend: num(f.get("trend")),
      dip: num(f.get("dip")),
      volatility: num(f.get("volatility")),
      liquidity: num(f.get("liquidity")),
      ml: num(f.get("ml")),
    });
    toast("Đã lưu trọng số đồng thuận.", "success");
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
  sock.onopen = () => setChip("live");
  sock.onmessage = (m) => {
    try {
      if (JSON.parse(m.data).event === "refresh") refreshAll();
    } catch (_) {}
  };
  sock.onclose = () => {
    setChip("reconnecting");
    setTimeout(connectWs, 5000); // auto-reconnect
  };
}
connectWs();


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
