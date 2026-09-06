"""
The arming threshold and the stop it arms are two different knobs, and nothing tied them together.

THE DEFECT, measured on the live book 2026-09-06 (testnet), not hypothesised:

  ``kss_trail_arm_tp_frac=0.6`` made the arm threshold ``min(5%, 0.6 x tp_pct)`` while the stop
  armed at ``max(grid_sl, lock_floor)`` — and at the arm tick ``grid_sl`` always collapses to
  ``avg``, so the stop is pinned at ``avg x (1 + kss_trail_lock_pct)`` = +2%. For any coin whose
  ``tp_pct < lock_pct / frac`` (3.33 at the live settings) the session therefore ARMS BELOW ITS
  OWN STOP: ~25 coins in the live book (BTC 2.63, TRX 1.38, ALGO 2.75, AVAX 3.39...).

  ``_evaluate_dynamic_exit``'s own comment asserted the opposite — "arming on current price
  guarantees price >= arm > lock floor at arm, so the SL sits below price (no immediate sub-floor
  stop)" — which was true only while the threshold was the flat 5%. The new knob walked underneath
  it and no validator noticed, because the two knobs are read in different modules.

  The cost is not a fee. Arming CANCELS THE DCA LADDER first (``_cancel_pending_waves``), so a
  session that arms under its stop throws away the averaging-down that is the entire strategy and
  then exits on the next 90s tick. Live: SEI armed 14:11:58 at +3.25% and was stopped out at
  14:25:30 at +2.0%, ladder already gone, against its own take-profit of +4.47%.

The invariant these tests pin is the one the comment claimed: a session never arms a stop that is
already at or above the price arming it. It is enforced twice — in ``arm_pct_for`` (no configuration
can put the threshold under the floor) and at the arming site (no arithmetic can either) — because
the first is a knob and the second is a fact.
"""

from __future__ import annotations

import pytest

from app import execution, models
from app.config import settings
from app.kss import dynamic_exit, service
from app.models import PENDING, REJECTED, KssSession, PendingOrder


@pytest.fixture(autouse=True)
def _live_trail_settings(monkeypatch):
    """The live book's own configuration, so these tests fail on what live actually ran."""
    monkeypatch.setattr(settings, "kss_dynamic_tp_enabled", True)
    monkeypatch.setattr(settings, "kss_trail_arm_pct", 5.0)
    monkeypatch.setattr(settings, "kss_trail_arm_tp_frac", 0.6)
    monkeypatch.setattr(settings, "kss_trail_lock_pct", 2.0)
    monkeypatch.setattr(settings, "kss_trail_min_pct", 3.0)
    monkeypatch.setattr(settings, "kss_trail_atr_mult", 1.0)
    monkeypatch.setattr(settings, "kss_tp_gap_pct", 5.0)
    monkeypatch.setattr(service, "_session_atr_pct", lambda symbol: 0.0)
    monkeypatch.setattr("app.notify.event", lambda *a, **k: None)


# --- the knob half: no configuration may put the threshold under the floor ---------------------


