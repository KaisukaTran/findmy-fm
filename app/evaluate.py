"""
app/evaluate.py — production-faithful scoring layer for a KSS configuration.

WHY this exists: `app.backtest.estimate_win_rate` is frozen production behaviour (the live
scanner calls it every cycle — see `app/scanner.py`) and reports per-symbol win/loss RATES,
not economics. Ranking two configurations needs one comparable NUMBER: profit earned per
dollar actually tied up, over time — not "win rate", which is blind to how much capital a
session commits or how long it sits there. This module is purely additive: it never changes
`app.backtest` or `app.scanner`, only calls the frozen `estimate_win_rate` with the right
arguments and aggregates the result.

Four defects made a previous strategy-evaluation measurement unusable — a confident
recommendation derived from it reversed completely once corrected. This module is
deliberately faithful to production on all four, by DEFAULT:

  1. COST — every score uses `costengine.round_trip_cost_pct()` (0.30%: 2x taker 0.1% +
     2x slippage 0.05%), never `binance_max_fee_pct * 2` (0.20%, fees only, no slippage).
     The cheaper number undercounts the true round trip and inflates every backtested win.
  2. TRIAL SPACING — `spacing_days` defaults to `settings.backtest_trial_spacing_days`
     (production's value, default 7 days). Omitting it lets overlapping trials replay the
     same regime hundreds of times and masquerade as independent evidence — e.g. ~2884
     overlapping trials where production, decorrelated, sees only ~416.
  3. EFFECTIVE TAKE-PROFIT — the price that actually rests on the book is
     `tp_pct + costengine.tp_fee_buffer_pct()` (about +0.24pp), not the nominal `tp_pct` a
     config dials in (see `app.kss.pyramid.PyramidSession._tp_target_pct`). Scoring the
     nominal number overstates edge — every trial is simulated at the EFFECTIVE target.
  4. BOTH INTRA-BAR BOUNDS — a single bar's true high/low order is unknowable.
     `evaluate_config` always returns the OPTIMISTIC bound (today's live default,
     low-then-high) AND the PESSIMISTIC bound (high-then-low) side by side — a caller can
     read one field alone, but the function never LOSES the other; nothing here reports a
     lone number that hides which bound produced it.
"""

from __future__ import annotations

from dataclasses import dataclass

from app import costengine
from app.backtest import estimate_win_rate
from app.config import settings
from app.data.providers import Candle

_DEFAULT_WAVE0_NOTIONAL_USD = 100.0


@dataclass
class ConfigScore:
    """Economics of one (distance, tp, max_waves) configuration over a set of symbols,
    under ONE intra-bar bound (see `pessimistic` — never trust this alone, see ConfigEvaluation)."""

    distance_pct: float
    tp_pct: float  # the NOMINAL tp_pct passed in — see effective_tp_pct for what was simulated
    max_waves: int
    pessimistic: bool  # which intrabar bound produced this score
    effective_tp_pct: float  # tp_pct + costengine.tp_fee_buffer_pct() — what was ACTUALLY simulated
    cost_pct: float  # costengine.round_trip_cost_pct() used for every trial
    spacing_days: float  # decorrelation spacing actually used

    symbols: int  # symbols that contributed >= 1 completed trial
    trials: int
    wins: int
    losses: int
    stops: int
    flats: int
    win_rate: float
    loss_rate: float
    expectancy: float  # mean net pnl %/trial, cost-adjusted
    avg_mae: float
    worst_mae: float
    avg_waves_filled: float

    total_pnl_usd: float  # Σ (pnl_pct/100 × exit_capital) across all counted trials
    total_capital_days: float  # Σ SimResult.capital_days across all counted trials
    profit_per_dollar_day: float  # total_pnl_usd / total_capital_days — THE ranking metric


@dataclass
class ConfigEvaluation:
    """Both intra-bar bounds for one configuration. Defect #4: never report just one."""

    optimistic: ConfigScore
    pessimistic: ConfigScore


