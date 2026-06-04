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
    alert(r.tp_triggered ? "TP triggered — sell queued" : "TP not reached");
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
  async toggleScheduler() {
    const state = await api("GET", "/api/scheduler");
    if (!state.enabled &&
        !confirm("Start the background scheduler? It will scan & manage sessions on an interval.")) return;
    await api("POST", "/api/scheduler", { enabled: !state.enabled });
    refreshAll();
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
