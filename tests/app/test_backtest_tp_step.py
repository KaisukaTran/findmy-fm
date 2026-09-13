"""`simulate_kss(tp_step_pct=...)` — the take-profit climbs per filled DCA rung, mirroring the
live `kss_tp_step_per_rung_pct` knob so the ladder-depth study measures the same rule the
engine will run. Synthetic candles, no network."""

import pytest
from test_backtest import candle  # tests/app is on sys.path under tests/app/pytest.ini

from app.backtest import simulate_kss


def _series(highs_after_dip):
    """Entry at 100 (day 0); day 1 dips to 96 (fills rungs at 98 and 96 with distance 2%);
    later days rally to the given highs."""
    return [candle(0, 100), candle(1, 96, high=96, low=96)] + [
        candle(2 + i, h, high=h, low=95) for i, h in enumerate(highs_after_dip)
    ]


@pytest.mark.parametrize("pessimistic", [False, True])
def test_step_zero_is_byte_identical(pessimistic):
    candles = _series([99, 101, 103, 105])
    kw = {"distance_pct": 2.0, "max_waves": 3, "tp_pct": 3.0, "deadline_days": 30,
          "sl_pct": 8.0, "cost_pct": 0.3, "pessimistic_intrabar": pessimistic}
    assert simulate_kss(candles, 0, **kw) == simulate_kss(candles, 0, tp_step_pct=0.0, **kw)


def test_two_rungs_filled_lift_the_target_by_two_steps():
    # Day 1 opens at 96, below both rung targets (98, 96.04), so both fill at the open
    # (B4 gap-below): avg = (100 + 2×96 + 3×96)/6 = 96.67. Flat TP 3% → 99.57; stepped
    # TP 3 + 0.5×2 = 4% → 100.53. A high of 100.0 clears the flat target only.
    candles = _series([100.0, 100.0, 100.0])
    flat = simulate_kss(candles, 0, distance_pct=2.0, max_waves=3, tp_pct=3.0,
                        deadline_days=30, sl_pct=8.0)
    stepped = simulate_kss(candles, 0, distance_pct=2.0, max_waves=3, tp_pct=3.0,
                           deadline_days=30, sl_pct=8.0, tp_step_pct=0.5)
    assert flat.tp_hit and flat.waves_filled == 3
    assert not stepped.tp_hit, "4% above the average was never reached"


def test_a_stepped_tp_pays_the_effective_percent():
    candles = _series([103.0, 103.0])
    res = simulate_kss(candles, 0, distance_pct=2.0, max_waves=3, tp_pct=3.0,
                       deadline_days=30, sl_pct=8.0, cost_pct=0.3, tp_step_pct=0.5)
    assert res.tp_hit and res.waves_filled == 3
    assert res.pnl_pct == pytest.approx(3.0 + 0.5 * 2 - 0.3)


def test_only_the_entry_filled_pays_the_base():
    candles = [candle(0, 100), candle(1, 104, high=104, low=100)]
    res = simulate_kss(candles, 0, distance_pct=2.0, max_waves=3, tp_pct=3.0,
                       deadline_days=30, sl_pct=8.0, tp_step_pct=0.5)
    assert res.tp_hit and res.waves_filled == 1
    assert res.pnl_pct == pytest.approx(3.0)
