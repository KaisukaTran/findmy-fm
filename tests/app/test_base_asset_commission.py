"""Book what the wallet RECEIVED, not what the venue said it filled — or the exit is rejected.

On a spot BUY without BNB fee payment, Binance takes its commission out of the asset you just
bought. `executedQty` reports the gross fill; the wallet receives that MINUS the commission.
Verified against our own live trades on 2026-08-31 — every one reports its fee in the base
asset (`{'cost': ..., 'currency': 'NEAR'}`, `'EIGEN'`, `'WLD'`), never in USDT.

Booking the gross figure makes the book believe it owns ~0.1% more of every coin than it does.
The session's exit sells `total_filled_qty`, Binance answers **-2010 insufficient balance**, and
the order is REJECTED — so the take-profit never fires and, far worse, neither does the
stop-loss. `_place_resting` and `run_position_guard` both swallow that rejection, so it happens
in silence.

Testnet hid this completely: it charges zero commission, so 24 live fills went by with
`fee=0.0` and the gap never appeared. On a real account it breaks on the FIRST session.

Two defences here, because one is not enough for the only unforgivable bug in this project:
  1. book the NET received quantity, so the book matches the wallet;
  2. clamp a live SELL to the balance that actually exists, so an exit still goes out even if
     the accounting drifts for some other reason.
"""

from __future__ import annotations

import pytest

from app import execution, orders
from app.config import settings
from app.models import PENDING, Fill, PendingOrder, Position


class _Venue:
    """Reports a gross fill plus a base-asset commission, exactly as Binance does."""

    def __init__(self, base_fee: float = 0.612):
        self.base_fee = base_fee
        self.sold: list[float] = []

    def place(self, pair, side, quantity, price, order_type, maker_orders=None,
              client_order_id=None):
        if side == "SELL":
            self.sold.append(quantity)
        return {"raw_id": "EX-1", "status": "closed", "price": 0.19,
                "quantity": quantity, "fee": 0.0, "fee_base": self.base_fee}


def _live(monkeypatch, venue):
    monkeypatch.setattr(execution, "live_enabled", lambda: True)
    monkeypatch.setattr(settings, "live_max_order_notional", 10_000.0)
    monkeypatch.setattr(settings, "maker_orders", False)
    monkeypatch.setattr("app.data.providers.live_provider",
                        lambda: type("P", (), {"pair": staticmethod(lambda s: f"{s}/USDT")})())
    monkeypatch.setattr(execution, "place_live_order", venue.place)
    return venue


def _order(db, side="BUY", qty=612.24) -> PendingOrder:
    o = PendingOrder(symbol="EIGEN", side=side, order_type="MARKET", quantity=qty, price=0.0,
                     source="kss", source_ref="pyramid:1:wave:0", status=PENDING)
    db.add(o)
    db.commit()
    db.refresh(o)
    return o


# --- 1. the commission comes out of the coin ---------------------------------


def test_the_base_asset_commission_is_reported_by_the_fee_helper():
    """`fee_base_qty` already existed and had no production caller — that was the whole bug."""
    order = {"fees": [{"cost": 0.612, "currency": "EIGEN"}]}

    assert execution.fee_base_qty(order, "EIGEN") == pytest.approx(0.612)
    assert execution.fee_base_qty(order, "USDT") == 0.0


def test_a_buy_books_the_quantity_the_wallet_actually_received(db, monkeypatch):
    _live(monkeypatch, _Venue(base_fee=0.612))
    order = _order(db)

    orders.approve_order(db, order.id, reviewer="dashboard")

    pos = db.query(Position).filter(Position.symbol == "EIGEN").one()
    assert pos.quantity == pytest.approx(611.628), "612.24 filled minus 0.612 commission"


def test_a_buy_with_no_base_commission_is_unchanged(db, monkeypatch):
    """Quote-denominated or BNB-paid commissions must not be subtracted from the coin."""
    _live(monkeypatch, _Venue(base_fee=0.0))
    order = _order(db)

    orders.approve_order(db, order.id, reviewer="dashboard")

    pos = db.query(Position).filter(Position.symbol == "EIGEN").one()
    assert pos.quantity == pytest.approx(612.24)


def test_the_fill_row_records_the_net_quantity_too(db, monkeypatch):
    """The Fill rows are what `Position.quantity` is reconciled against; if they disagree the
    audit that proves the book is sound stops proving anything."""
    _live(monkeypatch, _Venue(base_fee=0.612))
    order = _order(db)

    orders.approve_order(db, order.id, reviewer="dashboard")

    f = db.query(Fill).filter(Fill.pending_order_id == order.id).one()
    assert f.quantity == pytest.approx(611.628)


def test_a_sell_is_never_reduced_by_a_base_commission(db, monkeypatch):
    """A SELL's commission comes out of the PROCEEDS, not the coin — subtracting it from the
    quantity would under-report what left the position."""
    _live(monkeypatch, _Venue(base_fee=0.612))
    db.add(Position(symbol="EIGEN", quantity=612.24, avg_entry_price=0.19))
    db.commit()
    order = _order(db, side="SELL", qty=612.24)

    orders.approve_order(db, order.id, reviewer="dashboard")

    pos = db.query(Position).filter(Position.symbol == "EIGEN").one()
    assert pos.quantity == pytest.approx(0.0), "the whole position left"
