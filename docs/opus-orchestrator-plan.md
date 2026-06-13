# OPUS Orchestrator Mode — Detailed Long-Term Plan

> **Status:** PROPOSAL (not started). Paper-only until an explicitly gated live phase.
> **Codename:** OPUS mode (a.k.a. "Apex"). A *separate, independent* full-auto mode that
> runs alongside — never replacing — the current rule-based full-auto.
> **North-star KPI:** ≥ **1% net profit on invested capital per rolling 24h**, where
> *net* = gross PnL − trade fees − **2×** Opus API cost.

This document is the canonical spec for the build. It is intentionally complete; each
phase below is independently shippable and test-gated. Nothing here weakens the existing
"AI free inside a hard cage" posture — the cage (deterministic risk gates, circuit
breaker, kill switch, approval queue, paper-trade) remains inviolable. The new freedom in
requirement #3 (Opus may bypass KSS on winners) is granted *inside* those hard caps only.

---

## 0. Principles (non-negotiable)

1. **LLM is advisory inside a deterministic sandbox.** Opus never executes orders. It
   emits *intents* chosen from a code-validated action set; deterministic code clamps,
   risk-checks, and routes every intent through the existing approval queue. A perfectly
   prompt-injected Opus still cannot breach a hard cap. (Least privilege + structured I/O
   — see Security §8.)
2. **Independence.** OPUS mode has its own switch, its own capital allocation, its own
   tables, its own metrics. Toggling it never affects the current full-auto, and the two
   never manage the same dollars (capital isolation, §7).
3. **Cost-truth.** Every Opus call's token cost is metered and counted (×2) as a cost
   *before* net profit. A trade that doesn't beat its own fees + 2× its share of Opus cost
   is not "profit." The KPI is measured on this net number only.
4. **Paper-first.** All of this runs on the paper simulator until a separate, explicitly
   approved live phase (Phase 7). No real keys, no real venue, before then.
5. **KPI is a target, never a risk mandate.** Falling behind the 1%/24h pace must NEVER
   widen risk beyond the cage. Under-target → the system may only become *more selective*,
   or idle. It may never breach max-drawdown, max-deployed, daily-loss, or notional caps to
   "catch up."

---

## 1. Glossary of the new concepts

| Term | Meaning |
|---|---|
| **OPUS mode** | The advanced full-auto where Opus orchestrates entries/exits. |
| **Invested capital** | The capital *allocated to OPUS mode* (`opus_allocation_usd`), the KPI denominator. (Alternative: only deployed-in-positions capital — a decision to confirm, §11.) |
| **Discretionary trade** | A position Opus opened directly (source=`opus`), initially NOT under KSS. |
| **Watch window** | The first **3h** after a discretionary buy, during which the position is monitored. |
| **Ride** | Post-watch state for a *winner*: Opus keeps discretionary control (may bypass KSS) under hard caps. |
| **KSS rescue** | Post-watch handoff for a *loser*: the held position is adopted into a standard KSS pyramid session (normal DCA/SL/trailing/TP/deadline). |
| **Opus cost (×2)** | `2 × (input_tokens·price_in + output_tokens·price_out)` per call, accumulated. |
| **Net profit** | `realized + unrealized PnL − trade fees − 2×Opus cost`, over the window. |
| **Risk budget** | Remaining headroom under the hard caps at decision time (Opus is told this; code enforces it regardless). |

---

## 2. High-level architecture

```
                 ┌─────────────────────────────────────────────┐
                 │  OPUS scheduler loop (separate cadence)      │
                 │  app/orchestrator/loop.py                    │
                 └───────────────┬─────────────────────────────┘
                                 │ each tick
   ┌───────────────┐   snapshot  ▼   intents (strict JSON)   ┌──────────────────┐
   │ Data / Alpha  │──────────▶ Opus decision call ────────▶ │ Intent validator │
   │ (scanner,     │  (httpx +  app/orchestrator/brain.py    │ + risk clamp     │
   │  market,      │  prompt    Anthropic Messages API,      │ orchestrator/    │
   │  positions)   │  caching)  model=opus, JSON schema)     │ policy.py        │
   └───────────────┘                                          └────────┬─────────┘
                                                                       │ validated orders
                          ┌────────────────────────────────────────────▼─────────┐
                          │  EXISTING CAGE (unchanged):                            │
                          │  approval queue → circuit breaker → kill switch →      │
                          │  paper execution → fills → portfolio                   │
                          └───────────────────────────────────────────────────────┘
                                 │                         │
                  ┌──────────────▼─────┐      ┌────────────▼─────────────┐
                  │ 3h watch monitor   │      │ Cost + KPI ledger        │
                  │ orchestrator/      │      │ orchestrator/ledger.py   │
                  │ watch.py           │      │ (opus_cost, fees, net)   │
                  │ → Ride | KSS rescue│      │ → hourly metrics + chart │
                  └────────────────────┘      └──────────────────────────┘
```

