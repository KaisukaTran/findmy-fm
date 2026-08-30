"""
Tests for app/evaluate.py — the production-faithful scoring layer.

WHY each rule exists (see app/evaluate.py's module docstring for the full story): a previous
strategy-evaluation backtest could not rank two configurations, and a confident
recommendation derived from it reversed completely once corrected. Four defects made the old
measurement unusable; each test below pins one so it can never silently regress:

  1. COST must be costengine.round_trip_cost_pct() (0.30% by default settings: 2x taker 0.1%
     + 2x slippage 0.05%), never the cheaper binance_max_fee_pct*2 (0.20%, fees only, no
     slippage) — the cheaper number undercounts the true round trip and inflates every win.
  2. TRIAL SPACING must default to settings.backtest_trial_spacing_days (production's value,
     7 days) — never silently fall back to 0, which lets overlapping trials replay one regime
     hundreds of times and masquerade as independent evidence.
  3. The EFFECTIVE take-profit actually simulated is tp_pct + costengine.tp_fee_buffer_pct()
     (the price genuinely resting on the book, ~+0.24pp by default) — never the bare nominal
     tp_pct a config dials in.
  4. BOTH intrabar bounds (optimistic AND pessimistic — see app/backtest.py's
     pessimistic_intrabar) must always be reported together, never a single number that hides
     which bound produced it.

No network calls; no DB. Candles are synthetic, built the same way as tests/app/test_backtest.py.
"""

from __future__ import annotations

import pytest

from app import costengine
from app.config import settings
from app.evaluate import ConfigEvaluation, evaluate_config, score_config

_DAY = 86_400_000


def candle(day, close, high=None, low=None, open_=None):
    o = open_ if open_ is not None else close
    return {"ts": day * _DAY, "open": o, "high": high or close,
            "low": low if low is not None else close, "close": close, "volume": 1.0}


def _all_win_uptrend(n=60, start=100.0, step=0.01):
    """Steady 1%/day uptrend — every walk-forward trial reaches TP well before the deadline,
    so pnl_pct is a CLEAN, deterministic function of the effective tp and the cost. Useful for
    pinning defects 1 and 3 to an exact number rather than a loose direction."""
    out, price = [], start
    for d in range(n):
        out.append(candle(d, price, high=price, low=price * 0.999))
        price *= (1 + step)
    return out


# ---------------------------------------------------------------------------
# Defect 1: cost must be costengine.round_trip_cost_pct(), not binance_max_fee_pct * 2
# ---------------------------------------------------------------------------

def test_cost_used_is_round_trip_cost_not_binance_fee_only():
    """The simulated round-trip cost is costengine.round_trip_cost_pct() (0.30% by default
    settings), which is strictly larger than the old, wrong `binance_max_fee_pct * 2` (0.20%,
    fees only, no slippage) — proving the cheaper formula is NOT what gets simulated."""
    correct_cost_pct = 2 * settings.taker_fee_pct + 2 * settings.slippage_pct
    wrong_cost_pct = 2 * settings.binance_max_fee_pct
    assert correct_cost_pct != wrong_cost_pct, "fixture must actually distinguish the two formulas"

    effective_tp_pct = 3.0 + costengine.tp_fee_buffer_pct()
    score = score_config({"FAKE": _all_win_uptrend()}, distance_pct=2.0, tp_pct=3.0,
                         max_waves=1, spacing_days=0)

    assert score.trials > 0
    assert score.win_rate == 100.0
    assert score.expectancy == pytest.approx(round(effective_tp_pct - correct_cost_pct, 4), abs=1e-3)
    assert score.expectancy != pytest.approx(effective_tp_pct - wrong_cost_pct, abs=1e-3)
    assert score.cost_pct == round(costengine.round_trip_cost_pct(), 4)


# ---------------------------------------------------------------------------
# Defect 2: spacing_days must default to settings.backtest_trial_spacing_days
# ---------------------------------------------------------------------------

