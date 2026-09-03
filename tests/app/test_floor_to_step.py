"""``service._floor_to_step`` — Fix 2 (live evidence, INJ session 18, 2026-09-03).

The function floored STRICTLY, so a position that actually held ``8.06 + 16.13`` INJ (binary
float noise: ``24.189999999999998``, not the mathematically exact ``24.19``) had its
take-profit quantity floored a WHOLE step down to ``24.18``. INJ's real LOT_SIZE stepSize is
``0.01``, so ``24.19`` was perfectly step-legal — the venue had already accepted an order of
exactly that size for this same position. The floored-down TP left 0.01 INJ (~$0.048) stranded
below minNotional once it filled: the residue shape behind this book's orphan sweeps.

The fix is a RELATIVE epsilon (scaled to the value's own magnitude, not a fixed absolute one)
that snaps a value sitting within noise-distance of the NEXT step boundary UP to it, while a
value that is genuinely below the boundary by more than noise still floors down a whole step,
exactly as before — including inside ``_try_partial_rung``'s sizing division (Fix 1), which
must keep refusing to exceed the fund it was reserved from.
"""

from __future__ import annotations

from app.kss import service


def test_binary_float_noise_snaps_up_to_the_next_step():
    """INJ session 18, live: total_filled_qty = 8.06 + 16.13 == 24.189999999999998 in binary
    float, but the position genuinely holds 24.19 INJ (Binance itself accepted that exact
    quantity). stepSize 0.01 -> must floor to the step-legal 24.19, not down a whole step."""
    value = 8.06 + 16.13
    assert value != 24.19  # sanity: this really is float noise, not the exact value already
    assert service._floor_to_step(value, 0.01) == 24.19


def test_a_genuinely_lower_value_still_floors_down():
    """24.1849 is truly 0.0051 short of 24.19 — far more than float noise — so it must still
    floor DOWN a whole step to 24.18, exactly as the pre-fix strict flooring did."""
    assert service._floor_to_step(24.1849, 0.01) == 24.18


def test_a_huge_value_with_proportional_noise_still_snaps():
    """The epsilon is RELATIVE (scaled to the value's own magnitude via max(1, |value|)), so
    noise that is proportionally tiny but absolutely large on a big value must still snap up
    instead of being mistaken for a genuine shortfall."""
    value = (8.06 + 16.13) * 10_000  # same float-noise shape, scaled up ~10,000x
    assert repr(value) == "241899.99999999997"  # confirm this is still genuine float noise
    assert service._floor_to_step(value, 0.01) == 241900.0


def test_exact_step_legal_value_is_unaffected():
    """A value with no noise at all (already an exact multiple of step) must be returned
    unchanged, not nudged to the NEXT step up."""
    assert service._floor_to_step(24.19, 0.01) == 24.19


def test_zero_step_means_no_snapping():
    assert service._floor_to_step(24.189999999999998, 0.0) == 24.189999999999998


# --- regression against Fix 1: the partial-rung sizing division must still refuse to exceed --
# --- the fund it was reserved from (both fixes share this one helper) -------------------------


def test_partial_rung_sizing_division_still_refuses_to_exceed_the_fund():
    """`_try_partial_rung` floors `reserved_fund / price` through this SAME helper (ATOM
    session 16's real numbers: remaining $112.87818507, price 1.39055558, 1% slack reserved,
    stepSize 0.01). The float-noise tolerance must never let that division round UP past what
    the reservation actually set aside — it is a noise-scale nudge, not a real step's worth."""
    remaining = 112.87818507
    price = 1.39055558
    reserved = remaining * 0.99
    step = 0.01

    qty = service._floor_to_step(reserved / price, step)

    assert qty * price <= remaining
    assert qty * price <= reserved + 1e-9  # slack is noise-scale, not a whole extra step
