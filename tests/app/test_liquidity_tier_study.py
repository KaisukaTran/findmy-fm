"""
Tests for scripts/liquidity_tier_study.py.

WHY this file exists: the study's whole value is that its numbers can be trusted, and every
helper below is a place where a plausible-looking result would be wrong instead of noisy.

  - Counting the ENTRY bar's own high/low is the exact bug that once reported a 99.5% win rate
    in a bear market. The outcome loop must start at the next bar.
  - When one bar touches both the stop and the target, crediting the target inflates every
    tier. Stop-first is the pessimistic choice, and it must be the one implemented.
  - The tier must come from a TRAILING window: ranking a coin by its whole-sample average
    volume leaks its future into the bucket it is scored in.
  - Cost is charged per entry. Without it every tier looks ~0.3 points better than it is, which
    is the difference between "flat" and "losing" at these effect sizes.
"""

from __future__ import annotations

from scripts.liquidity_tier_study import (
    TIER_NAMES,
    simulate,
    tier_of,
    trailing_median_volume,
)


def bar(ts: int, high: float, low: float, close: float, qv: float = 0.0) -> tuple:
    return ("X", ts, high, low, close, qv)


class TestTierOf:
    def test_edges_land_in_the_documented_bucket(self):
        assert tier_of(50_000) == "<$100k"
        assert tier_of(100_000) == "$100k-1M"       # lower edge is inclusive
        assert tier_of(999_999) == "$100k-1M"
        assert tier_of(1_000_000) == "$1M-10M"      # the live universe floor
        assert tier_of(9_999_999) == "$1M-10M"
        assert tier_of(10_000_000) == ">$10M"

    def test_every_volume_gets_a_tier(self):
        for v in (0, 1, 1e12):
            assert tier_of(v) in TIER_NAMES


class TestTrailingMedianVolume:
    def test_median_not_mean(self):
        # A listing-day spike must not promote a dead coin into a higher tier for a month.
        assert trailing_median_volume([1_000, 1_000, 1_000, 50_000_000]) == 1_000.0

    def test_empty_window_is_zero(self):
        assert trailing_median_volume([]) == 0.0


class TestSimulateLookAhead:
    def test_the_entry_bar_cannot_pay_the_take_profit(self):
        # Entry bar's own high clears the target by miles; the next bar goes nowhere. A win
        # here would mean the study is buying at a price it already knows was beaten.
        bars = [bar(0, 200.0, 90.0, 100.0), bar(1, 100.5, 99.5, 100.0)]
        assert simulate(bars, 0, tp_pct=3.0, sl_pct=8.0, horizon=7)["exit"] == "deadline"

    def test_the_entry_bar_cannot_trigger_the_stop_either(self):
        bars = [bar(0, 200.0, 10.0, 100.0), bar(1, 100.5, 99.5, 100.0)]
        assert simulate(bars, 0, tp_pct=3.0, sl_pct=8.0, horizon=7)["exit"] == "deadline"

    def test_take_profit_on_a_later_bar_is_credited(self):
        bars = [bar(0, 100.0, 100.0, 100.0), bar(1, 103.5, 99.0, 103.0)]
        out = simulate(bars, 0, tp_pct=3.0, sl_pct=8.0, horizon=7)
        assert out["exit"] == "tp" and out["days"] == 1

    def test_stop_wins_when_one_bar_touches_both(self):
        # High clears +3% and low breaches -8% in the same bar. Crediting the target here
        # would flatter every tier in the table.
        bars = [bar(0, 100.0, 100.0, 100.0), bar(1, 110.0, 90.0, 95.0)]
        out = simulate(bars, 0, tp_pct=3.0, sl_pct=8.0, horizon=7)
        assert out["exit"] == "sl"
        assert out["pnl"] == -8.0

    def test_deadline_exit_marks_to_the_close(self):
        bars = [bar(0, 100.0, 100.0, 100.0)] + [bar(i, 101.0, 99.0, 101.0) for i in range(1, 9)]
        out = simulate(bars, 0, tp_pct=50.0, sl_pct=50.0, horizon=7)
        assert out["exit"] == "deadline"
        assert round(out["pnl"], 6) == 1.0
        assert out["days"] == 7

    def test_no_result_without_a_following_bar(self):
        assert simulate([bar(0, 100.0, 100.0, 100.0)], 0, 3.0, 8.0, 7) is None


class TestSimulateCost:
    def test_cost_is_charged_on_every_exit_kind(self):
        win = [bar(0, 100.0, 100.0, 100.0), bar(1, 103.5, 99.0, 103.0)]
        lose = [bar(0, 100.0, 100.0, 100.0), bar(1, 101.0, 90.0, 91.0)]
        flat = [bar(0, 100.0, 100.0, 100.0)] + [bar(i, 100.5, 99.5, 100.0) for i in range(1, 9)]
        assert simulate(win, 0, 3.0, 8.0, 7, cost_pct=0.30)["pnl"] == 2.70
        assert simulate(lose, 0, 3.0, 8.0, 7, cost_pct=0.30)["pnl"] == -8.30
        assert round(simulate(flat, 0, 3.0, 8.0, 7, cost_pct=0.30)["pnl"], 6) == -0.30

    def test_zero_cost_is_the_gross_number(self):
        win = [bar(0, 100.0, 100.0, 100.0), bar(1, 103.5, 99.0, 103.0)]
        assert simulate(win, 0, 3.0, 8.0, 7, cost_pct=0.0)["pnl"] == 3.0
