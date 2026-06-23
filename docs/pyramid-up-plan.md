# Pyramid-UP + Regime Router — implementation plan

Status: **PLAN / paper-only**. Owner: Opus orchestrates, subagents implement.
Created 2026-06-23. Evaluate ~2026-06-30; promote to live ONLY if effective.

## 1. Motivation (evidence)

WLFI session 26 (`data/findmy.db`): entry 0.05793 → rallied straight up to peak 0.0611
(+5.5%), so the DCA-down ladder's wave-1 dip-buy (target 0.0570) never triggered → only
**$80 of $1,597** reserved was ever deployed → realized **$1.28**. The trailing exit then
locked the +2% floor. Root cause = a single strategy mode (DCA buy-the-dip). A coin that
rips up after entry deploys almost nothing. Other open-source bots (Hummingbot Grid Strike,
cryptobots Breakout/Grid-trending, anti-martingale pyramiding) all add a **scale-into-strength**
branch. We add that branch.

## 2. Goal

Add a SECOND strategy mode `pyramid_up` (Anti-Martingale add-to-winners) plus a **regime
router** in the scanner that tags each candidate `dca_down` vs `pyramid_up` from TA signals
that already exist. Keep `pyramid.py` FROZEN; reuse `dynamic_exit.py` for exits. Paper-only,
behind a default-OFF knob.

## 3. Invariants (the cage — do not weaken)

- `app/kss/pyramid.py` stays FROZEN (DCA-down wave/price/TP/SL math unchanged).
- Pyramid-UP is **Anti-Martingale**: each add STRICTLY smaller (never an inverted pyramid);
  SL moves to BE+ before/at each add → every add is free-roll, net risk ≤ the initial risk.
- All ENTRY gates still apply: `_can_open` budget, per-symbol cap K-1, Grok, entry vetoes.
- Never gate SELL/exit. Exit via existing `dynamic_exit` Ride & Trail + the hard SL.
- Paper-only; `strategy_router_enabled=false` by default → zero behaviour change until ON.

## 4. Phases

### Phase 0 — Regime router (scanner)
- New pure module `app/kss/regime.py`: `classify_mode(signals) -> 'dca_down' | 'pyramid_up'`.
  `pyramid_up` when uptrend (htf up or st up) AND rel_strength_vs_BTC >
  `pyramid_up_min_rel_strength` AND MACDh > 0 AND ADX ≥ `pyramid_up_min_adx`; else `dca_down`
  (default/fallback). Pure, no I/O, unit-testable.
- `scanner.py`: attach `candidate.strategy_mode`, pass to `_open_session`. Behind
  `strategy_router_enabled` (default False).
- `db.py::_ensure_columns`: add `kss_sessions.strategy_mode TEXT DEFAULT 'dca_down'` (additive).

### Phase 1 — Pyramid-UP engine
- New pure-math `app/kss/pyramid_up.py` (mirror pyramid.py discipline; froze after its tests):
  - `add_trigger_price(entry, n) = entry × (1 + pyramid_up_step_pct/100)^n` — triggers ABOVE entry.
  - `add_qty(base_qty, n) = base_qty × (pyramid_up_size_ratio)^n` — DECREASING; first wave largest.
  - `max_adds = pyramid_up_max_adds` (default 2, cap 3).
  - `stop_after_add(avg) = avg × (1 + pyramid_up_lock_pct/100)`, floored at fee break-even.
  - `projected_pyramid_cost(...)` for `isolated_fund` sizing (largest-first).
- `service.py`:
  - `_open_session` dispatches on `strategy_mode`. For `pyramid_up`: fill the base wave at
    market (largest), register add-ons as **armed/conditional waves** (`status='armed'`,
    `trigger_price`) — NOT standing buy-limits (a buy-limit above market is immediately
    marketable and `_paper_execute` would fill it at once).
  - `manage_open_sessions` / `run_position_guard`: when `market ≥ wave.trigger_price` AND the
    prior add is filled AND `n < max_adds` → re-assert per-symbol cap + `_can_open` budget, fill
    the add (paper: marketable price), update avg, then **move SL to BE+**. Audit `pyramid_add`.
  - Exit: reuse `_evaluate_dynamic_exit` (Ride & Trail). Pyramid-up never queues DCA-down waves.

### Phase 2 — UI / knobs / persistence
- `routes.KssSettingsBody`: add `strategy_router_enabled`, `pyramid_up_min_rel_strength`,
  `pyramid_up_min_adx`, `pyramid_up_step_pct`, `pyramid_up_size_ratio`, `pyramid_up_max_adds`,
  `pyramid_up_lock_pct`. (A knob missing here is silently dropped by the form.)
- `templates/partials/kss_settings.html`: new VN section "Chế độ Pyramid-UP (theo đà tăng)" +
  per-field tooltips + config-key shown, matching the existing knob groups.
- Session list/modal: a `strategy_mode` badge (DCA↓ / PYR↑) for paper comparison.

### Phase 3 — Tests + verify (Opus owns)
- `tests/app/test_pyramid_up_invariants.py`: each add strictly smaller (anti-martingale, never
  inverted); SL ≥ BE+ after first add; triggers strictly increasing above entry; max_adds
  respected; per-symbol cap + budget enforced on each add.
- Regime-router tests: WLFI-like signal → pyramid_up; dip/downtrend → dca_down; router OFF →
  always dca_down.
- `test-runner`: full suite + ruff, report pass count.
- Opus paper-verify: hard-restart :8000 (kill listener, relaunch detached), observe a synthetic
  candidate's routing + a paper pyramid_up session adding on a rising mock price.

## 5. Agent delegation (Opus orchestrates)

| Agent | Task |
|---|---|
| kss-architect (read-only) | Validate module placement + how to model armed/conditional up-triggers without touching frozen pyramid.py or the paper-fill semantics. Pre-check. |
| kss-strategy | Implement `pyramid_up.py` + `regime.py` pure math (load kss-spec skill). |
| backend-builder | Wire service.py dispatch + armed-wave trigger loop, db.py column, routes.KssSettingsBody, scanner mode-attach. |
| frontend-htmx | kss_settings.html knob section + session mode badge. |
| test-runner | Run full suite + ruff, report. |
| Opus | Writes verification tests, reviews every diff, runs paper restart + verify, updates memory. |

## 6. Rollout & evaluation

PAPER ONLY. Ship behind `strategy_router_enabled=false`; turn ON in paper after tests green.
Monitor a few days: compare pyramid_up sessions' capital-deployed + realized PnL vs dca_down on
momentum coins. **DO NOT merge to live until evaluated effective (~2026-06-30).**