def test_spacing_days_defaults_to_the_production_setting(monkeypatch):
    """score_config(spacing_days=None) — the default — must forward
    settings.backtest_trial_spacing_days to estimate_win_rate, never silently fall back to 0
    (which would let overlapping trials replay one regime hundreds of times)."""
    import app.evaluate as evaluate_module

    monkeypatch.setattr(settings, "backtest_trial_spacing_days", 13.5)
    captured: dict = {}
    original = evaluate_module.estimate_win_rate

    def spy(*args, **kwargs):
        captured["spacing_days"] = kwargs.get("spacing_days")
        return original(*args, **kwargs)

    monkeypatch.setattr(evaluate_module, "estimate_win_rate", spy)

    score_config({"FAKE": _all_win_uptrend()}, distance_pct=2.0, tp_pct=3.0, max_waves=1)

    assert captured["spacing_days"] == 13.5


def test_spacing_days_explicit_override_is_not_replaced_by_the_setting(monkeypatch):
    """An explicit spacing_days=0 (the old, overlapping-trial behaviour, kept reachable ON
    PURPOSE for comparison) must reach estimate_win_rate as 0, not be silently promoted to
    the settings default."""
    import app.evaluate as evaluate_module

    monkeypatch.setattr(settings, "backtest_trial_spacing_days", 13.5)
    captured: dict = {}
    original = evaluate_module.estimate_win_rate

    def spy(*args, **kwargs):
        captured["spacing_days"] = kwargs.get("spacing_days")
        return original(*args, **kwargs)

    monkeypatch.setattr(evaluate_module, "estimate_win_rate", spy)

    score_config({"FAKE": _all_win_uptrend()}, distance_pct=2.0, tp_pct=3.0, max_waves=1,
                spacing_days=0)

    assert captured["spacing_days"] == 0


# ---------------------------------------------------------------------------
# Defect 3: the take-profit actually simulated is tp_pct + tp_fee_buffer_pct()
# ---------------------------------------------------------------------------

def test_effective_tp_pct_adds_the_fee_buffer_and_is_what_gets_simulated():
    """effective_tp_pct on the result is tp_pct + costengine.tp_fee_buffer_pct() — strictly
    above the nominal tp_pct — and expectancy on an all-win fixture proves that EFFECTIVE
    number, not the nominal one, is what was actually simulated."""
    buffer_pct = costengine.tp_fee_buffer_pct()
    assert buffer_pct > 0, "fixture assumes tp_fee_coverage/binance_max_fee_pct are non-zero"

    score = score_config({"FAKE": _all_win_uptrend()}, distance_pct=2.0, tp_pct=3.0,
                         max_waves=1, spacing_days=0)

    assert score.tp_pct == 3.0  # the nominal value is preserved on the result...
    assert score.effective_tp_pct == round(3.0 + buffer_pct, 4)  # ...but this is what ran
    assert score.effective_tp_pct > score.tp_pct

    cost_pct = costengine.round_trip_cost_pct()
    # If the NOMINAL tp had been simulated instead, expectancy would be 3.0 - cost_pct —
    # measurably smaller than what an all-win fixture actually reports.
    nominal_expectancy = round(3.0 - cost_pct, 4)
    assert score.expectancy > nominal_expectancy
    assert score.expectancy == pytest.approx(round(score.effective_tp_pct - cost_pct, 4), abs=1e-3)


# ---------------------------------------------------------------------------
# Defect 4: both intrabar bounds must always be reported together
# ---------------------------------------------------------------------------

def _sl_tp_divergence_fixture():
    """Same fixture as test_backtest.py's
    test_pessimistic_flip_turns_a_stop_loss_into_a_take_profit: one bar where the optimistic
    bound is a stop-loss and the pessimistic bound is a take-profit win — a real, measurable
    behavioural difference, not just two identical numbers under different labels."""
    return [candle(0, 100.0), candle(1, 95.0, high=104.0, low=90.0, open_=97.0)]


