"""
Tests for connecting the two KSS mechanisms that were running past each other (2026-09-06).

THE DEFECT, measured on the live book, not hypothesised:
  - `_evaluate_dynamic_exit`'s ride branch says it "suppresses the fixed TP so a runner is not
    capped". Under the maker/resting model that take-profit is not in the guard any more — it is
    RESTING ON THE EXCHANGE via `sync_resting_tp`, and it fills whatever the guard believes.
    Live exits: 25 fixed take-profit, 3 trailing, 1 hard stop.
  - The arm threshold was a flat 5% while autotune fits `tp_pct` per coin (2.8%-6.2%), and BOTH
    are anchored to the same average — so the race between them is decided by two constants,
    permanently. Four of six open sessions had `tp_pct < 5`: their trailing exit could never arm,
    not once, ever. AVAX's resting TP sat at +3.63% against an arm threshold of +5.00%.

Both halves are needed. Arming earlier with the old resting TP still lets the fixed order fill
first; re-pricing the resting order without arming earlier never happens because the session
never arms. Each test below therefore pins one half, and the last two pin them together.
"""

from __future__ import annotations

import pytest

from app import execution, models
from app.config import settings
from app.kss import dynamic_exit, service
from app.models import PENDING, KssSession, PendingOrder


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    monkeypatch.setattr(settings, "kss_dynamic_tp_enabled", True)
    monkeypatch.setattr(settings, "kss_trail_arm_pct", 5.0)
    monkeypatch.setattr(settings, "kss_trail_lock_pct", 2.0)
    monkeypatch.setattr(settings, "kss_trail_min_pct", 3.0)
    monkeypatch.setattr(settings, "kss_tp_gap_pct", 5.0)


class TestArmPctFollowsTheSessionsOwnTakeProfit:
    def test_off_by_default_so_behaviour_is_unchanged(self, monkeypatch):
        monkeypatch.setattr(settings, "kss_trail_arm_tp_frac", 0.0)
        assert dynamic_exit.arm_pct_for(2.82) == 5.0
        assert dynamic_exit.arm_threshold(100.0, 2.82) == pytest.approx(105.0)

    def test_a_coin_whose_take_profit_is_below_the_flat_threshold_can_now_arm(self, monkeypatch):
        # XLM, live: tp_pct 2.82 against a 5% arm threshold — it could never arm.
        # CORRECTED 2026-09-06: this asserted 1.692, which is BELOW the 2% lock floor the armed
        # stop lands on — the session armed already stopped out. The threshold is now floored at
        # lock + ARM_LOCK_MARGIN_PCT. It still arms (2.5% < its 2.82% take-profit), which is what
        # this test is for; it just no longer arms underneath its own stop. See
        # tests/app/test_trail_arm_floor.py for the invariant and the live evidence.
        monkeypatch.setattr(settings, "kss_trail_arm_tp_frac", 0.6)
        assert dynamic_exit.arm_pct_for(2.82) == pytest.approx(2.5)
        assert dynamic_exit.arm_threshold(100.0, 2.82) == pytest.approx(102.5)
        assert dynamic_exit.arm_threshold(100.0, 2.82) > dynamic_exit.lock_floor_price(100.0)

    def test_arming_always_lands_strictly_below_the_take_profit(self, monkeypatch):
        monkeypatch.setattr(settings, "kss_trail_arm_tp_frac", 0.6)
        for tp in (2.82, 3.39, 3.79, 4.46, 5.65, 6.19, 12.0):
            assert dynamic_exit.arm_pct_for(tp) < tp, tp

    def test_the_flat_cap_still_applies_to_a_wide_take_profit(self, monkeypatch):
        # A 20% TP must not push the arm threshold out to 12%; the flat value is a ceiling.
        monkeypatch.setattr(settings, "kss_trail_arm_tp_frac", 0.6)
        assert dynamic_exit.arm_pct_for(20.0) == 5.0

    def test_no_tp_pct_falls_back_to_the_flat_threshold(self, monkeypatch):
        monkeypatch.setattr(settings, "kss_trail_arm_tp_frac", 0.6)
        assert dynamic_exit.arm_pct_for(0.0) == 5.0

    def test_should_arm_uses_it(self, monkeypatch):
        monkeypatch.setattr(settings, "kss_trail_arm_tp_frac", 0.6)
        kw = {"avg": 100.0, "filled_qty": 1.0, "trail_active": False, "tp_pct": 2.82}
        assert not dynamic_exit.should_arm(market=101.0, **kw)
        assert not dynamic_exit.should_arm(market=102.0, **kw)  # under the 2% lock floor
        assert dynamic_exit.should_arm(market=103.0, **kw)      # cleared 102.5
        # ...and with the feature off, the same price does not arm.
        monkeypatch.setattr(settings, "kss_trail_arm_tp_frac", 0.0)
        assert not dynamic_exit.should_arm(market=103.0, **kw)


