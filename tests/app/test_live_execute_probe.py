"""A7: a lost wave-0 MARKET response must not double-place.

`_live_execute` sends every placement with a deterministic ``clientOrderId``
(``execution.client_order_id(order.id)``), but nothing probed the venue BEFORE a retry: if
the FIRST attempt's response was lost after Binance had already accepted it (timeout, process
restart...), ``exchange_order_id`` was never stamped, the row went back to PENDING, and the
next tick placed the FULL size again. The reactive duplicate-clientOrderId recovery inside
``place_live_order`` cannot catch this for a MARKET order — it only fires while the venue
still holds the order, and a MARKET fills (and leaves the book) immediately.

``_live_execute`` now probes ``execution.fetch_order_by_client_id`` first, for any row with no
``exchange_order_id`` yet: an existing order is ADOPTED (linked + booked, no second
placement); no order found means nothing was ever placed, so placement proceeds as before; and
a probe failure of any kind (rate limit, network blip, ...) also falls through to placement —
never gating a SELL exit, and no worse than pre-fix for a BUY.

Fix round A / item 1: the probe used to run UNCONDITIONALLY whenever ``exchange_order_id`` was
None — which is ALWAYS true right after the cancel-resting branch above it clears the link, so
it re-asked the venue about the SAME clientOrderId the cancel just resolved and could adopt (or
double-book) the order just cancelled. The tests below pin the corrected semantics: skipped
entirely after a cancel-resting pass or during a rate hold; adopts ONLY a clean lost-response
(real quantity, nothing booked yet against this row); links + defers to reconcile for a
still-open venue order; and never adopts onto a row that already has fills booked.
"""

from __future__ import annotations

import pytest

from app import execution, models, orders
from app.config import settings
from app.models import EXECUTED, PENDING, Fill, KssSession, PendingOrder, Position


class _StubProvider:
    def pair(self, symbol):
        return f"{symbol}/USDT"


def _live(monkeypatch):
    monkeypatch.setattr(execution, "live_enabled", lambda: True)
    monkeypatch.setattr(settings, "live_max_order_notional", 100_000.0)
    monkeypatch.setattr(settings, "maker_orders", False)
    monkeypatch.setattr("app.data.providers.live_provider", lambda: _StubProvider())


def _session(db, **kw) -> KssSession:
    defaults = {
        "symbol": "SOL", "entry_price": 100.0, "distance_pct": 2.0, "max_waves": 5,
        "isolated_fund": 500.0, "tp_pct": 3.0, "timeout_x_min": 60, "gap_y_min": 5,
        "status": models.SESSION_ACTIVE, "current_wave": 0, "avg_price": 0.0,
        "total_filled_qty": 0.0, "total_cost": 0.0, "sl_pct": 8.0,
    }
    defaults.update(kw)
    row = KssSession(**defaults)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _wave0_buy(db, session_id: int, qty=5.0, price=100.0) -> PendingOrder:
    o = PendingOrder(symbol="SOL", side="BUY", order_type="LIMIT", quantity=qty, price=price,
                      source="kss", source_ref=f"pyramid:{session_id}:wave:0", status=PENDING)
    db.add(o)
    db.commit()
    db.refresh(o)
    return o


def _sl_sell(db, session_id: int, qty=5.0) -> PendingOrder:
    o = PendingOrder(symbol="SOL", side="SELL", order_type="MARKET", quantity=qty, price=0.0,
                      source="kss", source_ref=f"pyramid:{session_id}:sl", status=PENDING)
    db.add(o)
    db.commit()
    db.refresh(o)
    return o


