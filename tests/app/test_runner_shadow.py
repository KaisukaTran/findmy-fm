"""Pure logic for the runner-shadow feature (app.kss.runner_shadow) — a SHADOW, compute-only
measurement that must never place, cancel or modify a real order. This file covers the pure
`ShadowState`/`step`/`mark_to_market` math only: no DB, no scheduler, no market module. See
test_runner_shadow_wiring.py for the DB glue, scheduler wiring and the API/knob.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.kss import runner_shadow as rs

T0 = datetime(2026, 9, 26, 12, 0, 0)
DEADLINE = T0 + timedelta(days=60)


def _params(**over) -> rs.ShadowParams:
    base = {"maker_fee_pct": 0.1, "taker_fee_pct": 0.1, "slip_pct": 0.1}
    base.update(over)
    return rs.ShadowParams(**base)


# --------------------------------------------------------------------------- V4a: arming


def test_v4a_peak_starts_at_the_tp_price_not_zero():
    """Spec: "peak starts at max(TP fill price, first observed price)" — before any
    observation, only the TP price is known."""
    state = rs.open_v4a(gap_pct=3.0, avg_price=100.0, qty=10.0, tp_price=110.0, v0_net=50.0,
                         v0_proceeds=1089.0, opened_at=T0, deadline_at=DEADLINE)
    assert state.peak == 110.0
    assert state.state == rs.STATE_WATCH


def test_v4a_initial_stop_is_the_gap_below_tp_when_that_beats_the_cost_floor():
    state = rs.open_v4a(gap_pct=3.0, avg_price=100.0, qty=10.0, tp_price=110.0, v0_net=50.0,
                         v0_proceeds=1089.0, opened_at=T0, deadline_at=DEADLINE)
    assert state.stop == pytest.approx(max(100.0 * 1.03, 110.0 * 0.97))
    assert state.stop == pytest.approx(106.7)


def test_v4a_initial_stop_is_floored_at_avg_times_1_03_when_gap_would_go_lower():
    """A tight TP (close to avg) with a wide gap means the gap-based stop would sit BELOW the
    cost floor — the floor must win."""
    state = rs.open_v4a(gap_pct=5.0, avg_price=100.0, qty=10.0, tp_price=101.0, v0_net=5.0,
                         v0_proceeds=100.0, opened_at=T0, deadline_at=DEADLINE)
    gap_based = 101.0 * 0.95
    floor = 100.0 * 1.03
    assert floor > gap_based  # sanity: this test is only meaningful if the floor actually binds
    assert state.stop == pytest.approx(floor)


# --------------------------------------------------------------------------- V4a: ratchet + trigger


def test_v4a_peak_ratchets_up_and_does_not_close_while_price_stays_above_stop():
    state = rs.open_v4a(gap_pct=3.0, avg_price=100.0, qty=10.0, tp_price=110.0, v0_net=50.0,
                         v0_proceeds=1089.0, opened_at=T0, deadline_at=DEADLINE)
    state = rs.step(state, 130.0, T0 + timedelta(minutes=5), _params())
    assert state.state == rs.STATE_WATCH
    assert state.peak == 130.0
    assert state.stop == pytest.approx(max(103.0, 130.0 * 0.97))


def test_v4a_triggers_when_price_falls_gap_pct_from_the_peak():
    params = _params()
    state = rs.open_v4a(gap_pct=3.0, avg_price=100.0, qty=10.0, tp_price=110.0, v0_net=50.0,
                         v0_proceeds=1089.0, opened_at=T0, deadline_at=DEADLINE)
    state = rs.step(state, 130.0, T0 + timedelta(minutes=5), params)  # peak -> 130, stop -> 126.1
    trigger_price = state.stop  # exactly at the stop: "price <= stop" must fire
    state = rs.step(state, trigger_price, T0 + timedelta(minutes=6), params)
    assert state.state == rs.STATE_CLOSED
    assert state.exit_reason == rs.REASON_STOP
    expected_fill = trigger_price * (1 - params.slip_pct / 100.0)
    assert state.exit_price == pytest.approx(expected_fill)
    expected_diff = 10.0 * (
        expected_fill * (1 - params.taker_fee_pct / 100.0)
        - 110.0 * (1 - params.maker_fee_pct / 100.0)
    )
    assert state.diff_usd == pytest.approx(expected_diff)
    assert state.net_usd == pytest.approx(50.0 + expected_diff)


def test_v4a_trigger_at_the_cost_floor_uses_the_floor_not_the_gap_level():
    params = _params()
    state = rs.open_v4a(gap_pct=5.0, avg_price=100.0, qty=10.0, tp_price=101.0, v0_net=5.0,
                         v0_proceeds=100.0, opened_at=T0, deadline_at=DEADLINE)
    floor = state.stop  # floor-bound at creation
    state = rs.step(state, floor, T0 + timedelta(minutes=1), params)
    assert state.state == rs.STATE_CLOSED
    assert state.stop == pytest.approx(floor)


# --------------------------------------------------------------------------- V4a: deadline


def test_v4a_closes_at_deadline_using_the_live_price_not_the_stop_level():
    params = _params()
    state = rs.open_v4a(gap_pct=3.0, avg_price=100.0, qty=10.0, tp_price=110.0, v0_net=50.0,
                         v0_proceeds=1089.0, opened_at=T0, deadline_at=T0 + timedelta(days=1))
    state = rs.step(state, 150.0, T0 + timedelta(days=2), params)  # well above stop, but past deadline
    assert state.state == rs.STATE_CLOSED
    assert state.exit_reason == rs.REASON_DEADLINE
    expected_fill = 150.0 * (1 - params.slip_pct / 100.0)
    assert state.exit_price == pytest.approx(expected_fill)


def test_a_stop_trigger_wins_over_a_simultaneous_deadline():
    params = _params()
    state = rs.open_v4a(gap_pct=3.0, avg_price=100.0, qty=10.0, tp_price=110.0, v0_net=50.0,
                         v0_proceeds=1089.0, opened_at=T0, deadline_at=T0 + timedelta(days=1))
    trigger_price = state.stop
    state = rs.step(state, trigger_price, T0 + timedelta(days=2), params)
    assert state.exit_reason == rs.REASON_STOP


# --------------------------------------------------------------------------- terminal idempotency


def test_step_on_a_closed_row_is_a_no_op():
    params = _params()
    state = rs.open_v4a(gap_pct=3.0, avg_price=100.0, qty=10.0, tp_price=110.0, v0_net=50.0,
                         v0_proceeds=1089.0, opened_at=T0, deadline_at=DEADLINE)
    state = rs.step(state, state.stop, T0 + timedelta(minutes=1), params)
    assert state.state == rs.STATE_CLOSED
    again = rs.step(state, 1.0, T0 + timedelta(days=10), params)
    assert again == state


def test_step_on_a_skipped_row_is_a_no_op():
    params = _params()
    state = rs.open_v5(gap_pct=2.0, avg_price=100.0, qty=10.0, tp_price=110.0, v0_net=-5.0,
                        v0_proceeds=1089.0, opened_at=T0, deadline_at=DEADLINE)
    state = rs.step(state, 120.0, T0 + timedelta(seconds=91), params)
    assert state.state == rs.STATE_SKIPPED
    again = rs.step(state, 999.0, T0 + timedelta(days=1), params)
    assert again == state


# --------------------------------------------------------------------------- V5: the 90s wait


def test_v5_stays_in_watch_before_90s_even_if_price_is_favorable():
    params = _params()
    state = rs.open_v5(gap_pct=2.0, avg_price=100.0, qty=10.0, tp_price=110.0, v0_net=50.0,
                        v0_proceeds=1089.0, opened_at=T0, deadline_at=DEADLINE)
    state = rs.step(state, 115.0, T0 + timedelta(seconds=89), params)
    assert state.state == rs.STATE_WATCH
    assert state.decision_at is None


def test_v5_decides_on_the_first_observation_at_or_after_90s():
    params = _params()
    state = rs.open_v5(gap_pct=2.0, avg_price=100.0, qty=10.0, tp_price=110.0, v0_net=50.0,
                        v0_proceeds=1089.0, opened_at=T0, deadline_at=DEADLINE)
    now = T0 + timedelta(seconds=90)
    state = rs.step(state, 115.0, now, params)
    assert state.decision_at == now
    assert state.state == rs.STATE_RUNNER


# --------------------------------------------------------------------------- V5: skip branches


def test_v5_skips_when_v0_net_is_not_positive_even_with_a_favorable_price():
    params = _params()
    state = rs.open_v5(gap_pct=2.0, avg_price=100.0, qty=10.0, tp_price=110.0, v0_net=0.0,
                        v0_proceeds=1089.0, opened_at=T0, deadline_at=DEADLINE)
    state = rs.step(state, 200.0, T0 + timedelta(seconds=90), params)
    assert state.state == rs.STATE_SKIPPED
    assert state.diff_usd == 0.0
    assert state.net_usd == 0.0
    assert state.exit_reason == rs.REASON_SKIPPED


def test_v5_skips_when_price_has_fallen_below_the_tp_price_at_the_decision():
    params = _params()
    state = rs.open_v5(gap_pct=2.0, avg_price=100.0, qty=10.0, tp_price=110.0, v0_net=50.0,
                        v0_proceeds=1089.0, opened_at=T0, deadline_at=DEADLINE)
    state = rs.step(state, 109.99, T0 + timedelta(seconds=90), params)
    assert state.state == rs.STATE_SKIPPED
    assert state.diff_usd == 0.0
    assert state.net_usd == 50.0


def test_v5_deadline_before_the_90s_decision_ever_ran_marks_skipped_not_a_fabricated_exit():
    params = _params()
    state = rs.open_v5(gap_pct=2.0, avg_price=100.0, qty=10.0, tp_price=110.0, v0_net=50.0,
                        v0_proceeds=1089.0, opened_at=T0, deadline_at=T0 + timedelta(seconds=30))
    state = rs.step(state, 200.0, T0 + timedelta(seconds=60), params)  # past deadline, before 90s
    assert state.state == rs.STATE_SKIPPED
    assert state.exit_reason == rs.REASON_DEADLINE
    assert state.diff_usd == 0.0


# --------------------------------------------------------------------------- V5: runner sizing + exit


def test_v5_runner_sizing_uses_the_risk_fraction_formula():
    params = _params()
    v0_net, v0_proceeds, gap = 50.0, 1089.0, 2.0
    state = rs.open_v5(gap_pct=gap, avg_price=100.0, qty=10.0, tp_price=110.0, v0_net=v0_net,
                        v0_proceeds=v0_proceeds, opened_at=T0, deadline_at=DEADLINE)
    decision_price = 115.0
    now = T0 + timedelta(seconds=90)
    state = rs.step(state, decision_price, now, params)
    risk_frac = gap / 100.0 + 0.002 + 0.03
    expected_runner_usd = min(v0_proceeds, v0_net / risk_frac)
    assert state.runner_usd == pytest.approx(expected_runner_usd)
    cost_per_unit = decision_price * (1 + params.slip_pct / 100.0) * (1 + params.taker_fee_pct / 100.0)
    assert state.qty == pytest.approx(expected_runner_usd / cost_per_unit)
    assert state.entry_price == decision_price
    assert state.peak == decision_price
    assert state.stop == pytest.approx(decision_price * (1 - gap / 100.0))


def test_v5_runner_sizing_is_capped_at_v0_proceeds():
    """A tiny gap makes v0_net/risk_frac huge — the cap must bind at v0_proceeds."""
    params = _params()
    state = rs.open_v5(gap_pct=2.0, avg_price=100.0, qty=10.0, tp_price=110.0, v0_net=10_000.0,
                        v0_proceeds=50.0, opened_at=T0, deadline_at=DEADLINE)
    state = rs.step(state, 115.0, T0 + timedelta(seconds=90), params)
    assert state.runner_usd == pytest.approx(50.0)


def test_v5_runner_trailing_stop_ratchets_then_triggers():
    params = _params()
    state = rs.open_v5(gap_pct=2.0, avg_price=100.0, qty=10.0, tp_price=110.0, v0_net=50.0,
                        v0_proceeds=1089.0, opened_at=T0, deadline_at=DEADLINE)
    state = rs.step(state, 115.0, T0 + timedelta(seconds=90), params)
    assert state.state == rs.STATE_RUNNER
    runner_usd, qty = state.runner_usd, state.qty

    state = rs.step(state, 130.0, T0 + timedelta(minutes=5), params)  # ratchet up
    assert state.state == rs.STATE_RUNNER
    assert state.peak == 130.0
    assert state.stop == pytest.approx(130.0 * 0.98)

    trigger_price = state.stop
    state = rs.step(state, trigger_price, T0 + timedelta(minutes=6), params)
    assert state.state == rs.STATE_CLOSED
    assert state.exit_reason == rs.REASON_STOP
    expected_exit = trigger_price * (1 - params.slip_pct / 100.0)
    expected_proceeds = qty * expected_exit * (1 - params.taker_fee_pct / 100.0)
    expected_diff = expected_proceeds - runner_usd
    assert state.diff_usd == pytest.approx(expected_diff)
    assert state.net_usd == pytest.approx(50.0 + expected_diff)


def test_v5_runner_deadline_close():
    params = _params()
    state = rs.open_v5(gap_pct=2.0, avg_price=100.0, qty=10.0, tp_price=110.0, v0_net=50.0,
                        v0_proceeds=1089.0, opened_at=T0, deadline_at=T0 + timedelta(days=1))
    state = rs.step(state, 115.0, T0 + timedelta(seconds=90), params)
    assert state.state == rs.STATE_RUNNER
    state = rs.step(state, 200.0, T0 + timedelta(days=2), params)  # never triggers the stop
    assert state.state == rs.STATE_CLOSED
    assert state.exit_reason == rs.REASON_DEADLINE


# --------------------------------------------------------------------------- bookkeeping


def test_max_gap_sec_tracks_the_largest_observation_gap():
    params = _params()
    state = rs.open_v4a(gap_pct=3.0, avg_price=100.0, qty=10.0, tp_price=110.0, v0_net=50.0,
                         v0_proceeds=1089.0, opened_at=T0, deadline_at=DEADLINE)
    state = rs.step(state, 120.0, T0 + timedelta(seconds=5), params)
    assert state.max_gap_sec == pytest.approx(5.0)
    state = rs.step(state, 121.0, T0 + timedelta(seconds=5 + 3600), params)  # a 1h gap (e.g. downtime)
    assert state.max_gap_sec == pytest.approx(3600.0)
    state = rs.step(state, 122.0, T0 + timedelta(seconds=5 + 3600 + 5), params)  # tiny gap after
    assert state.max_gap_sec == pytest.approx(3600.0)  # unchanged — still the largest


def test_last_price_and_last_seen_at_track_every_observation():
    params = _params()
    state = rs.open_v4a(gap_pct=3.0, avg_price=100.0, qty=10.0, tp_price=110.0, v0_net=50.0,
                         v0_proceeds=1089.0, opened_at=T0, deadline_at=DEADLINE)
    now = T0 + timedelta(minutes=1)
    state = rs.step(state, 123.45, now, params)
    assert state.last_price == 123.45
    assert state.last_seen_at == now


# --------------------------------------------------------------------------- mark_to_market


def test_mark_to_market_on_a_watch_row_matches_a_hypothetical_close():
    params = _params()
    state = rs.open_v4a(gap_pct=3.0, avg_price=100.0, qty=10.0, tp_price=110.0, v0_net=50.0,
                         v0_proceeds=1089.0, opened_at=T0, deadline_at=DEADLINE)
    state = rs.step(state, 120.0, T0 + timedelta(minutes=1), params)
    mtm = rs.mark_to_market(state, params)
    fill = 120.0 * (1 - params.slip_pct / 100.0)
    expected = 10.0 * (fill * (1 - params.taker_fee_pct / 100.0) - 110.0 * (1 - params.maker_fee_pct / 100.0))
    assert mtm == pytest.approx(expected)


def test_mark_to_market_before_a_v5_runner_is_bought_is_zero():
    params = _params()
    state = rs.open_v5(gap_pct=2.0, avg_price=100.0, qty=10.0, tp_price=110.0, v0_net=50.0,
                        v0_proceeds=1089.0, opened_at=T0, deadline_at=DEADLINE)
    state = rs.step(state, 90.0, T0 + timedelta(seconds=10), params)  # still watching
    assert rs.mark_to_market(state, params) == 0.0


def test_mark_to_market_on_a_closed_row_returns_its_realized_diff():
    params = _params()
    state = rs.open_v4a(gap_pct=3.0, avg_price=100.0, qty=10.0, tp_price=110.0, v0_net=50.0,
                         v0_proceeds=1089.0, opened_at=T0, deadline_at=DEADLINE)
    state = rs.step(state, state.stop, T0 + timedelta(minutes=1), params)
    assert state.state == rs.STATE_CLOSED
    assert rs.mark_to_market(state, params) == state.diff_usd


# --------------------------------------------------------------------------- worked example (XPL)
#
# Real numbers from a live paper TP fill: avg 0.09280176777477368, qty 1645.9,
# tp 0.09859259808391956, v0_net 9.2161. Two regimes on the SAME fill: a narrow gap (2%) where
# the peak-based trail governs, and a wide gap (5%) where the avg*1.03 cost floor binds instead.

_XPL_AVG = 0.09280176777477368
_XPL_QTY = 1645.9
_XPL_TP = 0.09859259808391956
_XPL_V0_NET = 9.2161


def test_xpl_v4a_gap2_peak_based_stop_governs():
    params = _params()
    v0_proceeds = _XPL_QTY * _XPL_TP * (1 - params.maker_fee_pct / 100.0)
    state = rs.open_v4a(gap_pct=2.0, avg_price=_XPL_AVG, qty=_XPL_QTY, tp_price=_XPL_TP,
                         v0_net=_XPL_V0_NET, v0_proceeds=v0_proceeds, opened_at=T0,
                         deadline_at=DEADLINE)
    gap_based = _XPL_TP * 0.98
    floor = _XPL_AVG * 1.03
    assert gap_based > floor  # this regime: the trail, not the cost floor, governs
    assert state.stop == pytest.approx(gap_based)

    trigger_price = state.stop
    state = rs.step(state, trigger_price, T0 + timedelta(minutes=1), params)
    assert state.state == rs.STATE_CLOSED
    fill = trigger_price * (1 - params.slip_pct / 100.0)
    expected_diff = _XPL_QTY * (
        fill * (1 - params.taker_fee_pct / 100.0) - _XPL_TP * (1 - params.maker_fee_pct / 100.0)
    )
    assert state.diff_usd == pytest.approx(expected_diff)
    assert state.net_usd == pytest.approx(_XPL_V0_NET + expected_diff)


def test_xpl_v4a_gap5_cost_floor_governs():
    params = _params()
    v0_proceeds = _XPL_QTY * _XPL_TP * (1 - params.maker_fee_pct / 100.0)
    state = rs.open_v4a(gap_pct=5.0, avg_price=_XPL_AVG, qty=_XPL_QTY, tp_price=_XPL_TP,
                         v0_net=_XPL_V0_NET, v0_proceeds=v0_proceeds, opened_at=T0,
                         deadline_at=DEADLINE)
    gap_based = _XPL_TP * 0.95
    floor = _XPL_AVG * 1.03
    assert floor > gap_based  # this regime: the cost floor governs, not the trail
    assert state.stop == pytest.approx(floor)


def test_xpl_v5_runner_sizing():
    params = _params()
    v0_proceeds = _XPL_QTY * _XPL_TP * (1 - params.maker_fee_pct / 100.0)
    gap = 3.0
    state = rs.open_v5(gap_pct=gap, avg_price=_XPL_AVG, qty=_XPL_QTY, tp_price=_XPL_TP,
                        v0_net=_XPL_V0_NET, v0_proceeds=v0_proceeds, opened_at=T0,
                        deadline_at=DEADLINE)
    decision_price = _XPL_TP * 1.01  # still at/above TP at the 90s mark
    state = rs.step(state, decision_price, T0 + timedelta(seconds=90), params)
    assert state.state == rs.STATE_RUNNER
    risk_frac = gap / 100.0 + 0.002 + 0.03
    expected_runner_usd = min(v0_proceeds, _XPL_V0_NET / risk_frac)
    assert state.runner_usd == pytest.approx(expected_runner_usd)