def score_config(
    candles_by_symbol: dict[str, list[Candle]],
    distance_pct: float,
    tp_pct: float,
    max_waves: int,
    *,
    deadline_days: float | None = None,
    sl_pct: float | None = None,
    split: float = 0.0,
    spacing_days: float | None = None,
    wave0_notional_usd: float = _DEFAULT_WAVE0_NOTIONAL_USD,
    pessimistic_intrabar: bool = False,
) -> ConfigScore:
    """Score one configuration across every symbol in `candles_by_symbol`, under ONE
    intrabar bound (see `pessimistic_intrabar`). Prefer `evaluate_config` unless a caller
    genuinely wants only one bound.

    Faithful to production by default (module docstring's four defects):
      - `cost_pct` = `costengine.round_trip_cost_pct()` (defect #1).
      - `spacing_days` defaults to `settings.backtest_trial_spacing_days` (defect #2);
        pass 0 explicitly to reproduce the old, overlapping-trial behaviour for comparison.
      - the take-profit actually simulated is `tp_pct + costengine.tp_fee_buffer_pct()`
        (defect #3) — `effective_tp_pct` on the result records exactly what was used.

    A symbol with zero completed trials (too little history for this config) contributes
    nothing and is not counted in `symbols`.
    """
    eff_deadline = settings.deadline_days if deadline_days is None else deadline_days
    eff_sl = settings.sl_pct if sl_pct is None else sl_pct
    eff_spacing = (
        settings.backtest_trial_spacing_days if spacing_days is None else spacing_days
    )
    effective_tp_pct = tp_pct + costengine.tp_fee_buffer_pct()
    cost_pct = costengine.round_trip_cost_pct()

    trials = wins = losses = stops = flats = 0
    pnl_sum = 0.0
    mae_sum = 0.0
    worst_mae = 0.0
    waves_filled_sum = 0
    total_pnl_usd = 0.0
    total_capital_days = 0.0
    symbols_with_trials = 0

    for candles in candles_by_symbol.values():
        wr = estimate_win_rate(
            candles, distance_pct, max_waves, effective_tp_pct, eff_deadline,
            split=split, sl_pct=eff_sl, cost_pct=cost_pct, spacing_days=eff_spacing,
            pessimistic_intrabar=pessimistic_intrabar, wave0_notional_usd=wave0_notional_usd,
        )
        if wr["trials"] <= 0:
            continue
        symbols_with_trials += 1
        trials += wr["trials"]
        wins += wr["wins"]
        losses += wr["losses"]
        stops += wr["stops"]
        flats += wr["flats"]
        pnl_sum += wr["expectancy"] * wr["trials"]
        mae_sum += wr["avg_mae"] * wr["trials"]
        worst_mae = min(worst_mae, wr["worst_mae"])
        waves_filled_sum += wr["waves_filled_sum"]
        total_pnl_usd += wr["pnl_usd"]
        total_capital_days += wr["capital_days"]

    win_rate = (wins / trials * 100) if trials else 0.0
    loss_rate = (losses / trials * 100) if trials else 0.0
    expectancy = (pnl_sum / trials) if trials else 0.0
    avg_mae = (mae_sum / trials) if trials else 0.0
    avg_waves_filled = (waves_filled_sum / trials) if trials else 0.0
    profit_per_dollar_day = (
        total_pnl_usd / total_capital_days if total_capital_days else 0.0
    )

    return ConfigScore(
        distance_pct=distance_pct, tp_pct=tp_pct, max_waves=max_waves,
        pessimistic=pessimistic_intrabar, effective_tp_pct=round(effective_tp_pct, 4),
        cost_pct=round(cost_pct, 4), spacing_days=eff_spacing,
        symbols=symbols_with_trials, trials=trials, wins=wins, losses=losses, stops=stops,
        flats=flats, win_rate=round(win_rate, 2), loss_rate=round(loss_rate, 2),
        expectancy=round(expectancy, 4), avg_mae=round(avg_mae, 4),
        worst_mae=round(worst_mae, 4), avg_waves_filled=round(avg_waves_filled, 2),
        total_pnl_usd=round(total_pnl_usd, 2), total_capital_days=round(total_capital_days, 4),
        profit_per_dollar_day=round(profit_per_dollar_day, 6),
    )


def evaluate_config(
    candles_by_symbol: dict[str, list[Candle]],
    distance_pct: float,
    tp_pct: float,
    max_waves: int,
    **kwargs: object,
) -> ConfigEvaluation:
    """Score one configuration under BOTH intrabar bounds (defect #4). See `score_config`
    for the accepted keyword arguments; `pessimistic_intrabar` is set by this function and
    must not be passed here."""
    if "pessimistic_intrabar" in kwargs:
        raise TypeError(
            "evaluate_config sets pessimistic_intrabar itself (both bounds) — "
            "call score_config directly for a single bound"
        )
    optimistic = score_config(
        candles_by_symbol, distance_pct, tp_pct, max_waves,
        pessimistic_intrabar=False, **kwargs,
    )
    pessimistic = score_config(
        candles_by_symbol, distance_pct, tp_pct, max_waves,
        pessimistic_intrabar=True, **kwargs,
    )
    return ConfigEvaluation(optimistic=optimistic, pessimistic=pessimistic)
