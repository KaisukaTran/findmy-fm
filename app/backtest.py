"""
Backtest / win-rate estimation for the KSS Pyramid DCA strategy.

Replays a pyramid over historical candles using the SAME math as
`app.kss.pyramid.PyramidSession` (see the `kss-spec` skill):

    target_price(n) = entry * (1 - distance_pct/100) ** n      # geometric ladder
    weight(n)       = n + 1                                     # (n+1) pips -> avg weighting
    avg             = Σ target(k)*weight(k) / Σ weight(k)  over filled waves
    take profit when  price >= avg * (1 + tp_pct/100)

Quantities scale all waves equally, so absolute pip size cancels out of the
average — the win/loss outcome depends only on price geometry, which is why this
can run deterministically with no exchange/network calls.

Outcome classification (3-way):
    win  = TP hit before stop or deadline
    loss = stop-loss hit  OR  deadline reached with pnl < 0
    flat = deadline reached with pnl >= 0  (profitable/breakeven timeout exit)

win_rate  = TP-only (excludes profitable deadline exits — keeps the metric strict)
loss_rate = SL or negative-deadline exits only (flat exits do NOT inflate loss_rate)
flat_rate = profitable/breakeven deadline exits
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from app.data.providers import Candle

_MS_PER_DAY = 86_400_000


@dataclass
class SimResult:
    tp_hit: bool
    days_to_tp: float | None
    waves_filled: int
    hit_deadline: bool
    pnl_pct: float  # realized net %: TP→tp−cost, SL→−sl−cost, deadline→(last−avg)/avg−cost
    stopped: bool = False  # hard stop-loss exit (a realized loss, distinct from incomplete)
    mae_pct: float = 0.0  # max adverse excursion: deepest unrealized dip vs running avg (≤ 0, %)
    # --- capital actually held (see simulate_kss's `wave0_notional_usd`) ---
    capital_days: float = 0.0  # Σ over bars of (capital deployed during that bar × bar length in
    # days). Wave 0's capital counts from the ENTRY bar; each deeper rung's capital counts from
    # the bar it filled onward — a session RESERVES a full ladder but only DEPLOYS what filled,
    # so profit must be measured against capital actually tied up over time, not per session.
    exit_capital: float = 0.0  # capital (USD) deployed at the moment the trial ended — multiply
    # by pnl_pct/100 to turn a realized % into realized dollars.


def _targets(entry: float, distance_pct: float, max_waves: int) -> list[float]:
    factor = 1 - distance_pct / 100
    return [entry * (factor ** n) for n in range(max_waves)]


def _fill_price(target: float, bar_open: float) -> float:
    """Return the realistic fill price when a wave triggers.

    Live pyramid execution (app/kss/pyramid.py:generate_wave / service tick)
    places a LIMIT BUY at `target_price`.  When the bar gaps below the target
    on open, the exchange fills the limit at the *open* price (better for the
    buyer), not at the original target.  We mirror that with min(target, open).

    Matches app/kss/pyramid.py:204 — target price = entry * (1 - d/100)^n —
    and the live fill-at-limit semantics: if the market opens below the limit,
    the order executes at the market open (gap-fill), never worse than the limit.
    """
    return min(target, bar_open)


def simulate_kss(
    candles: list[Candle],
    start: int,
    distance_pct: float,
    max_waves: int,
    tp_pct: float,
    deadline_days: float,
    sl_pct: float = 0.0,
    cost_pct: float = 0.0,
    *,
    pessimistic_intrabar: bool = False,
    wave0_notional_usd: float = 100.0,
) -> SimResult:
    """
    Simulate one pyramid entered at candle index `start` with the SAME exits the live
    strategy uses — take-profit, hard stop-loss, and the deadline — so the win/loss
    classification is realistic rather than "ride to TP forever".

    Wave 0 fills at the entry price; deeper waves fill when a later bar's low reaches their
    target (which lowers the running avg, and with it both the TP and SL lines).

    Within a bar, exits are checked stop-loss FIRST (conservative — assume the adverse move
    happens before the favourable one), then take-profit, then deadline:
      - SL  : bar low ≤ avg × (1 − sl_pct/100)  → LOSS, net pnl = −sl_pct − cost
      - TP  : bar high ≥ avg × (1 + tp_pct/100) → WIN,  net pnl =  tp_pct − cost
      - dl  : days ≥ deadline_days              → loss/flat, net pnl = (last−avg)/avg − cost

    `sl_pct=0` disables the stop (legacy behaviour); `cost_pct` is the round-trip fee+slippage
    subtracted from every realized pnl so a "win" reflects net, not gross.

    `pessimistic_intrabar` (default False = today's behaviour, byte-identical): a single bar's
    true high/low ORDER is unknowable. False assumes low-then-high — deeper waves fill first
    (bar low), lowering the average, and the SAME bar's high is then tested against that
    lowered average (SL then TP, both post-fill) — the OPTIMISTIC bound, since it lets the
    same candle "DCA down, then recover" for free. True assumes high-then-low: the take-profit
    is tested FIRST against the average as it stood BEFORE this bar's fills (no free DCA
    benefit), then the fills are applied, then the stop-loss is tested against the resulting
    post-fill average — the PESSIMISTIC bound. Neither is "correct"; the truth for any one bar
    is unknowable, which is why callers that need an honest range report both (see
    `app/evaluate.py`). Measured at distance=3.0/waves=2/tp=2.0: flipping this drops expectancy
    ~1.31%→0.40% and raises the stop rate ~3.9%→13.0%.

    `wave0_notional_usd` sizes `capital_days`/`exit_capital` in real dollars: wave 0 costs
    exactly this many dollars at entry (mirrors `app.kss.pyramid.PyramidSession.pip_size`'s
    `kss_first_wave_usd` override — qty scales (n+1)× per wave, so does dollar cost). It has
    NO effect on `pnl_pct`/`tp_hit`/etc. (those stay ratio-based, as before this field existed)
    and both new fields scale linearly with it, so a caller may rescale after the fact instead
    of re-simulating.
    """
    entry = candles[start]["close"]
    entry_ts = candles[start]["ts"]
    targets = _targets(entry, distance_pct, max_waves)
    weights = [n + 1 for n in range(max_waves)]

    # fill_prices tracks the realistic execution price for each wave (B4: gap-below fill).
    # Wave 0 always fills at the entry close (no gap for the entry bar itself).
    fill_prices = [entry] + [targets[i] for i in range(1, max_waves)]
    filled = 1  # wave 0 fills at entry
    tp_threshold_factor = 1 + tp_pct / 100
    sl_threshold_factor = 1 - sl_pct / 100
    mae_pct = 0.0  # deepest unrealized dip vs the running avg (≤ 0), tracked until exit

    def avg_price(k: int) -> float:
        num = sum(fill_prices[i] * weights[i] for i in range(k))
        den = sum(weights[i] for i in range(k))
        return num / den if den else entry

    # --- capital actually held (additive; SimResult.capital_days/exit_capital) ---
    # unit_qty: coins per weight-point, sized so wave 0 (weight=1, fills at `entry`) costs
    # exactly `wave0_notional_usd` — mirrors PyramidSession.pip_size's kss_first_wave_usd
    # override (qty scales (n+1)×, dollar cost = qty × the price actually paid for that rung).
    unit_qty = wave0_notional_usd / entry if entry > 0 else 0.0

    def wave_cost(i: int) -> float:
        return weights[i] * unit_qty * fill_prices[i]

    def bar_len_days(idx: int) -> float:
        """Duration of candles[idx] in days, derived from consecutive `ts` values — never
        from an assumed timeframe. Falls back to the preceding gap for the final candle,
        which has no next `ts` to measure forward from."""
        if idx + 1 < len(candles):
            return (candles[idx + 1]["ts"] - candles[idx]["ts"]) / _MS_PER_DAY
        if idx > 0:
            return (candles[idx]["ts"] - candles[idx - 1]["ts"]) / _MS_PER_DAY
        return 0.0

    capital_days_acc = 0.0  # integral already closed out, through the END of the previous bar
    deployed_capital = wave_cost(0)  # wave 0 deploys at entry — "counts from entry"
    prev_ts = entry_ts

    def close_capital(idx: int) -> float:
        """capital_days as of an exit inside candles[idx]: the already-closed integral plus
        this bar's own duration held at the CURRENT `deployed_capital`."""
        return round(capital_days_acc + deployed_capital * bar_len_days(idx), 6)

    # Start at the bar AFTER the entry. We buy at `start`'s CLOSE, so that bar's high and low
    # are already in the past — counting them was look-ahead, and it paid the take-profit out
    # of a move we could never have caught. It biased every parameter search toward
    # take-profits too small to actually reach: the smaller the target, the likelier the entry
    # candle's own wick had already cleared it. Measured on SOL (365d daily, 2026-08-30) it
    # reported a 100% win rate in a market that had fallen 48%.
    #
    # Calibrated against 1h candles over the same window (where skipping the entry bar costs an
    # hour, not a day): true avg_days_to_tp at tp=1.5% is 1.47. The old code said 0.48 — 3.0x
    # too fast. This says 1.57, ~7% slow, because `days` is measured from the bar's OPEN
    # timestamp while the buy lands at its close, so it is a strict upper bound quantised to a
    # whole bar. Erring long is the safe direction; shrink it by moving `backtest_timeframe`
    # off `1d` rather than by re-counting the entry bar.
    #
    # `days_to_tp` itself feeds only the deadline sanity-check in agents/aggregator.py and the
    # scanner display — the fix's real payoff is honest expectancy, win-rate and stop-rate.
    for j in range(start + 1, len(candles)):
        bar = candles[j]
        days = (bar["ts"] - entry_ts) / _MS_PER_DAY
        bar_open = bar.get("open", bar["close"])

        # Close out the interval since the previous bar boundary at the capital level that was
        # actually deployed throughout it (wave 0's from entry; each deeper rung's from the bar
        # it filled — untouched by pessimistic_intrabar, which only reorders the EXIT checks
        # below, not when a fill itself happens).
        capital_days_acc += deployed_capital * (bar["ts"] - prev_ts) / _MS_PER_DAY
        prev_ts = bar["ts"]

        if pessimistic_intrabar:
            # Pessimistic: assume this bar's HIGH happened BEFORE any fill, so take-profit must
            # clear the PRE-fill (higher, harder) average — no free same-candle DCA benefit.
            pre_avg = avg_price(filled)
            if pre_avg > 0:
                dd = (bar["low"] - pre_avg) / pre_avg * 100.0
                if dd < mae_pct:
                    mae_pct = dd
                if bar["high"] >= pre_avg * tp_threshold_factor:
                    return SimResult(True, round(days, 2), filled, False,
                                     round(tp_pct - cost_pct, 4), mae_pct=round(mae_pct, 4),
                                     capital_days=close_capital(j),
                                     exit_capital=round(deployed_capital, 6))

        # Fill deeper waves whose target the bar traded through.
        # B4: when the bar opens below the target (gap-down), the limit order
        # fills at the open price (cheaper), not at the target — use _fill_price.
        while filled < max_waves and bar["low"] <= targets[filled]:
            fill_prices[filled] = _fill_price(targets[filled], bar_open)
            deployed_capital += wave_cost(filled)
            filled += 1

        avg = avg_price(filled)

        # Track the worst unrealized drawdown vs the (DCA-lowered) avg before any exit. A coin
        # that "always recovers +tp%" but plunges deep first is worse than one that recovers
        # shallowly — this is the metric that DISCRIMINATES, since win-rate saturates near 100%.
        if avg > 0:
            dd = (bar["low"] - avg) / avg * 100.0
            if dd < mae_pct:
                mae_pct = dd

        if pessimistic_intrabar:
            # Stop-loss tested AFTER this bar's fills (post-fill average) — the mirror of the
            # pre-fill take-profit check above; deadline stays last, also post-fill.
            if sl_pct > 0 and bar["low"] <= avg * sl_threshold_factor:
                return SimResult(False, None, filled, False, round(-sl_pct - cost_pct, 4),
                                 stopped=True, mae_pct=round(mae_pct, 4),
                                 capital_days=close_capital(j),
                                 exit_capital=round(deployed_capital, 6))

            if days >= deadline_days:
                last = bar["close"]
                return SimResult(False, None, filled, True,
                                 round((last - avg) / avg * 100 - cost_pct, 4),
                                 mae_pct=round(mae_pct, 4),
                                 capital_days=close_capital(j),
                                 exit_capital=round(deployed_capital, 6))
        else:
            # Default (byte-identical to before capital_days/pessimistic_intrabar existed):
            # hard stop-loss first, then take-profit, both against the post-fill average — the
            # OPTIMISTIC ordering (low-then-high within the bar).
            if sl_pct > 0 and bar["low"] <= avg * sl_threshold_factor:
                return SimResult(False, None, filled, False, round(-sl_pct - cost_pct, 4),
                                 stopped=True, mae_pct=round(mae_pct, 4),
                                 capital_days=close_capital(j),
                                 exit_capital=round(deployed_capital, 6))

            if bar["high"] >= avg * tp_threshold_factor:
                return SimResult(True, round(days, 2), filled, False, round(tp_pct - cost_pct, 4),
                                 mae_pct=round(mae_pct, 4),
                                 capital_days=close_capital(j),
                                 exit_capital=round(deployed_capital, 6))

            if days >= deadline_days:
                last = bar["close"]
                return SimResult(False, None, filled, True,
                                 round((last - avg) / avg * 100 - cost_pct, 4),
                                 mae_pct=round(mae_pct, 4),
                                 capital_days=close_capital(j),
                                 exit_capital=round(deployed_capital, 6))

    # Ran out of data before deadline or any exit — incomplete trial.
    last = candles[-1]["close"]
    avg = avg_price(filled)
    return SimResult(False, None, filled, False, round((last - avg) / avg * 100 - cost_pct, 4),
                     mae_pct=round(mae_pct, 4),
                     capital_days=close_capital(len(candles) - 1),
                     exit_capital=round(deployed_capital, 6))


