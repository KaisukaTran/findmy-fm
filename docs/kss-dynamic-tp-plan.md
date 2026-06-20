# KSS Dynamic Trailing TP/SL — Implementation Plan

Status: **PLAN (approved logic, not yet built)** · Date: 2026-06-20 · Owner: Kai
Revision 2 (2026-06-20): **loss-defense** — trailing exits never realize a loss (incl. fees);
3× fee buffer on BOTH the TP and the SL edge; activation hysteresis; deferred ladder cancel.

Upgrade the KSS exit from a single fixed take-profit (`avg×(1+tp%)`) into a **wave-stepped
trailing channel** that activates once a session has a *real* profit: the stop-loss ratchets UP
through wave-grid levels (always above a fee-safe floor), a floating take-profit rides `gap%` above
it, and the position exits at whichever edge price reaches first. Adds a manual take-profit button
and a Telegram alert.

> **Guiding principle (rev 2):** the trailing channel is a PROFIT-LOCK, not a loss-cut. Both edges
> (TP and SL) are floored at `avg×(1 + 3×round_trip_fee)`, so **no trailing exit ever books a loss —
> not even a fee loss.** The only loss paths that remain are the pre-existing capital-preservation
> cage (deep hard SL / deadline in accumulate mode) and execution gap-slippage — see §9.

---

## 0. Hard constraints
- **`app/kss/pyramid.py` is FROZEN** (`tests/app/test_kss_invariants.py`). Wave/price/TP/SL formulas
  must NOT change. This feature lives entirely in the **service layer** (`app/kss/service.py`, near
  `check_stop`/`manage_open_sessions`) and builds *around* the frozen core. Frozen `check_tp`/
  `check_stop` are bypassed for a session only while the dynamic exit is active (§3.6).
- Every generated order still goes through the **pending-order approval queue** (KSS never executes
  directly).
- **Capital-preservation cage intact:** the dynamic layer may only TIGHTEN risk (raise the stop, lock
  profit). It never loosens a stop (monotonic) and never sells at a loss (fee-safe floor on both edges).

## 1. Locked decisions
| # | Decision | Choice |
|---|---|---|
| D1 | Auto exit trigger + fixed `tp_pct` | **Channel SL↔TP**: sell at `TP = SL×(1+gap)` (price up) OR at `SL` (price down), whichever first. Fixed `tp_pct` superseded once trailing. |
| D2 | SL level | **Wave-grid steps** on a **synthetic upward grid** `avg×(1+d)^k`, k≥1 (the DCA ladder only exists below entry). |
| D3 | DCA ladder when trailing starts | **Cancel remaining pending waves** at activation (one-way). |
| D4 | Telegram alert | Edge-triggered; push temporarily ON for test; durable visibility = session mode in `/summary` + KSS list. |
| D5 | Manual TP button | **Enabled iff trailing mode** (`trail_active`); on press → immediate market-sell ALL at market, no profit/fee gate (deliberate override). |
| **D6** | **Activation hysteresis (rev 2)** | Trailing activates only at `market ≥ avg×(1+d)` — one full wave-spacing above avg. A noise poke just above avg does NOT activate. |
| **D7** | **Zero-loss floors (rev 2)** | BOTH TP and SL are floored at `fee_floor = avg×(1 + exit_fee_mult×rt_fee)`, `exit_fee_mult = 3`. The original Stage-1 "SL below avg to reduce loss" is **SUPERSEDED** — the trailing SL is never below `fee_floor`. |
| **D8** | **Deferred ladder cancel (rev 2)** | Ladder is cancelled at the (now meaningful) activation point `avg×(1+d)`, not on a bare cross of avg. |

Notation: `avg` = session `avg_price`; `d` = `distance_pct`/100; `peak` = `peak_price` (high-water
mark of market); `market` = current price; `gap` = `kss_tp_gap_pct`/100 (default 0.05);
`rt_fee` = round-trip fee fraction (`costengine.round_trip_cost_pct()`/100);
`exit_fee_mult` = `kss_exit_fee_mult` (default 3); **`fee_floor = avg × (1 + exit_fee_mult×rt_fee)`**.

---

## 2. The original 8 logic issues → resolutions (rev 2)

**L1 — Overlap with `trailing_pct`/`check_stop`/K-trail.** The dynamic channel **replaces** legacy
peak-`trailing_pct` for a trailing session while `kss_dynamic_tp_enabled` is on. Precedence §3.6.
Frozen hard SL kept only as an ultimate floor for accumulate mode.

