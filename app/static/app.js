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

// --- U2: API key management (localStorage) -------------------------------
// The key is remembered PERMANENTLY on this machine, so the dashboard asks for
// it exactly once, ever. It used to live in sessionStorage, which is wiped when
// the tab closes — every new tab re-prompted before any settings change, which
// is pure friction for a single-operator app bound to 127.0.0.1 (anyone who can
// open this page can already read .env). Auth itself is untouched: the server
// still requires X-API-Key on every mutating call.
// Migration: an existing sessionStorage key is adopted once so the operator is
// not asked again just because of this change.
window.API_KEY = localStorage.getItem("api_key") || sessionStorage.getItem("api_key") || "";
if (window.API_KEY && !localStorage.getItem("api_key")) {
  localStorage.setItem("api_key", window.API_KEY);   // carry the old session key over
}

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

function apiHeaders() {
  const h = { "Content-Type": "application/json" };
  if (window.API_KEY) h["X-API-Key"] = window.API_KEY;
  return h;
}

// U2: On the first 401, prompt once for the API key, store it in localStorage
// (remembered for good — see above), and retry the failed call once. Subsequent
// 401s (wrong key) surface as errors.
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
      localStorage.setItem("api_key", entered);
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

// Hosts that have completed at least one request, keyed by id/hx-get (see the poll gate
// in DOMContentLoaded). A Set of KEYS, not a WeakSet of nodes: the self-swapping panes
// replace their own node on every swap.
const _polledOnce = new Set();

function fireRefresh(scope) {
  document.body.dispatchEvent(new CustomEvent(scope));
}

function refreshStatus()  { fireRefresh("refresh-status"); }
function refreshTrading() { fireRefresh("refresh-trading"); }
function refreshScanner() { fireRefresh("refresh-scanner"); }
function refreshOpus()    { fireRefresh("refresh-opus"); }
function refreshLosses()  { fireRefresh("refresh-losses"); }
function refreshAudit()   { fireRefresh("refresh-audit"); }
function refreshCosts()   { fireRefresh("refresh-costs"); }
function refreshSavings() { fireRefresh("refresh-savings"); }

function refreshAll() {
  // WS push: refresh what the user can actually SEE. Firing all eight scopes made every
  // hidden panel refetch too — measured on the running dashboard, one push cost 13 partial
  // requests instead of 4, and every one of them a DB round trip inside the same process
  // that runs the 90s exit guard. Nothing goes stale behind the user's back: each panel
  // refetches on `tab-shown` the moment it is revealed.
  refreshStatus();
  const panel = document.querySelector('[data-tab-panel][data-active="true"]');
  const scope = panel && _TAB_REFRESH_SCOPE[panel.dataset.tabPanel];
  if (scope) scope();
}

// Symbol filter for the audit feed lives on row classes (inside the swapped partial), so
// re-apply it after every poll/swap. The category filter is pure CSS on #audit-wrap.
function applyAuditSymbol() {
  const sym = window._auditSym || "";
  document.querySelectorAll(".audit-row").forEach((r) => {
    r.classList.toggle("audit-sym-hidden", sym !== "" && r.dataset.symbol !== sym);
  });
}
// --- Flash-on-change (U9) -------------------------------------------------
// The reader can't see what a poll changed unless we tell them. Every numeric
// cell carries data-k="<stable key>" (contract with the template layer). We
// snapshot pre-swap state on htmx:beforeSwap and compare post-swap state on
// htmx:afterSwap, flashing .tick-up/.tick-down on the delta.
//
// F1: scope is document, not e.detail.target. Four panes swap themselves with
// hx-target="this" hx-swap="outerHTML" (positions/trades/kss/audit) — for an
// outerHTML swap the OLD node is detached and e.detail.target still points at
// it, so scoping the snapshot/compare to the event target reads the very node
// it snapshotted and prev === next every time (nothing ever flashes). Running
// both passes over the whole document instead sidesteps htmx's target
// semantics entirely: it is correct for innerHTML and outerHTML swaps alike,
// and elements outside the swapped region are unchanged so they never flash
// (their prev/next values are identical). The page carries on the order of a
// hundred [data-k] elements, so a full pass costs nothing.
//
// Snapshots are still keyed by data-k VALUE (not DOM node identity) in one
// page-wide Map, since a fresh outerHTML-swapped node is a different object
// from the one queried on the previous pass even though we now always query
// via `document`.
const _tickSnap = new Map(); // data-k -> { text, num } last-seen

