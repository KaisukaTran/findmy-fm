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

A "win" = take-profit reached within `deadline_days`.
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


def _targets(entry: float, distance_pct: float, max_waves: int) -> list[float]:
    factor = 1 - distance_pct / 100
    return [entry * (factor ** n) for n in range(max_waves)]


def simulate_kss(
    candles: list[Candle],
    start: int,
    distance_pct: float,
    max_waves: int,
    tp_pct: float,
    deadline_days: float,
    sl_pct: float = 0.0,
    cost_pct: float = 0.0,
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
    """
    entry = candles[start]["close"]
    entry_ts = candles[start]["ts"]
    targets = _targets(entry, distance_pct, max_waves)
    weights = [n + 1 for n in range(max_waves)]

    filled = 1  # wave 0 fills at entry
    tp_threshold_factor = 1 + tp_pct / 100
    sl_threshold_factor = 1 - sl_pct / 100

    def avg_price(k: int) -> float:
        num = sum(targets[i] * weights[i] for i in range(k))
        den = sum(weights[i] for i in range(k))
        return num / den if den else entry

    for j in range(start, len(candles)):
        bar = candles[j]
        days = (bar["ts"] - entry_ts) / _MS_PER_DAY

        # Fill deeper waves whose target the bar traded through.
        while filled < max_waves and bar["low"] <= targets[filled]:
            filled += 1

        avg = avg_price(filled)

        # Hard stop-loss first (pessimistic intrabar ordering).
        if sl_pct > 0 and bar["low"] <= avg * sl_threshold_factor:
            return SimResult(False, None, filled, False, round(-sl_pct - cost_pct, 4), stopped=True)

        if bar["high"] >= avg * tp_threshold_factor:
            return SimResult(True, round(days, 2), filled, False, round(tp_pct - cost_pct, 4))

        if days >= deadline_days:
            last = bar["close"]
            return SimResult(False, None, filled, True,
                             round((last - avg) / avg * 100 - cost_pct, 4))

    # Ran out of data before deadline or any exit — incomplete trial.
    last = candles[-1]["close"]
    avg = avg_price(filled)
    return SimResult(False, None, filled, False, round((last - avg) / avg * 100 - cost_pct, 4))


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
) -> dict:
    """
    Walk-forward backtest: roll an entry across history and measure how the live exits
    (TP / stop-loss / deadline) would have played out. With `split` > 0 the first `split`
    fraction is in-sample and metrics use only the **out-of-sample** tail (regime-current,
    less overfit). `sl_pct`/`cost_pct` make each trial use the real stop and fees; a positive
    `spacing_days` decorrelates entries (≥ spacing apart) so one regime can't inflate the rate.

    A win = TP reached (net of cost) before the stop or deadline; a loss = stop-loss hit or
    deadline reached without TP. Incomplete trials (not enough look-ahead) are excluded.

    Returns win_rate (point), win_rate_lb (Wilson 95% lower bound — the trustworthy number),
    loss_rate, expectancy (mean net pnl %/trial — the bottom line), trials, wins, losses,
    stops, avg_days_to_tp, bar_days.
    """
    empty = {"win_rate": 0.0, "win_rate_lb": 0.0, "loss_rate": 0.0, "expectancy": 0.0,
             "trials": 0, "wins": 0, "losses": 0, "stops": 0, "avg_days_to_tp": None,
             "bar_days": 0.0}
    if not candles:
        return empty

    span_days = (candles[-1]["ts"] - candles[0]["ts"]) / _MS_PER_DAY / max(len(candles) - 1, 1)
    start_at = int(len(candles) * split) if 0 < split < 1 else 0

    # Decorrelate overlapping trials: skip ahead `spacing_days` between entries.
    eff_step = max(step, 1)
    if spacing_days > 0 and span_days > 0:
        eff_step = max(eff_step, round(spacing_days / span_days))

    wins = losses = stops = trials = 0
    days_sum = 0.0
    pnl_sum = 0.0
    for start in range(start_at, len(candles) - 1, eff_step):
        res = simulate_kss(candles, start, distance_pct, max_waves, tp_pct, deadline_days,
                           sl_pct=sl_pct, cost_pct=cost_pct)
        if not (res.tp_hit or res.hit_deadline or res.stopped):
            continue  # incomplete look-ahead
        trials += 1
        pnl_sum += res.pnl_pct
        if res.tp_hit:
            wins += 1
            days_sum += res.days_to_tp or 0.0
        else:
            losses += 1
            if res.stopped:
                stops += 1

    win_rate = (wins / trials * 100) if trials else 0.0
    loss_rate = (losses / trials * 100) if trials else 0.0
    avg_days = (days_sum / wins) if wins else None
    return {
        "win_rate": round(win_rate, 2),
        "win_rate_lb": _wilson_lower_bound(wins, trials),
        "loss_rate": round(loss_rate, 2),
        "expectancy": round(pnl_sum / trials, 4) if trials else 0.0,
        "trials": trials,
        "wins": wins,
        "losses": losses,
        "stops": stops,
        "avg_days_to_tp": round(avg_days, 2) if avg_days is not None else None,
        "bar_days": round(span_days, 4),
    }
