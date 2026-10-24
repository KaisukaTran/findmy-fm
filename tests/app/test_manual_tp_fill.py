"""O3: the fill of a manual take-profit (`pyramid:{id}:manual_tp`, three parts) completes the session.

`handle_fill_event` only knew `tp` / `stop` for a profitable exit; `manual_tp` matched neither
branch, fell through to `int(parts[3])` and raised IndexError. `orders.approve_order` swallows a
fill-hook exception, so the Fill was booked while the session stayed TP_TRIGGERED with its ladder
uncancelled - the "chốt lời ngay" button left a zombie behind it.
"""

from __future__ import annotations

import logging

import pytest

from app import models, orders, runtime
from app.kss import service

_EX = {"minQty": 0.00001, "stepSize": 0.00001, "maxQty": 10000.0}


@pytest.fixture
def mock_market(monkeypatch):
    monkeypatch.setattr("app.kss.pyramid.get_exchange_info", lambda s: _EX)
    monkeypatch.setattr("app.kss.pyramid.get_current_prices", lambda syms: {"BTC": 49000.0})
    monkeypatch.setattr("app.orders.get_current_prices", lambda syms: {"BTC": 49000.0})


def _armed_session(db) -> models.KssSession:
    """An ACTIVE BTC session that really holds inventory (wave 0 filled through the real approve
    path, so the Position, the Fill and the KSS hook are genuine) and is in trailing mode - the
    only state the manual take-profit button is offered in."""
    row = service.create_session(
        db, symbol="BTC", entry_price=50000, distance_pct=2, max_waves=3,
        isolated_fund=100000, tp_pct=3, timeout_x_min=30, gap_y_min=5, deadline_days=30,
    )
    res = service.start_session(db, row.id)
    orders.approve_order(db, res["pending_order_id"])
    db.refresh(row)
    assert row.status == models.SESSION_ACTIVE and row.total_filled_qty > 0
    row.trail_active = True
    db.commit()
    return row


def _manual_tp_orders(db, session_id: int) -> list[models.PendingOrder]:
    return (
        db.query(models.PendingOrder)
        .filter(models.PendingOrder.source_ref == f"pyramid:{session_id}:manual_tp")
        .all()
    )


def _pending_rungs(db, session_id: int) -> list[models.PendingOrder]:
    return (
        db.query(models.PendingOrder)
        .filter(
            models.PendingOrder.source_ref.like(f"pyramid:{session_id}:wave:%"),
            models.PendingOrder.status == models.PENDING,
        )
        .all()
    )


def _assert_no_hook_failure(caplog) -> None:
    assert not [r for r in caplog.records if "KSS fill hook failed" in r.getMessage()]


def test_manual_tp_fill_completes_session(db, mock_market, caplog):
    row = _armed_session(db)
    assert _pending_rungs(db, row.id)                 # the ladder is waiting on the book

    with caplog.at_level(logging.ERROR, logger="app.orders"):
        service.manual_take_profit(db, row.id)        # queues the SELL and approves it for real

    sell = _manual_tp_orders(db, row.id)
    assert len(sell) == 1 and sell[0].status == models.EXECUTED
    db.refresh(row)
    assert row.status == models.SESSION_COMPLETED
    assert _pending_rungs(db, row.id) == []
    assert runtime.get(db, "stop_cooldown:BTC") is None        # a voluntary exit sets no cooldown
    assert runtime.get(db, "deadline_cooldown:BTC") is None
    _assert_no_hook_failure(caplog)


def test_queued_manual_tp_completes_when_approved(db, mock_market, caplog):
    """The same fill reached the other way: a manual_tp SELL sitting PENDING, approved later."""
    row = _armed_session(db)
    sell, _ = orders.queue_order(
        db, symbol="BTC", side="SELL", quantity=row.total_filled_qty, price=0.0,
        order_type="MARKET", source="kss", source_ref=f"pyramid:{row.id}:manual_tp",
    )

    with caplog.at_level(logging.ERROR, logger="app.orders"):
        orders.approve_order(db, sell.id, reviewer="manual")

    db.refresh(row)
    assert row.status == models.SESSION_COMPLETED
    _assert_no_hook_failure(caplog)


def test_manual_tp_before_fix_shape_is_covered(db, mock_market):
    """The direct hook call that raised IndexError before the fix."""
    row = _armed_session(db)

    out = service.handle_fill_event(
        db, f"pyramid:{row.id}:manual_tp", row.total_filled_qty, row.avg_price)

    assert out["action"] == "completed" and "manual TP" in out["message"]
    db.refresh(row)
    assert row.status == models.SESSION_COMPLETED


def test_partial_manual_tp_keeps_remainder(db, mock_market):
    row = _armed_session(db)
    qty = row.total_filled_qty

    out = service.handle_fill_event(db, f"pyramid:{row.id}:manual_tp", qty / 2, row.avg_price)

    assert out["action"] == "partial_manual_tp"
    db.refresh(row)
    assert row.total_filled_qty == pytest.approx(qty / 2)
    assert row.status == models.SESSION_ACTIVE
