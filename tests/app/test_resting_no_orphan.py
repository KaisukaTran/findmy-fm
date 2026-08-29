"""A live order must never exist on the exchange without the row that tracks it.

Found by the first soak on testnet: with MAKER_ORDERS on, the scanner still opened wave 0
through the SYNCHRONOUS path (`approve_order` -> `_live_execute`). That path places the order
and then raises "live order returned no fill price", because a post-only maker order does not
fill on placement — and it raised BEFORE stamping `exchange_order_id`. The result was a real
DOT order resting on the venue that the database knew nothing about (so nothing would ever
reconcile it, cancel it, or count its fill), plus a scheduler cycle killed by the exception.
"""

from __future__ import annotations

import pytest

from app import execution, orders
from app.config import settings
from app.models import PENDING, PendingOrder


class _StubProvider:
    def pair(self, symbol):
        return f"{symbol}/USDT"


class _Venue:
    def __init__(self, result=None):
        self.placed: list[dict] = []
        self.cancelled: list[str] = []
        # A post-only maker order that rests: accepted, an id, but no fill and no price.
        self._result = result if result is not None else {
            "raw_id": "R1", "status": "open", "price": 0.0, "quantity": 0.0, "fee": 0.0,
        }

    def place(self, pair, side, quantity, price, order_type,
              maker_orders=None, client_order_id=None):
        self.placed.append({"pair": pair, "side": side, "price": price,
                            "maker_orders": maker_orders})
        return dict(self._result)

    def cancel(self, pair, order_id):
        self.cancelled.append(order_id)


def _live(monkeypatch, venue, *, maker=True):
    monkeypatch.setattr(execution, "live_enabled", lambda: True)
    monkeypatch.setattr("app.data.providers.live_provider", lambda: _StubProvider())
    monkeypatch.setattr(execution, "place_live_order", venue.place)
    monkeypatch.setattr(execution, "cancel_live_order", venue.cancel)
    monkeypatch.setattr(settings, "maker_orders", maker)
    monkeypatch.setattr(settings, "auto_trade", True)
    return venue


def _kss_rung(db, **kw) -> PendingOrder:
    defaults = {
        "symbol": "DOT", "side": "BUY", "order_type": "LIMIT", "quantity": 18.0,
        "price": 0.834, "source": "kss", "source_ref": "pyramid:1:wave:0", "status": PENDING,
    }
    defaults.update(kw)
    order = PendingOrder(**defaults)
    db.add(order)
    db.commit()
    db.refresh(order)
    return order


def test_the_synchronous_path_refuses_a_rung_that_belongs_to_the_resting_model(db, monkeypatch):
    """It must refuse BEFORE contacting the venue — an order placed and then abandoned is
    exactly the orphan this is about. (A DCA rung: wave 0 is the entry and takes.)"""
    venue = _live(monkeypatch, _Venue())
    order = _kss_rung(db, source_ref="pyramid:1:wave:2")

    with pytest.raises(ValueError, match="rest"):
        orders.approve_order(db, order.id, reviewer="auto-trader")

    assert venue.placed == [], "nothing may reach the exchange"
    db.refresh(order)
    assert order.status == PENDING, "the rung stays queued for sync_resting_orders"
    assert order.exchange_order_id is None


def test_maker_off_keeps_the_synchronous_path_working(db, monkeypatch):
    """With the resting model off, the legacy model must be untouched."""
    venue = _live(monkeypatch, _Venue(
        {"raw_id": "M1", "status": "closed", "price": 0.834, "quantity": 18.0, "fee": 0.01}),
        maker=False)
    order = _kss_rung(db)

    fill = orders.approve_order(db, order.id, reviewer="auto-trader")

    assert fill.quantity == pytest.approx(18.0)
    assert len(venue.placed) == 1


def test_a_placed_order_is_tracked_even_when_the_venue_reports_no_fill_price(db, monkeypatch):
    """Defence in depth: any path that DOES place must record the exchange id before it
    raises, or the order lives on the venue untracked."""
    venue = _live(monkeypatch, _Venue(), maker=False)  # legacy path, but a resting result
    order = _kss_rung(db)

    with pytest.raises(ValueError, match="no fill price"):
        orders.approve_order(db, order.id, reviewer="auto-trader")

    assert len(venue.placed) == 1
    fresh = db.get(PendingOrder, order.id)
    assert fresh.exchange_order_id == "R1", "placed but untracked = an orphan on the exchange"


def test_an_exit_is_never_refused_by_the_resting_guard(db, monkeypatch):
    """Exits are never gated. A SELL that reduces risk must still go through immediately."""
    venue = _live(monkeypatch, _Venue(
        {"raw_id": "S1", "status": "closed", "price": 0.9, "quantity": 18.0, "fee": 0.01}))
    order = _kss_rung(db, side="SELL", source_ref="pyramid:1:sl", order_type="MARKET", price=0.0)

    fill = orders.approve_order(db, order.id, reviewer="auto-trader")

    assert fill.side == "SELL"
    assert len(venue.placed) == 1