**L2 — `TP=SL+gap` vs fixed `tp_pct`.** Once trailing (D6 threshold), the channel governs all exits;
the fixed `tp_pct` is never executed in dynamic mode.

**L3 — K-2 / fee loss (rev 2: hardened).** BOTH edges floored at `fee_floor` (3× round-trip fee).
`TP = max(SL×(1+gap), fee_floor)`, `SL = max(grid, fee_floor)`. ⇒ **no trailing exit books even a fee
loss.** This SUPERSEDES the original cond-2 (SL below avg): a loss-cutting stop is no longer part of
the trailing layer; cutting a genuine loser remains the job of the accumulate-mode hard SL (§9).

**L4 — Monotonic ratchet.** `SL = max(prev_SL, new_candidate)`; only rises. Mocked on `peak`.

**L5 — No DCA + trail at once.** On activation (D6/D8) cancel all pending DCA wave orders. One-way;
no resurrection.

**L6 — Telegram vs SILENT-BY-DEFAULT.** Transition alert via `notify.event` (respects
`telegram_push_enabled`; push temporarily ON for test). Edge-triggered. Durable = `/summary` tag (§5).

**L7 — Manual TP only in profit (rev 2).** Enabled only when `market ≥ fee_floor` (not merely `>avg`),
so a manual click can never realize a fee loss either. Manual = deliberate → market-sell, bypass defer.

**L8 — "Nearest wave".** Obsolete below avg (Stage 1 removed). Above avg: synthetic grid `avg×(1+d)^k`.

---

## 3. State machine & per-tick algorithm (rev 2)

### 3.1 Modes (per session, persisted)
- `accumulate` — normal DCA; `trail_active = 0`. The deep hard SL + deadline cage applies here (§9).
- `trailing` — profit-lock; `trail_active = 1`. **One-way**, sticky.

### 3.2 Activation (accumulate → trailing) — with hysteresis (D6)
First tick where `kss_dynamic_tp_enabled AND total_filled_qty > 0 AND market ≥ avg×(1+d)`:
1. `trail_active = 1`.
2. Cancel all pending DCA wave orders for the session (L5/D8).
3. Compute initial `trail_sl_price` (§3.3), persist.
4. Edge-triggered Telegram alert (§5) + audit `dyn_tp_activated`.

Because activation requires a full wave-spacing of profit (`avg×(1+d)`, e.g. +1.5%), a noise poke just
above avg neither cancels the ladder nor starts the stop — killing the whipsaw/failed-breakout loss.

### 3.3 SL — volatility-aware trailing stop, snapped to the wave grid (rev 3)
The SL trails the high-water mark by a **volatility-aware** distance (NOT the raw grid step — a fixed
~`d`≈1.5% tolerance stops out on normal noise and chokes upside). It is then snapped DOWN to a wave-grid
level (clean, explainable steps), floored at `fee_floor`, and ratcheted up only.
```
trail_dist% = max( kss_trail_atr_mult × ATR%, kss_trail_min_pct )   # ATR% = TA bundle atr_pct (daily)
target_sl   = peak × (1 − trail_dist/100)
k           = floor( log(target_sl/avg) / log(1+d) )    # snap DOWN to a grid level ≤ target_sl
grid_sl     = avg × (1+d) ** k
sl_cand     = max(grid_sl, fee_floor)                   # never below the 3-fee floor (D7/L3)
trail_sl_price = round( max(trail_sl_price_prev, sl_cand), price_precision )   # ratchet up (L4)
```
`ATR%` (daily) barely moves intraday → `trail_dist` is computed in the 30-min cycle and cached on the
session (`trail_dist_pct`, §4); the fast guard (§10.1) applies the cached value to the live peak/price.
With defaults (`atr_mult=1.0, min_pct=3`): a calm coin trails ~3% below peak, a volatile one ~1×its
daily ATR — riding its normal range, exiting only on a genuine reversal. Early on (peak just above the
activation level) `target_sl` can fall below `avg`; the `fee_floor` clamp then pins SL at break-even+fee
so the stop is **still a non-loss** while the position is young. **SUPERSEDES the rev-1 "grid step just
below peak"** (too tight); the grid is now only the snap-to levels.

