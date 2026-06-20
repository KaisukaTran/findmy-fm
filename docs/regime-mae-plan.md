# Entry evaluation v2 — relative-strength regime gate + MAE discrimination

Status: **PLAN (not built)** · Date: 2026-06-20 · Owner: Kai

Goal: stop opening alt-DCA longs that systematically bleed vs the market. Evidence (2026-06-20):
our open basket was **−4.5%** from entry while **BTC +1.23%** over the same window → we
underperformed BTC by ~5.8pp. Not market beta — a selection/timing problem. The current per-coin
gates (entry-momentum, downtrend veto, MAE metric) miss the cross-sectional "alts weak vs BTC".

## Design constraint (user, 2026-06-20)
**Do NOT block entry solely because BTC is down.** A strong alt that is holding up or outperforming
while BTC falls is a *valid* entry. The gate must judge each coin RELATIVE to the market, and at most
THROTTLE breadth — never a blanket market-off switch on BTC direction alone.

## A. Per-coin relative strength vs BTC (PRIMARY)
For each candidate compute its return over a short lookback (default **7 daily bars**) and BTC's
return over the same window; require:

    coin_return_Nd  >=  btc_return_Nd  -  rel_strength_margin_pct

- A coin UP while BTC is down → outperforming → **passes** (satisfies the constraint above).
- A coin bleeding while BTC holds (the pattern that caused our losses) → **rejected**.
- Knobs: `rel_strength_enabled` (bool), `rel_strength_lookback_bars` (7), `rel_strength_margin_pct`
  (default 2.0 → allow lagging BTC by ≤2%; 0 = must match BTC).
- Cheap: BTC candles fetched once/scan; coin candles already prefetched for the backtest.
- Audit `skipped_rel_strength` (coin, coin_ret, btc_ret).

## B. Breadth-aware ramp (SOFT throttle — never a hard block)
Scale how MANY new sessions open per scan by market breadth, instead of refusing to open:

    breadth = % of scanned universe NOT in a confirmed downtrend
    effective_cap = max(1, round(max_new_sessions_per_scan * breadth_factor(breadth)))

- Strong breadth → full ramp; weak breadth → fewer opens (still takes the strongest RS setups).
- `breadth_factor`: e.g. linear from 0.2 (breadth≤30%) to 1.0 (breadth≥60%); never 0.
- Knob `regime_ramp_enabled` (bool). Reuses the breadth already computed for the downtrend veto.
- Calibration (now): BTC htf=down, breadth ~56% up / 44% down → mild throttle, NOT a stop — exactly
  the intended behaviour (still open the few strongest alts, just fewer of them).

> A and B together: enter only coins strong RELATIVE to BTC (A), and fewer of them when the whole
> market is weak (B). BTC direction alone never blocks a strong alt.

## C. MAE discrimination
Calibration: absolute `worst_mae` is too blunt — a `< −15%` gate rejects **81%** of the universe
(median worst_mae ≈ −45%; nearly every coin had a deep drawdown in 2y). So:
- **C1 (ranking, cheap, do first):** change the open/Grok ranking tiebreak from `avg_mae` →
  `worst_mae` (prefer shallower-tail coins). Metric already exists in `estimate_win_rate`.
- **C2 (gate, careful):** do NOT use an absolute threshold. Either
  - *relative:* reject the worst-quartile `worst_mae` WITHIN each scan (adaptive, never nukes all), or
  - *tail-frequency:* extend the backtest to count the % of trials breaching −X% (a coin that hit
    −40% once ≠ one that regularly hits −20%) and gate on that frequency.
  Recommend the relative-quartile gate for v1; defer tail-frequency (needs a backtest change).

## Build order (TDD each; runtime-tunable + visible knobs)
1. **A — relative-strength gate** (biggest lever, addresses the root episode). Tests: alt-up/BTC-down
   passes; alt-down/BTC-flat rejected; margin honoured.
2. **C1 — rank by worst_mae**. Test: shallow-tail ranks above deep-tail at equal consensus.
3. **B — breadth ramp**. Tests: weak breadth → fewer opens (≥1); strong breadth → full cap.
4. **C2 — relative-quartile MAE gate**. Test: bottom-quartile worst_mae rejected; never all.

All knobs default to **inert/neutral** (e.g. `rel_strength_enabled=False`, `regime_ramp_enabled=False`)
so behaviour is unchanged until explicitly turned on + calibrated on paper.

## Out of scope / explicitly rejected
- Blanket "BTC down → stop all opens" (per the user constraint). BTC direction only feeds the SOFT
  breadth throttle, never a hard per-coin block.
- Absolute `worst_mae` cutoff (nukes the universe).
