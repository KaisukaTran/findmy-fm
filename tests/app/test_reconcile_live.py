"""Live-readiness 1.4 — async order reconciliation.

`orders.reconcile_live_orders` books fills of resting live orders the exchange filled
since the last cycle. All offline: live_enabled / fetch_live_order / live_provider are
stubbed, so no network and no real keys. Paper path is never exercised here.
"""

import pytest

from app import execution, orders
from app.models import APPROVED, EXECUTED, Fill, PendingOrder, Position


class _StubProvider:
    def pair(self, symbol):
        return f"{symbol}/USDT"


def _enable_live(monkeypatch, fetch):
    """Turn live on and route fetch_live_order to *fetch* (a dict or a callable)."""
    monkeypatch.setattr(execution, "live_enabled", lambda: True)
    monkeypatch.setattr("app.data.providers.live_provider", lambda: _StubProvider())
    fn = fetch if callable(fetch) else (lambda pair, oid: fetch)
    monkeypatch.setattr(execution, "fetch_live_order", fn)


def _resting(db, **kw) -> PendingOrder:
    defaults = {
        "symbol": "SOL", "side": "BUY", "order_type": "LIMIT", "quantity": 5.0,
        "price": 100.0, "source": "manual", "status": APPROVED,
        "exchange_order_id": "X1", "exchange_status": "open",
    }
    defaults.update(kw)
    order = PendingOrder(**defaults)
    db.add(order)
    db.commit()
    db.refresh(order)
    return order


def test_new_to_filled_books_exactly_one_fill(db, monkeypatch):
    order = _resting(db, exchange_order_id="X1")
    _enable_live(monkeypatch, {"status": "closed", "filled": 5.0, "average": 100.0,
                               "fee": 0.5, "raw_id": "X1"})
    booked = orders.reconcile_live_orders(db)

    assert booked == [order.id]
    fills = db.query(Fill).filter(Fill.pending_order_id == order.id).all()
    assert len(fills) == 1
    assert (fills[0].quantity, fills[0].price, fills[0].fee) == (5.0, 100.0, 0.5)
    pos = db.query(Position).filter(Position.symbol == "SOL").one()
    assert pos.quantity == 5.0
    db.refresh(order)
    assert order.exchange_status == "closed"
    assert order.status == EXECUTED


def test_reconcile_is_idempotent(db, monkeypatch):
    order = _resting(db, exchange_order_id="X1")
    _enable_live(monkeypatch, {"status": "closed", "filled": 5.0, "average": 100.0,
                               "fee": 0.5, "raw_id": "X1"})
    orders.reconcile_live_orders(db)
    # Second pass: the order is terminal AND fully booked → no new fill either way.
    assert orders.reconcile_live_orders(db) == []
    assert db.query(Fill).filter(Fill.pending_order_id == order.id).count() == 1


def test_partial_then_full_accumulates(db, monkeypatch):
    order = _resting(db, exchange_order_id="X2")
    results = iter([
        {"status": "open", "filled": 2.0, "average": 100.0, "fee": 0.2, "raw_id": "X2"},
        {"status": "closed", "filled": 5.0, "average": 100.0, "fee": 0.5, "raw_id": "X2"},
    ])
    _enable_live(monkeypatch, lambda pair, oid: next(results))

    orders.reconcile_live_orders(db)   # books the first 2.0
    orders.reconcile_live_orders(db)   # books the remaining 3.0

    fills = db.query(Fill).filter(Fill.pending_order_id == order.id).order_by(Fill.id).all()
    assert [f.quantity for f in fills] == [2.0, 3.0]
    assert fills[1].fee == pytest.approx(0.3)  # incremental fee only (0.5 - 0.2)
    pos = db.query(Position).filter(Position.symbol == "SOL").one()
    assert pos.quantity == 5.0


def test_paper_mode_is_a_noop(db, monkeypatch):
    _resting(db, exchange_order_id="X3")
    # live_enabled stays False (default); fetch would return a fill but must never be called.
    monkeypatch.setattr(execution, "fetch_live_order",
                        lambda pair, oid: {"status": "closed", "filled": 5.0,
                                           "average": 100.0, "fee": 0.0, "raw_id": "X3"})
    assert orders.reconcile_live_orders(db) == []
    assert db.query(Fill).count() == 0


def test_sell_realizes_pnl_and_closes_position(db, monkeypatch):
    db.add(Position(symbol="SOL", quantity=5.0, avg_entry_price=90.0, total_cost=450.0))
    order = _resting(db, side="SELL", price=110.0, exchange_order_id="X4")
    _enable_live(monkeypatch, {"status": "closed", "filled": 5.0, "average": 110.0,
                               "fee": 0.0, "raw_id": "X4"})
    orders.reconcile_live_orders(db)

    fill = db.query(Fill).filter(Fill.pending_order_id == order.id).one()
    assert fill.realized_pnl == pytest.approx(5 * (110.0 - 90.0))  # 100
    pos = db.query(Position).filter(Position.symbol == "SOL").one()
    assert pos.quantity == 0.0


def test_kss_fill_hook_fires_on_booked_delta(db, monkeypatch):
    import app.kss.service as kss_service

    _resting(db, source="kss", source_ref="42", exchange_order_id="X5")
    calls: list[tuple] = []
    monkeypatch.setattr(kss_service, "handle_fill_event",
                        lambda db, ref, qty, price: calls.append((ref, qty, price)))
    _enable_live(monkeypatch, {"status": "closed", "filled": 5.0, "average": 100.0,
                               "fee": 0.0, "raw_id": "X5"})
    orders.reconcile_live_orders(db)
    assert calls == [("42", 5.0, 100.0)]


def test_unfilled_resting_order_books_nothing(db, monkeypatch):
    order = _resting(db, exchange_order_id="X6")
    _enable_live(monkeypatch, {"status": "open", "filled": 0.0, "average": 0.0,
                               "fee": 0.0, "raw_id": "X6"})
    assert orders.reconcile_live_orders(db) == []
    assert db.query(Fill).count() == 0
    db.refresh(order)
    assert order.status == APPROVED  # still resting, not executed
