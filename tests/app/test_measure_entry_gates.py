"""
Tests for scripts/measure_entry_gates.py — the harness that measures whether each of the
scanner's six entry gates actually earns its keep.

WHY this file exists (the failure modes it exists to catch):

  - LOOK-AHEAD. Every gate answer at candle `i` must be computable from candles `[..i]`
    alone. The TA helpers in `app/ta/*` and `app/backtest.py` all take a whole series and
    report "as of the last bar", so computing them ONCE on the full history and reading
    index `i` silently uses the future. That is exactly the class of bug that made
    `simulate_kss` report a 100% win rate in a market that fell 48%. The decisive test here
    appends a wildly different FUTURE to the series and asserts every gate answer at `i` is
    byte-identical.
  - MEAN-OF-RATIOS. Profit per dollar-day is a ratio of SUMS (Σpnl / Σcapital-days), not the
    mean of per-trial ratios — averaging ratios silently weights a $100 one-day trial the
    same as a $600 thirty-day one.
  - FAKE CONFIDENCE. Entries 7 days apart still overlap (the deadline is 30 days) and eight
    crypto symbols move together, so a plain i.i.d. bootstrap would understate the interval.
    The bootstrap must resample whole DATE CLUSTERS, not individual entries.
  - UNJUDGEABLE GATES. A gate that vetoes a handful of entries must be reported as
    "cannot judge", never as a number.
  - GATE ISOLATION. The expectancy/win-rate gate and the consensus gate both live inside one
    `decide()` call; measuring either one requires neutralising the other, and the harness
    must actually do so.

No network, no DB.
"""

from __future__ import annotations

import random

import pytest

from app.config import settings
from scripts.measure_entry_gates import (
    DEFAULT_TIMEFRAME,
    GATE_NAMES,
    GATE_SHIPS_ON,
    MIN_GROUP_N,
    EntryOutcome,
    backtest_gate_vetoes,
    btc_window_at,
    causal_window,
    cluster_bootstrap_lift_ci,
    consensus_gate_vetoes,
    entry_outcome,
    gate_snapshot,
    gates_forced_on,
    group_stats,
    mark_mae_quartile_drops,
    overlap_block_clusters,
    parse_args,
    rolled_entry_indices,
    stack_names,
    stacked_vetoed,
    verdict,
)

_DAY = 86_400_000


def _candle(day: int, close: float, high: float | None = None,
            low: float | None = None, open_: float | None = None) -> dict:
    return {
        "ts": day * _DAY,
        "open": open_ if open_ is not None else close,
        "high": high if high is not None else close * 1.01,
        "low": low if low is not None else close * 0.99,
        "close": close,
        "volume": 1_000_000.0,
    }


def _walk(n: int, seed: int = 7, start: float = 100.0, drift: float = 0.0) -> list[dict]:
    """Deterministic pseudo-random daily walk — the same seed always yields the same series."""
    rng = random.Random(seed)
    out: list[dict] = []
    price = start
    for d in range(n):
        price *= 1 + drift + rng.uniform(-0.03, 0.03)
        out.append(_candle(d, price, high=price * 1.015, low=price * 0.985, open_=price))
    return out


def _outcome(ts_day: int, pnl_usd: float, capital_days: float, *, symbol: str = "X",
             pnl_pct: float = 0.0, stopped: bool = False, tp_hit: bool = False) -> EntryOutcome:
    return EntryOutcome(symbol=symbol, index=0, ts=ts_day * _DAY, pnl_usd=pnl_usd,
                        capital_days=capital_days, pnl_pct=pnl_pct, stopped=stopped,
                        tp_hit=tp_hit)


# ---------------------------------------------------------------------------
# Causal slicing — the look-ahead defence
# ---------------------------------------------------------------------------


def test_causal_window_never_contains_a_future_candle():
    candles = _walk(50)
    window = causal_window(candles, 20, lookback_bars=1000)
    assert window[-1]["ts"] == candles[20]["ts"]
    assert all(c["ts"] <= candles[20]["ts"] for c in window)


def test_causal_window_is_capped_by_the_lookback():
    """Production only ever sees the last `backtest_lookback_days` bars — the window must
    ROLL, not grow, or a late entry would be judged on more history than the live scanner has."""
    candles = _walk(200)
    window = causal_window(candles, 150, lookback_bars=60)
    assert len(window) == 60
    assert window[-1]["ts"] == candles[150]["ts"]
    assert window[0]["ts"] == candles[91]["ts"]


