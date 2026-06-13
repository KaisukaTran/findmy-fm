# KSS Accounting Consistency — Audit & Cautious Upgrade Plan

> **Status:** PROPOSAL (not started). Paper-only. `app/kss/pyramid.py` math stays FROZEN —
> every fix is built *around* it (guards/config), never inside it.
> **Goal:** eliminate "profit-per-session but loss-on-the-book" exits and any similar
> cost-basis mismatch, with the smallest, test-gated, reversible steps.

## 1. Root cause (confirmed)

There are **two parallel accountings**:
- **Per-session** (`PyramidSession.avg_price`, `total_cost`): drives the **exit DECISIONS** —
  TP at `session_avg × (1+tp%)` ([pyramid.py:368](app/kss/pyramid.py#L368)); SL at
  `session_avg × (1−sl%)`, trailing at `session_peak × (1−trail%)`
  ([pyramid.py:423-429](app/kss/pyramid.py#L423)). Sells `session.total_filled_qty`.
- **Symbol-level aggregate** (`Position.avg_entry_price`): drives the **realized P&L** on
  every fill ([orders.py:288](app/orders.py#L288)).

They agree **only when a single owner holds a symbol**. They diverge when:
- **>1 KSS session on the same coin** (e.g. FET #107 + #112) — lots blend into one
  `Position` avg.
- **KSS + OPUS (or manual) on the same coin** — all route through the same `Position`.

When blended, `session_avg` < true aggregate avg, so a TP fires "in profit per session" but
**realizes a loss on the real book** → the `KSS-TP?` losses on the Loss Analysis page
(3 fills, −$7.11 in the current paper book).

**Important:** the aggregate `Position` book is internally **consistent** — total realized
P&L and equity are CORRECT. The bug is in the **decision basis** (and per-session
attribution), causing premature/underwater exits — not in the headline accounting.

## 2. Other logic-consistency observations (audit)

| # | Observation | Severity |
|---|---|---|
| A | **Fees excluded from session cost** (`total_cost += qty×price`, no fee), but aggregate includes fees → `session_avg` understates true cost; a "3% TP" really nets ~2.6–2.7% after round-trip fees+slippage. Single-session still positive, but thinner than it looks. | low |
| B | **`isolated_fund` is only a per-session cap** (`remaining_fund = isolated_fund − total_cost`); it is NOT checked against **global cash**. Many sessions × $1k can over-commit real capital — only `max_deployed_pct` at *open* time guards this. | medium |
| C | **Oversell clamp masks desync**: when a session sells `total_filled_qty` but the shared `Position` holds less, the clamp ([orders.py:289](app/orders.py#L289)) sells partial; the session marks COMPLETED while the book still holds leftover. | medium |
| D | **Exit qty = `session.total_filled_qty`** assumes the session owns that slice of the shared `Position`; with sharing it can sell another owner's lot. | medium |
| E | Per-session / per-OPUS **unrealized views** can look inconsistent with the aggregate, but the **portfolio equity uses the aggregate only** — so headline numbers are right (informational views差). | info |

## 3. Cautious upgrade plan (phased, test-gated)

### Phase K-1 — Prevent avg blending (LOW risk, do first)
The cheapest, highest-impact fix: **one owner per symbol**, so `session_avg == aggregate_avg`
and every TP/SL decision is on the true basis.
- Cap **1 active KSS session per symbol** (`max_sessions_per_symbol = 1`).
- **Strategy exclusivity**: the scanner must not open a KSS session for a coin that an OPUS
  position currently holds (watch/ride), and OPUS must not open a coin with an active KSS
  session. (The OPUS→KSS **rescue** handoff is a *transfer*, not concurrent ownership — keep
  it allowed: OPUS releases the symbol as KSS adopts it.)
- Result: no blending → no `KSS-TP?` losses; the take-profit becomes a real profit.
- Tests: open blocked when symbol owned by the other strategy / an active session; a
  single-owner TP realizes ≥ 0.

### Phase K-2 — Exit-basis safety net (LOW–MED risk)
Belt-and-suspenders so a residual blend can never sell underwater:
- Before queuing a TP sell, verify it clears the **true cost basis** + round-trip cost:
  gate the queued order on `current_price ≥ Position.avg_entry_price × (1 + round_trip_cost%)`.
  (A guard in `service.manage_open_sessions`, around the frozen `check_tp` — does NOT change
  pyramid math.)
- Fold **fees** into the session TP target so a "TP" is net-positive after costs.
- Tests: TP not queued / deferred when it would realize a loss on the aggregate.

### Phase K-3 — Global fund enforcement (MED risk)
- Enforce `isolated_fund` (and the next-wave cost) against **actual free cash**, not just the
  session cap (fixes obs. B). A per-wave check in the fill→next-wave path.

### Phase K-4 — Per-session lot isolation (LARGER, optional, only if needed)
- Track each session's holdings as its own lot, decoupled from the shared `Position`, so
  realized P&L is attributed per session and exits always hit the session's own lots
  (fixes C/D structurally). Bigger refactor — only if K-1/K-2 prove insufficient.

### Phase K-0 — Diagnostics (parallel, cheap)
- Add a reconciliation signal (session_avg vs Position.avg divergence) to the Loss page /
  audit, so any future blend is visible immediately.

## 4. Execution discipline (why this is "cautious")
- **Frozen math untouched** — all changes are guards/config/queue-gates around `pyramid.py`;
  `test_kss_invariants.py` must stay green.
- **One phase per PR**, each behind a flag where behavior changes, paper-only, with tests.
- **K-1 first** (biggest risk reduction, smallest change). Re-measure the Loss page after K-1
  before deciding whether K-2…K-4 are needed.
- No live trading implications — this is correctness, not new capability.

## 5. Decisions to confirm before starting
1. **K-1 concurrency:** hard-cap **1 session/coin** (recommended) — OK to reduce concurrency?
2. **Strategy exclusivity:** a coin is owned by KSS **or** OPUS at a time (rescue transfer
   still allowed) — agree?
3. Start with **K-1 only**, then re-evaluate from fresh Loss-page data? (recommended)
