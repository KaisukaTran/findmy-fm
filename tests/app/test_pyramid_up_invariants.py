"""
Invariant tests for the Pyramid-UP (Anti-Martingale) pure math.

Locks the shape that must never become an inverted pyramid: each add-on is
STRICTLY smaller than the previous wave and triggers STRICTLY above entry.
See docs/pyramid-up-plan.md and app/kss/pyramid_up.py for the rationale.
"""

import pytest

from app.kss.pyramid_up import (
    MAX_ADDS_CAP,
    WaveSpec,
    add_qty,
    add_trigger_price,
    build_ladder,
    projected_pyramid_cost,
    stop_after_add,
)


def test_trigger_prices_strictly_increasing_above_entry():
    entry = 100.0
    step_pct = 2.0
    prices = [add_trigger_price(entry, n, step_pct) for n in range(4)]

    assert prices[0] == pytest.approx(entry)
    for n in range(1, len(prices)):
        assert prices[n] > prices[n - 1]
        assert prices[n] > entry


def test_qty_strictly_decreasing_anti_martingale():
    base_qty = 10.0
    size_ratio = 0.6
    qtys = [add_qty(base_qty, n, size_ratio) for n in range(4)]

    assert qtys[0] == pytest.approx(base_qty)
    for n in range(1, len(qtys)):
        assert qtys[n] < qtys[n - 1], "add-on must never be larger than the previous wave"


def test_build_ladder_wave_count_is_max_adds_plus_one():
    waves = build_ladder(
        entry=100.0,
        target_fund=1000.0,
        max_adds=2,
        step_pct=2.0,
        size_ratio=0.6,
        step_size=0.0001,
        min_qty=0.0001,
    )
    assert len(waves) == 3  # base + 2 add-ons
    assert all(isinstance(w, WaveSpec) for w in waves)
    assert [w.n for w in waves] == [0, 1, 2]


def test_build_ladder_qty_strictly_decreasing_and_triggers_strictly_increasing():
    waves = build_ladder(
        entry=50.0,
        target_fund=500.0,
        max_adds=3,
        step_pct=3.0,
        size_ratio=0.5,
        step_size=0.001,
        min_qty=0.001,
    )
    for i in range(1, len(waves)):
        assert waves[i].qty < waves[i - 1].qty
        assert waves[i].trigger_price > waves[i - 1].trigger_price
        assert waves[i].trigger_price > 50.0
    # base wave fills at/near entry
    assert waves[0].trigger_price == pytest.approx(50.0)


def test_build_ladder_base_wave_is_largest_quantity():
    waves = build_ladder(
        entry=1.0,
        target_fund=100.0,
        max_adds=3,
        step_pct=5.0,
        size_ratio=0.7,
        step_size=0.0001,
        min_qty=0.0001,
    )
    base = waves[0]
    for w in waves[1:]:
        assert base.qty > w.qty


def test_max_adds_capped_at_three():
    waves = build_ladder(
        entry=100.0,
        target_fund=1000.0,
        max_adds=10,  # request way more than the cap
        step_pct=2.0,
        size_ratio=0.6,
        step_size=0.0001,
        min_qty=0.0001,
    )
    assert MAX_ADDS_CAP == 3
    assert len(waves) == MAX_ADDS_CAP + 1


def test_build_ladder_total_notional_approximates_target_fund():
    target_fund = 1000.0
    waves = build_ladder(
        entry=100.0,
        target_fund=target_fund,
        max_adds=2,
        step_pct=2.0,
        size_ratio=0.6,
        step_size=0.0001,
        min_qty=0.0001,
    )
    total_notional = sum(w.qty * w.trigger_price for w in waves)
    # within rounding tolerance of the target fund
    assert total_notional == pytest.approx(target_fund, rel=0.01)


def test_stop_after_add_at_least_avg_and_fee_floor():
    avg = 100.0
    fee_floor = 100.3  # round-trip fee buffer above avg

    # lock_pct that would land below the fee floor: fee floor still wins.
    floor_low_lock = stop_after_add(avg, lock_pct=0.1, fee_floor=fee_floor)
    assert floor_low_lock >= avg
    assert floor_low_lock >= fee_floor

    # lock_pct that exceeds the fee floor: lock_pct wins.
    floor_high_lock = stop_after_add(avg, lock_pct=5.0, fee_floor=fee_floor)
    assert floor_high_lock >= avg
    assert floor_high_lock == pytest.approx(avg * 1.05)


def test_stop_after_add_never_below_avg_even_with_zero_lock():
    avg = 250.0
    fee_floor = 0.0  # degenerate: no fee buffer configured
    floor = stop_after_add(avg, lock_pct=0.0, fee_floor=fee_floor)
    assert floor >= avg
    assert floor == pytest.approx(avg)


def test_projected_pyramid_cost_equals_manual_sum():
    entry = 100.0
    base_qty = 10.0
    max_adds = 2
    step_pct = 2.0
    size_ratio = 0.6

    expected = sum(
        add_qty(base_qty, n, size_ratio) * add_trigger_price(entry, n, step_pct)
        for n in range(max_adds + 1)
    )
    actual = projected_pyramid_cost(
        entry=entry,
        base_qty=base_qty,
        max_adds=max_adds,
        step_pct=step_pct,
        size_ratio=size_ratio,
    )
    assert actual == pytest.approx(expected)


def test_projected_pyramid_cost_caps_max_adds():
    cost_requested_10 = projected_pyramid_cost(
        entry=100.0, base_qty=10.0, max_adds=10, step_pct=2.0, size_ratio=0.6
    )
    cost_capped_3 = projected_pyramid_cost(
        entry=100.0, base_qty=10.0, max_adds=MAX_ADDS_CAP, step_pct=2.0, size_ratio=0.6
    )
    assert cost_requested_10 == pytest.approx(cost_capped_3)
