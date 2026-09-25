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

Runner mode (``kss_arm_at_tp``, docs/kss-dynamic-tp-plan.md rev 5): the session's OWN take-profit
arms the trail instead of selling, so a runner is never capped at TP and nothing changes below it.
Once armed the SL keeps ``kss_trail_lock_tp_ratio`` of the TP gain and ``kss_trail_keep_pct`` of the
peak gain, and the spike-grab ceiling is anchored to the peak (not the SL) so it cannot sit below
the price and cap the runner on a high-ATR coin.
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


def runner_mode(tp_price: float) -> bool:
    """True when the session's own take-profit ARMS the trail instead of selling (``kss_arm_at_tp``).
    Needs the session TP price; without one every function here keeps its Ride & Trail meaning."""
    return bool(settings.kss_arm_at_tp and tp_price > 0)


def arm_threshold(avg: float, tp_price: float = 0.0) -> float:
    """Price at/above which a profitable RIDING session ARMS its trailing stop.

    Ride & Trail: ``avg×(1+kss_trail_arm_pct)``. Below it the session rides (no fixed-TP cap,
    protected only by the hard SL) so a runner is not capped early and noise does not arm a thin
    stop. Runner mode: the session's own ``tp_price`` — below it nothing differs from a fixed TP
    (which could not have sold there either), so the mode adds no path that ends in a loss."""
    if runner_mode(tp_price):
        return tp_price
    return avg * (1 + settings.kss_trail_arm_pct / 100.0)


def should_arm(
    *, market: float, avg: float, filled_qty: float, trail_active: bool, tp_price: float = 0.0
) -> bool:
    """True only on a filled, not-yet-armed session whose market has cleared the arm threshold while
    the feature is enabled. One-way: callers flip ``trail_active`` permanently."""
    if not settings.kss_dynamic_tp_enabled or trail_active or filled_qty <= 0 or avg <= 0:
        return False
    return market >= arm_threshold(avg, tp_price)


def lock_floor_price(avg: float, tp_price: float = 0.0) -> float:
    """Lowest the ARMED trailing SL may sit, never below the fee floor (so still no fee loss).

    Ride & Trail: ``max(fee_floor, avg×(1+kss_trail_lock_pct))`` — stops a wide ATR trail from
    pinning the stop back at break-even. Runner mode: ``kss_trail_lock_tp_ratio`` of the TP gain,
    ``avg + ratio×(tp_price−avg)`` — the worst a reversal right after TP can leave is that share of
    the profit the fixed TP would have booked."""
    if runner_mode(tp_price) and tp_price > avg:
        locked = avg + settings.kss_trail_lock_tp_ratio * (tp_price - avg)
        return max(fee_floor_price(avg), locked)
    return max(fee_floor_price(avg), avg * (1 + settings.kss_trail_lock_pct / 100.0))


def keep_floor_price(*, peak: float, avg: float, tp_price: float = 0.0) -> float:
    """Runner mode: keep ``kss_trail_keep_pct`` of the PEAK gain, ``avg + keep×(peak−avg)``. Tight
    while the gain is small, loose once it is large — a wide ATR trail on a +20% runner can no longer
    give most of it back. 0.0 outside runner mode / below avg (no floor)."""
    if not runner_mode(tp_price) or peak <= avg:
        return 0.0
    return avg + settings.kss_trail_keep_pct / 100.0 * (peak - avg)


def trail_distance_pct(atr_pct: float) -> float:
    """Volatility-aware trailing distance = ``max(atr_mult×ATR%, min_pct)``. Falls back to
    ``min_pct`` when ATR is missing/zero (data gap) so the stop is never tighter than the floor."""
    atr = atr_pct if (atr_pct and atr_pct > 0) else 0.0
    return max(settings.kss_trail_atr_mult * atr, settings.kss_trail_min_pct)


def compute_sl(
    *, peak: float, avg: float, distance_pct: float, trail_dist_pct: float, prev_sl: float = 0.0,
    tp_price: float = 0.0,
) -> float:
    """Ratcheted ARMED trailing stop. Trails ``trail_dist%`` below ``peak``, snapped DOWN to a
    wave-grid level ``avg×(1+d)^k``, clamped at the lock floor (``max(fee_floor, avg×(1+lock_pct))``)
    so a wide ATR trail can't pin it back at break-even, and never below ``prev_sl`` (monotonic).
    In runner mode it is also held at the keep floor (a share of the peak gain).
    Always returns a price ≥ the lock floor (a real locked profit)."""
    d = distance_pct / 100.0
    floor = max(lock_floor_price(avg, tp_price), keep_floor_price(peak=peak, avg=avg, tp_price=tp_price))
    target = peak * (1 - trail_dist_pct / 100.0)
    if target > avg and d > 0:
        k = max(math.floor(math.log(target / avg) / math.log(1 + d)), 0)
        grid_sl = avg * (1 + d) ** k  # highest grid level ≤ target
    else:
        grid_sl = avg  # target at/below avg → the lock floor lifts it
    sl = max(grid_sl, floor, prev_sl)
    return round(sl, price_precision(avg))


def compute_tp(*, sl: float, avg: float, peak: float = 0.0, tp_price: float = 0.0) -> float:
    """Spike-grab TP ceiling = ``SL×(1+gap%)``, floored at ``fee_floor``. Always ≥ ``fee_floor``.

    Runner mode also floors it at ``peak×(1+gap%)``: a ceiling derived from the SL alone sits BELOW
    the price once the trail is wider than the gap (any coin with ATR ≳ 5% at the default gap 5), and
    then sells a steady runner on the next tick. Anchored to the peak it only grabs a real jump of
    ``gap%`` above the high-water mark between two checks."""
    gap = settings.kss_tp_gap_pct / 100.0
    tp = max(sl * (1 + gap), fee_floor_price(avg))
    if runner_mode(tp_price):
        tp = max(tp, peak * (1 + gap))
    return round(tp, price_precision(avg))


def dynamic_sl_tp(
    *, peak: float, avg: float, distance_pct: float, atr_pct: float, prev_sl: float = 0.0,
    tp_price: float = 0.0,
) -> tuple[float, float, float]:
    """Convenience bundle for a trailing session: returns ``(trail_dist_pct, sl, tp)``.

    ``trail_dist_pct`` is recomputed from the (slow-moving daily) ATR; the service layer caches it
    on the session so the fast guard loop can re-derive SL/TP from a live ticker cheaply."""
    td = trail_distance_pct(atr_pct)
    sl = compute_sl(peak=peak, avg=avg, distance_pct=distance_pct, trail_dist_pct=td, prev_sl=prev_sl,
                    tp_price=tp_price)
    tp = compute_tp(sl=sl, avg=avg, peak=peak, tp_price=tp_price)
    return td, sl, tp