def test_causal_window_handles_an_index_shorter_than_the_lookback():
    candles = _walk(200)
    window = causal_window(candles, 10, lookback_bars=60)
    assert len(window) == 11
    assert window[0]["ts"] == candles[0]["ts"]


def test_btc_window_at_excludes_bars_at_or_after_the_entry_time():
    btc = _walk(80, seed=3)
    cutoff = btc[40]["ts"]
    window = btc_window_at(btc, cutoff, lookback_bars=1000)
    assert window[-1]["ts"] == cutoff
    assert all(c["ts"] <= cutoff for c in window)
    # A symbol whose bar has no exact BTC counterpart still gets only past BTC bars.
    window2 = btc_window_at(btc, cutoff + _DAY // 2, lookback_bars=1000)
    assert window2[-1]["ts"] == cutoff


def test_btc_window_at_is_empty_before_btc_history_starts():
    btc = _walk(20, seed=3)
    assert btc_window_at(btc, btc[0]["ts"] - _DAY, lookback_bars=100) == []


def test_gate_snapshot_is_immune_to_the_future():
    """THE look-ahead test. Compute every gate answer at index `i`, then append a violently
    different future and recompute at the SAME index: nothing may move. A harness that
    computed indicators once over the whole series would fail this."""
    candles = _walk(320, seed=11)
    btc = _walk(320, seed=12)
    i = 260
    with gates_forced_on():
        before = gate_snapshot(
            causal_window(candles, i, 365), btc_window_at(btc, candles[i]["ts"], 365),
            distance_pct=2.0, tp_pct=3.0, max_waves=10, pessimistic_intrabar=False,
        )
        crash = [_candle(320 + d, 1.0, high=1.0, low=1.0, open_=1.0) for d in range(120)]
        after = gate_snapshot(
            causal_window(candles + crash, i, 365),
            btc_window_at(btc + crash, candles[i]["ts"], 365),
            distance_pct=2.0, tp_pct=3.0, max_waves=10, pessimistic_intrabar=False,
        )
    assert before is not None
    assert before == after


def test_gate_snapshot_returns_none_on_a_window_the_scanner_would_skip():
    """The live scanner drops symbols with < 30 candles; the harness must drop them too
    rather than judge a gate on a bundle built from neutral fallbacks."""
    with gates_forced_on():
        assert gate_snapshot([], [], distance_pct=2.0, tp_pct=3.0, max_waves=10,
                             pessimistic_intrabar=False) is None
        assert gate_snapshot(_walk(10), _walk(10), distance_pct=2.0, tp_pct=3.0,
                             max_waves=10, pessimistic_intrabar=False) is None


# ---------------------------------------------------------------------------
# Rolling entries — must mirror app.backtest.estimate_win_rate
# ---------------------------------------------------------------------------


def test_rolled_entry_indices_respects_the_production_spacing():
    candles = _walk(100)
    idx = rolled_entry_indices(candles, first_index=10, spacing_days=7.0)
    assert idx[0] == 10
    assert all(b - a == 7 for a, b in zip(idx, idx[1:], strict=False))
    assert idx[-1] < len(candles) - 1


def test_rolled_entry_indices_every_bar_when_spacing_is_zero():
    candles = _walk(30)
    assert rolled_entry_indices(candles, first_index=5, spacing_days=0.0) == list(range(5, 29))


# ---------------------------------------------------------------------------
# Gate isolation — the two gates that share one decide() call
# ---------------------------------------------------------------------------


def _wr(expectancy: float, win_rate_lb: float = 99.0, trials: int = 100,
        loss_rate: float = 0.0, days: float | None = 2.0) -> dict:
    return {"expectancy": expectancy, "win_rate": win_rate_lb, "win_rate_lb": win_rate_lb,
            "trials": trials, "loss_rate": loss_rate, "avg_days_to_tp": days}


def test_backtest_gate_ignores_the_consensus():
    """Gate 5 must answer on backtest evidence alone — otherwise its measured lift is
    contaminated by gate 6."""
    bad = _wr(expectancy=settings.min_expectancy_pct - 1.0)
    good = _wr(expectancy=settings.min_expectancy_pct + 1.0)
    assert backtest_gate_vetoes(bad, tp_pct=3.0) is True
    assert backtest_gate_vetoes(good, tp_pct=3.0) is False


def test_backtest_gate_fires_on_a_thin_sample_and_on_a_low_win_rate():
    assert backtest_gate_vetoes(
        _wr(expectancy=settings.min_expectancy_pct + 1.0,
            trials=max(0, settings.min_trials - 1)), tp_pct=3.0) is True
    assert backtest_gate_vetoes(
        _wr(expectancy=settings.min_expectancy_pct + 1.0,
            win_rate_lb=settings.min_win_rate - 1.0), tp_pct=3.0) is True


def test_consensus_gate_ignores_the_backtest_evidence():
    """Gate 6 must answer on the agent consensus alone — mirror of the test above."""
    assert consensus_gate_vetoes(settings.min_confidence - 0.1) is True
    assert consensus_gate_vetoes(settings.min_confidence + 0.1) is False


# ---------------------------------------------------------------------------
# Gate 4 — the cross-sectional MAE quartile gate (delegates to the REAL scanner function)
# ---------------------------------------------------------------------------


def test_mae_quartile_drops_the_deepest_quartile_of_a_cross_section():
    rows = [("A", -30.0), ("B", -20.0), ("C", -10.0), ("D", -5.0)]
    assert mark_mae_quartile_drops(rows) == {"A"}


def test_mae_quartile_is_a_noop_below_four_candidates():
    """Relative gate: the live function refuses to act on fewer than 4 candidates."""
    assert mark_mae_quartile_drops([("A", -30.0), ("B", -1.0), ("C", -2.0)]) == set()


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------


def test_group_stats_is_a_ratio_of_sums_not_a_mean_of_ratios():
    """A $600 thirty-day trial must not weigh the same as a $100 one-day trial."""
    outcomes = [_outcome(1, pnl_usd=1.0, capital_days=1.0),
                _outcome(2, pnl_usd=-3.0, capital_days=99.0)]
    stats = group_stats(outcomes)
    assert stats.n == 2
    assert stats.profit_per_dollar_day == pytest.approx(-2.0 / 100.0)
    # The mean of the per-trial ratios would be (1.0 + -3/99)/2 ≈ +0.485 — the wrong answer.
    assert stats.profit_per_dollar_day < 0


def test_group_stats_rates_and_expectancy():
    outcomes = [_outcome(1, 1.0, 10.0, pnl_pct=2.0, tp_hit=True),
                _outcome(2, -1.0, 10.0, pnl_pct=-8.0, stopped=True),
                _outcome(3, 0.0, 10.0, pnl_pct=0.0)]
    stats = group_stats(outcomes)
    assert stats.win_rate == pytest.approx(100 / 3)
    assert stats.stop_rate == pytest.approx(100 / 3)
    assert stats.expectancy == pytest.approx(-2.0)


def test_group_stats_of_an_empty_group_is_zeroed_not_a_crash():
    stats = group_stats([])
    assert stats.n == 0
    assert stats.profit_per_dollar_day == 0.0


def test_cluster_bootstrap_is_deterministic_for_a_seed():
    passed = [_outcome(d, 1.0, 10.0) for d in range(40)]
    vetoed = [_outcome(d, -1.0, 10.0) for d in range(40)]
    a = cluster_bootstrap_lift_ci(passed, vetoed, rounds=200, seed=42)
    b = cluster_bootstrap_lift_ci(passed, vetoed, rounds=200, seed=42)
    assert a == b
    assert a is not None
    assert a[0] <= a[1]


def test_cluster_bootstrap_resamples_whole_dates_not_single_entries():
    """Entries sharing an entry DATE are ONE observation (eight correlated crypto symbols on
    the same day, plus overlapping 30-day trials spaced 7 days apart). With a single date
    there is exactly one cluster, so every resample reproduces the original sample and the
    interval must collapse to a point — even though the entries WITHIN it differ wildly.
    An i.i.d. bootstrap over individual entries would shuffle those differing entries and
    report a spuriously narrow but NON-zero interval."""
    passed = [_outcome(0, float(k) - 5.0, 10.0, symbol=f"S{k}") for k in range(20)]
    vetoed = [_outcome(0, 3.0 - float(k), 10.0, symbol=f"T{k}") for k in range(20)]
    lo, hi = cluster_bootstrap_lift_ci(passed, vetoed, rounds=200, seed=1)
    assert lo == pytest.approx(hi)
    expected = (sum(o.pnl_usd for o in passed) - sum(o.pnl_usd for o in vetoed)) / 200.0
    assert lo == pytest.approx(expected)


def test_cluster_bootstrap_is_none_when_a_group_is_empty():
    assert cluster_bootstrap_lift_ci([], [_outcome(1, 1.0, 10.0)], rounds=50, seed=1) is None


def test_overlap_block_length_covers_the_whole_deadline():
    """Entries 7 days apart with a 30-day deadline overlap ~5 deep: bars inside one trial are
    also inside the next four. Resampling single dates would treat those five as independent
    evidence, so the block must span the overlap."""
    assert overlap_block_clusters(deadline_days=30, spacing_days=7.0) == 5
    assert overlap_block_clusters(deadline_days=30, spacing_days=30.0) == 1
    assert overlap_block_clusters(deadline_days=30, spacing_days=0.0) == 1


def test_cluster_bootstrap_block_of_one_is_the_plain_cluster_bootstrap():
    passed = [_outcome(d, float(d % 5) - 2.0, 10.0) for d in range(40)]
    vetoed = [_outcome(d, 1.0 - float(d % 3), 10.0) for d in range(40)]
    plain = cluster_bootstrap_lift_ci(passed, vetoed, rounds=300, seed=9)
    blocked = cluster_bootstrap_lift_ci(passed, vetoed, rounds=300, seed=9, block=1)
    assert plain == blocked


def test_cluster_bootstrap_with_a_block_spanning_every_date_collapses_to_a_point():
    """A circular block as long as the whole series can only ever be a ROTATION of it, so
    every resample has the same composition and the interval must be a point. This is what
    proves the blocks are contiguous runs of dates rather than dates drawn one at a time."""
    passed = [_outcome(d, float(d) - 10.0, 10.0) for d in range(20)]
    vetoed = [_outcome(d, 4.0 - float(d), 10.0) for d in range(20)]
    lo, hi = cluster_bootstrap_lift_ci(passed, vetoed, rounds=100, seed=3, block=20)
    assert lo == pytest.approx(hi)
    expected = (sum(o.pnl_usd for o in passed) - sum(o.pnl_usd for o in vetoed)) / 200.0
    assert lo == pytest.approx(expected)


def test_cluster_bootstrap_block_widens_the_interval_on_serially_correlated_data():
    """The whole point of blocking: when neighbouring dates carry the same sign (a regime,
    not 40 independent draws), pretending each date is independent understates the interval."""
    passed = [_outcome(d, 1.0 if d < 20 else -1.0, 10.0) for d in range(40)]
    vetoed = [_outcome(d, 0.0, 10.0) for d in range(40)]
    narrow = cluster_bootstrap_lift_ci(passed, vetoed, rounds=800, seed=4, block=1)
    wide = cluster_bootstrap_lift_ci(passed, vetoed, rounds=800, seed=4, block=10)
    assert (wide[1] - wide[0]) > (narrow[1] - narrow[0])


# ---------------------------------------------------------------------------
# Verdicts
# ---------------------------------------------------------------------------


def test_stacked_selection_vetoes_when_any_member_gate_vetoes():
    """The live scanner short-circuits: the first veto skips the candidate and the remaining
    gates are never consulted. A stacked row must model that, not a majority vote."""
    flags = {"downtrend": False, "falling_knife": True, "rel_strength": False}
    assert stacked_vetoed(flags, ["downtrend", "rel_strength"]) is False
    assert stacked_vetoed(flags, ["downtrend", "falling_knife"]) is True
    assert stacked_vetoed(flags, []) is False


def test_stack_names_as_shipped_covers_only_the_gates_that_are_switched_on():
    shipped = stack_names(only_shipped=True)
    assert set(shipped) == {n for n in GATE_NAMES if GATE_SHIPS_ON[n]}
    assert set(stack_names(only_shipped=False)) == set(GATE_NAMES)
    # rel_strength and mae_quartile ship OFF, so an "as shipped" stack must exclude them.
    assert "rel_strength" not in shipped
    assert "mae_quartile" not in shipped


def test_verdict_cannot_judge_a_gate_that_vetoes_almost_nothing():
    """'A gate that vetoes 3 entries out of 400 cannot be judged' — say so, don't report
    a number."""
    assert verdict(passed_n=400, vetoed_n=3, lift=0.9, ci=(0.5, 1.2)) == "cannot judge"
    assert verdict(passed_n=3, vetoed_n=400, lift=0.9, ci=(0.5, 1.2)) == "cannot judge"


def test_verdict_reads_the_interval_not_the_point_estimate():
    n = MIN_GROUP_N + 1
    assert verdict(n, n, lift=0.9, ci=(0.5, 1.2)) == "helps"
    assert verdict(n, n, lift=-0.9, ci=(-1.2, -0.5)) == "hurts"
    assert verdict(n, n, lift=0.9, ci=(-0.5, 2.0)) == "noise"
    assert verdict(n, n, lift=0.9, ci=None) == "cannot judge"


# ---------------------------------------------------------------------------
# Outcome simulation
# ---------------------------------------------------------------------------


def test_entry_outcome_drops_a_trial_with_no_room_to_finish():
    """An entry too close to the end of history has no verdict — excluded, exactly as
    app.backtest.estimate_win_rate excludes incomplete trials."""
    candles = _walk(60)
    assert entry_outcome(candles, len(candles) - 2, "X", distance_pct=2.0, tp_pct=3.0,
                         max_waves=10, pessimistic_intrabar=False,
                         wave0_notional_usd=100.0) is None


def test_entry_outcome_scales_with_the_wave0_notional():
    candles = _walk(300, seed=5)
    small = entry_outcome(candles, 40, "X", distance_pct=2.0, tp_pct=3.0, max_waves=10,
                          pessimistic_intrabar=False, wave0_notional_usd=100.0)
    big = entry_outcome(candles, 40, "X", distance_pct=2.0, tp_pct=3.0, max_waves=10,
                        pessimistic_intrabar=False, wave0_notional_usd=1000.0)
    assert small is not None and big is not None
    assert big.pnl_usd == pytest.approx(small.pnl_usd * 10, rel=1e-6)
    assert big.capital_days == pytest.approx(small.capital_days * 10, rel=1e-6)
    assert big.pnl_pct == pytest.approx(small.pnl_pct)


def test_entry_outcome_never_reads_a_bar_before_the_entry():
    """The outcome is forward-only: replacing history BEFORE the entry must not change it."""
    candles = _walk(300, seed=5)
    i = 120
    base = entry_outcome(candles, i, "X", distance_pct=2.0, tp_pct=3.0, max_waves=10,
                         pessimistic_intrabar=False, wave0_notional_usd=100.0)
    rewritten = [_candle(d, 42.0) for d in range(i)] + candles[i:]
    after = entry_outcome(rewritten, i, "X", distance_pct=2.0, tp_pct=3.0, max_waves=10,
                          pessimistic_intrabar=False, wave0_notional_usd=100.0)
    assert base is not None
    assert base == after


# ---------------------------------------------------------------------------
# Harness hygiene
# ---------------------------------------------------------------------------


def test_gates_forced_on_restores_every_setting_it_touched():
    """Measuring what an OFF gate WOULD have done means switching it on in-process; leaking
    that into the rest of the suite (or a later measurement) would be a silent behaviour change."""
    before = (settings.entry_momentum_gate, settings.rel_strength_enabled,
              settings.mae_quartile_gate_enabled, settings.block_downtrend_adx)
    with gates_forced_on():
        assert settings.entry_momentum_gate is True
        assert settings.rel_strength_enabled is True
        assert settings.mae_quartile_gate_enabled is True
        assert settings.block_downtrend_adx > 0
    assert (settings.entry_momentum_gate, settings.rel_strength_enabled,
            settings.mae_quartile_gate_enabled, settings.block_downtrend_adx) == before


def test_gates_forced_on_restores_settings_even_when_the_body_raises():
    before = settings.rel_strength_enabled
    with pytest.raises(RuntimeError):
        with gates_forced_on():
            raise RuntimeError("boom")
    assert settings.rel_strength_enabled == before


def test_default_timeframe_matches_what_the_gates_actually_run_on():
    """The gates are computed from the SAME candles the live scanner backtests
    (settings.backtest_timeframe, daily) — measuring them on another timeframe changes what
    `rel_strength_lookback_bars` and `htf_trend`'s down-sampling even mean."""
    assert DEFAULT_TIMEFRAME == settings.backtest_timeframe
    assert parse_args([]).timeframe == settings.backtest_timeframe


def test_cli_is_read_only_market_data_in_table_out():
    """No database: the harness must not open a session (it stubs the one `audit.log` call
    the real quartile gate makes)."""
    import scripts.measure_entry_gates as m

    src = open(m.__file__, encoding="utf-8").read()
    assert "SessionLocal" not in src
    assert ".commit()" not in src
