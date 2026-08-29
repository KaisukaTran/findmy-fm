"""Live-readiness 1.5 — the live resting-maker model.

`orders.sync_resting_orders` places queued KSS limits on the exchange IN ADVANCE (instead
of waiting for the market to reach the limit and then sending a marketable order) and takes
them off the book again when they are rejected or time out. All offline: live_enabled,
place_live_order, cancel_live_order and live_provider are stubbed, so no network and no
real keys. The paper path is never exercised here.
"""

from datetime import datetime, timedelta

import pytest

from ccxt.base.errors import OrderNotFound

from app import execution, orders, runtime
from app.config import settings
from app.models import EXECUTED, PENDING, REJECTED, Fill, PendingOrder, Position


class _StubProvider:
    def pair(self, symbol):
        return f"{symbol}/USDT"


class _Venue:
    """Records placements/cancels and replays canned results."""

    def __init__(self, place_result=None, place_error=None, cancel_error=None,
                 fetch_result=None, fetch_error=None):
        self.placed: list[dict] = []
        self.cancelled: list[str] = []
        self._result = place_result or {
            "raw_id": "X1", "status": "open", "price": 0.0, "quantity": 0.0, "fee": 0.0,
        }
        self._place_error = place_error
        self._cancel_error = cancel_error
        # What a final status read reports. Default: an order that came off the book without
        # filling — nothing to book, which is what most of these tests assume.
        self._fetch_result = fetch_result or {
            "status": "canceled", "filled": 0.0, "average": 0.0, "fee": 0.0, "raw_id": "X1",
        }
        self._fetch_error = fetch_error

    def fetch(self, pair, order_id):
        if self._fetch_error:
            raise self._fetch_error
        return dict(self._fetch_result)

    def place(self, pair, side, quantity, price, order_type,
              maker_orders=None, client_order_id=None):
        self.placed.append({
            "pair": pair, "side": side, "quantity": quantity, "price": price,
            "order_type": order_type, "maker_orders": maker_orders,
            "client_order_id": client_order_id,
        })
        if self._place_error:
            raise self._place_error
        return dict(self._result)

    def cancel(self, pair, order_id):
        if self._cancel_error:
            raise self._cancel_error
        self.cancelled.append(order_id)


def _live(monkeypatch, venue: _Venue, *, maker=True, live=True, auto_trade=True) -> _Venue:
    monkeypatch.setattr(execution, "live_enabled", lambda: live)
    monkeypatch.setattr("app.data.providers.live_provider", lambda: _StubProvider())
    monkeypatch.setattr(execution, "place_live_order", venue.place)
    monkeypatch.setattr(execution, "cancel_live_order", venue.cancel)
    monkeypatch.setattr(execution, "fetch_live_order", venue.fetch)
    settings.maker_orders = maker
    settings.auto_trade = auto_trade
    return venue


def _queued(db, **kw) -> PendingOrder:
    defaults = {
        "symbol": "SOL", "side": "BUY", "order_type": "LIMIT", "quantity": 1.0,
        "price": 10.0, "source": "kss", "source_ref": "pyramid:1:wave:2", "status": PENDING,
    }
    defaults.update(kw)
    order = PendingOrder(**defaults)
    db.add(order)
    db.commit()
    db.refresh(order)
    return order


# --- the model is off unless live AND maker are both on ---------------------


def test_paper_never_places_anything(db, monkeypatch):
    venue = _live(monkeypatch, _Venue(), live=False)
    _queued(db)

    assert orders.sync_resting_orders(db) == {"placed": 0, "cancelled": 0}
    assert venue.placed == []


def test_maker_off_keeps_the_synchronous_model(db, monkeypatch):
    venue = _live(monkeypatch, _Venue(), maker=False)
    _queued(db)

    assert orders.sync_resting_orders(db) == {"placed": 0, "cancelled": 0}
    assert venue.placed == []


# --- placement --------------------------------------------------------------


