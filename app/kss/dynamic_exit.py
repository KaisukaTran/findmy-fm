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

# Minimum room, in percentage points, between the arm threshold and the lock floor the armed stop
# lands on. Not a knob: it is the width of a bug, not a preference. Small enough that it never
# pushes the threshold past a coin's own take-profit (the narrowest live tp_pct is 1.38, and the
# floor+margin only binds where the threshold was already unusable), large enough that a session
# arming at the floor is not stopped out by the first tick of noise.
ARM_LOCK_MARGIN_PCT = 0.5


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


def arm_pct_for(tp_pct: float = 0.0) -> float:
    """The arm percentage this session actually uses.

    A flat ``kss_trail_arm_pct`` is a trap once the take-profit is per-coin. Autotune fits
    ``tp_pct`` from each coin's ATR (2.8%-6.2% across the live book) while the arm threshold was
    a global 5%, and BOTH are anchored to the same average — so their race is decided by the two
    percentages alone, permanently. Measured on the live book 2026-09-06: four of six open
    sessions had ``tp_pct < 5``, which means their trailing exit could never arm, not once, ever.
    The resting take-profit simply filled first, every time (25 of 29 live exits).

    With ``kss_trail_arm_tp_frac > 0`` the threshold becomes a FRACTION of this session's own
    take-profit, so arming always happens strictly before the fixed exit is reachable and the
    trail gets its window. 0 disables it (the old flat behaviour, byte-identical).

    FLOORED AT THE LOCK, always. The armed stop is ``max(grid_sl, lock_floor)`` and at the arm
    tick ``grid_sl`` collapses to ``avg``, so the stop lands on ``avg×(1+kss_trail_lock_pct)``
    whatever this returns. A threshold under that floor arms a session that is ALREADY stopped
    out — and arming cancels the DCA ladder first, so it throws the ladder away to do it. Measured
    live 2026-09-06 (testnet): at ``frac=0.6`` every coin with ``tp_pct < 3.33`` (BTC 2.63, TRX
    1.38, ALGO 2.75 — ~25 of the book) armed below its own stop; SEI armed at +3.25% and was
    stopped out 13 minutes later at +2.0% with its ladder already cancelled. The two knobs live in
    different modules, so nothing caught it. This floor is that missing validator, and it binds the
    flat knob too (``arm_pct`` 1% against a 2% lock is the same trap without the fraction).
    """
    pct = settings.kss_trail_arm_pct
    frac = settings.kss_trail_arm_tp_frac
    if frac > 0 and tp_pct > 0:
        pct = min(pct, frac * tp_pct)
    return max(pct, settings.kss_trail_lock_pct + ARM_LOCK_MARGIN_PCT)


def arm_threshold(avg: float, tp_pct: float = 0.0) -> float:
    """Price at/above which a profitable RIDING session ARMS its trailing stop (Ride & Trail):
    ``avg×(1+arm_pct_for(tp_pct))``. Below it the session rides (no fixed-TP cap, protected only
    by the hard SL) so a runner is not capped early and noise does not arm a thin stop."""
    return avg * (1 + arm_pct_for(tp_pct) / 100.0)


def should_arm(*, market: float, avg: float, filled_qty: float, trail_active: bool,
               tp_pct: float = 0.0) -> bool:
    """True only on a filled, not-yet-armed session whose market has cleared the arm threshold while
    the feature is enabled. One-way: callers flip ``trail_active`` permanently."""
    if not settings.kss_dynamic_tp_enabled or trail_active or filled_qty <= 0 or avg <= 0:
        return False
    return market >= arm_threshold(avg, tp_pct)


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
