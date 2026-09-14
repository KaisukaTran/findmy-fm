"""`simulate_kss(trail_after_tp_pct=...)` — Ride & Trail v2: once a bar's high reaches the
take-profit target, ARM a trailing stop floored at that TP price instead of selling, so the
trade can only end at the TP price or higher. Synthetic candles, no network.

`trail_after_tp_pct=0.0` (the default) must stay byte-identical to the plain take-profit exit
that predates this parameter — see test_trail_zero_is_byte_identical below and the full
existing suite in test_backtest.py / test_backtest_tp_step.py, which exercise `simulate_kss`
with no knowledge of this parameter at all.
"""

import pytest
from test_backtest import candle  # tests/app is on sys.path under tests/app/pytest.ini

from app.backtest import simulate_kss


@pytest.mark.parametrize("pessimistic", [False, True])
def test_trail_zero_is_byte_identical(pessimistic):
    """trail_after_tp_pct defaults to 0.0; passing it explicitly changes nothing, on either
    intrabar bound, even on a fixture that touches TP, keeps rising, then pulls back hard
    (exactly the shape that WOULD diverge if the trail were active)."""
    candles = [
        candle(0, 100.0),
        candle(1, 103.0, high=103.0, low=100.0),
        candle(2, 110.0, high=112.0, low=110.5),
        candle(3, 90.0, high=90.0, low=85.0),
    ]
    kw = {"distance_pct": 2, "max_waves": 5, "tp_pct": 3, "deadline_days": 30,
          "sl_pct": 8, "cost_pct": 0.3, "pessimistic_intrabar": pessimistic}
    implicit = simulate_kss(candles, 0, **kw)
    explicit = simulate_kss(candles, 0, trail_after_tp_pct=0.0, **kw)
    assert implicit == explicit
    # And it must still be the plain, immediate take-profit exit — not armed.
    assert implicit.tp_hit is True
    assert implicit.armed_exit_pct is None
    assert implicit.pnl_pct == round(3 - 0.3, 4)


@pytest.mark.parametrize("pessimistic", [False, True])
def test_touches_tp_then_rises_then_pulls_back_exits_above_tp(pessimistic):
    """Bar 1 touches TP exactly (103, avg=100 with max_waves=1) -> arms with floor=103,
    peak=103. Bar 2 rises to a new peak (112) without dipping below the 2%-trail stop. Bar 3
    pulls back ~3% from that peak (to 108.6, below the 109.76 stop) without setting a new high,
    so the exit price (and therefore the outcome) is identical under both intrabar bounds."""
    candles = [
        candle(0, 100.0),
        candle(1, 103.0, high=103.0, low=100.0),
        candle(2, 110.0, high=112.0, low=110.5),
        candle(3, 109.0, high=109.5, low=108.6),
    ]
    r = simulate_kss(candles, 0, distance_pct=2, max_waves=1, tp_pct=3, deadline_days=30,
                     sl_pct=8, cost_pct=0, trail_after_tp_pct=2.0,
                     pessimistic_intrabar=pessimistic)
    assert r.tp_hit is True
    assert r.pnl_pct == pytest.approx(9.76)          # exit at stop = 112*0.98 = 109.76
    assert r.armed_exit_pct == pytest.approx(6.76)   # 9.76 - eff_tp(3.0)
    assert r.armed_exit_pct > 0
    assert r.pnl_pct > 3.0                            # above the flat take-profit


@pytest.mark.parametrize("pessimistic", [False, True])
def test_touches_tp_then_falls_straight_back_exits_at_the_floor(pessimistic):
    """Bar 1 touches TP exactly (peak == floor == 103). Bar 2 collapses straight back (low=95,
    high=100 — no new peak) so the trail gives back everything down to the floor: pnl equals
    the plain take-profit's net (eff_tp - cost), and armed_exit_pct is exactly 0."""
    candles = [
        candle(0, 100.0),
        candle(1, 103.0, high=103.0, low=100.0),
        candle(2, 96.0, high=100.0, low=95.0),
    ]
    r = simulate_kss(candles, 0, distance_pct=2, max_waves=1, tp_pct=3, deadline_days=30,
                     sl_pct=8, cost_pct=0.3, trail_after_tp_pct=2.0,
                     pessimistic_intrabar=pessimistic)
    assert r.tp_hit is True
    assert r.pnl_pct == round(3 - 0.3, 4)
    assert r.armed_exit_pct == 0.0


@pytest.mark.parametrize("pessimistic", [False, True])
def test_floor_holds_even_on_a_crash_bar(pessimistic):
    """The mirror of the previous test with a much deeper crash (low=10 instead of 95): the
    floor guarantees the SAME exit price and pnl regardless of how far the crash bar's own low
    goes — the trade exits at the stop, never at the bar's low."""
    candles = [
        candle(0, 100.0),
        candle(1, 103.0, high=103.0, low=100.0),
        candle(2, 20.0, high=100.0, low=10.0),
    ]
    r = simulate_kss(candles, 0, distance_pct=2, max_waves=1, tp_pct=3, deadline_days=30,
                     sl_pct=8, cost_pct=0.3, trail_after_tp_pct=2.0,
                     pessimistic_intrabar=pessimistic)
    assert r.tp_hit is True
    assert r.pnl_pct == round(3 - 0.3, 4), "the floor must hold even on a crash bar"
    assert r.armed_exit_pct == 0.0
    assert r.stopped is False, "never a stop-loss once armed — the floor is what governs"


def test_pessimistic_branch_checks_the_old_stop_before_the_new_peak():
    """The clearest divergence for the pessimistic bound: a bar whose low sits exactly at the
    OLD stop (computed from the peak carried in from the previous bar) AND whose high makes a
    NEW, higher peak. The trail must check the low against the OLD stop (before updating peak
    with this bar's high) and exit there — not recompute a higher stop from the new peak first
    and let a bar that should have exited "get away" at a better price.

    Bar 1: touches TP at 103 (avg=100) -> arms, floor=103, peak=103.
    Bar 2: rises to 120 (no dip) -> peak becomes 120; old stop for bar 3 = max(103, 120*0.95)
           = 114.
    Bar 3: low=114 (== the old stop) and high=130 (a new peak, would give stop 123.5 if peak
           were updated first). Correct behaviour exits at 114, not 123.5.
    """
    candles = [
        candle(0, 100.0),
        candle(1, 103.0, high=103.0, low=100.0),
        candle(2, 120.0, high=120.0, low=115.0),
        candle(3, 116.0, high=130.0, low=114.0),
    ]
    r = simulate_kss(candles, 0, distance_pct=2, max_waves=1, tp_pct=3, deadline_days=30,
                     sl_pct=8, cost_pct=0, trail_after_tp_pct=5.0,
                     pessimistic_intrabar=True)
    assert r.tp_hit is True
    assert r.pnl_pct == pytest.approx(14.0), "must exit at the OLD stop (114), not 123.5"
    assert r.armed_exit_pct == pytest.approx(11.0)