New package **`app/orchestrator/`** (keeps OPUS code isolated from the rule-based engine):
- `loop.py` — the OPUS scheduler tick (independent of `app/scheduler.py`).
- `brain.py` — builds the snapshot, calls Opus (reuses guardian.py's httpx + prompt-cache
  pattern; model = `claude-opus-4-8`), returns parsed intents.
- `policy.py` — the deterministic sandbox: validates/clamps intents to the allowed action
  set and the risk budget; rejects anything out of bounds; emits orders to the queue.
- `watch.py` — the 3h watch-window state machine (Ride vs KSS-rescue).
- `ledger.py` — cost metering, fee aggregation, net-profit + KPI computation, hourly rollups.
- `models.py` — new tables (see §4). `service.py` — orchestration helpers / adopt-into-KSS.

Reused as-is (no edits to frozen code): `app/kss/pyramid.py`, the approval queue
(`orders.py`), `circuit.py`, `runtime.py`, `guardian.py` (optional second veto layer),
`market.py`/providers, `portfolio.py`, `charts.py`.

---

## 3. The OPUS decision loop (one tick)

Cadence: `opus_interval_min` = **5 min** (confirmed §11), cost-bounded by §6.

1. **Build snapshot** (deterministic, compact, untrusted-data-isolated):
   - Account: `opus_allocation_usd`, deployed, cash, equity (MTM), risk budget remaining.
   - Candidates: top-K from the existing scanner (already cost/edge-filtered).
   - Open OPUS positions with state (watch/ride/rescue), age, unrealized PnL.
   - KPI status: net profit last 24h, pace vs 1%/24h, Opus spend today vs cap.
   - Market context: prices, short feature vector per candidate (trend, vol, win-rate).
2. **Call Opus** with a *static* system prompt (cached) defining: role, the EXACT JSON
   action schema, the hard rules it must respect, and that market text is untrusted data.
3. **Parse → validate → clamp** in `policy.py`:
   - Strict JSON schema; reject/repair malformed output (fail-safe = do nothing).
   - Each intent must reference a *validated candidate* or an *existing OPUS position*.
   - Clamp size to: per-trade notional cap, `max_deployed_pct`, remaining risk budget,
     min-notional, fee-floor (net edge ≥ 2× highest fee). Drop anything that can't fit.
   - Optional: run the existing **AI Guardian** as an independent second veto.
4. **Route** surviving intents as orders to the **approval queue** (source=`opus`), which
   then flows through the unchanged cage (breaker/kill-switch/paper-fill).
5. **Record** the decision, token cost (×2), and rationale to the audit log + cost ledger.

> Defense-in-depth: even if steps 1–2 are compromised (hallucination / injection), steps
> 3–4 guarantee no order can exceed a hard cap or touch non-OPUS capital.

---

## 4. Data model (new tables, additive — no migration risk to existing)

- **`opus_positions`** — one row per discretionary trade: `id, symbol, opened_at, entry_price,
  qty, avg_price, state(watch|ride|rescue|closed), watch_started_at, went_negative_at,
  kss_session_id(nullable), realized_pnl, closed_at`.
- **`opus_cost_ledger`** — one row per Opus call: `id, ts, input_tokens, output_tokens,
  price_in, price_out, raw_cost, billed_cost(=2×raw), purpose, request_id`.
- **`opus_metrics_hourly`** — rollups: `hour_ts, gross_pnl, fees, opus_cost_billed,
  net_pnl, invested_capital, net_pct, trades, win_trades`. Drives the chart + KPI.
- **`runtime_config`** keys (no schema change): `opus_mode` (on/off), `opus_allocation_usd`,
  `opus_spend_today`, `opus_kpi_24h`, watch-state bookkeeping.

All OPUS orders/fills are tagged `source="opus"` so they are cleanly separable in
`portfolio.py` and never co-mingle with rule-based full-auto accounting.

---

## 5. The 3-hour rule (requirement #3) — precise state machine