def test_evaluate_config_reports_both_bounds_and_they_can_genuinely_differ():
    """On a fixture crafted to diverge, optimistic and pessimistic scores must actually be
    DIFFERENT numbers — proving both are independently computed, not one bound duplicated
    under two labels."""
    ev = evaluate_config({"FAKE": _sl_tp_divergence_fixture()}, distance_pct=2.0, tp_pct=2.5,
                         max_waves=5, sl_pct=3.0, spacing_days=0)

    assert isinstance(ev, ConfigEvaluation)
    assert ev.optimistic.pessimistic is False
    assert ev.pessimistic.pessimistic is True

    # The fixture's single trial is a stop under the optimistic bound and a win under the
    # pessimistic one — win_rate and loss_rate must reflect that, not agree.
    assert ev.optimistic.win_rate != ev.pessimistic.win_rate
    assert ev.optimistic.stops >= 1
    assert ev.pessimistic.wins >= 1


def test_evaluate_config_rejects_explicit_pessimistic_intrabar():
    """evaluate_config OWNS pessimistic_intrabar (it sets both True and False itself) — a
    caller passing it explicitly is almost certainly a mistake (they meant score_config)."""
    with pytest.raises(TypeError):
        evaluate_config({"FAKE": _all_win_uptrend()}, distance_pct=2.0, tp_pct=3.0,
                        max_waves=1, pessimistic_intrabar=True)


# ---------------------------------------------------------------------------
# Aggregation across symbols (independent of the four defects, but load-bearing for the CLI)
# ---------------------------------------------------------------------------

def test_score_config_aggregates_totals_across_symbols():
    """trials/wins/capital_days/pnl_usd sum exactly across symbols — not averaged, not
    overwritten by the last symbol processed."""
    from app.backtest import estimate_win_rate

    candles_a = _all_win_uptrend(n=60, start=100.0)
    candles_b = _all_win_uptrend(n=60, start=50.0)

    score = score_config({"A": candles_a, "B": candles_b}, distance_pct=2.0, tp_pct=3.0,
                         max_waves=1, spacing_days=0)

    effective_tp_pct = 3.0 + costengine.tp_fee_buffer_pct()
    cost_pct = costengine.round_trip_cost_pct()
    wr_a = estimate_win_rate(candles_a, 2.0, 1, effective_tp_pct, settings.deadline_days,
                             sl_pct=settings.sl_pct, cost_pct=cost_pct, spacing_days=0)
    wr_b = estimate_win_rate(candles_b, 2.0, 1, effective_tp_pct, settings.deadline_days,
                             sl_pct=settings.sl_pct, cost_pct=cost_pct, spacing_days=0)

    assert score.symbols == 2
    assert score.trials == wr_a["trials"] + wr_b["trials"]
    assert score.wins == wr_a["wins"] + wr_b["wins"]
    assert score.total_capital_days == pytest.approx(
        round(wr_a["capital_days"] + wr_b["capital_days"], 4), abs=1e-2
    )
    assert score.total_pnl_usd == pytest.approx(
        round(wr_a["pnl_usd"] + wr_b["pnl_usd"], 2), abs=1e-2
    )


def test_score_config_excludes_symbols_with_zero_trials():
    """A symbol whose history is too short to complete even one trial contributes nothing to
    `symbols`, and does not crash the aggregation."""
    too_short = [candle(0, 100.0), candle(1, 100.5)]
    score = score_config({"THIN": too_short, "GOOD": _all_win_uptrend()}, distance_pct=2.0,
                         tp_pct=3.0, max_waves=1, spacing_days=0)
    assert score.symbols == 1


def test_profit_per_dollar_day_is_the_ratio_and_handles_zero_capital_days():
    """profit_per_dollar_day == total_pnl_usd / total_capital_days when capital was deployed,
    and is exactly 0.0 (never a ZeroDivisionError) when no symbol contributed a trial."""
    score = score_config({"FAKE": _all_win_uptrend()}, distance_pct=2.0, tp_pct=3.0,
                         max_waves=1, spacing_days=0)
    assert score.total_capital_days > 0
    assert score.profit_per_dollar_day == pytest.approx(
        score.total_pnl_usd / score.total_capital_days, abs=1e-6
    )

    empty_score = score_config({}, distance_pct=2.0, tp_pct=3.0, max_waves=1)
    assert empty_score.total_capital_days == 0.0
    assert empty_score.profit_per_dollar_day == 0.0