def test_wave_rests_on_the_exchange(db, monkeypatch):
    venue = _live(monkeypatch, _Venue())
    order = _queued(db)

    assert orders.sync_resting_orders(db)["placed"] == 1

    sent = venue.placed[0]
    assert sent["order_type"] == "LIMIT" and sent["maker_orders"] is True
    assert sent["client_order_id"] == execution.client_order_id(order.id)
    assert (sent["side"], sent["price"]) == ("BUY", 10.0)
    db.refresh(order)
    assert order.exchange_order_id == "X1"
    assert order.exchange_status == "open"
    # Still queued locally: the venue owns the fill, reconcile books it (1.4).
    assert order.status == PENDING


def test_second_pass_does_not_place_again(db, monkeypatch):
    venue = _live(monkeypatch, _Venue())
    _queued(db)

    orders.sync_resting_orders(db)
    assert orders.sync_resting_orders(db)["placed"] == 0
    assert len(venue.placed) == 1


def test_auto_fill_skips_an_order_already_resting(db, monkeypatch):
    """The two live models must never both act on one rung (that would double the buy)."""
    _live(monkeypatch, _Venue())
    order = _queued(db)
    orders.sync_resting_orders(db)
    # Market has reached the limit — the synchronous path would approve it now.
    monkeypatch.setattr("app.orders.get_current_prices", lambda syms: {"SOL": 9.0})

    assert orders.auto_fill_due_orders(db) == []
    db.refresh(order)
    assert order.status == PENDING


def test_only_kss_limits_rest(db, monkeypatch):
    venue = _live(monkeypatch, _Venue())
    _queued(db, source="manual")               # human approval, never rests
    _queued(db, order_type="MARKET")           # risk exits take, never rest
    _queued(db, price=0.0)                     # no limit to rest at

    assert orders.sync_resting_orders(db)["placed"] == 0
    assert venue.placed == []


def test_auto_trade_off_holds_placement(db, monkeypatch):
    venue = _live(monkeypatch, _Venue(), auto_trade=False)
    _queued(db)

    assert orders.sync_resting_orders(db)["placed"] == 0
    assert venue.placed == []


# --- BUY re-gating (exits are never gated) ----------------------------------


def test_frozen_breaker_blocks_a_buy_but_never_an_exit(db, monkeypatch):
    venue = _live(monkeypatch, _Venue())
    runtime.freeze(db, "test")
    buy = _queued(db, side="BUY")
    sell = _queued(db, side="SELL", source_ref="pyramid:1:tp")

    assert orders.sync_resting_orders(db)["placed"] == 1
    assert [p["side"] for p in venue.placed] == ["SELL"]
    db.refresh(buy)
    db.refresh(sell)
    assert buy.exchange_order_id is None
    assert sell.exchange_order_id == "X1"


def test_vetoed_buy_is_not_placed(db, monkeypatch):
    venue = _live(monkeypatch, _Venue())
    _queued(db, auto_veto=True)

    assert orders.sync_resting_orders(db)["placed"] == 0
    assert venue.placed == []


def test_notional_cap_refuses_a_buy(db, monkeypatch):
    venue = _live(monkeypatch, _Venue())
    settings.live_max_order_notional = 25.0
    _queued(db, quantity=10.0, price=10.0)  # $100 > cap

    assert orders.sync_resting_orders(db)["placed"] == 0
    assert venue.placed == []


# --- venue answers ----------------------------------------------------------


def test_post_only_reject_leaves_the_order_queued(db, monkeypatch):
    """Rejected = the book already reached the rung; retry next cycle, never a phantom link."""
    venue = _live(monkeypatch, _Venue(place_result={
        "raw_id": None, "status": "rejected", "price": 0.0, "quantity": 0.0, "fee": 0.0,
    }))
    order = _queued(db)

    assert orders.sync_resting_orders(db)["placed"] == 0
    assert len(venue.placed) == 1
    db.refresh(order)
    assert order.exchange_order_id is None and order.status == PENDING


def test_placement_error_leaves_the_order_queued(db, monkeypatch):
    _live(monkeypatch, _Venue(place_error=RuntimeError("exchange down")))
    order = _queued(db)

    assert orders.sync_resting_orders(db)["placed"] == 0
    db.refresh(order)
    assert order.exchange_order_id is None and order.status == PENDING