// F2 fallback parser (only used when data-v is absent): pull the FIRST
// number out of the string instead of stripping the whole cell to digits —
// a cell with two numbers ("$40.58 (+2.03%)", "$2.1K (12.0%)") used to
// concatenate into garbage ("40.582.03") and get silently skipped. Honours a
// K/M/B suffix immediately after that first number (money_kmb output) and a
// leading '-' as negative. No digits found = not a number (letters-only or
// dash-only cells must never flash).
function _tickNum(text) {
  const s = String(text);
  const m = s.match(/-?[0-9][0-9,]*(?:\.[0-9]+)?/);
  if (!m) return null;
  let n = Number(m[0].replace(/,/g, ""));
  if (!Number.isFinite(n)) return null;
  const suffix = s.charAt(m.index + m[0].length).toUpperCase();
  const mult = { K: 1e3, M: 1e6, B: 1e9 }[suffix];
  if (mult) n *= mult;
  return n;
}

// F2: prefer the raw unformatted value the template emits alongside data-k
// (data-v="<raw float>") — falls back to parsing the rendered text only for
// cells that don't carry one.
function _tickReadNum(el) {
  const raw = el.dataset.v;
  if (raw !== undefined && raw !== "") {
    const n = Number(raw);
    if (Number.isFinite(n)) return n;
  }
  return _tickNum(el.textContent.trim());
}

function _tickFlash(el, cls) {
  // Remove-then-reflow-then-add: if the class is already present (two changes
  // in quick succession) simply re-adding it is a no-op to the CSS animation
  // engine — the reflow forces the browser to notice the removal first so the
  // keyframes actually replay.
  el.classList.remove("tick-up", "tick-down");
  void el.offsetWidth;
  el.classList.add(cls);
  setTimeout(() => el.classList.remove(cls), 700);
}

function _tickSnapshot(root) {
  if (!root || !root.querySelectorAll) return;
  root.querySelectorAll("[data-k]").forEach((el) => {
    _tickSnap.set(el.dataset.k, { text: el.textContent.trim(), num: _tickReadNum(el) });
  });
}

function _tickCompare(root) {
  if (!root || !root.querySelectorAll) return;
  root.querySelectorAll("[data-k]").forEach((el) => {
    const key = el.dataset.k;
    const prev = _tickSnap.get(key); // undefined = never seen -> first load, not a change
    const nextText = el.textContent.trim();
    const nextNum = _tickReadNum(el);
    _tickSnap.set(key, { text: nextText, num: nextNum }); // baseline for the next swap regardless of outcome below
    if (!prev || prev.text === nextText) return;
    if (prev.num === null || nextNum === null || prev.num === nextNum) return; // non-numeric or equal-value change
    _tickFlash(el, nextNum > prev.num ? "tick-up" : "tick-down");
  });
}

