"""Tests for the order lifecycle: queue -> approve/reject -> fill -> position."""

import pytest

from app import models, orders, portfolio
from app.config import settings


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


# --- hard cash floor: a BUY may never drive account cash below the floor -------


def test_buy_partial_fills_to_available_cash(db, monkeypatch):
    """A BUY larger than free cash is partial-filled down to what cash allows; the resulting
    Cash is ≥ 0 and never negative."""
    monkeypatch.setattr(settings, "account_equity", 1000.0)
    monkeypatch.setattr(settings, "cash_floor_usd", 0.0)
    o, _ = orders.queue_order(db, symbol="BTC", side="BUY", quantity=1.0, price=50000.0)  # $50k
    fill = orders.approve_order(db, o.id)
    assert fill.quantity < 1.0  # capped to a partial
    cash = portfolio.summary_view(db)["cash"]
    assert cash >= -1e-6                 # NEVER negative — the whole point
    assert cash < 5.0                    # spent down close to the floor
    assert db.query(models.AuditLog).filter_by(action="partial_fill_cash").count() == 1


def test_buy_rejected_when_below_min_notional(db, monkeypatch):
    """When free cash can't fund even a min-notional slice, the BUY is rejected (raised) and
    left PENDING (so it retries when cash frees) — no fill, no negative cash."""
    monkeypatch.setattr(settings, "account_equity", 1.0)
    monkeypatch.setattr(settings, "scan_min_notional", 10.0)
    o, _ = orders.queue_order(db, symbol="BTC", side="BUY", quantity=1.0, price=50000.0)
    with pytest.raises(orders.InsufficientCashError):
        orders.approve_order(db, o.id)
    db.refresh(o)
    assert o.status == models.PENDING          # untouched → retried later
    assert db.query(models.Fill).count() == 0


def test_sell_never_gated_by_cash_floor(db, monkeypatch):
    """SELL exits are NEVER blocked by the cash floor, even when cash is exhausted."""
    monkeypatch.setattr(settings, "account_equity", 100_000.0)
    b, _ = orders.queue_order(db, symbol="ETH", side="BUY", quantity=1.0, price=1000.0)
    orders.approve_order(db, b.id)
    monkeypatch.setattr(settings, "account_equity", 0.0)  # cash now exhausted vs invested
    s, _ = orders.queue_order(db, symbol="ETH", side="SELL", quantity=1.0, price=1100.0)
    fill = orders.approve_order(db, s.id)
    assert fill.quantity == pytest.approx(1.0)  # full exit, not gated


def test_cash_floor_buffer_is_respected(db, monkeypatch):
    """cash_floor_usd > 0 keeps a buffer: a BUY partial-fills so Cash stays ≥ the floor."""
    monkeypatch.setattr(settings, "account_equity", 1000.0)
    monkeypatch.setattr(settings, "cash_floor_usd", 200.0)
    o, _ = orders.queue_order(db, symbol="BTC", side="BUY", quantity=1.0, price=50000.0)
    orders.approve_order(db, o.id)
    assert portfolio.summary_view(db)["cash"] >= 200.0 - 1e-6
