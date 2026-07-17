# OPUS 3%/day — Full Autonomy Inside the Cage

> **Status:** APPROVED 2026-07-16 (Kai) — build P1→P5. Paper-only.
> **Goal (Kai):** OPUS decides fully on its own, covers its own API cost, and targets
> **≥3% net/day on its assigned capital** — where net = gross − trade fees − 2× API cost.
> **Reality framing (agreed):** 3%/day is a *stretch KPI* that regulates behaviour
> (behind-pace ⇒ MORE selective, never more risk). Honest expectation per the 2026-07-12
> audit is ~0.5%/day system-wide. We measure truthfully and hard-brake losers; we do not
> promise 3%.

Extends `docs/opus-godmode-plan.md` (O-FIX/O-COPY/O-LEARN built 2026-06-22, commit
d19e050). O-LIVE was deferred there; this plan supersedes its V1–V3 with Kai's 2026-07-16
decisions.

## 0. Verified state (2026-07-16, paper DB + live probes)

1. **Anthropic key is dead — now 401 invalid** (was 400 no-credit on 2026-06-22). Probe of
   `/v1/messages` with the `.env` key → `401 invalid x-api-key`. **P0 blocker, Kai's task.**
2. OPUS has never traded: `opus_positions` = 0, audit actor `opus` = 0 rows,
   `opus_lessons` = 0, `opus_mode=0`, `opus_shadow=1`.
3. **Cost pollution:** every row in `opus_cost_ledger` is `grok_scanner` (serves the KSS
   scanner) yet `service.spend_today` + `ledger.rollup_hour` sum ALL purposes → the
   scanner eats OPUS's $5/day cap and drags OPUS net P&L.
4. **Config prices 3× too high:** `opus_price_*` = 15/75 $/MTok; Opus 4.8 list price is
   **$5 in / $25 out** (verified platform.claude.com 2026-07-16). With the ×2 multiplier
   OPUS was being charged ~6× reality.
5. **Cache tokens unmetered:** brain.py reads only `input_tokens`/`output_tokens`;
   `cache_read_input_tokens` (0.1×) and `cache_creation_input_tokens` (1.25×, 5m) are
   dropped.
6. `opus_solo_open` knob exists (config/runtime/routes/UI) but nothing in
   `app/orchestrator/` reads it — consensus is still a hard AND-gate.

## 1. Kai's decisions (2026-07-16)

| Decision | Choice |
|---|---|
| KPI | **3%/day stretch** + hard brakes (daily-loss stop, 7-day auto-freeze); measure honestly |
| Autonomy | **Full solo** — OPUS opens without Grok and without a consensus floor (floor knob wired but set 0). Grok keeps either-close veto. Whitelist (scanner candidates), notional cap, ride-SL, breaker, queue all stay |
| Capital | **$500 trial** (from $2,000); scale back up only after the P5 gate passes |
| Scope | Build **P1→P5** in one campaign |

## 2. Phases

### P0 — Revive the brain (Kai, operational)
New Anthropic API key + credit in `.env` (both instances). Done when the OPUS tab badge
shows `brain_health = ok`. Code phases proceed with mocked clients meanwhile.

### P1 — Cost-truth v2 (accounting before measuring)
- `OPUS_OWN_PURPOSES = {decision, grok_decision, distill, triage}`; `spend_today` +
  `rollup_hour` count ONLY those. `grok_scanner` stays in the global AI-cost tab
  (`app/costs.py` unchanged — it groups by purpose already).
- Price defaults 15/75 → **5/25**; meter cache tokens (read 0.1×, 5m-write 1.25×) via new
  additive `opus_cost_ledger` columns.
- `opus_kpi_target_pct` 1.0 → **3.0**.
- Daily (UTC calendar) KPI aggregation + table on the OPUS tab:
  ngày | gross | phí API | net | %/vốn | trades | WR.

### P2 — O-LIVE: the autonomy switch
- Wire `opus_solo_open`: when on, OPUS opens alone; `opus_solo_min_consensus` floor is
  enforced in `policy._open` against the candidate's deterministic consensus (**cage-side,
  not prompt-side**); trial sets floor = 0 per Kai. Grok, when enabled, keeps either-close.
  Grok alone still cannot open (risk role).
- New knob `opus_daily_loss_stop_pct` (default 3.0): today's net ≤ −3% of allocation ⇒
  no new opens until next UTC day (closes still allowed). Enforced in policy, surfaced in
  state + UI.
- Shadow stays ON until trial start; flipping `opus_shadow=0` + `opus_mode=1` is a runtime
  act after P0 + P5 checklist.

### P3 — Fuel for the target: throughput + cost efficiency
- **Haiku triage** (claude-haiku-4-5, ~$0.001/call, purpose="triage"): each tick decides
  "is an Opus decision warranted?"; full Opus call only on triage-yes or every
  `opus_max_decision_gap_min` (default 60). Same $5 cap buys decisions at the RIGHT
  moments.
- New intent `reduce` (partial take-profit on ride) — validated/clamped in policy like
  open/close.
- Snapshot: add per-candidate TA bundle fields (reuse existing `app/ta` builders) and 24h
  momentum for held positions. Token-bounded.

### P4 — Accountability
- Daily Telegram report ~00:05 UTC via `notify.send`: yesterday net vs 3% target, gross,
  API cost, coverage ratio (net/cost), WR, positions.
- **Auto-freeze:** rolling 7-day net < 0 AND ≥5 closed positions ⇒ set `opus_mode=0` +
  notify. Manual re-arm only.

### P5 — Trial & gate (7–14 days, $500, paper)
Success gate to KEEP solo mode / scale capital:
(a) `brain_health=ok` ≥99% of ticks; (b) net > 0 AND net ≥ 2× API cost;
(c) not worse than leaving the same $500 with the KSS engine over the same window
(benchmark view). Fail ⇒ capital returns to the engine, OPUS back to advisory.

Trial runtime config: `opus_allocation_usd=500`, `opus_max_trade_notional=100`
(else one trade = 40% of the envelope), `opus_solo_open=1`, `opus_solo_min_consensus=0`,
`opus_daily_loss_stop_pct=3`, `opus_kpi_target_pct=3`, cap $5/day.

## 3. Invariants (unchanged, FROZEN)
Advisory-LLM-in-a-cage; `policy.apply_intents` is the only order path; candidate
whitelist; K-1 exclusivity; approval queue + circuit breaker; ride hard-SL; capital
isolation from the rule-based engine; behind-pace ⇒ more selective, never more risk.