class TestTheArmThresholdCannotSitUnderTheStopItArms:
    @pytest.mark.parametrize("tp_pct", [1.38, 2.54, 2.63, 2.75, 2.82, 3.0, 3.33, 3.39, 4.46,
                                        5.65, 6.19, 8.23, 10.9, 15.0, 20.0])
    def test_the_arm_percentage_clears_the_lock_percentage(self, tp_pct):
        # The whole live tp_pct range, including the four coins that motivated the frac knob.
        assert dynamic_exit.arm_pct_for(tp_pct) > settings.kss_trail_lock_pct, tp_pct

    @pytest.mark.parametrize("tp_pct", [1.38, 2.63, 3.39, 6.19, 10.9])
    def test_the_arm_price_clears_the_lock_floor_price(self, tp_pct):
        avg = 100.0
        assert dynamic_exit.arm_threshold(avg, tp_pct) > dynamic_exit.lock_floor_price(avg), tp_pct

    def test_a_flat_threshold_below_the_lock_is_lifted_too(self, monkeypatch):
        # The same trap without the fraction knob: an operator setting arm 1% against a 2% lock.
        monkeypatch.setattr(settings, "kss_trail_arm_tp_frac", 0.0)
        monkeypatch.setattr(settings, "kss_trail_arm_pct", 1.0)
        assert dynamic_exit.arm_pct_for(0.0) > settings.kss_trail_lock_pct

    def test_the_live_configuration_is_unchanged_where_it_was_already_safe(self, monkeypatch):
        # Regression guard: the clamp must not move a threshold that already cleared the floor.
        monkeypatch.setattr(settings, "kss_trail_arm_tp_frac", 0.0)
        assert dynamic_exit.arm_pct_for(2.82) == 5.0
        assert dynamic_exit.arm_pct_for(0.0) == 5.0
        monkeypatch.setattr(settings, "kss_trail_arm_tp_frac", 0.6)
        assert dynamic_exit.arm_pct_for(20.0) == 5.0      # the flat value is still the ceiling
        assert dynamic_exit.arm_pct_for(6.19) == pytest.approx(3.714)

    def test_arming_still_lands_strictly_below_the_take_profit_it_replaces(self):
        # The clamp must not push the threshold past the fixed TP, or the resting order fills
        # first and the trail never gets its window — the defect the frac knob was added to fix.
        for tp in (3.39, 3.79, 4.46, 5.65, 6.19, 10.9):
            assert dynamic_exit.arm_pct_for(tp) < tp, tp


# --- the site half: no arithmetic may arm a stop at or above the price -------------------------