def test_probe_adopts_an_existing_filled_order_and_does_not_place_again(db, monkeypatch):
    _live(monkeypatch)
    session = _session(db)
    order = _wave0_buy(db, session.id)
    placements: list[dict] = []
    monkeypatch.setattr(
        execution, "fetch_order_by_client_id",
        lambda pair, cid: {"price": 100.0, "quantity": 5.0, "fee": 0.05, "fee_base": 0.0,
                           "raw_id": "EXIST1", "status": "closed"},
    )

    def _place(*a, **kw):
        placements.append({"args": a, "kwargs": kw})
        raise AssertionError("must not place a second order once the probe adopted one")

    monkeypatch.setattr(execution, "place_live_order", _place)

    fill = orders.approve_order(db, order.id, reviewer="dashboard")

    assert placements == [], "the probe found an existing order — no second placement"
    db.refresh(order)
    assert order.exchange_order_id == "EXIST1"
    assert order.status == EXECUTED
    assert fill.quantity == 5.0 and fill.price == 100.0
    pos = db.query(Position).filter(Position.symbol == "SOL").one()
    assert pos.quantity == 5.0, "the adopted fill must still be booked into the position"


def test_probe_finds_nothing_and_placement_proceeds(db, monkeypatch):
    _live(monkeypatch)
    session = _session(db)
    order = _wave0_buy(db, session.id)
    monkeypatch.setattr(execution, "fetch_order_by_client_id", lambda pair, cid: None)
    placed: list[dict] = []

    def _place(pair, side, quantity, price, order_type, maker_orders=None, client_order_id=None):
        placed.append({"quantity": quantity, "cid": client_order_id})
        return {"raw_id": "EX-NEW", "status": "closed", "price": 101.0,
                "quantity": quantity, "fee": 0.01, "fee_base": 0.0}

    monkeypatch.setattr(execution, "place_live_order", _place)

    fill = orders.approve_order(db, order.id, reviewer="dashboard")

    assert len(placed) == 1, "no order found by the probe — placement must proceed as before"
    assert fill.quantity == 5.0 and fill.price == 101.0


def test_a_probe_error_on_a_sell_never_blocks_the_exit(db, monkeypatch):
    _live(monkeypatch)
    session = _session(db, current_wave=1, avg_price=100.0, total_filled_qty=5.0,
                       total_cost=500.0)
    db.add(Position(symbol="SOL", quantity=5.0, avg_entry_price=100.0, total_cost=500.0))
    db.commit()
    order = _sl_sell(db, session.id)

    def _boom(pair, cid):
        raise ConnectionError("network blip")

    monkeypatch.setattr(execution, "fetch_order_by_client_id", _boom)
    placed: list[dict] = []

    def _place(pair, side, quantity, price, order_type, maker_orders=None, client_order_id=None):
        placed.append({"quantity": quantity})
        return {"raw_id": "EX-SL", "status": "closed", "price": 90.0,
                "quantity": quantity, "fee": 0.01, "fee_base": 0.0}

    monkeypatch.setattr(execution, "place_live_order", _place)

    fill = orders.approve_order(db, order.id, reviewer="dashboard")

    assert len(placed) == 1, "a probe failure must fall through to placement — never gate a SELL"
    assert fill.quantity == 5.0 and fill.price == 90.0


# --- Fix round A / item 1: the corrected probe semantics --------------------------------------


def test_cancel_resting_path_never_probes(db, monkeypatch):
    """(a) The cancel-resting branch already asked the venue for this order's final state
    (_cancel_resting -> _book_delta) — the probe must not run at all afterward, or it would
    re-ask about the SAME clientOrderId and could adopt/double-book the order just cancelled."""
    _live(monkeypatch)
    session = _session(db, current_wave=1, avg_price=100.0, total_filled_qty=0.0, total_cost=0.0)
    order = _wave0_buy(db, session.id)
    order.exchange_order_id = "OLD1"
    order.exchange_status = "open"
    db.commit()

    def _cancel_ok(db_, o):  # a clean cancel: nothing filled, unlink
        o.exchange_order_id = None
        o.exchange_status = None
        return True

    monkeypatch.setattr(orders, "_cancel_resting", _cancel_ok)

    # Record calls instead of raising: a broad `except Exception` around the (pre-fix) probe
    # would otherwise silently swallow an AssertionError here and mask the bug this pins.
    probe_calls: list[int] = []

    def _tracked_probe(pair, cid):
        probe_calls.append(1)
        return None

    monkeypatch.setattr(execution, "fetch_order_by_client_id", _tracked_probe)

    placed: list[float] = []

    def _place(pair, side, quantity, price, order_type, maker_orders=None, client_order_id=None):
        placed.append(quantity)
        return {"raw_id": "NEW1", "status": "closed", "price": 101.0, "quantity": quantity,
                "fee": 0.0, "fee_base": 0.0}

    monkeypatch.setattr(execution, "place_live_order", _place)

    fill = orders.approve_order(db, order.id, reviewer="dashboard")

    assert probe_calls == [], "the probe must not run after a cancel-resting pass"
    assert len(placed) == 1
    assert fill.price == 101.0


