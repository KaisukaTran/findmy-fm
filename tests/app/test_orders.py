"""Tests for the order lifecycle: queue -> approve/reject -> fill -> position."""

import pytest

from app import models, orders


def test_queue_order_pending(db):
    order, risk_note = orders.queue_order(
        db, symbol="BTC", side="BUY", quantity=0.001, price=60000.0
    )
    assert order.id is not None
    assert order.status == models.PENDING
    assert db.query(models.PendingOrder).count() == 1


def test_queue_requires_qty_or_pips(db):
    with pytest.raises(ValueError):
        orders.queue_order(db, symbol="BTC", side="BUY")


def test_approve_buy_creates_fill_and_position(db):
    order, _ = orders.queue_order(db, symbol="BTC", side="BUY", quantity=0.01, price=50000.0)
    fill = orders.approve_order(db, order.id, reviewer="tester")

    assert fill.id is not None
    db.refresh(order)
    assert order.status == models.EXECUTED
    # BUY effective price includes +slippage; fee applied
    assert fill.price >= 50000.0
    assert fill.fee > 0

    pos = db.query(models.Position).filter_by(symbol="BTC").one()
    assert pos.quantity == pytest.approx(0.01)
    assert pos.avg_entry_price == pytest.approx(fill.price + fill.fee / 0.01, rel=1e-6)


def test_sell_realizes_pnl(db):
    # buy then sell higher
    b, _ = orders.queue_order(db, symbol="ETH", side="BUY", quantity=1.0, price=1000.0)
    orders.approve_order(db, b.id)
    s, _ = orders.queue_order(db, symbol="ETH", side="SELL", quantity=1.0, price=1200.0)
    sell_fill = orders.approve_order(db, s.id)

    assert sell_fill.realized_pnl > 0  # profit
    pos = db.query(models.Position).filter_by(symbol="ETH").one()
    assert pos.quantity == pytest.approx(0.0)
    assert pos.realized_pnl == pytest.approx(sell_fill.realized_pnl)


def test_reject_order(db):
    order, _ = orders.queue_order(db, symbol="BTC", side="BUY", quantity=0.01, price=50000.0)
    rejected = orders.reject_order(db, order.id, reason="not now", reviewer="tester")
    assert rejected.status == models.REJECTED
    assert rejected.reject_reason == "not now"
    # cannot approve a rejected order
    with pytest.raises(ValueError):
        orders.approve_order(db, order.id)


def test_pips_sizing_used(db, monkeypatch):
    monkeypatch.setattr(orders, "calculate_order_qty", lambda s, pips: 0.0002 * pips)
    order, _ = orders.queue_order(db, symbol="BTC", side="BUY", pips=3, price=50000.0)
    assert order.quantity == pytest.approx(0.0006)
