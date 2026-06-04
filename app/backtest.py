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

from dataclasses import dataclass

from app.data.providers import Candle

_MS_PER_DAY = 86_400_000


@dataclass
class SimResult:
    tp_hit: bool
    days_to_tp: float | None
    waves_filled: int
    hit_deadline: bool
    pnl_pct: float  # realized tp_pct if hit, else mark-to-last (avg vs last close)


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
) -> SimResult:
    """
    Simulate one pyramid entered at candle index `start`.

    Wave 0 fills at the entry price; deeper waves fill when a later bar's low
    reaches their target. Take-profit triggers when a bar's high reaches the
    running average × (1 + tp_pct/100). Stops at `deadline_days`.
    """
    entry = candles[start]["close"]
    entry_ts = candles[start]["ts"]
    targets = _targets(entry, distance_pct, max_waves)
    weights = [n + 1 for n in range(max_waves)]

    filled = 1  # wave 0 fills at entry
    tp_threshold_factor = 1 + tp_pct / 100

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
        if bar["high"] >= avg * tp_threshold_factor:
            return SimResult(True, round(days, 2), filled, False, round(tp_pct, 4))

        if days >= deadline_days:
            last = bar["close"]
            return SimResult(False, None, filled, True, round((last - avg) / avg * 100, 4))

    # Ran out of data before deadline or TP — incomplete trial.
    last = candles[-1]["close"]
    avg = avg_price(filled)
    return SimResult(False, None, filled, False, round((last - avg) / avg * 100, 4))


def estimate_win_rate(
    candles: list[Candle],
    distance_pct: float,
    max_waves: int,
    tp_pct: float,
    deadline_days: float,
    step: int = 1,
    split: float = 0.0,
) -> dict:
    """
    Walk-forward win-rate: roll an entry across history and measure how often TP
    is reached within the deadline. With `split` > 0 the first `split` fraction of
    history is treated as in-sample and metrics are computed only on the remaining
    **out-of-sample** tail — a more honest, regime-current estimate that reduces
    overfitting (supports the loss-minimizing posture).

    A win = TP within deadline; a loss = deadline reached without TP. Incomplete
    trials (not enough look-ahead) are excluded so the rates aren't truncation-biased.

    Returns {win_rate, loss_rate, trials, wins, losses, avg_days_to_tp, bar_days}.
    """
    if not candles:
        return {"win_rate": 0.0, "loss_rate": 0.0, "trials": 0, "wins": 0,
                "losses": 0, "avg_days_to_tp": None, "bar_days": 0.0}

    span_days = (candles[-1]["ts"] - candles[0]["ts"]) / _MS_PER_DAY / max(len(candles) - 1, 1)
    start_at = int(len(candles) * split) if 0 < split < 1 else 0

    wins = losses = trials = 0
    days_sum = 0.0
    for start in range(start_at, len(candles) - 1, max(step, 1)):
        res = simulate_kss(candles, start, distance_pct, max_waves, tp_pct, deadline_days)
        if not res.tp_hit and not res.hit_deadline:
            continue  # incomplete look-ahead
        trials += 1
        if res.tp_hit:
            wins += 1
            days_sum += res.days_to_tp or 0.0
        else:
            losses += 1

    win_rate = (wins / trials * 100) if trials else 0.0
    loss_rate = (losses / trials * 100) if trials else 0.0
    avg_days = (days_sum / wins) if wins else None
    return {
        "win_rate": round(win_rate, 2),
        "loss_rate": round(loss_rate, 2),
        "trials": trials,
        "wins": wins,
        "losses": losses,
        "avg_days_to_tp": round(avg_days, 2) if avg_days is not None else None,
        "bar_days": round(span_days, 4),
    }
