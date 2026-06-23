"""
Pure math for the Pyramid-UP (Anti-Martingale "add to winners") strategy mode.

See docs/pyramid-up-plan.md. This is the OPPOSITE shape of the frozen DCA-down
ladder in ``app/kss/pyramid.py``:
  - DCA-down: add BELOW entry, quantity INCREASES each wave (pip × (n+1)).
  - Pyramid-up: add ABOVE entry, quantity DECREASES each wave (anti-martingale).
A coin that rips straight up after entry (no dip ever triggers a DCA buy) still
deploys capital by scaling INTO strength instead of sitting on idle reserve —
the WLFI session-26 case (entry 0.05793 → straight to +5.5%, wave-1 dip target
never touched, only $80 of $1,597 ever deployed).

FROZEN-safe: pure functions/dataclass only — no DB, no network, no `settings`
import. Every tunable is a function argument so this module is fully
unit-testable and reusable from the service layer without import-time
side effects. ``app/kss/pyramid.py`` is never touched.

Anti-martingale invariant (the cage): each add-on is STRICTLY smaller than the
previous one (0 < size_ratio < 1) and triggers STRICTLY above entry. This must
never become an inverted pyramid (larger adds the higher price goes) — that
would compound risk into a chasing trade instead of free-rolling a winner.
"""

from __future__ import annotations

from dataclasses import dataclass

MAX_ADDS_CAP = 3


def price_precision(reference_price: float) -> int:
    """Decimal places for trigger/avg prices, mirroring
    ``pyramid._calculate_price_precision`` / ``dynamic_exit.price_precision``
    (BTC-like → 2, ETH-like → 4, small alts → 6) so Pyramid-UP prices round
    exactly like the rest of the KSS engine."""
    if reference_price >= 10_000:
        return 2
    if reference_price >= 100:
        return 4
    return 6


def _round_qty(raw_qty: float, step_size: float, min_qty: float) -> float:
    """Round a raw quantity to the exchange step size, floored at min_qty —
    same rounding discipline as ``PyramidSession.generate_wave``."""
    if step_size <= 0:
        qty = raw_qty
    else:
        qty = round(raw_qty / step_size) * step_size
    return max(qty, min_qty)


@dataclass
class WaveSpec:
    """A single Pyramid-UP wave: the base (n=0) or an add-on (n>=1)."""

    n: int
    trigger_price: float
    qty: float


def add_trigger_price(entry: float, n: int, step_pct: float) -> float:
    """Add-on trigger price for wave ``n``: ``entry × (1 + step_pct/100)^n``.

    STRICTLY INCREASING in n (n=0 → entry itself, the base wave fills at/near
    entry). Add-ons only trigger ABOVE entry — never a dip-buy, that is the
    DCA-down job and stays in ``pyramid.py``.
    """
    return entry * (1 + step_pct / 100.0) ** n


def add_qty(base_qty: float, n: int, size_ratio: float) -> float:
    """Raw (unrounded) quantity for wave ``n``: ``base_qty × size_ratio^n``.

    STRICTLY DECREASING for ``0 < size_ratio < 1`` — the anti-martingale
    invariant. ``size_ratio`` outside (0, 1) would either freeze quantity
    (ratio=1, not a pyramid) or invert it (ratio>1, the forbidden shape), so
    callers must clamp it before use; this function trusts its input range
    is already validated by the caller (kept pure/simple, like pyramid.py).
    """
    return base_qty * (size_ratio**n)


def stop_after_add(avg: float, lock_pct: float, fee_floor: float) -> float:
    """Break-even-plus stop floor that moves up after every add: ``max(fee_floor,
    avg × (1 + lock_pct/100))``. Always ≥ avg (and ≥ fee_floor) so each add is a
    "free-roll" — once it fills, net risk on the position can never exceed the
    risk taken on the very first (base) wave. ``lock_pct`` may be 0 (lock at
    breakeven) but must not be negative (that would drop the floor below avg
    and reintroduce risk on a position that already proved itself a winner)."""
    floor = avg * (1 + max(lock_pct, 0.0) / 100.0)
    return max(fee_floor, floor, avg)


def projected_pyramid_cost(
    *,
    entry: float,
    base_qty: float,
    max_adds: int,
    step_pct: float,
    size_ratio: float,
    step_size: float = 0.0,
    min_qty: float = 0.0,
    rounded: bool = False,
) -> float:
    """Σ qty(n) × trigger_price(n) for n=0..max_adds — the full pyramid notional,
    used for ``isolated_fund`` sizing before any wave fills.

    ``rounded=False`` (default) sums raw (unrounded) qty × raw trigger price —
    useful when sizing a target fund from scratch. ``rounded=True`` matches
    what ``build_ladder`` will actually post (step/min_qty rounded, price
    rounded to ``price_precision``), useful for an exact post-build cost check.
    """
    max_adds = min(max_adds, MAX_ADDS_CAP)
    total = 0.0
    for n in range(max_adds + 1):
        trigger = add_trigger_price(entry, n, step_pct)
        qty = add_qty(base_qty, n, size_ratio)
        if rounded:
            trigger = round(trigger, price_precision(entry))
            qty = _round_qty(qty, step_size, min_qty)
        total += qty * trigger
    return total


def build_ladder(
    *,
    entry: float,
    target_fund: float,
    max_adds: int,
    step_pct: float,
    size_ratio: float,
    step_size: float,
    min_qty: float,
) -> list[WaveSpec]:
    """Build the ordered Pyramid-UP ladder (base + add-ons) so the FULL pyramid
    notional approximates ``target_fund`` — base wave largest, every add-on
    smaller, distributed geometrically by ``size_ratio``.

    Derivation: with raw qty(n) = base_qty × size_ratio^n and raw trigger(n) =
    entry × (1+step_pct/100)^n, the raw (unrounded) total cost is
    ``base_qty × Σ (size_ratio × (1+step_pct/100))^n`` for n=0..max_adds — a
    geometric series in ``base_qty``. Solve for ``base_qty`` from
    ``target_fund``, then round each wave to the exchange step/precision
    (rounding only ever moves actual notional slightly, never breaks the
    strictly-decreasing-qty / strictly-increasing-price invariants since both
    raw sequences are already strictly monotonic before rounding).

    ``max_adds`` is capped at ``MAX_ADDS_CAP`` (3) regardless of the caller's
    input — the anti-martingale ladder is meant to stay shallow (free-rolling
    a handful of adds), not turn into its own deep pyramid.
    """
    max_adds = min(max_adds, MAX_ADDS_CAP)
    if entry <= 0 or target_fund <= 0:
        return []

    ratio_per_step = 1 + step_pct / 100.0
    series_sum = sum((size_ratio * ratio_per_step) ** n for n in range(max_adds + 1))
    # series_sum > 0 always (sum of positive terms); base_qty solves
    # target_fund = entry × base_qty × series_sum.
    base_qty = target_fund / (entry * series_sum)

    waves: list[WaveSpec] = []
    for n in range(max_adds + 1):
        trigger = round(add_trigger_price(entry, n, step_pct), price_precision(entry))
        qty = _round_qty(add_qty(base_qty, n, size_ratio), step_size, min_qty)
        waves.append(WaveSpec(n=n, trigger_price=trigger, qty=qty))
    return waves
