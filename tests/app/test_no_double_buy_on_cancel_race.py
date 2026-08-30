"""Cancelling a rung the venue already filled must not buy it a second time.

A regression I introduced. `_live_execute` used to let the venue's -2011 ("I no longer hold
that order") propagate, so the row went back to PENDING with its link intact and reconcile
booked the fill on the next pass — one order, correct books. Routing the cancel through
`_cancel_resting` fixed the lost-fill problem but broke this one: `_cancel_resting` treats
-2011 as the FILL case, books it, unlinks and returns True — and True was read as "the book is
clear, go ahead and place". The result was a full second order for a rung already bought.

The partial case is the same mistake at a smaller size: 4 of 10 filled, then the full 10
placed again — 14 units bought for a 10-unit rung.

Rule: after taking a resting order off the book, only the part the venue did NOT fill may be
placed.
"""

from __future__ import annotations

import pytest
from ccxt.base.errors import OrderNotFound

from app import execution, orders
from app.config import settings
from app.models import PENDING, Fill, PendingOrder, Position


class _StubProvider:
    def pair(self, symbol):
        return f"{symbol}/USDT"


class _Venue:
    def __init__(self, *, cancel_error=None, final=None):
        self.placed: list[dict] = []
        self._cancel_error = cancel_error
        self._final = final or {"status": "canceled", "filled": 0.0, "average": 0.0,
                                "fee": 0.0, "raw_id": "EX-A"}

    def cancel(self, pair, oid):
        if self._cancel_error:
            raise self._cancel_error

    def fetch(self, pair, oid):
        return dict(self._final)

    def place(self, pair, side, quantity, price, order_type, maker_orders=None,
              client_order_id=None):
        self.placed.append({"quantity": quantity, "price": price})
        return {"raw_id": "EX-NEW", "status": "closed", "price": price or 10.0,
                "quantity": quantity, "fee": 0.0}


def _live(monkeypatch, venue):
    monkeypatch.setattr(execution, "live_enabled", lambda: True)
    monkeypatch.setattr(settings, "live_max_order_notional", 10_000.0)
    monkeypatch.setattr(settings, "maker_orders", False)
    monkeypatch.setattr("app.data.providers.live_provider", lambda: _StubProvider())
    monkeypatch.setattr(execution, "cancel_live_order", venue.cancel)
    monkeypatch.setattr(execution, "fetch_live_order", venue.fetch)
    monkeypatch.setattr(execution, "place_live_order", venue.place)
    return venue


def _rung(db) -> PendingOrder:
    o = PendingOrder(symbol="SOL", side="BUY", order_type="LIMIT", quantity=10.0, price=10.0,
                     source="kss", source_ref="pyramid:1:wave:0", status=PENDING,
                     exchange_order_id="EX-A", exchange_status="open")
    db.add(o)
    db.commit()
    db.refresh(o)
    return o


def _held(db) -> float:
    pos = db.query(Position).filter(Position.symbol == "SOL").one_or_none()
    return pos.quantity if pos else 0.0


def test_a_rung_the_venue_already_filled_is_not_bought_again(db, monkeypatch):
    venue = _live(monkeypatch, _Venue(
        cancel_error=OrderNotFound('binance {"code":-2011,"msg":"Unknown order sent."}'),
        final={"status": "closed", "filled": 10.0, "average": 10.0, "fee": 0.0, "raw_id": "EX-A"},
    ))
    order = _rung(db)

    orders.approve_order(db, order.id, reviewer="dashboard")

    assert venue.placed == [], "the rung is already bought — nothing more may be sent"
    assert _held(db) == pytest.approx(10.0), "10 bought, not 20"


def test_only_the_unfilled_remainder_is_re_placed(db, monkeypatch):
    venue = _live(monkeypatch, _Venue(
        final={"status": "canceled", "filled": 4.0, "average": 10.0, "fee": 0.0, "raw_id": "EX-A"},
    ))
    order = _rung(db)

    orders.approve_order(db, order.id, reviewer="dashboard")

    assert [p["quantity"] for p in venue.placed] == pytest.approx([6.0]), "10 - 4 already filled"
    assert _held(db) == pytest.approx(10.0), "the rung totals 10, not 14"


def test_an_untouched_rung_is_re_placed_in_full(db, monkeypatch):
    venue = _live(monkeypatch, _Venue())
    order = _rung(db)

    orders.approve_order(db, order.id, reviewer="dashboard")

    assert [p["quantity"] for p in venue.placed] == pytest.approx([10.0])
    assert _held(db) == pytest.approx(10.0)


def test_the_synchronous_fill_is_tagged_with_its_exchange_order(db, monkeypatch):
    """Otherwise this path keeps minting NULL-tagged fills, and a row that reaches a THIRD
    exchange order re-acquires the netting bug the tagging was added to kill."""
    venue = _live(monkeypatch, _Venue())
    order = _rung(db)

    orders.approve_order(db, order.id, reviewer="dashboard")

    placed_fill = (
        db.query(Fill).filter(Fill.pending_order_id == order.id)
        .order_by(Fill.id.desc()).first()
    )
    assert placed_fill.exchange_order_id == "EX-NEW"


# --- the dead-link reaper must ask the venue before letting go ---------------


def test_a_terminal_link_with_an_unbooked_fill_is_not_released(db, monkeypatch):
    """`_live_execute` stamps a raw placement status and books nothing when the venue reports
    no usable price — leaving PENDING + link + terminal status with a REAL fill unrecorded.
    Releasing that link strips the "already resting" guard, and the rung is then bought again.
    """
    venue = _live(monkeypatch, _Venue(
        final={"status": "closed", "filled": 10.0, "average": 10.0, "fee": 0.0, "raw_id": "EX-A"},
    ))
    monkeypatch.setattr(settings, "maker_orders", True)
    monkeypatch.setattr(settings, "auto_trade", True)
    order = _rung(db)
    order.source_ref = "pyramid:1:wave:1"
    order.exchange_status = "closed"        # terminal, but nothing booked yet
    db.commit()

    orders.sync_resting_orders(db)

    booked = sum(f.quantity for f in
                 db.query(Fill).filter(Fill.pending_order_id == order.id).all())
    assert booked == pytest.approx(10.0), "the unbooked fill must be recorded, not discarded"
    assert venue.placed == [], "and the rung must not be bought a second time"


def test_a_genuinely_empty_terminal_link_is_released(db, monkeypatch):
    """The common case: cancelled with nothing filled. That link is dead and must be freed so
    the rung can be placed again."""
    venue = _live(monkeypatch, _Venue())
    monkeypatch.setattr(settings, "maker_orders", True)
    monkeypatch.setattr(settings, "auto_trade", True)
    order = _rung(db)
    order.source_ref = "pyramid:1:wave:1"
    order.exchange_status = "canceled"
    db.commit()

    orders.sync_resting_orders(db)

    db.refresh(order)
    assert order.exchange_order_id != "EX-A"