document.addEventListener("DOMContentLoaded", () => {
  // U2: sync key indicator on load.
  _updateKeyIndicator();

  document.body.addEventListener("htmx:afterSwap", applyAuditSymbol);
  // F1: scope is `document`, not the (possibly-detached, for outerHTML swaps)
  // event target — see the Flash-on-change comment above.
  document.body.addEventListener("htmx:beforeSwap", () => _tickSnapshot(document));
  document.body.addEventListener("htmx:afterSwap", () => _tickCompare(document));

  // P3: connection chip — htmx request lifecycle.
  // htmx:afterRequest fires for every completed request (success or error).
  document.body.addEventListener("htmx:afterRequest", (e) => {
    if (e.detail && e.detail.successful) setChip("live");
  });
  document.body.addEventListener("htmx:responseError", () => setChip("error"));
  document.body.addEventListener("htmx:sendError", () => setChip("error"));

  // Poll gate. The `hx-trigger` filters `[tabActive(this) && visibilityState==='visible']`
  // have NEVER run: htmx compiles them with the Function constructor, and this app's own CSP
  // (`default-src 'self'`, no `unsafe-eval`) blocks that — the browser console fills with
  // EvalError from htmx.min.js and htmx then polls unconditionally. Measured on the running
  // dashboard: sitting on Giao dịch still refetched opus, losses, costs, savings, scanner and
  // both capital hosts. This does the same job without eval.
  //
  // Only TIMER polls are gated (`requestConfig.triggeringEvent` is null for those; a click, a
  // `tab-shown`, or a scoped `refresh-*` always carries one and is never blocked). Each host
  // keeps its first load so a hidden panel still has content the moment it is revealed —
  // keyed by id/hx-get, not node identity, because the four self-swapping panes replace
  // themselves with `outerHTML` and a fresh node would otherwise look "first" every time.
  document.body.addEventListener("htmx:beforeRequest", (e) => {
    const cfg = e.detail && e.detail.requestConfig;
    if (!cfg) return;
    // Broadcast = nobody asked for THIS panel: a poll timer (no triggering event) or a
    // scoped `refresh-*` fired at the whole body. A click, a submit or a `tab-shown` is a
    // direct request for this panel and is never blocked.
    const te = cfg.triggeringEvent;
    const broadcast = !te || (typeof te.type === "string" && te.type.indexOf("refresh-") === 0);
    if (!broadcast) return;
    const el = e.detail.elt;
    if (!el || !el.closest) return;
    const key = el.id || el.getAttribute("hx-get") || "";
    if (key && !_polledOnce.has(key)) { _polledOnce.add(key); return; }
    if (document.visibilityState !== "visible") { e.preventDefault(); return; }
    const panel = el.closest("[data-tab-panel]");
    if (panel && panel.dataset.active === "false") e.preventDefault();
  });

  // P3: Esc closes the shortcut overlay, then the ladder modal — one Esc closes
  // ONE thing. Without the return, closing the overlay used to fall straight through
  // into the next close on the same keypress.
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    if (_kbdOverlay && !_kbdOverlay.hidden) { _toggleKbdOverlay(false); return; }
    // The ladder modal is the only modal left (the manual order / new-session / pyramid
    // preview dialogs and their Alpine state were removed with their header buttons).
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
  async toggleLiveTrading(mode) {
    const enable = mode === "enable";
    if (enable) {
      const phrase = prompt(
        "BẬT GIAO DỊCH TIỀN THẬT.\nGõ chính xác 'LIVE-TRADING' để xác nhận:");
      if (phrase === null) return;
      await api("POST", "/api/live-trading", { enabled: true, confirm: phrase });
      toast("Đã bật LIVE — lệnh mới sẽ đặt bằng tiền thật.", "success");
    } else {
      if (!confirm("Tắt LIVE và quay lại chế độ paper (mô phỏng)?")) return;
      await api("POST", "/api/live-trading", { enabled: false });
      toast("Đã tắt LIVE — quay lại paper.", "info");
    }
    fireRefresh("refresh-live"); refreshStatus();
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
  async kssTakeProfit(id) {
    if (!confirm("Chốt lời NGAY phiên " + id + "?\nBán TOÀN BỘ ở giá thị trường, bất kể đang lời bao nhiêu.")) return;
    const r = await api("POST", `/api/kss/sessions/${id}/take-profit`);
    toast(r.message ? `${r.message} @ ${r.price}` : "Đã chốt lời.", "success");
    refreshTrading(); refreshStatus();
  },
  async kssDcaNext(id) {
    // Gợi ý số $ của nấc kế tiếp (read-only, fail-soft: thiếu vẫn DCA+ được).
    let pv = null;
    try {
      const r = await fetch(`/api/kss/sessions/${id}/dca-preview`);
      if (r.ok) pv = await r.json();
    } catch (_) { /* preview là tuỳ chọn */ }

    let head = "DCA+ thủ công — session " + id + ".\n";
    if (pv) {
      head +=
        `Rung kế tiếp #${pv.wave_num}: BUY ${pv.quantity} @ ${pv.price} ≈ $${pv.cost}\n` +
        `Tiền nhàn rỗi khả dụng: $${pv.idle_deployable}\n`;
      if (pv.ladder_full)
        head += `⚠ Ladder đã đầy (max_waves=${pv.max_waves}) — để TRỐNG sẽ bị chặn; nhập USD để nới thêm 1 nấc.\n`;
      if (pv.below_sl)
        head += `⚠ Nấc này dưới SL floor ${pv.sl_floor} — sẽ bị từ chối, nới SL của session trước.\n`;
    }
    const raw = prompt(
      head +
      "Nhập số USD muốn bơm từ tiền nhàn rỗi (gồm cả phần dự phòng).\n" +
      "Để TRỐNG = rung mặc định theo ladder" + (pv ? ` (~$${pv.cost})` : "") + ".",
      // Ladder đầy → pre-fill số gợi ý (đường custom-USD tự nới thêm 1 nấc, tránh lỗi "Ladder exhausted").
      pv && pv.ladder_full ? String(pv.cost) : "");
    if (raw === null) return;  // user cancelled
    const txt = raw.trim();
    const amount = txt === "" ? null : Number(txt);
    if (amount !== null && (!isFinite(amount) || amount <= 0)) {
      toast("Số USD không hợp lệ.", "error"); return;
    }
    const r = await api("POST", `/api/kss/sessions/${id}/dca-next`,
      amount !== null ? { amount_usd: amount } : undefined);
    toast(`Đã đưa sóng ${r.wave_num} vào hàng chờ: LIMIT BUY ${r.quantity} @ ${r.price} (~$${r.cost}).`, "success");
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
  async recordWithdrawal() {
    const amt = num(document.getElementById("wd-amount") && document.getElementById("wd-amount").value);
    if (amt == null || amt <= 0) { toast("Nhập số tiền rút dương (USD).", "error"); return; }
    const noteEl = document.getElementById("wd-note");
    const note = (noteEl && noteEl.value || "").trim();
    await api("POST", "/api/withdrawals", { amount: amt, note: note || null });
    const a = document.getElementById("wd-amount"); if (a) a.value = "";
    if (noteEl) noteEl.value = "";
    toast("Đã ghi nhận lệnh rút.");
    refreshCosts();
  },
  async addSavings(mode) {
    const sym = (document.getElementById("sv-symbol")?.value || "").trim();
    const qty = num(document.getElementById("sv-qty")?.value);
    const cost = num(document.getElementById("sv-cost")?.value);
    if (!sym) { toast("Nhập mã coin.", "error"); return; }
    if (qty == null || qty <= 0) { toast("Nhập số lượng dương.", "error"); return; }
    if (cost == null || cost < 0) { toast("Nhập giá vốn ≥ 0.", "error"); return; }
    const note = (document.getElementById("sv-note")?.value || "").trim();
    await api("POST", "/api/savings",
      { symbol: sym, quantity: qty, avg_cost: cost, note: note || null, mode: mode || "add" });
    ["sv-symbol","sv-qty","sv-cost","sv-note"].forEach((id) => { const e = document.getElementById(id); if (e) e.value = ""; });
    toast(mode === "set" ? "Đã ghi đè holding." : "Đã tích thêm.");
    refreshSavings();
  },
  async removeSavings(sym) {
    if (!confirm(`Xoá ${sym} khỏi sổ savings? (không bán coin — chỉ xoá ghi nhận)`)) return;
    await api("DELETE", `/api/savings/${encodeURIComponent(sym)}`);
    toast(`Đã xoá ${sym} khỏi savings.`);
    refreshSavings();
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
    localStorage.removeItem("api_key");
    sessionStorage.removeItem("api_key");   // also drop any pre-migration copy
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

// tabActive(el) used to gate hx-trigger polling: `every Ns [tabActive(this)]`. Those filters
// were removed from every template because htmx compiles them with the Function constructor,
// which this app's CSP blocks — they never ran, and each one threw an EvalError on every poll,
// keeping the console permanently red and hiding real errors. The poll gate registered in
// DOMContentLoaded does the same job in JavaScript, so the helper has no callers left.
function tabActive(el) {
  const panel = el && el.closest ? el.closest("[data-tab-panel]") : null;
  return !panel || panel.dataset.active !== "false";
}

function _applyTab(name) {
  document.querySelectorAll("[data-tab]").forEach((b) => {
    const isActive = b.dataset.tab === name;
    b.classList.toggle("active", isActive);
    b.setAttribute("aria-selected", isActive ? "true" : "false");
  });
  document.querySelectorAll("[data-tab-panel]").forEach((p) => {
    const isActive = p.dataset.tabPanel === name;
    // data-active first, then display: the CSS fade keys off the attribute, and setting it
    // after the element is already shown would skip the animation on the revealed panel.
    p.dataset.active = isActive ? "true" : "false";
    p.style.display = isActive ? "" : "none";
    if (isActive) {
      // Fire tab-shown so inactive panels that just became active trigger their
      // first poll immediately rather than waiting up to Ns for the interval.
      try { htmx.trigger(p, "tab-shown"); } catch (_) {}
    }
  });
}

// U8: tab names read from the sidebar DOM (not a hardcoded list) so a tab added
// later is deep-linkable/shortcut-able without another edit to this file.
function _knownTabs() {
  return Array.from(document.querySelectorAll("[data-tab]")).map((b) => b.dataset.tab);
}

function _tabFromHash() {
  const hash = location.hash.replace("#", "").trim();
  const known = _knownTabs();
  // Unknown/missing hash must not throw — fall back to the first (default) tab.
  return known.includes(hash) ? hash : (known[0] || "overview");
}

function showTab(name) {
  // Applied SYNCHRONOUSLY, on purpose. This used to be wrapped in
  // document.startViewTransition() for a crossfade, and that turned out to make the tab
  // switch depend on the compositor producing a frame: measured on the running dashboard
  // with a MutationObserver, the panel had still not flipped SIX SECONDS after the call,
  // and each click only landed when the next one was made. A throttled or backgrounded
  // window would freeze navigation outright. The fade is now a plain CSS animation on the
  // revealed panel (style.css .panel-in), which is cosmetic only and can never delay or
  // swallow the state change.
  _applyTab(name);
  // U8: persist active tab in the URL. replaceState (not location.hash=, which pushes) so
  // tab switches never grow the back-stack — only real navigation should be back/forward-able.
  try { history.replaceState(null, "", "#" + name); } catch (_) {}
}

document.addEventListener("click", (e) => {
  const tabBtn = e.target.closest("[data-tab]");
  if (tabBtn) showTab(tabBtn.dataset.tab);
});
document.addEventListener("DOMContentLoaded", () => {
  // U8: restore tab from hash on load.
  // F3: call _applyTab directly, not showTab — showTab's View Transition
  // snapshots every stacked [data-tab-panel] (nothing hides them by default
  // until _applyTab runs) and crossfades to the restored tab, producing a
  // full-dashboard flash on every page load. showTab (with the transition)
  // stays reserved for real user navigation (click/shortcut/hashchange).
  const name = _tabFromHash();
  _applyTab(name);
  try { history.replaceState(null, "", "#" + name); } catch (_) {}
});
// U8: back/forward (or a hand-typed/linked #tab) still switches panels even
// though our own writes use replaceState (which does not itself fire this).
window.addEventListener("hashchange", () => showTab(_tabFromHash()));

// Close the ladder modal when clicking the dark backdrop (outside the box).
document.addEventListener("click", (e) => {
  const m = document.getElementById("ladder-modal");
  if (m && e.target === m) m.classList.add("hidden");
});

// --- Keyboard shortcuts (U9) ----------------------------------------------
// 1-9 tabs, r refresh, s scan, ? help overlay. Built once, reused — the overlay
// node lives outside any htmx swap target so it survives every poll.

let _kbdOverlay = null;

function _buildKbdOverlay() {
  if (_kbdOverlay) return _kbdOverlay;
  const overlay = document.createElement("div");
  overlay.className = "kbd-overlay";
  overlay.hidden = true;
  overlay.setAttribute("role", "dialog");
  overlay.setAttribute("aria-modal", "true");
  overlay.setAttribute("aria-label", "Phím tắt");

  const panel = document.createElement("div");
  panel.className = "kbd-panel";
  overlay.appendChild(panel);

  const title = document.createElement("h3");
  title.textContent = "Phím tắt";
  panel.appendChild(title);

  const addRow = (key, label) => {
    const row = document.createElement("div");
    row.className = "kbd-row";
    const kbd = document.createElement("span");
    kbd.className = "kbd-key";
    kbd.textContent = key;
    const lbl = document.createElement("span");
    lbl.textContent = label;
    row.appendChild(kbd);
    row.appendChild(lbl);
    panel.appendChild(row);
  };

  // Tab rows mirror the sidebar's own order/labels (read from the DOM, not
  // duplicated as a string literal here) — stays correct if the sidebar copy
  // changes. Only the first 9 are bound to number keys.
  document.querySelectorAll("[data-tab]").forEach((b, i) => {
    if (i >= 9) return;
    const spans = b.querySelectorAll("span");
    const label = spans.length ? spans[spans.length - 1].textContent.trim() : b.textContent.trim();
    addRow(String(i + 1), label);
  });
  addRow("r", "Làm mới tab hiện tại");
  addRow("s", "Quét thị trường (scan)");
  addRow("?", "Hiện/ẩn bảng phím tắt này");
  addRow("Esc", "Đóng hộp thoại đang mở");

  document.body.appendChild(overlay);
  // Click the dark backdrop (outside kbd-panel) to close — same pattern as ladder-modal.
  overlay.addEventListener("click", (e) => { if (e.target === overlay) _toggleKbdOverlay(false); });
  _kbdOverlay = overlay;
  return overlay;
}

function _toggleKbdOverlay(force) {
  const overlay = _buildKbdOverlay();
  overlay.hidden = force !== undefined ? !force : !overlay.hidden;
}

// r: each tab panel already re-fires its own hx-get on a named refresh-* event
// (see fireRefresh/refreshXxx above) — reuse those instead of a new fetch path.
// Panels with no dedicated push-refresh (calendar; the KSS settings form, which
// is deliberately load-once so it never clobbers an unsaved edit) just get the
// always-visible top status bar refreshed, which is a harmless no-op for them.
const _TAB_REFRESH_SCOPE = {
  overview: () => { refreshScanner(); refreshTrading(); },
  trading: refreshTrading,
  opus: refreshOpus,
  losses: refreshLosses,
  costs: refreshCosts,
  savings: refreshSavings,
  strategy: () => fireRefresh("refresh-live"),
  logs: () => fireRefresh("refresh-audit"),
};

function _refreshCurrentTab() {
  refreshStatus(); // top summary/status bar is visible on every tab
  const panel = document.querySelector('[data-tab-panel][data-active="true"]');
  const scope = panel && _TAB_REFRESH_SCOPE[panel.dataset.tabPanel];
  if (scope) scope();
}

function _isTypingTarget(t) {
  if (!t) return false;
  const tag = t.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || !!t.isContentEditable;
}

document.addEventListener("keydown", (e) => {
  // Never hijack typing (the symbol box, forms, contenteditable) or a
  // modifier chord (ctrl/alt/meta) — only shift is allowed through, since "?"
  // is shift+/ on a US layout.
  if (e.ctrlKey || e.altKey || e.metaKey) return;
  if (_isTypingTarget(e.target)) return;

  if (e.key === "?") {
    e.preventDefault();
    _toggleKbdOverlay();
    return;
  }
  if (e.key >= "1" && e.key <= "9") {
    const tabs = document.querySelectorAll("[data-tab]");
    const btn = tabs[Number(e.key) - 1];
    if (btn) { e.preventDefault(); showTab(btn.dataset.tab); }
    return;
  }
  const k = e.key.toLowerCase();
  if (k === "r") {
    e.preventDefault();
    _refreshCurrentTab();
  } else if (k === "s") {
    e.preventDefault();
    Promise.resolve(actions.scan()).catch(() => {}); // reuse the existing scan action, no second fetch
  }
});

// --- forms --------------------------------------------------------------

function num(v) {
  return v === "" || v == null ? null : Number(v);
}

document.addEventListener("submit", async (e) => {
  const form = e.target;
  if (form.id === "kss-settings-form") {
    e.preventDefault();
    const f = new FormData(form);
    await api("POST", "/api/kss-settings", {
      scan_distance_pct: num(f.get("scan_distance_pct")),
      scan_tp_pct: num(f.get("scan_tp_pct")),
      tp_fee_coverage: num(f.get("tp_fee_coverage")),
      scan_max_waves: num(f.get("scan_max_waves")),
      scan_fund: num(f.get("scan_fund")),
      sl_pct: num(f.get("sl_pct")),
      trailing_pct: num(f.get("trailing_pct")),
      deadline_days: num(f.get("deadline_days")),
      max_concurrent_sessions: num(f.get("max_concurrent_sessions")),
      max_sessions_per_symbol: num(f.get("max_sessions_per_symbol")),
      max_deployed_pct: num(f.get("max_deployed_pct")),
      equity_backup_pct: num(f.get("equity_backup_pct")),
      cash_floor_usd: num(f.get("cash_floor_usd")),
      kss_ladder_reserve_slack_pct: num(f.get("kss_ladder_reserve_slack_pct")),
      kss_partial_last_rung_enabled: f.get("kss_partial_last_rung_enabled") === "1",
      loss_streak_block_k: num(f.get("loss_streak_block_k")),
      loss_streak_window_days: num(f.get("loss_streak_window_days")),
      min_expectancy_pct: num(f.get("min_expectancy_pct")),
      min_net_edge: num(f.get("min_net_edge")),
      min_win_rate: num(f.get("min_win_rate")),
      min_confidence: num(f.get("min_confidence")),
      min_trials: num(f.get("min_trials")),
      block_downtrend_adx: num(f.get("block_downtrend_adx")),
      kss_first_wave_usd: num(f.get("kss_first_wave_usd")),
      scan_max_symbols: num(f.get("scan_max_symbols")),
      min_quote_volume: num(f.get("min_quote_volume")),
      entry_momentum_gate: f.get("entry_momentum_gate") === "1",
      max_avg_mae_pct: num(f.get("max_avg_mae_pct")),
      kss_dynamic_tp_enabled: f.get("kss_dynamic_tp_enabled") === "1",
      kss_tp_gap_pct: num(f.get("kss_tp_gap_pct")),
      kss_exit_fee_mult: num(f.get("kss_exit_fee_mult")),
      kss_trail_atr_mult: num(f.get("kss_trail_atr_mult")),
      kss_trail_min_pct: num(f.get("kss_trail_min_pct")),
      kss_trail_arm_pct: num(f.get("kss_trail_arm_pct")),
      kss_trail_lock_pct: num(f.get("kss_trail_lock_pct")),
      kss_exit_check_sec: num(f.get("kss_exit_check_sec")),
      kss_crash_drop_pct: num(f.get("kss_crash_drop_pct")),
      kss_live_stop_orders: f.get("kss_live_stop_orders") === "1",
      rel_strength_enabled: f.get("rel_strength_enabled") === "1",
      rel_strength_lookback_bars: num(f.get("rel_strength_lookback_bars")),
      rel_strength_margin_pct: num(f.get("rel_strength_margin_pct")),
      regime_ramp_enabled: f.get("regime_ramp_enabled") === "1",
      mae_quartile_gate_enabled: f.get("mae_quartile_gate_enabled") === "1",
      strategy_router_enabled: f.get("strategy_router_enabled") === "1",
      pyramid_up_min_rel_strength: num(f.get("pyramid_up_min_rel_strength")),
      pyramid_up_min_adx: num(f.get("pyramid_up_min_adx")),
      pyramid_up_step_pct: num(f.get("pyramid_up_step_pct")),
      pyramid_up_size_ratio: num(f.get("pyramid_up_size_ratio")),
      pyramid_up_max_adds: num(f.get("pyramid_up_max_adds")),
      pyramid_up_lock_pct: num(f.get("pyramid_up_lock_pct")),
      opus_copy_mode: f.get("opus_copy_mode") === "1",
      opus_solo_open: f.get("opus_solo_open") === "1",
      opus_solo_min_consensus: num(f.get("opus_solo_min_consensus")),
      opus_lessons_max: num(f.get("opus_lessons_max")),
      opus_history_n: num(f.get("opus_history_n")),
      heartbeat_url: f.get("heartbeat_url"),
      placement_alert_after: num(f.get("placement_alert_after")),
      exchange_timeout_sec: num(f.get("exchange_timeout_sec")),
      simulated_fee_pct: num(f.get("simulated_fee_pct")),
      fee_safety_margin_pct: num(f.get("fee_safety_margin_pct")),
    });
    toast("Đã lưu cấu hình KSS — áp dụng cho phiên mới.", "success");
    refreshTrading(); refreshStatus();
  } else if (form.id === "live-exec-form") {
    e.preventDefault();
    const f = new FormData(form);
    await api("POST", "/api/kss-settings", {
      maker_orders: f.get("maker_orders") === "1",
      order_fill_timeout_sec: num(f.get("order_fill_timeout_sec")),
      live_use_testnet: f.get("live_use_testnet") === "1",
    });
    toast("Đã lưu cấu hình LIVE.", "success");
    refreshStatus();
  } else if (form.id === "grok-fail-mode-form") {
    e.preventDefault();
    const f = new FormData(form);
    await api("POST", "/api/kss-settings", {
      grok_scanner_fail_mode: f.get("grok_scanner_fail_mode"),
      grok_scanner_batch_max: num(f.get("grok_scanner_batch_max")),
      grok_live_search: f.get("grok_live_search") === "1",
      grok_search_max_results: num(f.get("grok_search_max_results")),
    });
    toast("Đã lưu cấu hình Grok scanner.", "success");
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
  }
});

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

// The poll gate suppresses background refreshes while the tab is hidden, so catch up the
// moment it comes back rather than leaving the operator looking at minutes-old numbers.
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") refreshAll();
});


// --- Alpine (CSP build): modal visibility only -------------------------

document.addEventListener("alpine:init", () => {
  // eslint-disable-next-line no-undef
  // The mobile "Automation" popover used an inline `x-data="{ autoOpen: false }"` with
  // `@click="autoOpen = !autoOpen"` and `:aria-expanded="autoOpen.toString()"`. Alpine's
  // CSP build cannot interpret ANY of those three — it allows a bare property reference and
  // nothing else — so the console threw on every status poll and the button did nothing.
  // Same shape as `ui` above: the logic lives here, the markup only names things.
  Alpine.data("autoPopover", () => ({
    autoOpen: false,
    toggleAuto() { this.autoOpen = !this.autoOpen; },
  }));
});
