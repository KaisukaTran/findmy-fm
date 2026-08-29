"""Wave 0 is an ENTRY: it takes. Waves 1..n are DCA rungs: they rest.

The first testnet soak opened five sessions and filled none of them. KSS anchors wave 0 at the
live market price, and a BUY at the market crosses the ask — so as a post-only LIMIT_MAKER the
venue rejects it (-2010). Three of four queued rungs sat exactly at best ask. Sessions went
ACTIVE holding nothing while occupying max_concurrent_sessions slots.

The ladder's own shape says which is which: wave 0 is meant to fill NOW (it is the entry the
session is built on), while waves 1..n sit BELOW the market waiting for a dip — those are what
the resting model exists for, and they still earn the maker side.
"""

from __future__ import annotations

import pytest

from app import execution, orders
from app.config import settings
from app.models import EXECUTED, PENDING, PendingOrder


class _StubProvider:
    def pair(self, symbol):
        return f"{symbol}/USDT"


class _Venue:
    def __init__(self):
        self.placed: list[dict] = []
        self.cancelled: list[str] = []

    def place(self, pair, side, quantity, price, order_type,
              maker_orders=None, client_order_id=None):
        self.placed.append({"pair": pair, "side": side, "price": price,
                            "order_type": order_type, "maker_orders": maker_orders})
        # A taker fill returns a price; a resting maker order would not.
        if maker_orders:
            return {"raw_id": "M1", "status": "open", "price": 0.0, "quantity": 0.0, "fee": 0.0}
        return {"raw_id": "T1", "status": "closed", "price": price or 1.0,
                "quantity": quantity, "fee": 0.01}

    def cancel(self, pair, order_id):
        self.cancelled.append(order_id)


def _live(monkeypatch, venue):
    monkeypatch.setattr(execution, "live_enabled", lambda: True)
    monkeypatch.setattr("app.data.providers.live_provider", lambda: _StubProvider())
    monkeypatch.setattr(execution, "place_live_order", venue.place)
    monkeypatch.setattr(execution, "cancel_live_order", venue.cancel)
    monkeypatch.setattr(settings, "maker_orders", True)
    monkeypatch.setattr(settings, "auto_trade", True)
    return venue


def _rung(db, wave: int, **kw) -> PendingOrder:
    defaults = {
        "symbol": "DOT", "side": "BUY", "order_type": "LIMIT", "quantity": 18.0,
        "price": 0.834, "source": "kss", "source_ref": f"pyramid:1:wave:{wave}",
        "status": PENDING,
    }
    defaults.update(kw)
    order = PendingOrder(**defaults)
    db.add(order)
    db.commit()
    db.refresh(order)
    return order


def test_wave_0_takes_instead_of_resting(db, monkeypatch):
    venue = _live(monkeypatch, _Venue())
    order = _rung(db, 0)

    fill = orders.approve_order(db, order.id, reviewer="auto-trader")

    assert fill.quantity == pytest.approx(18.0)
    assert venue.placed[0]["maker_orders"] is False, "an entry must not be post-only"
    db.refresh(order)
    assert order.status == EXECUTED


def test_a_dca_rung_still_refuses_the_synchronous_path(db, monkeypatch):
    venue = _live(monkeypatch, _Venue())
    order = _rung(db, 2)

    with pytest.raises(ValueError, match="rest"):
        orders.approve_order(db, order.id, reviewer="auto-trader")

    assert venue.placed == []
    db.refresh(order)
    assert order.status == PENDING


def test_sync_resting_orders_leaves_wave_0_alone(db, monkeypatch):
    """Wave 0 is the entry — the synchronous path owns it, so resting must not also place it
    (two live orders for one row)."""
    venue = _live(monkeypatch, _Venue())
    entry = _rung(db, 0)
    dca = _rung(db, 1, price=0.817)

    counts = orders.sync_resting_orders(db)

    assert counts["placed"] == 1
    assert [p["price"] for p in venue.placed] == [0.817]
    db.refresh(entry)
    assert entry.exchange_order_id is None
    db.refresh(dca)
    assert dca.exchange_order_id == "M1"


def test_the_defensive_wave_rests_like_any_other_rung(db, monkeypatch):
    """pyramid_up's defensive rung (wave -1) sits BELOW the market like a DCA rung."""
    venue = _live(monkeypatch, _Venue())
    _rung(db, -1, price=0.80)

    counts = orders.sync_resting_orders(db)

    assert counts["placed"] == 1
    assert venue.placed[0]["maker_orders"] is True