When Opus opens a discretionary BUY → create `opus_positions` row, `state=watch`,
`watch_started_at=now`.

Every monitor tick (`watch.py`, runs each loop):
- **During [0, 3h):** Opus may still actively manage/close the position. (No continuous
  min-tracking needed — the ride/rescue decision uses a single 3h-mark check, confirmed §11.)
- **At/after 3h, evaluate once (single check at the 3h mark — confirmed §11):**
  - **WINNER → `ride`:** if **uPnL ≥ 0 at the 3h mark** (net of fees + cost share).
    Opus retains discretion; it may bypass
    the KSS ladder/TP and ride the winner — **but** the hard caps still apply: circuit
    breaker, max-drawdown, daily-loss, kill switch, and a per-position hard stop
    (`opus_ride_hard_sl_pct`) so a "winner that reverses" can't become an unbounded loss.
  - **LOSER → `rescue`:** if uPnL < 0 at/after 3h, **hand off to standard KSS**: build a
    KSS pyramid session seeded from the held position (`entry≈avg_price`, remaining
    waves/fund per normal config) via `service.adopt_position_into_kss()`. From here the
    *normal* KSS rules govern (DCA distance ladder, SL/trailing, TP, deadline). Opus no
    longer manages this position. Link `kss_session_id`.

`adopt_position_into_kss()` does NOT touch frozen pyramid math — it constructs a session
object around an already-held quantity and registers it with `kss/service.py`. New unit
tests lock this seam (§9).

---

## 6. Cost accounting + KPI (requirements #1 & #5)

