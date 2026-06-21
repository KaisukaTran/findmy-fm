"""
Pure math for the KSS dynamic trailing TP/SL exit (see docs/kss-dynamic-tp-plan.md).

FROZEN-safe: this module is pure — no DB, no network, no `PyramidSession`. The service layer
feeds it numbers and applies the result; `app/kss/pyramid.py` is never touched. Phase 1 = these
functions + their tests only (no wiring, no persistence, no orders).

The exit is a volatility-aware **trailing channel** that a session enters once it clears one full
wave-spacing of profit (`avg×(1+distance%)`):
  - SL trails the high-water mark by `max(atr_mult×ATR%, min_pct)` (snapped DOWN to a wave-grid
    level `avg×(1+d)^k`), clamped at a fee-safe floor and ratcheted up only.
  - TP is a spike-grab ceiling `SL×(1+gap%)`.
Both edges are floored at `fee_floor` so **no automatic exit ever books a loss — not even a fee
loss** (the manual take-profit button is the only exit allowed below the floor, by user choice).
"""

from __future__ import annotations

import math

from app import costengine
from app.config import settings


def price_precision(reference_price: float) -> int:
    """Decimal places for SL/TP, mirroring ``pyramid._calculate_price_precision`` so the dynamic
    levels round exactly like wave prices (BTC-like → 2, ETH-like → 4, small alts → 6)."""
    if reference_price >= 10_000:
        return 2
    if reference_price >= 100:
        return 4
    return 6


def fee_floor_price(avg: float) -> float:
    """Lowest price at which an automatic exit still clears the round-trip cost with the configured
    multiplier — both SL and TP are floored here so neither books a (fee) loss.

    ``avg × (1 + kss_exit_fee_mult × round_trip_cost%/100)``.
    """
    return avg * (1 + settings.kss_exit_fee_mult * costengine.round_trip_cost_pct() / 100.0)


def arm_threshold(avg: float) -> float:
    """Price at/above which a profitable RIDING session ARMS its trailing stop (Ride & Trail):
    ``avg×(1+kss_trail_arm_pct)``. Below it the session rides (no fixed-TP cap, protected only by
    the hard SL) so a runner is not capped early and noise does not arm a thin stop."""
    return avg * (1 + settings.kss_trail_arm_pct / 100.0)


def should_arm(*, market: float, avg: float, filled_qty: float, trail_active: bool) -> bool:
    """True only on a filled, not-yet-armed session whose market has cleared the arm threshold while
    the feature is enabled. One-way: callers flip ``trail_active`` permanently."""
    if not settings.kss_dynamic_tp_enabled or trail_active or filled_qty <= 0 or avg <= 0:
        return False
    return market >= arm_threshold(avg)


def lock_floor_price(avg: float) -> float:
    """Lowest the ARMED trailing SL may sit: ``max(fee_floor, avg×(1+kss_trail_lock_pct))``. The
    lock floor stops a wide ATR trail from pinning the stop back at break-even — once armed we lock
    at least ``kss_trail_lock_pct`` profit (and never below the fee floor, so still no fee loss)."""
    return max(fee_floor_price(avg), avg * (1 + settings.kss_trail_lock_pct / 100.0))


def trail_distance_pct(atr_pct: float) -> float:
    """Volatility-aware trailing distance = ``max(atr_mult×ATR%, min_pct)``. Falls back to
    ``min_pct`` when ATR is missing/zero (data gap) so the stop is never tighter than the floor."""
    atr = atr_pct if (atr_pct and atr_pct > 0) else 0.0
    return max(settings.kss_trail_atr_mult * atr, settings.kss_trail_min_pct)


def compute_sl(
    *, peak: float, avg: float, distance_pct: float, trail_dist_pct: float, prev_sl: float = 0.0
) -> float:
    """Ratcheted ARMED trailing stop. Trails ``trail_dist%`` below ``peak``, snapped DOWN to a
    wave-grid level ``avg×(1+d)^k``, clamped at the lock floor (``max(fee_floor, avg×(1+lock_pct))``)
    so a wide ATR trail can't pin it back at break-even, and never below ``prev_sl`` (monotonic).
    Always returns a price ≥ the lock floor (a real locked profit)."""
    d = distance_pct / 100.0
    floor = lock_floor_price(avg)
    target = peak * (1 - trail_dist_pct / 100.0)
    if target > avg and d > 0:
        k = max(math.floor(math.log(target / avg) / math.log(1 + d)), 0)
        grid_sl = avg * (1 + d) ** k  # highest grid level ≤ target
    else:
        grid_sl = avg  # target at/below avg → the lock floor lifts it
    sl = max(grid_sl, floor, prev_sl)
    return round(sl, price_precision(avg))


def compute_tp(*, sl: float, avg: float) -> float:
    """Spike-grab TP ceiling = ``SL×(1+gap%)``, floored at ``fee_floor``. Always ≥ ``fee_floor``."""
    tp = max(sl * (1 + settings.kss_tp_gap_pct / 100.0), fee_floor_price(avg))
    return round(tp, price_precision(avg))


def dynamic_sl_tp(
    *, peak: float, avg: float, distance_pct: float, atr_pct: float, prev_sl: float = 0.0
) -> tuple[float, float, float]:
    """Convenience bundle for a trailing session: returns ``(trail_dist_pct, sl, tp)``.

    ``trail_dist_pct`` is recomputed from the (slow-moving daily) ATR; the service layer caches it
    on the session so the fast guard loop can re-derive SL/TP from a live ticker cheaply."""
    td = trail_distance_pct(atr_pct)
    sl = compute_sl(peak=peak, avg=avg, distance_pct=distance_pct, trail_dist_pct=td, prev_sl=prev_sl)
    tp = compute_tp(sl=sl, avg=avg)
    return td, sl, tp