### 3.4 TP — spike-grab ceiling (rev 3)
```
tp_price = round( max( trail_sl_price * (1 + gap), fee_floor ), price_precision )   # D7/L3
```
The TP catches a single interval that JUMPS price above the prior tick's `tp_price` (a sharp
continuation / blow-off between checks). It is evaluated against the value carried from the PREVIOUS
tick, BEFORE `peak` advances (§3.5). If `gap` is large vs `trail_dist`, `tp_price` sits above `peak` and
TP rarely fires → the trailing SL is the sole exit (pure chandelier). A high `gap` disables the
spike-grab; a moderate `gap`=5 grabs gap-ups.

### 3.5 Channel exit — ORDER MATTERS (every tick)
Using `trail_sl_price`/`tp_price` carried from the PREVIOUS tick (initial values set at activation, §3.2):
```
1. if   market >= tp_price:       MARKET SELL full qty  (pyramid:{id}:tp)        # spike grab
2. elif market <= trail_sl_price: MARKET SELL full qty  (pyramid:{id}:trail_sl)  # trailing stop
3. else: peak = max(peak, market); recompute trail_dist→SL (ratchet, §3.3)→TP; persist.
```
Both edges ≥ `fee_floor` ⇒ **every (automatic) trailing exit nets ≥ +3×fee (a profit).** Exits queue
through the approval queue like the frozen TP path.

### 3.6 Precedence (per tick, dynamic enabled)
1. `trail_active` → run the channel (§3.5); skip frozen `check_tp` + legacy `trailing_pct`.
2. else accumulate → if `market ≥ avg×(1+d)` activate (§3.2) then channel; else frozen
   `check_tp`/`check_stop` + DCA continue (the cage, §9, lives here).

`kss_dynamic_tp_enabled` OFF → evaluator is a no-op; frozen exits unchanged.

---

## 4. Persistence (additive, via `db._ensure_columns`)
`kss_sessions`: `trail_active` INTEGER NOT NULL DEFAULT 0 · `trail_sl_price` REAL NOT NULL DEFAULT 0.0 ·
`trail_dist_pct` REAL NOT NULL DEFAULT 0.0 (the ATR-based trailing distance, refreshed in the 30-min
cycle, applied by the fast guard). `peak_price` already exists (high-water mark).

## 5. Telegram + `/summary` visibility
- **Edge alert** on activation: `notify.event("trade", "[MODE] {SYM} → trailing-TP | avg SL TP")`
  (respects `telegram_push_enabled`; push temporarily ON for test).
- **`/summary` + KSS list:** per-session tag — `DCA (wave n/N)` vs `trailing-TP (SL=.. TP=..)`.
- Optional per-SL-step alert behind a sub-flag (default off).

## 6. Manual take-profit (D5/L7, rev 3 — user-specified)
- UI: "Chốt lời ngay" button in the session modal, **enabled iff the session is in trailing mode**
  (`trail_active=1`) — NOT gated by current price.
- `POST /api/kss/sessions/{id}/take-profit` → **immediate** MARKET SELL of the full qty at market
  (`source_ref pyramid:{id}:manual_tp`), **bypassing the K-2 defer and every floor** — sells EVERYTHING
  now regardless of current profit (a deliberate user override). HTTP 400 only if the session is not in
  trailing mode or has no filled qty.
- Note: because it is only enabled in trailing mode (the session has already been in profit) and is a
  deliberate manual action, this is the one exit NOT bound by `fee_floor` — by explicit user choice.

## 7. Runtime knobs (Strategy tab, persisted, tooltip)
| Knob | Type | Default | Meaning |
|---|---|---|---|
| `kss_dynamic_tp_enabled` | bool | **False** | Master toggle for the dynamic trailing channel. |
| `kss_tp_gap_pct` | float | **5.0** | TP spike-grab ceiling: this % above the ratcheted SL (§3.4). High value → pure trailing stop. |
| `kss_trail_atr_mult` | float | **1.0** | Trailing-stop distance = this × the coin's daily ATR% (volatility-aware: rides normal range, exits on a real reversal). |
| `kss_trail_min_pct` | float | **3.0** | Floor for the trailing distance — SL never trails closer than this % below the peak (calm coins still get room; never stops on tiny noise). |
| `kss_exit_fee_mult` | float | **3.0** | Fee-safe floor multiplier: both TP and SL floored at `avg×(1 + this×round_trip_fee)`. ≥1; 3 = comfortably above fees so no exit books a fee loss. |
| `kss_exit_check_sec` | int | **90** | Interval of the lightweight `position_guard` loop (§10.1) that checks OPEN-session exits (tickers only), decoupled from the 30-min full scan. Lower = smaller gap window, more ticker calls. |
| `kss_crash_drop_pct` | float | **12.0** | Crash-detect (§10.3): if a guard check sees price drop > this % since the last observation AND price ≤ SL, exit at market immediately. 0 = off. |
| `kss_live_stop_orders` | bool | **False** | LIVE only (§10.2): maintain a resting STOP-MARKET on the exchange at the current SL (server-side gap protection). Inert on paper / when automation off. |