def _session(db, *, symbol="BTC", avg=100.0, tp_pct=2.63, qty=1.0):
    row = KssSession(
        symbol=symbol, entry_price=avg, distance_pct=3.2, max_waves=3,
        isolated_fund=240.0, tp_pct=tp_pct, timeout_x_min=60, gap_y_min=5,
        status=models.SESSION_ACTIVE, current_wave=1, avg_price=avg,
        total_filled_qty=qty, total_cost=avg * qty,
        trail_active=False, trail_sl_price=0.0,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _ladder(db, row, n=2):
    """The still-pending DCA rungs an arming session would cancel."""
    for k in range(1, n + 1):
        db.add(PendingOrder(
            symbol=row.symbol, side="BUY", quantity=1.0,
            price=row.avg_price * (1 - 0.032 * k), order_type="LIMIT", status=PENDING,
            source_ref=f"pyramid:{row.id}:wave:{k}", strategy_name=f"Pyramid_{row.symbol}",
        ))
    db.commit()


def _pending_rungs(db, row):
    return (db.query(PendingOrder)
            .filter(PendingOrder.source_ref.like(f"pyramid:{row.id}:wave:%"),
                    PendingOrder.status == PENDING).count())


class TestASessionNeverArmsUnderItsOwnStop:
    @pytest.mark.parametrize("tp_pct", [1.38, 2.63, 2.75, 3.0, 3.39, 4.46, 6.19, 10.9])
    def test_whenever_it_arms_the_stop_is_strictly_below_the_price(self, db, tp_pct):
        row = _session(db, tp_pct=tp_pct)
        armed_at = None
        for step in range(1, 121):                 # walk price up from +0.1% to +12%
            price = row.avg_price * (1 + step / 1000)
            service._evaluate_dynamic_exit(db, row, price)
            if row.trail_active:
                armed_at = price
                break
        assert armed_at is not None, f"tp_pct={tp_pct} never armed"
        assert row.trail_sl_price < armed_at, (
            f"tp_pct={tp_pct}: armed at {armed_at} with a stop at {row.trail_sl_price} — "
            f"the session is already stopped out at the moment it arms")

    def test_the_price_that_used_to_arm_below_the_floor_no_longer_arms(self, db):
        # BTC live: tp_pct 2.63 armed at +1.578% while its stop floor sat at +2%.
        row = _session(db, tp_pct=2.63)
        _ladder(db, row)
        service._evaluate_dynamic_exit(db, row, row.avg_price * 1.016)
        assert not row.trail_active

    def test_a_refused_arming_keeps_the_ladder(self, db):
        # The expensive half: arming cancels the DCA rungs FIRST. A session that declines to arm
        # must still be able to average down.
        row = _session(db, tp_pct=2.63)
        _ladder(db, row, n=2)
        service._evaluate_dynamic_exit(db, row, row.avg_price * 1.016)
        assert not row.trail_active
        assert _pending_rungs(db, row) == 2, "the ladder was cancelled by an arming that never happened"

    def test_the_ladder_is_still_cancelled_when_it_does_arm(self, db):
        # ...and the ride-up commitment itself is unchanged: you cannot average down and trail up.
        row = _session(db, tp_pct=6.19)
        _ladder(db, row, n=2)
        for step in range(1, 121):
            service._evaluate_dynamic_exit(db, row, row.avg_price * (1 + step / 1000))
            if row.trail_active:
                break
        assert row.trail_active
        assert _pending_rungs(db, row) == 0
        rejected = (db.query(PendingOrder)
                    .filter(PendingOrder.source_ref.like(f"pyramid:{row.id}:wave:%"),
                            PendingOrder.status == REJECTED).count())
        assert rejected == 2

    def test_an_armed_session_is_not_stopped_out_on_the_very_next_tick(self, db):
        # The end-to-end shape of the live SEI failure: arm, then the next 90s tick fires a
        # full-size MARKET exit because price <= carried_sl.
        row = _session(db, tp_pct=2.63)
        armed_at = None
        for step in range(1, 121):
            price = row.avg_price * (1 + step / 1000)
            service._evaluate_dynamic_exit(db, row, price)
            if row.trail_active:
                armed_at = price
                break
        assert armed_at is not None
        service._evaluate_dynamic_exit(db, row, armed_at)     # same price, next tick
        assert row.status == models.SESSION_ACTIVE
        exits = (db.query(PendingOrder)
                 .filter(PendingOrder.source_ref == f"pyramid:{row.id}:trail_sl").count())
        assert exits == 0, "armed and stopped out at the same price"


# --- the wide end of the distribution: arming must never LOWER the exit ------------------------


class _StubProvider:
    def pair(self, symbol):
        return f"{symbol}/USDT"


def _live(monkeypatch):
    monkeypatch.setattr(execution, "live_enabled", lambda: True)
    monkeypatch.setattr("app.data.providers.live_provider", lambda: _StubProvider())
    monkeypatch.setattr(settings, "maker_orders", True)
    monkeypatch.setattr(settings, "auto_trade", True)


class TestArmingNeverLowersTheRestingExit:
    @pytest.mark.parametrize("tp_pct", [8.23, 10.9, 15.0])
    def test_a_wide_take_profit_is_not_repriced_down_to_the_trail_ceiling(
        self, db, monkeypatch, tp_pct
    ):
        # ARB 10.9, DASH 10.9, PROM/NFP/HFT 15.0 live. At arming the ceiling is
        # avg x lock x gap = avg x 1.071 — BELOW the fixed take-profit these coins already rest at.
        # The old test parametrized only 2.82-6.19 (the six sessions open that day) and the commit
        # generalised from it.
        _live(monkeypatch)
        avg = 10.0
        row = _session(db, symbol="ARB", avg=avg, tp_pct=tp_pct, qty=3.0)
        row.trail_active = True
        row.trail_sl_price = dynamic_exit.lock_floor_price(avg)
        db.commit()
        service.sync_resting_tp(db)
        order = (db.query(PendingOrder)
                 .filter(PendingOrder.source_ref == f"pyramid:{row.id}:tp",
                         PendingOrder.status == PENDING)
                 .order_by(PendingOrder.id.desc()).first())
        assert order is not None
        fixed_tp = avg * (1 + tp_pct / 100)
        assert order.price >= fixed_tp * 0.999, (
            f"arming LOWERED the exit from {fixed_tp} to {order.price} for tp_pct={tp_pct}")
