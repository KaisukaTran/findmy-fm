"""Booking must be keyed to the exchange order that produced the fill.

`_booked_qty_fee` summed every Fill on a pending_order (lifetime), while `_book_delta` compared
that total against the cumulative `filled` of ONE exchange order. For a row that only ever has
one exchange order those are the same number — but a row can acquire a second one (a resting
rung cancelled and re-placed, or an operator forcing a resting order to fill now). After that
the delta is poisoned: the previous order's fills are subtracted from the new order's total, so
real fills are silently skipped.

The other half: `_live_execute` cancelled a resting order and dropped its exchange link WITHOUT
booking what it had already filled — the same shape as the `_cancel_resting` bug already fixed,
never applied here. A probe had the venue fill 4.0, then approve cancelled and re-placed the
full 10.0: 4.0 units bought and never recorded.
"""

from __future__ import annotations

import pytest

from app import execution, orders
from app.config import settings
from app.models import PENDING, Fill, PendingOrder


class _StubProvider:
    def pair(self, symbol):
        return f"{symbol}/USDT"


def _live(monkeypatch, *, cancel=None, fetch=None, place=None, maker=False):
    monkeypatch.setattr(execution, "live_enabled", lambda: True)
    monkeypatch.setattr(settings, "live_max_order_notional", 10_000.0)
    monkeypatch.setattr("app.data.providers.live_provider", lambda: _StubProvider())
    monkeypatch.setattr(settings, "maker_orders", maker)
    monkeypatch.setattr(execution, "cancel_live_order", cancel or (lambda pair, oid: None))
    monkeypatch.setattr(execution, "fetch_live_order", fetch or (lambda pair, oid: {
        "status": "canceled", "filled": 0.0, "average": 0.0, "fee": 0.0, "raw_id": oid}))
    monkeypatch.setattr(execution, "place_live_order", place or (lambda *a, **k: {
        "raw_id": "NEW", "status": "closed", "price": 10.0, "quantity": 1.0, "fee": 0.0}))


def _order(db, **kw) -> PendingOrder:
    defaults = {
        "symbol": "SOL", "side": "BUY", "order_type": "LIMIT", "quantity": 10.0,
        "price": 10.0, "source": "kss", "source_ref": "pyramid:1:wave:0", "status": PENDING,
    }
    defaults.update(kw)
    o = PendingOrder(**defaults)
    db.add(o)
    db.commit()
    db.refresh(o)
    return o


def test_the_synchronous_path_books_what_the_resting_order_already_filled(db, monkeypatch):
    """Approve-now on a rung the venue has partly filled must not lose that fill."""
    _live(monkeypatch, fetch=lambda pair, oid: {
        "status": "canceled", "filled": 4.0, "average": 10.0, "fee": 0.0, "raw_id": oid})
    order = _order(db, exchange_order_id="EX-A", exchange_status="open")

    orders.approve_order(db, order.id, reviewer="dashboard")

    booked = sum(f.quantity for f in
                 db.query(Fill).filter(Fill.pending_order_id == order.id).all())
    assert booked >= 4.0, "the 4.0 the venue already filled must be recorded"


def test_a_fill_is_tagged_with_the_exchange_order_that_produced_it(db, monkeypatch):
    _live(monkeypatch)
    order = _order(db, exchange_order_id="EX-A", exchange_status="open")

    orders._book_delta(db, order, {
        "status": "closed", "filled": 3.0, "average": 10.0, "fee": 0.01, "raw_id": "EX-A"})
    db.commit()

    fill = db.query(Fill).filter(Fill.pending_order_id == order.id).one()
    assert fill.exchange_order_id == "EX-A"


def test_a_second_exchange_order_starts_its_own_count(db, monkeypatch):
    """The poisoned-delta case: after a re-place, the new order's fills must be booked in
    full rather than netted against the old order's."""
    _live(monkeypatch)
    order = _order(db, exchange_order_id="EX-A", exchange_status="open")

    orders._book_delta(db, order, {
        "status": "closed", "filled": 4.0, "average": 10.0, "fee": 0.0, "raw_id": "EX-A"})
    db.commit()

    order.exchange_order_id = "EX-B"          # cancelled and re-placed
    order.exchange_status = "open"
    db.commit()
    orders._book_delta(db, order, {
        "status": "closed", "filled": 6.0, "average": 10.0, "fee": 0.0, "raw_id": "EX-B"})
    db.commit()

    total = sum(f.quantity for f in
                db.query(Fill).filter(Fill.pending_order_id == order.id).all())
    assert total == pytest.approx(10.0), "4.0 on EX-A plus 6.0 on EX-B, not 6.0 total"


def test_re_booking_the_same_exchange_order_is_still_idempotent(db, monkeypatch):
    _live(monkeypatch)
    order = _order(db, exchange_order_id="EX-A", exchange_status="open")
    res = {"status": "closed", "filled": 4.0, "average": 10.0, "fee": 0.0, "raw_id": "EX-A"}

    orders._book_delta(db, order, res)
    orders._book_delta(db, order, res)
    db.commit()

    assert db.query(Fill).filter(Fill.pending_order_id == order.id).count() == 1


# --- a dead exchange link must not strand the rung ---------------------------


def test_an_externally_cancelled_rung_is_freed_for_replacement(db, monkeypatch):
    """An operator cancelling in the Binance UI (or an exchange-side expiry) leaves the row
    PENDING with a terminal exchange_status. Every query then excluded it: never reconciled,
    never re-placed, never cancelled — the session's ladder silently died."""
    _live(monkeypatch, maker=True)   # the resting model is what owns these links
    monkeypatch.setattr(settings, "auto_trade", True)
    order = _order(db, source_ref="pyramid:1:wave:1", exchange_order_id="EX-DEAD",
                   exchange_status="canceled")

    orders.sync_resting_orders(db)

    db.refresh(order)
    assert order.exchange_order_id != "EX-DEAD", "a dead link must be released"
