# OPUS "God Mode" — Make It Actually Profitable

> **Status:** PROPOSAL (approved scope, not yet built). Paper-only.
> **Decision (2026-06-22, user):** (1) "God mode" = *super-intelligence inside the hard
> cage* — learn + copy + maximal information, turn shadow off and loosen consensus so OPUS
> may act, **but every hard cap stays inviolable** (ride-SL, per-trade notional cap,
> approval queue, circuit breaker, kill switch, paper-only). (2) Brain = real **Opus 4.8**
> once the Anthropic account is topped up. (3) Build **O-COPY and O-LEARN in parallel**.
> **North-star KPI (unchanged):** ≥ 1% net/24h on the OPUS allocation, where net = gross −
> trade fees − 2× Opus API cost.

This extends `docs/opus-orchestrator-plan.md`. It does **not** weaken the "AI free inside a
hard cage" posture. The new capability is *better information + a learning/copying loop +
the right to act on a single strong signal* — all still clamped by `policy.py`.

---

## 0. Root cause — why OPUS has produced $0 (verified 2026-06-22)

Evidence from the live paper DB (`data/findmy.db`) + a direct API probe:

| Finding | Evidence |
|---|---|
| OPUS never opened a single trade | `opus_positions` = **0 rows** |
| The Opus brain call fails 100% of the time | audit actor `opus` has only `decide_error` ×4; a direct probe of the configured key+model returns **HTTP 400 — "Your credit balance is too low to access the Anthropic API."** |
| The failure is invisible | `brain.decide` swallows the status: it logs only `type(exc).__name__` (`HTTPStatusError`), never the 400 body — see `app/orchestrator/brain.py:172-175`. |
| Even a live brain wouldn't open | OPEN needs **both** Opus *and* Grok to agree (`consensus.combine`, `app/orchestrator/consensus.py:21`). With Opus dead, `agreed_open=0` on every tick (4/4 on 2026-06-20). |
| Shadow blocks execution anyway | `opus_shadow=1` in `runtime_config` — intents are logged, never routed (`policy.apply_intents`, `app/orchestrator/policy.py:137-143`). |
| The brain is information-starved | `brain._candidates` (`app/orchestrator/brain.py:68-77`) forwards only `consensus/win_rate/est_days_to_tp`; it **drops** `expectancy`, `win_rate_lb`, `trials`, `decision`, `reason` that already exist on `Candidate` (`app/models.py:316`). OPUS is asked to make alpha with *less* data than the free rule-based engine. |
| Total realized "loss" so far | `opus_metrics_hourly.net_pnl = −$0.013` (API cost only; 0 trade fees). |

**Conclusion:** OPUS is not "unprofitable" — it has never run. Five blockers stack up:
no API credit → no intents → AND-gate kills opens → shadow kills execution → starved
snapshot would make it weak even if the first four were fixed. The plan removes all five.

---

## 1. Principles (reaffirmed — the cage stays)

1. **LLM is advisory inside a deterministic sandbox.** Opus still only emits *intents*;
   `policy.apply_intents` remains the ONLY path to an order and still clamps to
   `opus_max_trade_notional`, the capital envelope, the per-symbol K-1 exclusivity, the
   approval queue, and the circuit breaker. God mode adds *information and permission to
   act*, never the ability to breach a hard cap.
2. **More-conservative-only.** Behind-pace on the KPI may only make OPUS *more* selective
   or idle — never widen risk to chase the target.
3. **Cost-truth (×2).** Every Opus/Grok call is still metered ×2 before it counts as net.
4. **Paper-first.** No live keys/venue. All new knobs are runtime-editable + UI-visible.
5. **The rule-based engine is the teacher.** "Copy" means OPUS learns from the engine that
   already wins; it is never allowed to manage the same dollars (capital isolation holds).

---

## 2. Phases

### Phase O-FIX — unblock the brain + make failure visible (foundation)

The minimum to get OPUS *running and observable*.

- **F1 — Surface the real error.** In `brain._call_opus`/`decide` (and `grok.decide`),
  on `httpx.HTTPStatusError` capture `exc.response.status_code` + a truncated body and
  audit `decide_error{status, detail}`. A credit/key outage must be loud, not silent.
- **F2 — OPUS health on the dashboard.** Add to `service.state()` a `brain_health` field
  derived from the last `decide` audit row (`ok` / `http_400_credit` / `http_401_key` /
  `parse` / `disabled`). Surface a badge in `templates/partials/opus.html`. This is the
  signal that would have caught the dead brain on day one.
- **F3 — Enrich the snapshot.** `brain._candidates` forwards the full row:
  `expectancy`, `win_rate_lb`, `trials`, `decision`, `reason`, plus per-candidate live
  `price`. `build_snapshot` already has prices — just stop dropping columns.
- **F4 — Credit prerequisite.** Operational, not code: top up the Anthropic API account
  used in `.env` (`ANTHROPIC_API_KEY`). Build can proceed and be tested with a mocked
  client; the live trial only starts after credit is confirmed (F2 badge = ok).

*Tests:* `decide` on a 400 records `status=400` + `brain_health=http_400_credit`;
snapshot includes the new fields; mocked 200 path unchanged.

### Phase O-COPY — copy the winning engine (the biggest profit lever)

OPUS today guesses with thin data while a profitable deterministic engine runs next to it.
Give OPUS that engine's eyes and its moves.

- **C1 — Full scanner signal in the prompt.** Extend the snapshot candidates with the
  same evidence the rule-based gate trades on: the TA bundle (`app/ta/`), relative-strength
  vs BTC, market breadth/regime, `avg_mae`/`worst_mae`. Reuse the existing builders; do not
  recompute. The system prompt explains each field (mirror the Grok scanner prompt).