class TestArmedCeilingBeatsTheFixedTakeProfit:
    """The economic property the wiring depends on: arming must RAISE the exit, never lower it."""

    @pytest.mark.parametrize("tp_pct", [2.82, 3.39, 3.79, 4.46, 5.65, 6.19])
    def test_the_trail_ceiling_sits_above_the_fixed_take_profit_it_replaces(self, tp_pct):
        avg = 100.0
        fixed_tp = avg * (1 + tp_pct / 100)
        # Worst case for the trail: the stop sits exactly on its lock floor.
        sl = dynamic_exit.lock_floor_price(avg)
        ceiling = dynamic_exit.compute_tp(sl=sl, avg=avg)
        assert ceiling > fixed_tp, f"arming would LOWER the exit for tp_pct={tp_pct}"

    def test_the_ceiling_ratchets_up_with_the_peak(self):
        avg = 100.0
        prev = 0.0
        ceilings = []
        for peak in (106.0, 110.0, 118.0):
            sl = dynamic_exit.compute_sl(peak=peak, avg=avg, distance_pct=3.2,
                                         trail_dist_pct=3.0, prev_sl=prev)
            prev = sl
            ceilings.append(dynamic_exit.compute_tp(sl=sl, avg=avg))
        assert ceilings == sorted(ceilings)
        assert ceilings[-1] > ceilings[0]

    def test_the_locked_floor_still_guarantees_a_profitable_exit(self):
        # Even at the worst trailing outcome the stop is above the fee floor, so the connected
        # design can trade a +2.8% fixed win for a +2.0% trailed win — never for a loss.
        avg = 100.0
        assert dynamic_exit.lock_floor_price(avg) > dynamic_exit.fee_floor_price(avg) * 0.999
        assert dynamic_exit.lock_floor_price(avg) > avg


# --- the other half: the order actually resting on the exchange --------------


class _StubProvider:
    def pair(self, symbol):
        return f"{symbol}/USDT"


def _live(monkeypatch):
    """Live + maker, with every network call stubbed (mirrors tests/app/test_resting_tp.py)."""
    monkeypatch.setattr(execution, "live_enabled", lambda: True)
    monkeypatch.setattr("app.data.providers.live_provider", lambda: _StubProvider())
    monkeypatch.setattr(execution, "fetch_live_order", lambda pair, oid: {
        "status": "canceled", "filled": 0.0, "average": 0.0, "fee": 0.0, "raw_id": oid,
    })
    monkeypatch.setattr(settings, "maker_orders", True)
    monkeypatch.setattr(settings, "auto_trade", True)


def _session(db, *, avg=10.0, qty=3.0, tp_pct=3.0, trail_active=False, trail_sl=0.0):
    row = KssSession(
        symbol="SOL", entry_price=10.0, distance_pct=2.0, max_waves=5,
        isolated_fund=1000.0, tp_pct=tp_pct, timeout_x_min=60, gap_y_min=5,
        status=models.SESSION_ACTIVE, current_wave=1, avg_price=avg,
        total_filled_qty=qty, total_cost=avg * qty,
        trail_active=trail_active, trail_sl_price=trail_sl,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _resting_tp(db, sid):
    return (db.query(PendingOrder)
            .filter(PendingOrder.source_ref == f"pyramid:{sid}:tp",
                    PendingOrder.status == PENDING)
            .order_by(PendingOrder.id.desc()).first())


class TestRestingTpFollowsTheTrail:
    def test_an_unarmed_session_still_rests_at_the_fixed_take_profit(self, db, monkeypatch):
        _live(monkeypatch)
        row = _session(db, tp_pct=3.0)
        service.sync_resting_tp(db)
        order = _resting_tp(db, row.id)
        assert order is not None
        assert order.price == pytest.approx(10.0 * 1.03, rel=0.01)

    def test_an_armed_session_rests_at_the_trail_ceiling_instead(self, db, monkeypatch):
        # The whole point: once armed, the exit on the exchange must be the ratcheting ceiling,
        # not the fixed take-profit that would otherwise fill first and cap the runner.
        _live(monkeypatch)
        row = _session(db, tp_pct=3.0, trail_active=True, trail_sl=10.6)
        service.sync_resting_tp(db)
        order = _resting_tp(db, row.id)
        assert order is not None
        expected = dynamic_exit.compute_tp(sl=10.6, avg=10.0)
        assert order.price == pytest.approx(expected, rel=1e-6)
        assert order.price > 10.0 * 1.03, "arming must RAISE the exit, never lower it"

    def test_the_resting_exit_walks_up_as_the_stop_ratchets(self, db, monkeypatch):
        _live(monkeypatch)
        row = _session(db, tp_pct=3.0, trail_active=True, trail_sl=10.6)
        service.sync_resting_tp(db)
        first = _resting_tp(db, row.id).price

        row.trail_sl_price = 11.4          # the peak rose, the stop ratcheted up behind it
        db.commit()
        service.sync_resting_tp(db)
        assert _resting_tp(db, row.id).price > first

    def test_an_armed_session_is_never_left_without_a_resting_exit(self, db, monkeypatch):
        # Cancel+replace must never produce a gap: after every sync there is exactly one
        # pending exit for the session. An exit is never removed without a replacement.
        _live(monkeypatch)
        row = _session(db, tp_pct=3.0, trail_active=True, trail_sl=10.6)
        for sl in (10.6, 11.0, 11.9):
            row.trail_sl_price = sl
            db.commit()
            service.sync_resting_tp(db)
            live_tps = (db.query(PendingOrder)
                        .filter(PendingOrder.source_ref == f"pyramid:{row.id}:tp",
                                PendingOrder.status == PENDING).all())
            assert len(live_tps) == 1, f"{len(live_tps)} resting exits at sl={sl}"
