"""Pure-math tests for app.kss.dynamic_exit (KSS dynamic trailing TP/SL — Phase 1).

No DB / network. The headline guarantee: every automatic exit edge (SL and TP) is ≥ fee_floor, so
no automatic trailing exit can ever book a loss — not even a fee loss.
"""

from __future__ import annotations

import math

import pytest

from app.config import settings
from app.kss import dynamic_exit as dx


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    """Deterministic knobs: round_trip_cost = 2×0.1 + 2×0.05 = 0.3% → fee_floor = avg×1.009."""
    monkeypatch.setattr(settings, "taker_fee_pct", 0.1)
    monkeypatch.setattr(settings, "slippage_pct", 0.05)
    monkeypatch.setattr(settings, "kss_exit_fee_mult", 3.0)
    monkeypatch.setattr(settings, "kss_tp_gap_pct", 5.0)
    monkeypatch.setattr(settings, "kss_trail_atr_mult", 1.0)
    monkeypatch.setattr(settings, "kss_trail_min_pct", 3.0)
    monkeypatch.setattr(settings, "kss_dynamic_tp_enabled", True)


# ----- fee floor & precision -----

def test_fee_floor_price():
    assert dx.fee_floor_price(100.0) == pytest.approx(100.9)  # 100×(1+3×0.3/100)


def test_price_precision_buckets():
    assert dx.price_precision(50_000) == 2
    assert dx.price_precision(250) == 4
    assert dx.price_precision(0.23) == 6


# ----- activation -----

def test_activation_threshold():
    assert dx.activation_threshold(100.0, 1.5) == pytest.approx(101.5)


def test_should_activate_branches(monkeypatch):
    base = dict(avg=100.0, distance_pct=1.5, filled_qty=10.0, trail_active=False)
    assert dx.should_activate(market=101.5, **base) is True      # exactly at threshold
    assert dx.should_activate(market=101.4, **base) is False     # below threshold
    assert dx.should_activate(market=120.0, **{**base, "trail_active": True}) is False  # already on
    assert dx.should_activate(market=120.0, **{**base, "filled_qty": 0.0}) is False     # nothing filled
    monkeypatch.setattr(settings, "kss_dynamic_tp_enabled", False)
    assert dx.should_activate(market=120.0, **base) is False     # feature off


# ----- trailing distance (volatility-aware) -----

def test_trail_distance_atr_dominates():
    assert dx.trail_distance_pct(6.0) == pytest.approx(6.0)      # 1.0×6 > min 3

def test_trail_distance_floor_when_calm_or_missing():
    assert dx.trail_distance_pct(2.0) == pytest.approx(3.0)      # 1.0×2 < min 3 → 3
    assert dx.trail_distance_pct(0.0) == pytest.approx(3.0)      # ATR missing → min
    assert dx.trail_distance_pct(None) == pytest.approx(3.0)     # type: ignore[arg-type]

def test_trail_distance_atr_mult(monkeypatch):
    monkeypatch.setattr(settings, "kss_trail_atr_mult", 1.5)
    assert dx.trail_distance_pct(4.0) == pytest.approx(6.0)      # 1.5×4


# ----- SL: ratchet, grid snap, floor clamp -----

def test_compute_sl_ratchet_never_lowers():
    # prev_sl far above any candidate → returned unchanged (monotonic).
    assert dx.compute_sl(peak=120.0, avg=100.0, distance_pct=1.5, trail_dist_pct=6.0,
                         prev_sl=200.0) == pytest.approx(200.0)

def test_compute_sl_floored_when_young():
    # peak just above activation, wide trail → target < avg → clamp to fee_floor (a NON-loss).
    sl = dx.compute_sl(peak=101.6, avg=100.0, distance_pct=1.5, trail_dist_pct=6.0, prev_sl=0.0)
    assert sl == pytest.approx(dx.fee_floor_price(100.0))        # = 100.9

def test_compute_sl_snaps_below_target_and_above_floor():
    avg, d, td = 100.0, 1.5, 6.0
    peak = 130.0
    sl = dx.compute_sl(peak=peak, avg=avg, distance_pct=d, trail_dist_pct=td, prev_sl=0.0)
    target = peak * (1 - td / 100)
    assert sl <= target + 1e-6                                  # snapped DOWN to a grid level ≤ target
    assert sl >= dx.fee_floor_price(avg)                        # never below the floor
    # it IS a wave-grid level avg×(1+d)^k
    k = round(math.log(sl / avg) / math.log(1 + d / 100))
    assert sl == pytest.approx(round(avg * (1 + d / 100) ** k, dx.price_precision(avg)))

def test_compute_sl_monotone_in_peak():
    f = lambda pk: dx.compute_sl(peak=pk, avg=100.0, distance_pct=1.5, trail_dist_pct=6.0, prev_sl=0.0)
    assert f(110.0) <= f(130.0) <= f(160.0)                     # higher peak → SL steps up, never down


# ----- TP -----

def test_compute_tp_gap_above_sl():
    assert dx.compute_tp(sl=110.0, avg=100.0) == pytest.approx(115.5)   # 110×1.05

def test_compute_tp_floored():
    # tiny SL → TP still clamped to fee_floor.
    assert dx.compute_tp(sl=1.0, avg=100.0) == pytest.approx(dx.fee_floor_price(100.0))


# ----- the headline invariant: no automatic exit below fee_floor -----

def test_no_exit_below_fee_floor_invariant():
    avg, d = 100.0, 1.5
    floor = dx.fee_floor_price(avg)
    for peak in [100.5, 101.5, 105.0, 120.0, 175.0, 240.0]:
        for atr in [0.0, 1.0, 3.0, 7.0, 20.0]:
            td, sl, tp = dx.dynamic_sl_tp(peak=peak, avg=avg, distance_pct=d, atr_pct=atr)
            assert sl >= floor - 1e-6, f"SL {sl} < floor {floor} (peak={peak}, atr={atr})"
            assert tp >= floor - 1e-6, f"TP {tp} < floor {floor} (peak={peak}, atr={atr})"
            assert tp >= sl                                     # TP ceiling at/above SL


def test_dynamic_sl_tp_bundle_shape():
    td, sl, tp = dx.dynamic_sl_tp(peak=130.0, avg=100.0, distance_pct=1.5, atr_pct=6.0)
    assert td == pytest.approx(6.0)
    assert sl > dx.fee_floor_price(100.0)
    assert tp == pytest.approx(round(sl * 1.05, dx.price_precision(100.0)))