Added to `Settings` (config.py), registered in `runtime.KSS_SETTING_FIELDS`, exposed in the Strategy
tab. Activation threshold + grid spacing reuse the per-session `distance_pct` (immutable mid-session).

## 8. Edge cases
- **Gap up** past `avg×(1+d)` in one candle: activates; SL = grid step ≤ peak (≥ fee_floor); if
  `market ≥ TP` → immediate sell at a locked profit.
- **Small `distance_pct`** (< 3×rt_fee): `fee_floor` clamp binds so SL/TP still net positive.
- **ATR unavailable / data gap**: `trail_dist` falls back to `kss_trail_min_pct` (3%) — never tighter.
- **Young trailing session** (peak just above activation): ATR trail would put `target_sl` below avg →
  `fee_floor` clamp pins SL at break-even+fee (a non-loss), then it ratchets up as peak rises.
- **One-way / sticky**: ladder cancelled at activation; crossing avg back never resets mode or lowers SL.
- **Rounding** to session price precision.
- **Knob off mid-session**: evaluator no-ops; a session already `trail_active` (ladder gone) rides the
  frozen fixed `tp_pct` + hard SL — recommend not toggling off mid-session.
- **Zero filled qty**: guarded (`total_filled_qty > 0`).

## 9. The SL is ONE continuous stop — never added, never removed
There is no separate "hard SL" to add or decide on. The stop is a single always-on mechanism whose
LEVEL changes; it is never dropped:
- **Accumulate (DCA):** SL = `avg×(1-sl%)` (below avg) — the existing cage + deadline.
- **On activation:** SL JUMPS UP to `max(grid, fee_floor) ≥ avg×(1+3·rt_fee)` (above avg) and from
  there only ratchets higher (L4). The dynamic SL is always strictly above both `avg` and the old
  hard SL, so the transition is purely upward — protection is continuous at every instant.

So "hard SL" and "dynamic trailing SL" are the **same stop at different heights**; the plan only ever
RAISES it. Nothing is removed.

**What bounds a loss, then:**
1. **The accumulate-SL level is the worst-case bound — by design, not a gap.** A loss only occurs if a
   coin falls and never recovers to `avg×(1+d)` (so trailing never engages); the stop then cuts at
   `avg×(1-sl%)`. That is the deliberate risk limit (a long-only DCA bot must be able to cut a coin
   that goes to zero). Tunable via `sl_pct`; it is intrinsic to the plan, not an optional add-on.
2. **The ONE genuine execution caveat — gap-slippage.** The stop is evaluated on the scheduler cadence
   (~30 min). Between ticks price can gap THROUGH `trail_sl_price`; the MARKET SELL then fills below
   the SL level — a locked profit can erode toward break-even or, on a violent gap, below avg. This is
   execution risk inherent to ANY periodic stop (dynamic or not), not a logic flaw.
   **Mitigation:** a layered anti-gap execution architecture (decoupled fast guard loop + live native
   stop + crash-detect) — fully specified in **§10**.

## 10. Anti-gap execution architecture (mechanisms 1–3)
A periodic stop on the 30-min scheduler cadence (§9) leaves a wide gap window. Three LAYERED defences
shrink it **without** raising the expensive full-scan frequency — the full scan stays at
`scan_interval_min`; only the cheap ticker-based exit check runs often. (Raising the full-scan rate is
the wrong lever: it re-fetches OHLCV for ~300 symbols + re-runs backtests + a metered Grok call, gains
the scanner nothing on a daily timeframe, and risks the shared-IP rate limit — yet still cannot beat a
sub-minute flash crash.)

### 10.1 Decoupled `position_guard` loop — paper + live
A separate, lightweight async loop (interval `kss_exit_check_sec`, default 90s), independent of the
full-scan cycle. Each tick, for ACTIVE sessions only: fetch cached tickers (`get_current_prices`), run
the dynamic-exit evaluation (§3) + the frozen TP/SL for accumulate sessions. On a triggered exit it
**queues AND executes the SELL immediately** — it does NOT wait for the 30-min `auto_fill` in
`run_cycle` (else a fast detection would still take up to 30 min to fill). Its DB writes serialize with
`run_cycle` via a guard lock (no SQLite-writer collision; mirrors the scan mutex). Touches only
open-session exits — no universe fetch, no backtest, no Grok → negligible cost.

