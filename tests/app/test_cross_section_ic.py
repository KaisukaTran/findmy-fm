"""
Tests for scripts/cross_section_ic.py — the harness that decides which selection signals are
worth building on.

WHY this file exists: this harness will be used to accept or reject signals, so a bug in it
does not produce a wrong number, it produces a wrong DECISION. Three properties carry that
weight:

  - Causality. A feature must see bars up to and including the signal day and nothing after.
    Every past look-ahead in this project (the 99.5% win rate, the phantom instant-flip)
    started as a plausible-looking result from a function that could see one bar too far.
  - The IC must be computed WITHIN a day. A market-wide up day makes every feature correlate
    with every outcome; only the cross-section carries information about which coin to pick.
  - The verdict thresholds are pre-registered. They are asserted here so nobody can quietly
    move the goalposts after seeing a table.
"""

from __future__ import annotations

import pytest

from scripts.cross_section_ic import (
    COST_HURDLE,
    USABLE,
    _ret,
    _sma_ratio,
    amihud_illiquidity,
    daily_ic,
    realized_vol,
    spearman,
    summarize,
    trend_composite,
    turnover_volatility,
)


def bars(closes: list[float], vols: list[float] | None = None) -> list[tuple]:
    """(symbol, ts, high, low, close, quote_volume) — the dataset's row shape."""
    vols = vols or [1_000_000.0] * len(closes)
    return [("X", i * 86_400_000, c * 1.01, c * 0.99, c, v)
            for i, (c, v) in enumerate(zip(closes, vols, strict=True))]


class TestPreRegisteredThresholds:
    def test_the_thresholds_are_what_the_plan_said(self):
        # 0.019 = break-even against a 0.30% round trip at a ~5-day hold; 0.05 = "good".
        assert COST_HURDLE == 0.019
        assert USABLE == 0.05


class TestFeatureCausality:
    """Every feature must be blind to bars after i."""

    @pytest.mark.parametrize("fn", [trend_composite, turnover_volatility,
                                    amihud_illiquidity, realized_vol])
    def test_future_bars_cannot_change_a_feature(self, fn):
        history = [100.0 + i for i in range(80)]
        b = bars(history)
        before = fn(b, 60)

        crash = bars(history[:61] + [1.0] * 19)     # everything after bar 60 collapses
        moon = bars(history[:61] + [9_999.0] * 19)  # ...or explodes
        assert fn(crash, 60) == before
        assert fn(moon, 60) == before

    def test_return_and_sma_are_causal_too(self):
        history = [100.0 + i for i in range(80)]
        b, moon = bars(history), bars(history[:61] + [9_999.0] * 19)
        assert _ret(moon, 60, 7) == _ret(b, 60, 7)
        assert _sma_ratio(moon, 60, 20) == _sma_ratio(b, 60, 20)

    def test_a_feature_is_none_before_its_window_is_full(self):
        b = bars([100.0] * 10)
        assert trend_composite(b, 5) is None          # needs 50 bars
        assert realized_vol(b, 5) is None             # needs 30
        assert _ret(b, 2, 7) is None


class TestFeatureDirection:
    def test_trend_composite_is_higher_for_a_riser(self):
        up = trend_composite(bars([100.0 + i for i in range(60)]), 59)
        down = trend_composite(bars([160.0 - i for i in range(60)]), 59)
        assert up > 1.0 > down

    def test_amihud_is_higher_for_the_thinner_coin(self):
        closes = [100.0 + (i % 5) for i in range(60)]
        thin = amihud_illiquidity(bars(closes, [1_000.0] * 60), 59)
        thick = amihud_illiquidity(bars(closes, [10_000_000.0] * 60), 59)
        assert thin > thick

    def test_realized_vol_separates_calm_from_wild(self):
        calm = realized_vol(bars([100.0 + (i % 2) * 0.1 for i in range(60)]), 59)
        wild = realized_vol(bars([100.0 + (i % 2) * 40 for i in range(60)]), 59)
        assert wild > calm


class TestSpearman:
    def test_perfect_agreement_is_one(self):
        assert spearman([1, 2, 3, 4, 5], [10, 20, 30, 40, 50]) == pytest.approx(1.0)

    def test_perfect_disagreement_is_minus_one(self):
        assert spearman([1, 2, 3, 4, 5], [50, 40, 30, 20, 10]) == pytest.approx(-1.0)

    def test_too_few_points_returns_none(self):
        assert spearman([1, 2], [3, 4]) is None

    def test_a_constant_feature_returns_none(self):
        # No cross-sectional information: a flat feature must not read as a correlation.
        assert spearman([7, 7, 7, 7, 7, 7], [1, 2, 3, 4, 5, 6]) is None


class TestDailyIc:
    def test_ic_is_computed_within_a_day_not_across_days(self):
        # Day A: feature ranks match outcomes. Day B: the same feature values with the SAME
        # ordering but far larger outcomes. Pooling would find a strong correlation driven by
        # the day, not by the pick; a per-day IC sees two clean +1s.
        def day(scale):
            return [{"f": i, "outcome": i * scale} for i in range(1, 9)]

        panel = {"2026-01-01": day(1.0), "2026-01-02": day(100.0)}
        assert daily_ic(panel, "f", min_names=5) == pytest.approx([1.0, 1.0])

    def test_days_with_too_few_names_are_dropped(self):
        panel = {"2026-01-01": [{"f": 1, "outcome": 1}, {"f": 2, "outcome": 2}]}
        assert daily_ic(panel, "f", min_names=20) == []

    def test_rows_missing_the_feature_are_skipped_not_zeroed(self):
        panel = {"2026-01-01": [{"f": i, "outcome": i} for i in range(1, 7)]
                 + [{"outcome": 99.0}] * 3}
        assert daily_ic(panel, "f", min_names=5) == pytest.approx([1.0])


class TestSummarize:
    def test_too_few_days_reports_no_ic_rather_than_a_fragile_one(self):
        assert "ic" not in summarize([0.1] * 19)

    def test_a_constant_ic_series_has_a_tight_interval_around_it(self):
        s = summarize([0.05] * 200)
        assert s["ic"] == pytest.approx(0.05)
        assert s["lo"] == pytest.approx(0.05) and s["hi"] == pytest.approx(0.05)

    def test_a_zero_mean_series_straddles_zero(self):
        s = summarize([0.2, -0.2] * 100)
        assert s["lo"] < 0 < s["hi"]
