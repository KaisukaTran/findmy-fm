"""Live-native resting stop (task 1.10 / live-readiness plan).

`app.kss.service._maintain_live_stop` keeps a STOP_LOSS_LIMIT SELL resting on the exchange at
the session's current `trail_sl_price`, so the venue executes the stop in microseconds instead
of the ~90s guard detecting the breach after the fact and sending a MARKET sell (measured
2026-09-14: those filled 0.09-0.34% BELOW the intended stop). LIVE ONLY, OFF by default.

Everything here is offline: `app.execution` is stubbed entirely (patterns from
tests/app/test_resting_live.py and tests/app/test_market_exit_frees_resting_tp.py) and
`app.kss.pyramid.get_exchange_info` is pinned so lot-size math never depends on a real network
call. No live keys, no network.
"""

from __future__ import annotations

import pytest

from app import execution, orders
from app.config import settings
from app.kss import service
from app.models import (
    EXECUTED,
    PENDING,
    REJECTED,
    AuditLog,
    Fill,
    KssSession,
    PendingOrder,
)


class _StubProvider:
    def pair(self, symbol):
        return f"{symbol}/USDT"


class _Venue:
    """Records every venue call in order; replays canned results."""

    def __init__(self, *, place_result=None, place_error=None, cancel_error=None,
                 fetch_result=None, market_place_result=None, free=None, locked=0.0):
        self.events: list[tuple] = []
        self.placed_stops: list[dict] = []
        self.placed_market: list[dict] = []
        self.cancelled: list[str] = []
        self._place_result = place_result or {
            "raw_id": "S1", "status": "open", "price": 0.0, "quantity": 0.0, "fee": 0.0,
        }
        self._place_error = place_error
        self._cancel_error = cancel_error
        self._fetch_result = fetch_result or {
            "status": "canceled", "filled": 0.0, "average": 0.0, "fee": 0.0, "raw_id": "S1",
        }
        self._market_place_result = market_place_result or {
            "raw_id": "M1", "status": "closed", "price": 10.0, "quantity": 0.0, "fee": 0.0,
        }
        self.free = free
        self.locked = locked

    # -- resting stop (execution.place_live_stop_order) --------------------------------
    def place_stop(self, pair, quantity, stop_price, limit_price, client_order_id=None):
        entry = {"pair": pair, "quantity": quantity, "stop_price": stop_price,
                 "limit_price": limit_price}
        self.placed_stops.append(entry)
        self.events.append(("place_stop", entry))
        if self._place_error:
            raise self._place_error
        res = dict(self._place_result)
        res["quantity"] = res.get("quantity") or quantity
        return res

    # -- generic MARKET/LIMIT placement (execution.place_live_order) -------------------
    def place_market(self, pair, side, quantity, price, order_type, maker_orders=None,
                      client_order_id=None):
        entry = {"pair": pair, "side": side, "quantity": quantity, "order_type": order_type}
        self.placed_market.append(entry)
        self.events.append(("place_market", entry))
        res = dict(self._market_place_result)
        res["quantity"] = quantity
        return res

    def cancel(self, pair, order_id):
        self.cancelled.append(order_id)
        self.events.append(("cancel", order_id))
        if self._cancel_error:
            raise self._cancel_error

    def fetch(self, pair, order_id):
        res = dict(self._fetch_result)
        res["raw_id"] = order_id
        return res

    def balance(self, asset):
        return None if self.free is None else (self.free, self.locked)


def _live(monkeypatch, venue: _Venue, *, live_trading=True, knob=True,
          rate_hold=False, budget_ok=True) -> _Venue:
    monkeypatch.setattr(execution, "live_enabled", lambda: True)
    monkeypatch.setattr("app.data.providers.live_provider", lambda: _StubProvider())
    monkeypatch.setattr(execution, "place_live_stop_order", venue.place_stop)
    monkeypatch.setattr(execution, "place_live_order", venue.place_market)
    monkeypatch.setattr(execution, "cancel_live_order", venue.cancel)
    monkeypatch.setattr(execution, "fetch_live_order", venue.fetch)
    monkeypatch.setattr(execution, "fetch_asset_balance", venue.balance)
    monkeypatch.setattr(execution, "rate_hold_active", lambda: rate_hold)
    if budget_ok:
        monkeypatch.setattr(execution, "assert_order_budget_available", lambda urgent=False: None)
    else:
        def _raise(urgent=False):
            raise execution.RateLimited("order-count budget near cap")
        monkeypatch.setattr(execution, "assert_order_budget_available", _raise)
    monkeypatch.setattr("app.orders.get_current_prices", lambda syms: dict.fromkeys(syms, 10.0))
    settings.kss_live_stop_orders = knob
    settings.live_trading = live_trading
    settings.kss_stop_ratchet_step_pct = 0.5
    settings.kss_stop_limit_slip_pct = 0.3
    settings.kss_stop_max_replaces = 40
    settings.maker_orders = True
    settings.auto_trade = True
    return venue