### 10.2 Live native stop order — true ms-level gap defence (LIVE only)
When live keys are present and `kss_live_stop_orders` is on, the guard maintains a resting
**STOP-MARKET** on the exchange at `trail_sl_price` (and at the accumulate hard SL `avg×(1-sl%)` before
trailing): place on activation, **replace when the SL ratchets up**, cancel on manual close / TP fill.
The exchange watches price continuously (ms), so a crash triggers server-side without waiting for our
poll; `reconcile_live_orders` books the fill. Exits are never gated (risk-reducing), so the protective
stop is auto-placed. **Caveats:** a stop-market still slips on a true gap (fills at the next available
price); a stop-limit may not fill if price jumps past the limit. Paper cannot place real orders → paper
relies on §10.1. Inert while live automation is OFF.

### 10.3 Crash-detect on each guard check — backstop
The guard stores each session's last-observed price. If a check sees a drop since the last observation
greater than `kss_crash_drop_pct` (default 12; 0 = off) AND price is at/below the active SL, it exits at
market immediately and audits `gap_exit`. It cannot recover the gapped portion, but caps further bleed,
leaves a trail, and is a hook for future volatility-aware tightening.

**Honest limit (unchanged from §9):** no polling bot eliminates gap loss between observations; only
§10.2 reacts continuously, and even it slips with no liquidity at the level. Layering 10.1+10.2+10.3
shrinks the window to ~seconds (live) / `kss_exit_check_sec` (paper); the residual is irreducible.

## 11. Build phases (TDD; review diff + restart-verify each)
1. **Core math** (pure): `dynamic_sl_tp(...)` → (sl, tp); grid (synthetic upward); `fee_floor` clamp on
   both edges; ratchet; activation predicate (`market ≥ avg×(1+d)`). Unit tests: every branch +
   monotonicity + floor-binding + "never below fee_floor" invariant for both SL and TP.
2. **Wiring** into `manage_open_sessions`: activation (cancel ladder + audit), channel exit (queue
   SELL), precedence vs frozen exits. Tests: sell-at-TP, sell-at-SL (both ≥ fee_floor → profit),
   no-activate on a sub-`d` poke, ladder-cancelled-on-activate, legacy-trailing-superseded, no-op off.
3. **Manual TP**: endpoint + guard (`market ≥ fee_floor`) + modal button. Tests: blocked below floor,
   sells full qty, bypasses defer.
4. **Telegram + `/summary` + knobs**: edge alert (once), per-session mode tag, knobs persist & apply
   (Strategy tab + tooltip). Tests: alert edge-trigger, summary tag, knob round-trip.
5. **Anti-gap execution (§10):** decoupled `position_guard` loop (queue + execute exits immediately;
   guard lock), crash-detect (§10.3), and (live) native stop-order maintenance (§10.2). Tests: guard
   triggers + fills WITHOUT the 30-min cycle; crash-detect fires an immediate market exit on a
   simulated gap; guard lock serializes with `run_cycle`; (live, mocked exchange) stop placed/replaced
   on ratchet, cancelled on close/TP.

## 12. Acceptance criteria
- Frozen suite green (`test_kss_invariants.py` unchanged); full `tests/app` green.
- `kss_dynamic_tp_enabled=False` → behaviour identical to today (regression).
- ON: a session reaching `avg×(1+d)` cancels its ladder, ratchets SL up the grid (always ≥ fee_floor),
  TP floats `gap%` above SL (≥ fee_floor); **every channel exit (TP or SL) nets ≥ +3×fee** — verified
  by an invariant test that no `trail_sl`/`tp` fill is below `fee_floor`.
- A sub-`d` poke above avg does NOT activate (no ladder cancel, no stop).
- Manual TP blocked below `fee_floor`; transition alert fires once; `/summary` shows the mode.
- Residual losses (§9) acknowledged and unchanged: deep hard SL cage kept; gap-slippage mitigated, not
  eliminated.
- **Anti-gap (§10):** the `position_guard` loop executes a triggered exit within `kss_exit_check_sec`
  (not the 30-min cycle), verified live; crash-detect fires an immediate market exit + `gap_exit` audit
  on a simulated > `kss_crash_drop_pct` drop; (live, mocked) a resting stop is placed at the SL,
  replaced when it ratchets, and cancelled on manual close / TP.
- Verified on the running paper instance (restart to load).