def test_probe_returns_canceled_zero_fill_placement_proceeds(db, monkeypatch):
    """(b) A terminal status with nothing filled is not a fill to adopt — place normally."""
    _live(monkeypatch)
    session = _session(db)
    order = _wave0_buy(db, session.id)
    monkeypatch.setattr(
        execution, "fetch_order_by_client_id",
        lambda pair, cid: {"price": 0.0, "quantity": 0.0, "fee": 0.0, "fee_base": 0.0,
                           "raw_id": "OLDX", "status": "canceled"},
    )
    placed: list[float] = []

    def _place(pair, side, quantity, price, order_type, maker_orders=None, client_order_id=None):
        placed.append(quantity)
        return {"raw_id": "NEW1", "status": "closed", "price": 101.0, "quantity": quantity,
                "fee": 0.01, "fee_base": 0.0}

    monkeypatch.setattr(execution, "place_live_order", _place)

    fill = orders.approve_order(db, order.id, reviewer="dashboard")

    assert len(placed) == 1
    assert fill.price == 101.0


def test_probe_returns_open_zero_fill_links_and_awaits_reconcile(db, monkeypatch):
    """(c) A still-resting venue order with nothing to cleanly adopt is LINKED (not placed
    again, not invented a fill price for) and handed to reconcile via the PENDING failure path.
    A SECOND attempt with the link now present must go down the cancel-resting branch, not the
    probe again — proving this does not become a crash-loop."""
    _live(monkeypatch)
    session = _session(db)
    order = _wave0_buy(db, session.id)
    monkeypatch.setattr(
        execution, "fetch_order_by_client_id",
        lambda pair, cid: {"price": 0.0, "quantity": 0.0, "fee": 0.0, "fee_base": 0.0,
                           "raw_id": "REST1", "status": "open"},
    )

    def _boom_place(*a, **kw):
        raise AssertionError("must not place — a resting order was found to link instead")

    monkeypatch.setattr(execution, "place_live_order", _boom_place)

    with pytest.raises(ValueError, match="awaiting reconcile"):
        orders.approve_order(db, order.id, reviewer="dashboard")

    db.refresh(order)
    assert order.status == PENDING
    assert order.exchange_order_id == "REST1"
    assert order.exchange_status == "open"

    # Second attempt: the link is present now, so this must route through cancel-resting.
    probe_calls: list[int] = []

    def _tracked_probe(pair, cid):
        probe_calls.append(1)
        return None

    monkeypatch.setattr(execution, "fetch_order_by_client_id", _tracked_probe)

    def _cancel_ok(db_, o):
        o.exchange_order_id = None
        o.exchange_status = None
        return True

    monkeypatch.setattr(orders, "_cancel_resting", _cancel_ok)

    def _place2(pair, side, quantity, price, order_type, maker_orders=None, client_order_id=None):
        return {"raw_id": "NEW2", "status": "closed", "price": 102.0, "quantity": quantity,
                "fee": 0.0, "fee_base": 0.0}

    monkeypatch.setattr(execution, "place_live_order", _place2)

    fill = orders.approve_order(db, order.id, reviewer="dashboard")

    assert probe_calls == [], "a linked row must go through cancel-resting, never the probe again"
    assert fill.price == 102.0