def test_immediate_fill_is_left_for_reconcile_to_book(db, monkeypatch):
    """A venue that reports terminal at placement must not be stamped terminal: reconcile
    skips terminal rows, and that fill still has to be booked."""
    _live(monkeypatch, _Venue(place_result={
        "raw_id": "X9", "status": "closed", "price": 10.0, "quantity": 1.0, "fee": 0.01,
    }))
    order = _queued(db)

    assert orders.sync_resting_orders(db)["placed"] == 1
    db.refresh(order)
    assert order.exchange_order_id == "X9"
    assert order.exchange_status is None  # picked up by reconcile_live_orders' filter


# --- taking orders off the book --------------------------------------------


def test_rejected_order_is_cancelled_on_the_exchange(db, monkeypatch):
    venue = _live(monkeypatch, _Venue())
    order = _queued(db)
    orders.sync_resting_orders(db)

    order.status = REJECTED
    db.commit()
    assert orders.sync_resting_orders(db)["cancelled"] == 1
    assert venue.cancelled == ["X1"]
    db.refresh(order)
    assert order.exchange_order_id is None and order.exchange_status is None


def test_timeout_cancels_and_rejects_the_rung(db, monkeypatch):
    venue = _live(monkeypatch, _Venue())
    settings.order_fill_timeout_sec = 60
    order = _queued(db)
    orders.sync_resting_orders(db)
    order.created_at = datetime.utcnow() - timedelta(hours=2)
    db.commit()

    assert orders.sync_resting_orders(db)["cancelled"] == 1
    assert venue.cancelled == ["X1"]
    db.refresh(order)
    assert order.status == REJECTED and order.reviewer == "resting-timeout"


def test_timeout_zero_waits_forever(db, monkeypatch):
    """0 = the DCA default: a rung that never dips keeps waiting."""
    venue = _live(monkeypatch, _Venue())
    settings.order_fill_timeout_sec = 0
    order = _queued(db)
    orders.sync_resting_orders(db)
    order.created_at = datetime.utcnow() - timedelta(days=30)
    db.commit()

    assert orders.sync_resting_orders(db)["cancelled"] == 0
    assert venue.cancelled == []


def test_failed_cancel_keeps_the_link(db, monkeypatch):
    """Unlinking after a failed cancel would orphan a live order nothing tracks."""
    _live(monkeypatch, _Venue(cancel_error=RuntimeError("venue down")))
    order = _queued(db)
    orders.sync_resting_orders(db)
    order.status = REJECTED
    db.commit()

    assert orders.sync_resting_orders(db)["cancelled"] == 0
    db.refresh(order)
    assert order.exchange_order_id == "X1"


# --- a cancel must never lose a fill the venue already made -----------------
#
# reconcile_live_orders only looks at rows that still carry an exchange link, so dropping the
# link on an order the venue had already (partly) filled loses that fill for good: the app
# keeps a position it no longer holds, or misses one it paid for.


def _filled(qty, avg, status="canceled", fee=0.0):
    return {"status": status, "filled": qty, "average": avg, "fee": fee, "raw_id": "X1"}


def test_cancel_books_a_partial_fill_before_unlinking(db, monkeypatch):
    venue = _live(monkeypatch, _Venue(fetch_result=_filled(0.4, 10.0)))
    order = _queued(db)
    orders.sync_resting_orders(db)
    order.status = REJECTED
    db.commit()

    assert orders.sync_resting_orders(db)["cancelled"] == 1

    fills = db.query(Fill).filter(Fill.pending_order_id == order.id).all()
    assert [(f.quantity, f.price) for f in fills] == [(0.4, 10.0)]
    pos = db.query(Position).filter(Position.symbol == "SOL").one()
    assert pos.quantity == pytest.approx(0.4)
    db.refresh(order)
    assert order.exchange_order_id is None  # only unlinked AFTER the fill was booked


