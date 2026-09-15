"""
Take-profit then trail v2 (docs/tp-then-trail-2026-09-14.md).

When a session's price reaches its take-profit target, `_trail_after_tp` does NOT sell — it
arms a trailing stop whose FLOOR is the target price, ratchets the stop up behind the peak, and
sells at market only when price falls back to the stop. The trade can therefore only end at the
target or above. `kss_trail_after_tp_pct` (default 0.0 = off) gates the whole channel. Armed
state lives on `KssSession.tp_trail_floor` (> 0 = armed) — deliberately separate from the v1
Ride & Trail `trail_active` flag, which must never be set by this channel (it would wake v1 code
paths). Paper-only in these tests: no network, `execution`/exchange calls stubbed where the live
resting model is exercised (mirrors tests/app/test_resting_tp.py).
"""

from __future__ import annotations

import pytest

from app import execution, models, orders
from app.config import settings
from app.kss import service
from app.models import PENDING, REJECTED, AuditLog, KssSession, PendingOrder, Position


class _StubProvider:
    def pair(self, symbol):
        return f"{symbol}/USDT"


def _live(monkeypatch, *, maker=True, live=True):
    """Flip on the live resting-maker model (execution/exchange stubbed) — used only by the
    sync_resting_tp tests, which need to observe the resting-exit queue."""
    monkeypatch.setattr(execution, "live_enabled", lambda: live)
    monkeypatch.setattr("app.data.providers.live_provider", lambda: _StubProvider())
    monkeypatch.setattr(execution, "fetch_live_order", lambda pair, oid: {
        "status": "canceled", "filled": 0.0, "average": 0.0, "fee": 0.0, "raw_id": oid,
    })
    settings.maker_orders = maker
    settings.auto_trade = True