def test_probe_refuses_to_adopt_when_row_already_has_booked_fills(db, monkeypatch):
    """(d) Even a real, non-zero-quantity probe result must NOT be adopted onto a row that
    already booked fills — adopting a SECOND time on top of what is already recorded would
    double-book. Placement proceeds instead (always recoverable via the venue's own
    duplicate-clientOrderId rejection)."""
    _live(monkeypatch)
    session = _session(db)
    order = _wave0_buy(db, session.id, qty=5.0, price=100.0)
    db.add(Fill(pending_order_id=order.id, symbol="SOL", side="BUY", quantity=2.0, price=100.0,
               fee=0.0, source_ref=order.source_ref))
    db.commit()

    monkeypatch.setattr(
        execution, "fetch_order_by_client_id",
        lambda pair, cid: {"price": 100.0, "quantity": 5.0, "fee": 0.05, "fee_base": 0.0,
                           "raw_id": "EXIST9", "status": "closed"},
    )
    placed: list[float] = []

    def _place(pair, side, quantity, price, order_type, maker_orders=None, client_order_id=None):
        placed.append(quantity)
        return {"raw_id": "NEW3", "status": "closed", "price": 101.0, "quantity": quantity,
                "fee": 0.0, "fee_base": 0.0}

    monkeypatch.setattr(execution, "place_live_order", _place)

    fill = orders.approve_order(db, order.id, reviewer="dashboard")

    assert len(placed) == 1, "adoption must be refused once the row already has booked fills"
    assert fill.price == 101.0


def test_rate_hold_active_skips_the_probe_sell_still_placed(db, monkeypatch):
    """(f) A signed probe call during a rate/order-budget hold would amplify it — skip the
    probe entirely, but a SELL exit must still reach placement (never-gate-exits)."""
    _live(monkeypatch)
    session = _session(db, current_wave=1, avg_price=100.0, total_filled_qty=5.0,
                       total_cost=500.0)
    db.add(Position(symbol="SOL", quantity=5.0, avg_entry_price=100.0, total_cost=500.0))
    db.commit()
    order = _sl_sell(db, session.id)

    monkeypatch.setattr(execution, "rate_hold_active", lambda: True)

    # Record calls instead of raising: a broad `except Exception` around the (pre-fix) probe
    # would otherwise silently swallow an AssertionError here and mask the bug this pins.
    probe_calls: list[int] = []

    def _tracked_probe(pair, cid):
        probe_calls.append(1)
        return None

    monkeypatch.setattr(execution, "fetch_order_by_client_id", _tracked_probe)
    placed: list[float] = []

    def _place(pair, side, quantity, price, order_type, maker_orders=None, client_order_id=None):
        placed.append(quantity)
        return {"raw_id": "EX-SL2", "status": "closed", "price": 90.0, "quantity": quantity,
                "fee": 0.01, "fee_base": 0.0}

    monkeypatch.setattr(execution, "place_live_order", _place)

    fill = orders.approve_order(db, order.id, reviewer="dashboard")

    assert probe_calls == [], "the probe must not run while a rate/order-budget hold is active"
    assert len(placed) == 1
    assert fill.quantity == 5.0 and fill.price == 90.0


def test_a_genuine_adoption_credits_the_orders_budget(db, monkeypatch):
    """The lost first attempt never got to call record_order_placed/record_order_filled — a
    genuine adoption must credit both, or the ORDERS-budget tracker under-counts real venue
    usage forever for that order."""
    _live(monkeypatch)
    session = _session(db)
    order = _wave0_buy(db, session.id)
    monkeypatch.setattr(
        execution, "fetch_order_by_client_id",
        lambda pair, cid: {"price": 100.0, "quantity": 5.0, "fee": 0.05, "fee_base": 0.0,
                           "raw_id": "EXIST-BUDGET", "status": "closed"},
    )
    calls: list[str] = []
    monkeypatch.setattr(execution, "record_order_placed", lambda *a, **k: calls.append("placed"))
    monkeypatch.setattr(execution, "record_order_filled", lambda *a, **k: calls.append("filled"))

    def _boom_place(*a, **kw):
        raise AssertionError("must not place — adopted instead")

    monkeypatch.setattr(execution, "place_live_order", _boom_place)

    orders.approve_order(db, order.id, reviewer="dashboard")

    assert calls == ["placed", "filled"]
