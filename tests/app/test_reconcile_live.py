"""Live-readiness 1.4 — async order reconciliation.

`orders.reconcile_live_orders` books fills of resting live orders the exchange filled
since the last cycle. All offline: live_enabled / fetch_live_order / live_provider are
stubbed, so no network and no real keys. Paper path is never exercised here.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app import execution, orders
from app.clock import utcnow
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


# --- Fill.executed_at stamped with the VENUE's fill time, not the reconcile time -----------
#
# Live evidence: four DCA rungs filled during a 30h outage all recorded `executed_at` as the
# reconcile pass's own time, 2 to 24 hours after the venue actually filled them. `_book_delta`
# must prefer `res["filled_at_ms"]` (execution.fetch_live_order's normalised venue time).


def _dt_from_ms(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).replace(tzinfo=None)


def test_fill_executed_at_uses_the_venues_last_trade_time(db, monkeypatch):
    order = _resting(db, exchange_order_id="X7")
    ms = 1788313020000  # 2026-09-02 01:37:00 UTC
    _enable_live(monkeypatch, {"status": "closed", "filled": 5.0, "average": 100.0,
                               "fee": 0.5, "raw_id": "X7", "filled_at_ms": ms})
    orders.reconcile_live_orders(db)

    fill = db.query(Fill).filter(Fill.pending_order_id == order.id).one()
    assert fill.executed_at == _dt_from_ms(ms)


def test_fill_executed_at_falls_back_to_now_when_filled_at_ms_absent(db, monkeypatch):
    order = _resting(db, exchange_order_id="X8")
    before = utcnow()
    _enable_live(monkeypatch, {"status": "closed", "filled": 5.0, "average": 100.0,
                               "fee": 0.5, "raw_id": "X8"})  # no filled_at_ms key at all
    orders.reconcile_live_orders(db)
    after = utcnow()

    fill = db.query(Fill).filter(Fill.pending_order_id == order.id).one()
    assert before <= fill.executed_at <= after


def test_fill_executed_at_falls_back_to_now_for_garbage_ms(db, monkeypatch):
    order = _resting(db, exchange_order_id="X9")
    before = utcnow()
    _enable_live(monkeypatch, {"status": "closed", "filled": 5.0, "average": 100.0,
                               "fee": 0.5, "raw_id": "X9", "filled_at_ms": 0})
    orders.reconcile_live_orders(db)
    after = utcnow()

    fill = db.query(Fill).filter(Fill.pending_order_id == order.id).one()
    assert before <= fill.executed_at <= after


def test_fill_executed_at_falls_back_to_now_for_a_future_timestamp(db, monkeypatch):
    """Clock skew must never write a FUTURE fill — more than 24h ahead falls back to now."""
    order = _resting(db, exchange_order_id="X10")
    future_ms = int((utcnow() + timedelta(hours=48)).replace(tzinfo=timezone.utc).timestamp() * 1000)
    before = utcnow()
    _enable_live(monkeypatch, {"status": "closed", "filled": 5.0, "average": 100.0,
                               "fee": 0.5, "raw_id": "X10", "filled_at_ms": future_ms})
    orders.reconcile_live_orders(db)
    after = utcnow()

    fill = db.query(Fill).filter(Fill.pending_order_id == order.id).one()
    assert before <= fill.executed_at <= after


def test_paper_fill_keeps_the_default_executed_at(db, monkeypatch):
    """The synchronous paper path (`_paper_execute`) never touches `filled_at_ms` — it keeps
    today's behaviour (the column default, current time)."""
    from app import market

    monkeypatch.setattr(market, "get_current_prices", lambda syms: {"SOL": 100.0})
    order, _ = orders.queue_order(
        db, symbol="SOL", side="BUY", quantity=1.0, price=0.0, order_type="MARKET",
    )
    before = utcnow()
    fill = orders.approve_order(db, order.id)
    after = utcnow()
    assert before <= fill.executed_at <= after