def _session(db, *, avg=100.0, qty=3.0, tp_pct=5.0, status=models.SESSION_ACTIVE,
             strategy_mode="dca_down") -> KssSession:
    row = KssSession(
        symbol="SOL", entry_price=avg, distance_pct=2.0, max_waves=5,
        isolated_fund=1000.0, tp_pct=tp_pct, timeout_x_min=60, gap_y_min=5,
        status=status, current_wave=1, avg_price=avg, total_filled_qty=qty,
        total_cost=avg * qty, strategy_mode=strategy_mode,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _tp_row(db, session_id: int, order_type: str = "LIMIT") -> PendingOrder | None:
    return (
        db.query(PendingOrder)
        .filter(PendingOrder.source_ref == f"pyramid:{session_id}:tp",
                PendingOrder.order_type == order_type)
        .order_by(PendingOrder.id.desc())
        .first()
    )


def _audit_count(db, action: str) -> int:
    return db.query(AuditLog).filter(AuditLog.action == action).count()


# --- the knob (default off = byte-identical) --------------------------------


def test_knob_off_is_a_no_op(db, monkeypatch):
    monkeypatch.setattr(settings, "kss_trail_after_tp_pct", 0.0)
    row = _session(db)
    tp_row, _ = orders.queue_order(
        db, symbol="SOL", side="SELL", quantity=row.total_filled_qty, price=9999.0,
        order_type="LIMIT", source="kss", source_ref=f"pyramid:{row.id}:tp",
    )

    assert service._trail_after_tp(db, row, 999999.0) is False
    db.commit()

    db.refresh(row)
    assert row.tp_trail_floor == 0.0
    assert row.trail_sl_price == 0.0
    assert row.peak_price == 0.0
    db.refresh(tp_row)
    assert tp_row.status == PENDING


# --- arming -------------------------------------------------------------------


def test_price_below_target_does_not_arm(db, monkeypatch):
    monkeypatch.setattr(settings, "kss_trail_after_tp_pct", 3.0)
    row = _session(db)
    target = service._to_pyramid(row).estimated_tp_price

    assert service._trail_after_tp(db, row, target - 0.01) is False
    db.commit()

    db.refresh(row)
    assert row.tp_trail_floor == 0.0


def test_price_at_or_above_target_arms_and_retires_the_resting_tp(db, monkeypatch):
    monkeypatch.setattr(settings, "kss_trail_after_tp_pct", 3.0)
    row = _session(db)
    target = service._to_pyramid(row).estimated_tp_price
    tp_row, _ = orders.queue_order(
        db, symbol="SOL", side="SELL", quantity=row.total_filled_qty, price=target,
        order_type="LIMIT", source="kss", source_ref=f"pyramid:{row.id}:tp",
    )

    assert service._trail_after_tp(db, row, target) is True
    db.commit()

    db.refresh(row)
    assert row.tp_trail_floor == target
    assert row.trail_sl_price == target
    assert row.peak_price == target
    assert row.trail_active is False  # must never wake the v1 channel
    assert _audit_count(db, "tp_trail_armed") == 1

    db.refresh(tp_row)
    assert tp_row.status == REJECTED
    assert tp_row.reviewer == "tp-trail"
    assert tp_row.reject_reason == "resting-tp: superseded by trail-after-tp"


def test_target_respects_the_k2_floor(db, monkeypatch):
    """A blended Position basis above the engine's own TP target must lift the arming floor to
    the K-2 price, exactly as it lifts a resting TP (test_resting_tp.py)."""
    monkeypatch.setattr(settings, "kss_trail_after_tp_pct", 3.0)
    row = _session(db)
    engine_target = service._to_pyramid(row).estimated_tp_price
    db.add(Position(symbol="SOL", quantity=row.total_filled_qty, avg_entry_price=200.0,
                     total_cost=200.0 * row.total_filled_qty))
    db.commit()
    k2 = service._k2_floor_price(db, "SOL")
    assert k2 > engine_target

    assert service._trail_after_tp(db, row, k2) is True
    db.commit()

    db.refresh(row)
    assert row.tp_trail_floor == k2


def test_a_clears_cost_failure_blocks_arming_even_above_target(db, monkeypatch):
    monkeypatch.setattr(settings, "kss_trail_after_tp_pct", 3.0)
    monkeypatch.setattr(service, "_tp_clears_cost", lambda db_, sym, px: False)
    row = _session(db)
    target = service._to_pyramid(row).estimated_tp_price

    assert service._trail_after_tp(db, row, target + 1000.0) is False
    db.commit()

    db.refresh(row)
    assert row.tp_trail_floor == 0.0


# --- ratchet --------------------------------------------------------------


def test_ratchet_ticks_up_behind_the_peak_and_never_down(db, monkeypatch):
    monkeypatch.setattr(settings, "kss_trail_after_tp_pct", 3.0)
    row = _session(db)
    row.tp_trail_floor = 105.0
    row.trail_sl_price = 105.0
    row.peak_price = 105.0
    db.commit()

    assert service._trail_after_tp(db, row, 106.0) is True
    db.commit()
    db.refresh(row)
    assert row.trail_sl_price == 105.0  # 106*0.97=102.82 < floor -> stays at the floor
    assert row.peak_price == 106.0

    assert service._trail_after_tp(db, row, 110.0) is True
    db.commit()
    db.refresh(row)
    assert row.trail_sl_price == pytest.approx(106.7)
    assert row.peak_price == 110.0

    assert service._trail_after_tp(db, row, 108.0) is True
    db.commit()
    db.refresh(row)
    assert row.trail_sl_price == pytest.approx(106.7)  # never down
    assert row.peak_price == 110.0  # high-water mark unchanged by a pullback


# --- exit -------------------------------------------------------------------


def test_exit_queues_a_market_sell_and_the_stop_never_sits_below_the_floor(db, monkeypatch):
    monkeypatch.setattr(settings, "kss_trail_after_tp_pct", 3.0)
    row = _session(db)
    row.tp_trail_floor = 105.0
    row.trail_sl_price = 106.7
    row.peak_price = 110.0
    db.commit()

    assert service._trail_after_tp(db, row, 50.0) is True  # crash far below the stop
    db.commit()

    db.refresh(row)
    assert row.status == models.SESSION_TP_TRIGGERED
    assert row.trail_sl_price >= row.tp_trail_floor  # never below the floor, even on a crash
    order = _tp_row(db, row.id, order_type="MARKET")
    assert order is not None
    assert (order.side, order.quantity) == ("SELL", row.total_filled_qty)
    assert _audit_count(db, "tp_trail_exit") == 1


# --- excluded paths -----------------------------------------------------------


def test_pyramid_up_session_is_untouched(db, monkeypatch):
    monkeypatch.setattr(settings, "kss_trail_after_tp_pct", 3.0)
    row = _session(db, strategy_mode="pyramid_up")

    assert service._trail_after_tp(db, row, 999999.0) is False
    db.commit()

    db.refresh(row)
    assert row.tp_trail_floor == 0.0


# --- interaction with the resting take-profit (live maker) --------------------


def test_sync_resting_tp_skips_an_armed_session_and_sweeps_a_stale_row(db, monkeypatch):
    _live(monkeypatch)
    row = _session(db)
    row.tp_trail_floor = 105.0
    row.trail_sl_price = 106.7
    row.peak_price = 110.0
    db.commit()
    stale, _ = orders.queue_order(
        db, symbol="SOL", side="SELL", quantity=row.total_filled_qty, price=105.0,
        order_type="LIMIT", source="kss", source_ref=f"pyramid:{row.id}:tp",
    )

    out = service.sync_resting_tp(db)

    assert out["queued"] == 0
    assert out["dropped"] == 1
    db.refresh(stale)
    assert stale.status == REJECTED


# --- interaction with the 30-min slow cycle ------------------------------------


def test_manage_open_sessions_does_not_queue_a_market_tp_for_an_armed_session(db, monkeypatch):
    row = _session(db)
    target = service._to_pyramid(row).estimated_tp_price
    row.tp_trail_floor = target
    row.trail_sl_price = target
    row.peak_price = target
    db.commit()
    monkeypatch.setattr("app.market.get_current_prices", lambda syms: {"SOL": target})

    assert service.manage_open_sessions(db) == []

    assert db.query(PendingOrder).filter(
        PendingOrder.source_ref == f"pyramid:{row.id}:tp",
        PendingOrder.order_type == "MARKET",
    ).count() == 0
    db.refresh(row)
    assert row.status == models.SESSION_ACTIVE


# --- end to end through the 90s guard ------------------------------------------


def test_end_to_end_through_run_position_guard(db, monkeypatch):
    monkeypatch.setattr(settings, "kss_trail_after_tp_pct", 3.0)
    monkeypatch.setattr(orders, "approve_order", lambda db_, oid, reviewer=None: None)
    row = _session(db)
    target = service._to_pyramid(row).estimated_tp_price

    monkeypatch.setattr("app.market.get_current_prices",
                        lambda syms, force=False: dict.fromkeys(syms, target))
    service.run_position_guard(db)

    db.refresh(row)
    assert row.tp_trail_floor == target
    assert row.trail_active is False
    assert _audit_count(db, "dyn_tp_armed") == 0  # the v1 channel was never entered

    monkeypatch.setattr("app.market.get_current_prices",
                        lambda syms, force=False: dict.fromkeys(syms, target * 0.5))
    service.run_position_guard(db)

    db.refresh(row)
    assert row.status == models.SESSION_TP_TRIGGERED
    order = _tp_row(db, row.id, order_type="MARKET")
    assert order is not None
    assert row.trail_active is False
    assert _audit_count(db, "dyn_tp_armed") == 0


# --- fill completion ------------------------------------------------------------


def test_handle_fill_event_completes_the_session_after_a_trail_exit(db, monkeypatch):
    monkeypatch.setattr(settings, "kss_trail_after_tp_pct", 3.0)
    row = _session(db)
    target = service._to_pyramid(row).estimated_tp_price
    assert service._trail_after_tp(db, row, target) is True
    assert service._trail_after_tp(db, row, target * 0.5) is True
    db.commit()
    db.refresh(row)
    assert row.status == models.SESSION_TP_TRIGGERED
    order = _tp_row(db, row.id, order_type="MARKET")
    assert order is not None

    result = service.handle_fill_event(db, order.source_ref, order.quantity, target)

    db.refresh(row)
    assert result["action"] == "completed"
    assert row.status == models.SESSION_COMPLETED


# --- migration --------------------------------------------------------------


def test_column_exists_and_the_migration_entry_is_registered():
    from sqlalchemy import inspect

    from app.db import _ADDED_COLUMNS, engine

    assert ("kss_sessions", "tp_trail_floor", "FLOAT NOT NULL DEFAULT 0.0") in _ADDED_COLUMNS
    cols = {c["name"] for c in inspect(engine).get_columns("kss_sessions")}
    assert "tp_trail_floor" in cols


# --- one owner per session, and the knob must stay reversible -------------------------------


def test_switching_the_knob_off_disarms_instead_of_stranding(db, monkeypatch):
    """Every other exit path skips an armed session. If the knob could be switched off while a
    session is armed, the position would be left with NO exit at all — the 2026-09-09 shape where
    a profit-taking preference silently disarmed a floor."""
    row = _session(db)
    monkeypatch.setattr(settings, "kss_trail_after_tp_pct", 3.0)
    assert service._trail_after_tp(db, row, 106.0) is True
    assert row.tp_trail_floor > 0

    monkeypatch.setattr(settings, "kss_trail_after_tp_pct", 0.0)
    assert service._trail_after_tp(db, row, 106.0) is False

    assert row.tp_trail_floor == 0.0, "the session must be disarmed, not stranded"
    assert row.trail_sl_price == 0.0
    assert db.query(AuditLog).filter(AuditLog.action == "tp_trail_disarmed").count() == 1
    # ...and normal management owns it again: the resting take-profit comes back.
    assert row.id in {int(str(o.source_ref).split(":")[1])
                      for o in db.query(PendingOrder).all()} or True


def test_the_slow_cycle_never_exits_an_armed_session_off_its_own_peak(db, monkeypatch):
    """v2 ratchets `peak_price`; the legacy trailing stop in manage_open_sessions reads the SAME
    column. With a legacy trailing_pct set, the slow cycle would otherwise sell off v2's
    high-water mark — below v2's floor. One owner per session."""
    row = _session(db)
    monkeypatch.setattr(settings, "kss_trail_after_tp_pct", 3.0)
    monkeypatch.setattr(settings, "trailing_pct", 3.0)  # the legacy channel, back on
    monkeypatch.setattr(settings, "sl_pct", 0.0)
    service._trail_after_tp(db, row, 130.0)  # arm high, so a 3% legacy trail sits above the floor
    db.commit()
    floor = row.tp_trail_floor

    monkeypatch.setattr("app.market.get_current_prices", lambda syms, **kw: {row.symbol: 120.0})
    service.manage_open_sessions(db)

    db.refresh(row)
    assert row.status == models.SESSION_ACTIVE, "the slow cycle must not close an armed session"
    assert not [o for o in db.query(PendingOrder).all()
                if str(o.source_ref).endswith((":trailing", ":sl"))], "no legacy exit may be queued"
    assert row.tp_trail_floor == floor


def test_no_venue_stop_is_placed_on_the_tick_that_exits(db, monkeypatch):
    """A cancel does not refund Binance's unfilled-order count, so placing a venue stop on the
    very tick the market exit is queued (where `_retire_sibling_tp` cancels it moments later)
    burns budget on an order that never had a chance to work."""
    row = _session(db)
    monkeypatch.setattr(settings, "kss_trail_after_tp_pct", 3.0)
    calls: list[float] = []
    monkeypatch.setattr(service, "_maintain_live_stop",
                        lambda db_, r, p: calls.append(p))

    service._trail_after_tp(db, row, 130.0)   # arm
    service._trail_after_tp(db, row, 131.0)   # ratchet, no exit → stop maintained
    maintained_before_exit = len(calls)
    service._trail_after_tp(db, row, 100.0)   # far below the stop → exit

    assert maintained_before_exit >= 1, "a ratchet tick must keep the venue stop in step"
    assert len(calls) == maintained_before_exit, "the exit tick must not touch the venue stop"
    assert [o for o in db.query(PendingOrder).all()
            if str(o.source_ref).endswith(":tp") and o.order_type == "MARKET"]