def test_cancel_of_an_order_the_venue_already_filled_still_books_it(db, monkeypatch):
    """-2011 means the venue no longer holds it — which is exactly when a fill is waiting."""
    venue = _live(monkeypatch, _Venue(
        cancel_error=OrderNotFound('binance {"code":-2011,"msg":"Unknown order sent."}'),
        fetch_result=_filled(1.0, 10.0, status="closed"),
    ))
    order = _queued(db)
    orders.sync_resting_orders(db)
    order.status = REJECTED
    db.commit()

    assert orders.sync_resting_orders(db)["cancelled"] == 1
    fills = db.query(Fill).filter(Fill.pending_order_id == order.id).all()
    assert [(f.quantity, f.price) for f in fills] == [(1.0, 10.0)]
    db.refresh(order)
    assert order.exchange_order_id is None
    assert venue.cancelled == []  # the venue refused the cancel; we booked anyway


def test_a_transient_cancel_error_books_nothing_and_keeps_the_link(db, monkeypatch):
    _live(monkeypatch, _Venue(cancel_error=RuntimeError("venue down"),
                              fetch_result=_filled(0.4, 10.0)))
    order = _queued(db)
    orders.sync_resting_orders(db)
    order.status = REJECTED
    db.commit()

    assert orders.sync_resting_orders(db)["cancelled"] == 0
    assert db.query(Fill).filter(Fill.pending_order_id == order.id).count() == 0
    db.refresh(order)
    assert order.exchange_order_id == "X1"  # still tracked, still on the book


def test_a_failed_final_read_keeps_the_link_so_the_fill_is_not_lost(db, monkeypatch):
    """The cancel landed but we cannot see what filled — keep the link and retry, rather
    than unlinking on a guess."""
    _live(monkeypatch, _Venue(fetch_error=RuntimeError("venue down")))
    order = _queued(db)
    orders.sync_resting_orders(db)
    order.status = REJECTED
    db.commit()

    assert orders.sync_resting_orders(db)["cancelled"] == 0
    db.refresh(order)
    assert order.exchange_order_id == "X1"


def test_a_cancelled_order_that_filled_is_not_stamped_rejected_by_the_timeout(db, monkeypatch):
    """A rung that turned out FILLED must not be recorded as a timed-out rejection."""
    _live(monkeypatch, _Venue(fetch_result=_filled(1.0, 10.0, status="closed")))
    settings.order_fill_timeout_sec = 60
    order = _queued(db)
    orders.sync_resting_orders(db)
    order.created_at = datetime.utcnow() - timedelta(hours=2)
    db.commit()

    orders.sync_resting_orders(db)

    db.refresh(order)
    assert order.status == EXECUTED and order.reviewer != "resting-timeout"


def test_executed_orders_are_left_alone(db, monkeypatch):
    """A partially filled synchronous order must keep resting until reconcile finishes it."""
    venue = _live(monkeypatch, _Venue())
    order = _queued(db, status=EXECUTED, exchange_order_id="SY1", exchange_status="open")

    assert orders.sync_resting_orders(db)["cancelled"] == 0
    assert venue.cancelled == []
    db.refresh(order)
    assert order.exchange_order_id == "SY1"


# --- no double placement through the synchronous path -----------------------


def test_live_execute_cancels_the_resting_order_first(db, monkeypatch):
    """The position-guard forces a crash exit through here, so a resting order must come OFF
    the book and then be placed — never two live orders for one row."""
    venue = _live(monkeypatch, _Venue(place_result={
        "raw_id": "X2", "status": "closed", "price": 10.0, "quantity": 1.0, "fee": 0.01,
    }))
    order = _queued(db, side="SELL", source_ref="pyramid:1:tp")
    orders.sync_resting_orders(db)
    db.refresh(order)
    resting_id = order.exchange_order_id

    fill = orders._live_execute(db, order)

    assert venue.cancelled == [resting_id]  # the resting order was pulled first
    assert len(venue.placed) == 2           # then re-placed for the immediate fill
    assert fill.quantity == 1.0


def test_live_execute_aborts_when_the_cancel_fails(db, monkeypatch):
    """A cancel we cannot confirm means placing again could double the exposure."""
    venue = _live(monkeypatch, _Venue())
    order = _queued(db, side="SELL", source_ref="pyramid:1:tp")
    orders.sync_resting_orders(db)
    venue._cancel_error = RuntimeError("venue down")
    db.refresh(order)

    with pytest.raises(RuntimeError, match="venue down"):
        orders._live_execute(db, order)
    assert len(venue.placed) == 1  # nothing new was placed