@pytest.fixture(autouse=True)
def _pin_exchange_info(monkeypatch):
    """No network for lot-size math: minQty/stepSize/minNotional pinned and coarse enough that
    the qtys these tests use never round away."""
    monkeypatch.setattr(
        "app.kss.pyramid.get_exchange_info",
        lambda symbol: {"minQty": 0.001, "stepSize": 0.001, "minNotional": 5.0},
    )


def _session(db, *, symbol="SOL", avg=100.0, qty=3.0, status=service.SESSION_ACTIVE,
             trail_sl_price=95.0, stop_replaces=0) -> KssSession:
    row = KssSession(
        symbol=symbol, entry_price=avg, distance_pct=2.0, max_waves=5,
        isolated_fund=1000.0, tp_pct=5.0, timeout_x_min=60, gap_y_min=5,
        status=status, current_wave=1, avg_price=avg, total_filled_qty=qty,
        total_cost=avg * qty, trail_sl_price=trail_sl_price, stop_replaces=stop_replaces,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _stop_row(db, session_id: int, *, price=95.0, qty=3.0, exchange_order_id="S1") -> PendingOrder:
    row = PendingOrder(
        symbol="SOL", side="SELL", order_type="STOP", quantity=qty, price=price,
        source="kss", source_ref=f"pyramid:{session_id}:stop", status=PENDING,
        exchange_order_id=exchange_order_id, exchange_status="open",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _audits(db, action):
    return db.query(AuditLog).filter(AuditLog.action == action).all()


def _live_stop_rows(db, session_id):
    return (
        db.query(PendingOrder)
        .filter(PendingOrder.source_ref == f"pyramid:{session_id}:stop",
                PendingOrder.order_type == "STOP")
        .all()
    )


# --- the gate: off by default, paper, disabled-live -------------------------------------


def test_off_by_default_makes_no_venue_call(db, monkeypatch):
    venue = _live(monkeypatch, _Venue(), knob=False)
    row = _session(db)

    service._maintain_live_stop(db, row, 90.0)

    assert venue.placed_stops == []
    assert venue.cancelled == []
    assert _live_stop_rows(db, row.id) == []


def test_paper_makes_no_venue_call(db, monkeypatch):
    venue = _live(monkeypatch, _Venue(), live_trading=False)
    row = _session(db)

    service._maintain_live_stop(db, row, 90.0)

    assert venue.placed_stops == []
    assert _live_stop_rows(db, row.id) == []


def test_disabled_live_key_makes_no_venue_call(db, monkeypatch):
    """live_trading is on but execution.live_enabled() (no keys) is not."""
    venue = _live(monkeypatch, _Venue())
    monkeypatch.setattr(execution, "live_enabled", lambda: False)
    row = _session(db)

    service._maintain_live_stop(db, row, 90.0)

    assert venue.placed_stops == []
    assert _live_stop_rows(db, row.id) == []


def test_flat_session_makes_no_venue_call(db, monkeypatch):
    venue = _live(monkeypatch, _Venue())
    row = _session(db, qty=0.0)

    service._maintain_live_stop(db, row, 90.0)

    assert venue.placed_stops == []


def test_unarmed_session_makes_no_venue_call(db, monkeypatch):
    venue = _live(monkeypatch, _Venue())
    row = _session(db, trail_sl_price=0.0)

    service._maintain_live_stop(db, row, 90.0)

    assert venue.placed_stops == []


def test_non_active_session_makes_no_venue_call(db, monkeypatch):
    venue = _live(monkeypatch, _Venue())
    row = _session(db, status=service.SESSION_STOPPED)

    service._maintain_live_stop(db, row, 90.0)

    assert venue.placed_stops == []


# --- first placement ----------------------------------------------------------------------


def test_first_placement_writes_the_row(db, monkeypatch):
    venue = _live(monkeypatch, _Venue())
    row = _session(db, avg=100.0, qty=3.0, trail_sl_price=95.0)

    service._maintain_live_stop(db, row, 96.0)

    assert len(venue.placed_stops) == 1
    sent = venue.placed_stops[0]
    assert sent["pair"] == "SOL/USDT"
    assert sent["quantity"] == pytest.approx(3.0)
    assert sent["stop_price"] == pytest.approx(95.0)
    assert sent["limit_price"] == pytest.approx(95.0 * (1 - 0.3 / 100.0))

    rows = _live_stop_rows(db, row.id)
    assert len(rows) == 1
    stop_order = rows[0]
    assert stop_order.exchange_order_id == "S1"
    assert stop_order.status == PENDING
    assert stop_order.side == "SELL"
    assert stop_order.source == "kss"
    assert stop_order.quantity == pytest.approx(3.0)
    assert stop_order.price == pytest.approx(95.0)
    assert len(_audits(db, "live_stop_placed")) == 1


def test_qty_below_min_qty_is_skipped(db, monkeypatch):
    monkeypatch.setattr("app.kss.pyramid.get_exchange_info",
                         lambda symbol: {"minQty": 10.0, "stepSize": 0.001, "minNotional": 5.0})
    venue = _live(monkeypatch, _Venue())
    row = _session(db, qty=3.0)

    service._maintain_live_stop(db, row, 96.0)

    assert venue.placed_stops == []
    assert _live_stop_rows(db, row.id) == []


# --- ratchet: replace only past the step ------------------------------------------------


def test_small_ratchet_does_not_replace(db, monkeypatch):
    venue = _live(monkeypatch, _Venue())
    row = _session(db, trail_sl_price=95.0)
    _stop_row(db, row.id, price=95.0)
    # 0.2% move, below the 0.5% ratchet step
    row.trail_sl_price = 95.0 * 1.002
    db.commit()

    service._maintain_live_stop(db, row, 97.0)

    assert venue.placed_stops == []
    assert venue.cancelled == []
    rows = _live_stop_rows(db, row.id)
    assert len(rows) == 1 and rows[0].status == PENDING


def test_ratchet_past_the_step_replaces(db, monkeypatch):
    venue = _live(monkeypatch, _Venue(place_result={
        "raw_id": "S2", "status": "open", "price": 0.0, "quantity": 0.0, "fee": 0.0,
    }))
    row = _session(db, trail_sl_price=95.0)
    old = _stop_row(db, row.id, price=95.0, exchange_order_id="S1")
    # 1% move clears the 0.5% ratchet step
    row.trail_sl_price = 95.0 * 1.01
    db.commit()

    service._maintain_live_stop(db, row, 97.0)

    assert venue.cancelled == ["S1"]
    assert len(venue.placed_stops) == 1
    assert venue.placed_stops[0]["stop_price"] == pytest.approx(row.trail_sl_price)
    db.refresh(old)
    assert old.status == REJECTED
    assert old.exchange_order_id is None
    rows = [o for o in _live_stop_rows(db, row.id) if o.status == PENDING]
    assert len(rows) == 1
    assert rows[0].exchange_order_id == "S2"
    db.refresh(row)
    assert row.stop_replaces == 1
    assert len(_audits(db, "live_stop_replaced")) == 1


# --- replace cap --------------------------------------------------------------------------


def test_replace_cap_holds_the_old_stop(db, monkeypatch):
    venue = _live(monkeypatch, _Venue())
    settings.kss_stop_max_replaces = 2  # after _live(), which resets it to its own default
    row = _session(db, trail_sl_price=95.0, stop_replaces=2)
    old = _stop_row(db, row.id, price=95.0, exchange_order_id="S1")
    row.trail_sl_price = 95.0 * 1.05  # well past the ratchet step
    db.commit()

    service._maintain_live_stop(db, row, 100.0)

    assert venue.cancelled == []
    assert venue.placed_stops == []
    db.refresh(old)
    assert old.status == PENDING
    assert old.exchange_order_id == "S1"
    assert len(_audits(db, "live_stop_replace_capped")) == 1

    # A second tick over the cap must not re-audit.
    service._maintain_live_stop(db, row, 101.0)
    assert len(_audits(db, "live_stop_replace_capped")) == 1


# --- a refused cancel never doubles the live stop ------------------------------------------


def test_refused_cancel_leaves_exactly_one_live_stop(db, monkeypatch):
    venue = _live(monkeypatch, _Venue(cancel_error=RuntimeError("venue down")))
    row = _session(db, trail_sl_price=95.0)
    old = _stop_row(db, row.id, price=95.0, exchange_order_id="S1")
    row.trail_sl_price = 95.0 * 1.05
    db.commit()

    service._maintain_live_stop(db, row, 100.0)

    assert venue.placed_stops == []
    db.refresh(old)
    assert old.status == PENDING
    assert old.exchange_order_id == "S1"
    live_rows = [o for o in _live_stop_rows(db, row.id) if o.exchange_order_id]
    assert len(live_rows) == 1


# --- budget / rate hold: defer, never raise, never place -----------------------------------


def test_rate_hold_defers_without_raising(db, monkeypatch):
    venue = _live(monkeypatch, _Venue(), rate_hold=True)
    row = _session(db, trail_sl_price=95.0)

    service._maintain_live_stop(db, row, 96.0)  # must not raise

    assert venue.placed_stops == []
    assert len(_audits(db, "live_stop_deferred")) == 1


def test_budget_unavailable_defers_without_raising(db, monkeypatch):
    venue = _live(monkeypatch, _Venue(), budget_ok=False)
    row = _session(db, trail_sl_price=95.0)

    service._maintain_live_stop(db, row, 96.0)  # must not raise

    assert venue.placed_stops == []
    assert len(_audits(db, "live_stop_deferred")) == 1


def test_budget_hold_leaves_the_old_stop_in_place_on_a_replace(db, monkeypatch):
    venue = _live(monkeypatch, _Venue(), budget_ok=False)
    row = _session(db, trail_sl_price=95.0)
    old = _stop_row(db, row.id, price=95.0, exchange_order_id="S1")
    row.trail_sl_price = 95.0 * 1.05
    db.commit()

    service._maintain_live_stop(db, row, 100.0)

    assert venue.cancelled == []
    db.refresh(old)
    assert old.status == PENDING and old.exchange_order_id == "S1"


# --- never raise on a venue error -----------------------------------------------------------


def test_placement_error_is_caught_and_audited(db, monkeypatch):
    _live(monkeypatch, _Venue(place_error=RuntimeError("exchange down")))
    row = _session(db, trail_sl_price=95.0)

    service._maintain_live_stop(db, row, 96.0)  # must not raise

    assert _live_stop_rows(db, row.id) == []
    assert len(_audits(db, "live_stop_failed")) == 1


def test_rejected_placement_is_caught_and_audited(db, monkeypatch):
    _live(monkeypatch, _Venue(place_result={
        "raw_id": None, "status": "rejected", "price": 0.0, "quantity": 0.0, "fee": 0.0,
    }))
    row = _session(db, trail_sl_price=95.0)

    service._maintain_live_stop(db, row, 96.0)  # must not raise

    assert _live_stop_rows(db, row.id) == []
    assert len(_audits(db, "live_stop_failed")) == 1


# --- reconcile books a fill of the resting stop ---------------------------------------------


def test_reconcile_books_a_full_fill_and_completes_the_session(db, monkeypatch):
    _live(monkeypatch, _Venue(fetch_result={
        "status": "closed", "filled": 3.0, "average": 95.0, "fee": 0.0, "raw_id": "S1",
    }))
    row = _session(db, avg=100.0, qty=3.0, trail_sl_price=95.0)
    stop_order = _stop_row(db, row.id, price=95.0, qty=3.0, exchange_order_id="S1")

    booked = orders.reconcile_live_orders(db)

    assert booked == [stop_order.id]
    db.refresh(stop_order)
    assert stop_order.status == EXECUTED
    fills = db.query(Fill).filter(Fill.pending_order_id == stop_order.id).all()
    assert len(fills) == 1 and fills[0].quantity == pytest.approx(3.0)
    db.refresh(row)
    assert row.status == service.SESSION_COMPLETED


def test_reconcile_books_a_partial_fill_and_keeps_the_session_active(db, monkeypatch):
    _live(monkeypatch, _Venue(fetch_result={
        "status": "open", "filled": 1.0, "average": 95.0, "fee": 0.0, "raw_id": "S1",
    }))
    row = _session(db, avg=100.0, qty=3.0, trail_sl_price=95.0)
    stop_order = _stop_row(db, row.id, price=95.0, qty=3.0, exchange_order_id="S1")

    orders.reconcile_live_orders(db)

    db.refresh(stop_order)
    assert stop_order.status == PENDING  # still resting on the venue
    db.refresh(row)
    assert row.status == service.SESSION_ACTIVE
    assert row.total_filled_qty == pytest.approx(2.0)


# --- handle_fill_event routes "stop" through the tp branch -----------------------------------


def test_handle_fill_event_full_stop_fill_completes_the_session(db):
    row = _session(db, avg=100.0, qty=3.0, trail_sl_price=95.0)
    ref = f"pyramid:{row.id}:stop"

    result = service.handle_fill_event(db, ref, 3.0, 95.0)

    db.refresh(row)
    assert result["action"] == "completed"
    assert row.status == service.SESSION_COMPLETED


def test_handle_fill_event_partial_stop_fill_keeps_the_session_active(db):
    row = _session(db, avg=100.0, qty=3.0, trail_sl_price=95.0)
    ref = f"pyramid:{row.id}:stop"

    result = service.handle_fill_event(db, ref, 1.0, 95.0)

    db.refresh(row)
    assert result["action"] == "partial_stop"
    assert row.status == service.SESSION_ACTIVE
    assert row.total_filled_qty == pytest.approx(2.0)


def test_handle_fill_event_stop_never_sets_a_reentry_cooldown(db, monkeypatch):
    from app import runtime

    row = _session(db, avg=100.0, qty=3.0, trail_sl_price=95.0)
    service.handle_fill_event(db, f"pyramid:{row.id}:stop", 3.0, 95.0)

    assert runtime.get(db, f"stop_cooldown:{row.symbol}") is None


# --- auto_fill_due_orders / sync_resting_orders never touch a STOP row -----------------------


def test_auto_fill_due_orders_never_touches_a_stop_row(db, monkeypatch):
    row = _session(db, trail_sl_price=95.0)
    # Unlinked (no exchange_order_id) so it WOULD otherwise be picked up by the "kss +
    # unlinked" query; price sits below the stubbed market (10.0), which is exactly the
    # dangerous "due" shape a stop's trigger price always has.
    stop_order = PendingOrder(
        symbol="SOL", side="SELL", order_type="STOP", quantity=3.0, price=5.0,
        source="kss", source_ref=f"pyramid:{row.id}:stop", status=PENDING,
    )
    db.add(stop_order)
    db.commit()
    monkeypatch.setattr("app.orders.get_current_prices", lambda syms: {"SOL": 10.0})

    approved = orders.auto_fill_due_orders(db)

    assert approved == []
    db.refresh(stop_order)
    assert stop_order.status == PENDING


def test_sync_resting_orders_ignores_a_stop_row(db, monkeypatch):
    venue = _live(monkeypatch, _Venue())
    row = _session(db, trail_sl_price=95.0)
    stop_order = PendingOrder(
        symbol="SOL", side="SELL", order_type="STOP", quantity=3.0, price=95.0,
        source="kss", source_ref=f"pyramid:{row.id}:stop", status=PENDING,
    )
    db.add(stop_order)
    db.commit()

    out = orders.sync_resting_orders(db)

    assert out["placed"] == 0
    assert venue.placed_market == []
    db.refresh(stop_order)
    assert stop_order.exchange_order_id is None


# --- a MARKET risk exit retires the resting stop first ----------------------------------------


def test_market_exit_retires_the_resting_stop_first(db, monkeypatch):
    venue = _live(monkeypatch, _Venue(free=3.0))
    row = _session(db, trail_sl_price=95.0)
    stop_order = _stop_row(db, row.id, price=95.0, qty=3.0, exchange_order_id="STOP1")
    exit_order = PendingOrder(
        symbol="SOL", side="SELL", order_type="MARKET", quantity=3.0, price=0.0,
        source="kss", source_ref=f"pyramid:{row.id}:sl", status=PENDING,
    )
    db.add(exit_order)
    db.commit()
    db.refresh(exit_order)

    fill = orders._live_execute(db, exit_order)

    assert venue.cancelled == ["STOP1"]
    cancel_at = next(i for i, e in enumerate(venue.events) if e[0] == "cancel")
    place_at = next(i for i, e in enumerate(venue.events) if e[0] == "place_market")
    assert cancel_at < place_at, "the resting stop must come off the book BEFORE the exit"
    assert venue.placed_market and venue.placed_market[0]["order_type"] == "MARKET"
    assert fill.quantity == pytest.approx(3.0)
    db.refresh(stop_order)
    assert stop_order.status == REJECTED
    assert stop_order.reject_reason == "resting-stop: superseded by market exit"
    assert stop_order.exchange_order_id is None
    assert len(_audits(db, "stop_retired_for_exit")) == 1