- **C2 — Mirror feed: show OPUS what the engine actually did.** Add a `rule_engine` block
  to the snapshot: the latest scan's `decision="trade"` symbols (what the engine is about
  to open) and recent KSS opens/exits with outcomes. OPUS sees the teacher's moves and may
  align with or diverge from them — explicitly, with a reason.
- **C3 — `copy_mode` knob (`opus_copy_mode`, default off).** When on, the prompt instructs
  OPUS to *prefer* symbols the engine endorsed this scan and justify any divergence. This
  is a soft bias, not a hard filter — OPUS keeps discretion but starts from proven picks.
  Anti-injection unchanged: `policy._open` still only allows current scanner candidates.

*Tests:* snapshot carries `ta`/`rel_strength`/`rule_engine`; with `copy_mode` on, the
system prompt contains the copy directive; `policy._open` still rejects a non-candidate
symbol even when OPUS "copies" it.

### Phase O-LEARN — learn from its own and the engine's history (parallel with O-COPY)

Today every call is amnesiac. Give OPUS a memory of what worked.

- **L1 — Outcome ledger in the snapshot.** A `self_history` block: OPUS's last N closed
  positions (symbol, hold hours, realized PnL, ride/rescue outcome) + its rolling win-rate
  and net/24h. Built from `OpusPosition` + `OpusMetricHourly` (already persisted).
- **L2 — Distilled lessons table.** New additive table `opus_lessons` (id, ts, scope,
  lesson_text, evidence_json). A periodic distiller (cheap, throttled — reuse the cost-cap
  backoff) asks Opus to summarize recent wins/losses into ≤N short lessons; the top lessons
  are injected into the *static* (cached) system block so learning compounds across calls,
  not just within one. Hard-capped count + length so the prompt can't balloon.
- **L3 — Feedback in the decision prompt.** The decision turn states the current KPI gap
  and the active lessons, with the standing rule: behind-pace ⇒ be *more* selective.

*Tests:* `self_history` reflects closed `OpusPosition` rows; distiller writes bounded
`opus_lessons`; lessons are injected and respect the count/length cap.

### Phase O-LIVE — let OPUS act, inside the cage (paper)

Turn the remaining keys, all runtime-editable on the Strategy tab, all cap-preserving.

- **V1 — Shadow off.** `opus_shadow=false` so `policy.apply_intents` routes orders.
- **V2 — Loosen consensus (`opus_solo_open`, default off).** When on, OPUS may open on its
  own *strong* conviction without Grok's second vote — gated by a confidence threshold
  `opus_solo_min_consensus` (the candidate's deterministic consensus must clear a floor).
  CLOSE stays union (either agent can exit). Grok, when enabled, still votes; solo is the
  fallback when Grok is off or abstains. The AND-gate was the entry brake; this trades it
  for a *signal-strength* brake, which is still a cage, not a hole.
- **V3 — Allocation & cadence knobs** (`opus_allocation_usd`, `opus_interval_min`,
  `opus_daily_cost_cap_usd`) reviewed for the trial. No change to `opus_max_trade_notional`
  or `opus_ride_hard_sl_pct` (hard caps).

*Tests:* with `opus_solo_open` on and a high-consensus candidate, a single Opus open
intent executes; below the floor it is rejected; all `opus_max_trade_notional` /
envelope / frozen-breaker clamps still fire.

### Phase O-EVAL — measure, then trust

- KPI net/24h for OPUS vs the rule-based engine over the same window (reuse
  `ledger.metrics_series` + the savings/cost views).
- A short paper trial (days, like the capital-auto-sizing trial) before any talk of live.
- Decision rule: keep `opus_solo_open`/`copy_mode` only if OPUS beats its own 2× cost AND
  is not net-negative vs simply leaving the capital with the rule-based engine.

---

## 3. New runtime knobs (all on the Strategy tab, persisted, tooltip + config-key shown)

| Knob | Default | Meaning |
|---|---|---|
| `opus_copy_mode` | off | Bias OPUS toward symbols the rule-based engine endorsed this scan. |
| `opus_solo_open` | off | Allow OPUS to open without Grok's vote when conviction clears the floor. |
| `opus_solo_min_consensus` | 70 | Deterministic-consensus floor required for a solo open. |
| `opus_lessons_max` | 8 | Max distilled lessons injected into the system prompt. |
| `opus_history_n` | 20 | Closed positions shown in the self-history block. |

`opus_shadow`, `opus_allocation_usd`, `opus_interval_min`, `opus_daily_cost_cap_usd`,
`opus_max_trade_notional`, `opus_ride_hard_sl_pct` already exist; ensure all are present in
`routes.KssSettingsBody`/the OPUS settings body (a missing field silently drops the knob —
the bug fixed for KSS on 2026-06-20).

## 4. Test & rollout

- TDD per phase; full suite + ruff must stay green; report the pass count.
- O-FIX, O-COPY, O-LEARN are independently shippable; O-LIVE flips the switches last.
- Promote paper→live only by `git merge` after an explicit go-ahead (never edit live).
- `pyramid.py` and all hard caps untouched (FROZEN). Capital isolation between OPUS and the
  rule-based engine is preserved end to end.

## 5. Out of scope / non-goals

- Removing any hard cap (ride-SL, notional cap, approval queue, kill switch). Explicitly
  rejected by the user's "super-intelligence inside the cage" choice.
- Letting OPUS and the rule-based engine manage the same dollars.
- Live trading. Separate, explicitly gated phase later.