- **Opus cost per call** = `input_tokens·price_in + output_tokens·price_out`, read from the
  Anthropic response usage; **billed = 2× raw** (requirement #5). Accumulate in
  `opus_cost_ledger` and a daily counter.
- **Net profit (window)** = `Σ realized PnL + Σ unrealized PnL(OPUS positions) − Σ trade
  fees(OPUS) − Σ billed Opus cost`.
- **KPI** = `net_profit_rolling_24h / invested_capital`. Target ≥ 1%.
- **Cost guardrails (also safety):**
  - `opus_daily_cost_cap_usd` — hard ceiling on Opus spend/day; exceeded → OPUS mode
    auto-pauses new decisions (positions still risk-managed by deterministic code).
  - Decision-rate cap (max Opus calls/hour) and prompt-cache reuse to keep cost low.
  - If 2×Opus-cost makes a strategy structurally unprofitable, the loop *reduces decision
    frequency* automatically (cost-aware backoff).

---

## 7. Independence & capital isolation (requirement #4)

- Separate switch `OPUS_MODE` (persisted in `runtime_config`, like `FULL_AUTO`), **off by
  default**. **Runs concurrently** with the rule-based full-auto on **disjoint capital
  envelopes** (confirmed §11): OPUS sees `opus_allocation_usd`; the rule-based mode sees
  `equity − opus_allocation_usd`. No shared dollars — risk caps are computed per envelope so
  one mode can never spend the other's capital.
- Separate scheduler loop (`orchestrator/loop.py`) so its cadence/failures are isolated
  from `app/scheduler.py`.
- Separate dashboard tab ("OPUS") so observation never confuses the two.

---

## 8. Security (requirement #7)

- **Key handling:** reuse `ANTHROPIC_API_KEY` via `SecretStr`; never logged, never echoed,
  never placed in any prompt or audit payload. Guardian already models this.
- **Prompt-injection / untrusted data:** all market/news text is labelled untrusted in the
  prompt and is *data, not instructions*; Opus output is parsed against a strict JSON
  schema; **the LLM has no tools and no execution path** — it only returns intents that
  `policy.py` re-validates. This is the "predefined action set, least privilege" pattern
  from current research. A successful injection yields, at worst, intents that the sandbox
  rejects.
- **Defense in depth:** approval queue + circuit breaker + kill switch + daily cost cap +
  decision-rate cap + full audit log of every Opus call (purpose, tokens, parsed intents,
  accept/reject). Optional Guardian as a second independent veto.
- **Outbound surface:** only the Anthropic API (already used) + the existing public market
  data provider. No new external endpoints. Web research for *building* this never includes
  secrets, keys, balances, or proprietary strategy text.
- **Paper-only** until Phase 7; live needs its own review (separate keys, testnet first).

---

## 9. Metrics & chart (requirement #2)

- `ledger.py` rolls fills + cost into `opus_metrics_hourly`.
- New partial `/partials/opus` + endpoints: `GET /api/opus` (state, KPI, spend),
  `POST /api/opus` (toggle), `GET /api/opus/metrics` (hourly series).
- **Charts (SVG, CSP-safe, via `charts.py`):**
  - *Net P/L per auto-running hour* (bar/line) — the headline requirement-#2 metric.
  - *Cumulative net profit vs the 1%/24h target line.*
  - *Opus cost (×2) overlay* so cost-vs-profit is visible.
  - KPI gauge: current rolling-24h net % vs 1% target.
- All on a dedicated **OPUS dashboard tab** with Vietnamese hover tooltips (consistent with
  the tooltip system just shipped).

---

## 10. Phased roadmap (each phase = shippable + test-gated)

| Phase | Deliverable | Gate |
|---|---|---|
| **O-0 Scaffolding** | `app/orchestrator/` package, `OPUS_MODE` flag, tables, capital envelope, OPUS dashboard tab (empty). No Opus calls yet. | Tables create; toggle works; existing suite green. |
| **O-1 Cost ledger + metrics + chart** | `ledger.py`, hourly rollups, net-profit math (×2 cost), SVG charts, `/api/opus/metrics`. Driven by *simulated* fills first. | Unit tests on net-profit & KPI math. |
| **O-2 Brain (advisory, shadow)** | `brain.py` calls Opus on the snapshot but runs in **shadow** (intents logged, NOT executed). Measures decision quality + real token cost. | Shadow audit shows sane intents; cost within cap. |
| **O-3 Sandbox execution** | `policy.py` validate/clamp; route accepted intents through the approval queue (paper). Hard caps + Guardian veto wired. | Property tests: no intent ever exceeds a cap; injection corpus rejected. |
| **O-4 3h watch state machine** | `watch.py` + `adopt_position_into_kss()`; Ride vs KSS-rescue. | State-machine tests incl. winner-reverses and loser-handoff. |
| **O-5 KPI control loop** | Cost-aware backoff, selectivity under-target, daily cost cap auto-pause. | Soak on paper; KPI + cost behave; cage never breached. |
| **O-6 Paper soak** | Run OPUS mode on paper for an extended period; daily net-% + drawdown report. | Stable, cage intact, costs sane over weeks. |
| **O-7 (gated) Live** | Testnet adapter, reconciliation, real keys. **Requires explicit go-ahead.** | Only after O-6 proven; separate sign-off. |

---

## 11. Decisions — CONFIRMED (2026-06-05)

1. **KPI denominator** → net % on the **fixed `opus_allocation_usd`** (idle capital is
   penalised; stable, easy to measure).
2. **"Continuously positive for 3h"** → **simply uPnL ≥ 0 at the 3h check** (single
   evaluation at the 3h mark; intra-window dips ignored). Simpler, more "ride"-friendly.
   `watch.py` therefore only needs the 3h-mark sample, not a continuous min-tracker.
3. **Opus allocation (paper)** → default **`opus_allocation_usd = 2000`** (of the $10k
   paper equity). Adjustable via env; confirm exact figure at O-0 if desired.
4. **Daily Opus cost cap** → **$5/day**; **decision cadence → every 5 min**
   (`opus_interval_min = 5`, `opus_daily_cost_cap_usd = 5`).
5. **Concurrency** → **run concurrently** with the current full-auto on **disjoint capital
   envelopes**: OPUS gets `opus_allocation_usd`; the rule-based mode sees
   `equity − opus_allocation_usd`. No shared dollars; both may be on at once.

---

## 12. Realism note (honest framing)

1% net/24h sustained is extremely aggressive (naively ~37× per year compounded). Treat it
as a *target/guardrail that tunes selectivity*, not a promise. The system's job is to pursue
it **without ever loosening the cage**; if the edge isn't there after 2× Opus cost + fees,
the correct behavior is to trade less or idle — capital preservation always wins.

---

### Sources (research, no secrets transmitted)
- TradingAgents multi-agent LLM framework — https://medium.com/@intellectyxai/tradingagents-a-multi-agent-llm-financial-trading-framework-78d08acfef63
- Agentic trading overview — https://wundertrading.com/journal/en/agentic-trading
- LLM guardrails 2026 — https://orq.ai/blog/llm-guardrails
- Design patterns for securing LLM agents vs prompt injection — https://arxiv.org/html/2506.08837v2
- FinVault financial-agent safety benchmark — https://arxiv.org/pdf/2601.07853
- TrustTrade (decision stability) — https://arxiv.org/pdf/2603.22567