def _wilson_lower_bound(wins: int, n: int, z: float = 1.96) -> float:
    """Lower bound of the 95% Wilson score interval for a binomial rate, in percent.

    Honest small-sample win-rate: 1/1 yields ~20%, not 100%. Wide when n is small, so a
    handful of lucky trials can't masquerade as a high-confidence edge.
    """
    if n <= 0:
        return 0.0
    phat = wins / n
    denom = 1 + z * z / n
    centre = phat + z * z / (2 * n)
    margin = z * math.sqrt((phat * (1 - phat) + z * z / (4 * n)) / n)
    return round(max(0.0, (centre - margin) / denom) * 100, 2)


def estimate_win_rate(
    candles: list[Candle],
    distance_pct: float,
    max_waves: int,
    tp_pct: float,
    deadline_days: float,
    step: int = 1,
    split: float = 0.0,
    sl_pct: float = 0.0,
    cost_pct: float = 0.0,
    spacing_days: float = 0.0,
    *,
    pessimistic_intrabar: bool = False,
    wave0_notional_usd: float = 100.0,
) -> dict:
    """
    Walk-forward backtest: roll an entry across history and measure how the live exits
    (TP / stop-loss / deadline) would have played out. With `split` > 0 the first `split`
    fraction is in-sample and metrics use only the **out-of-sample** tail (regime-current,
    less overfit). `sl_pct`/`cost_pct` make each trial use the real stop and fees; a positive
    `spacing_days` decorrelates entries (≥ spacing apart) so one regime can't inflate the rate.
    `pessimistic_intrabar`/`wave0_notional_usd` pass straight through to `simulate_kss` — see
    its docstring; default False/100.0 reproduce today's behaviour exactly.

    A win = TP reached (net of cost) before the stop or deadline; a loss = stop-loss hit or
    deadline reached with negative pnl; a flat = deadline reached with pnl >= 0.
    Incomplete trials (not enough look-ahead) are excluded.

    Returns win_rate (point), win_rate_lb (Wilson 95% lower bound — the trustworthy number),
    loss_rate (SL + negative-deadline only), flat_rate, flats count, expectancy
    (mean net pnl %/trial — the bottom line), trials, wins, losses, stops,
    avg_days_to_tp, bar_days, capital_days (Σ SimResult.capital_days across counted trials),
    pnl_usd (Σ realized dollars = pnl_pct/100 × exit_capital across counted trials — divide by
    capital_days for profit-per-dollar-day), waves_filled_sum (Σ SimResult.waves_filled, for an
    average-waves-filled rollup across many symbols).

    B3: win_rate is TP-only; loss_rate excludes flat (profitable/breakeven deadline) exits so
    the max_loss_rate gate is not punished by sessions that close out-of-time with a gain.
    """
    empty = {"win_rate": 0.0, "win_rate_lb": 0.0, "loss_rate": 0.0, "flat_rate": 0.0,
             "expectancy": 0.0, "trials": 0, "wins": 0, "losses": 0, "flats": 0,
             "stops": 0, "avg_days_to_tp": None, "bar_days": 0.0,
             "avg_mae": 0.0, "worst_mae": 0.0,
             "capital_days": 0.0, "pnl_usd": 0.0, "waves_filled_sum": 0}
    if not candles:
        return empty

    span_days = (candles[-1]["ts"] - candles[0]["ts"]) / _MS_PER_DAY / max(len(candles) - 1, 1)
    start_at = int(len(candles) * split) if 0 < split < 1 else 0

    # Decorrelate overlapping trials: skip ahead `spacing_days` between entries.
    eff_step = max(step, 1)
    if spacing_days > 0 and span_days > 0:
        eff_step = max(eff_step, round(spacing_days / span_days))

    wins = losses = flats = stops = trials = 0
    days_sum = 0.0
    pnl_sum = 0.0
    mae_sum = 0.0
    worst_mae = 0.0
    capital_days_sum = 0.0
    pnl_usd_sum = 0.0
    waves_filled_sum = 0
    for start in range(start_at, len(candles) - 1, eff_step):
        res = simulate_kss(candles, start, distance_pct, max_waves, tp_pct, deadline_days,
                           sl_pct=sl_pct, cost_pct=cost_pct,
                           pessimistic_intrabar=pessimistic_intrabar,
                           wave0_notional_usd=wave0_notional_usd)
        if not (res.tp_hit or res.hit_deadline or res.stopped):
            continue  # incomplete look-ahead
        trials += 1
        pnl_sum += res.pnl_pct
        mae_sum += res.mae_pct
        worst_mae = min(worst_mae, res.mae_pct)
        capital_days_sum += res.capital_days
        pnl_usd_sum += res.pnl_pct / 100.0 * res.exit_capital
        waves_filled_sum += res.waves_filled
        if res.tp_hit:
            wins += 1
            days_sum += res.days_to_tp or 0.0
        elif res.stopped:
            # Hard stop-loss: always a loss.
            losses += 1
            stops += 1
        elif res.hit_deadline and res.pnl_pct >= 0:
            # B3: deadline exit that is profitable or breakeven — flat, not a loss.
            flats += 1
        else:
            # Deadline exit with negative pnl — genuine loss.
            losses += 1

    win_rate = (wins / trials * 100) if trials else 0.0
    loss_rate = (losses / trials * 100) if trials else 0.0
    flat_rate = (flats / trials * 100) if trials else 0.0
    avg_days = (days_sum / wins) if wins else None
    return {
        "win_rate": round(win_rate, 2),
        "win_rate_lb": _wilson_lower_bound(wins, trials),
        "loss_rate": round(loss_rate, 2),
        "flat_rate": round(flat_rate, 2),
        "expectancy": round(pnl_sum / trials, 4) if trials else 0.0,
        "trials": trials,
        "wins": wins,
        "losses": losses,
        "flats": flats,
        "stops": stops,
        "avg_days_to_tp": round(avg_days, 2) if avg_days is not None else None,
        "bar_days": round(span_days, 4),
        # Drawdown discrimination: avg_mae = mean deepest dip vs avg per trial (≤ 0); worst_mae =
        # the single worst across trials. Shallower (closer to 0) = safer entry → ranked higher.
        "avg_mae": round(mae_sum / trials, 4) if trials else 0.0,
        "worst_mae": round(worst_mae, 4),
        # Capital actually held (see SimResult.capital_days) and the dollars it earned — the
        # ratio pnl_usd/capital_days is "profit per dollar-day", app/evaluate.py's ranking metric.
        "capital_days": round(capital_days_sum, 4),
        "pnl_usd": round(pnl_usd_sum, 4),
        "waves_filled_sum": waves_filled_sum,
    }
