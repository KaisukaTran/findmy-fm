"""Live-readiness 1.5 — the resting take-profit.

In live maker mode a session's exit rests on the exchange from the moment it holds
inventory, and follows the session average as waves fill, instead of being sold at market
when the TP triggers. `service.sync_resting_tp` owns that queue; nothing here touches the
network (live_enabled and the exchange calls are stubbed) and paper behaviour must not move.
"""

from app import execution, models, orders
from app.config import settings
from app.kss import service
from app.models import PENDING, REJECTED, KssSession, PendingOrder, Position


class _StubProvider:
    def pair(self, symbol):
        return f"{symbol}/USDT"


def _live(monkeypatch, *, maker=True, live=True):
    monkeypatch.setattr(execution, "live_enabled", lambda: live)
    monkeypatch.setattr("app.data.providers.live_provider", lambda: _StubProvider())
    # Taking an order off the book reads its final status before unlinking (a cancel races
    # the venue), so this has to be stubbed or the test would hit the real exchange.
    monkeypatch.setattr(execution, "fetch_live_order", lambda pair, oid: {
        "status": "canceled", "filled": 0.0, "average": 0.0, "fee": 0.0, "raw_id": oid,
    })
    settings.maker_orders = maker
    settings.auto_trade = True


def _session(db, *, avg=10.0, qty=3.0, tp_pct=3.0, status=models.SESSION_ACTIVE) -> KssSession:
    row = KssSession(
        symbol="SOL", entry_price=10.0, distance_pct=2.0, max_waves=5,
        isolated_fund=1000.0, tp_pct=tp_pct, timeout_x_min=60, gap_y_min=5,
        status=status, current_wave=1, avg_price=avg, total_filled_qty=qty,
        total_cost=avg * qty,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _tp_order(db, session_id: int) -> PendingOrder | None:
    return (
        db.query(PendingOrder)
        .filter(PendingOrder.source_ref == f"pyramid:{session_id}:tp")
        .order_by(PendingOrder.id.desc())
        .first()
    )


# --- off unless live AND maker ---------------------------------------------


def test_paper_queues_no_resting_tp(db, monkeypatch):
    _live(monkeypatch, live=False)
    row = _session(db)

    assert service.sync_resting_tp(db) == {"queued": 0, "replaced": 0, "dropped": 0}
    assert _tp_order(db, row.id) is None


def test_maker_off_queues_no_resting_tp(db, monkeypatch):
    _live(monkeypatch, maker=False)
    row = _session(db)

    assert service.sync_resting_tp(db)["queued"] == 0
    assert _tp_order(db, row.id) is None


# --- the resting exit -------------------------------------------------------


def test_tp_rests_at_the_session_target(db, monkeypatch):
    _live(monkeypatch)
    row = _session(db, avg=10.0, qty=3.0)
    expected = service._to_pyramid(row).estimated_tp_price

    assert service.sync_resting_tp(db)["queued"] == 1

    order = _tp_order(db, row.id)
    assert (order.side, order.order_type, order.status) == ("SELL", "LIMIT", PENDING)
    assert order.quantity == 3.0
    assert order.price == expected
    assert expected > row.avg_price  # covers tp_pct + the fee buffer


def test_resting_tp_never_sits_below_the_k2_floor(db, monkeypatch):
    """A blended Position basis above the session avg must lift the resting price, exactly as
    K-2 defers a triggered TP that would realize under the true cost."""
    _live(monkeypatch)
    row = _session(db, avg=10.0, qty=3.0)
    db.add(Position(symbol="SOL", quantity=3.0, avg_entry_price=20.0, total_cost=60.0))
    db.commit()

    service.sync_resting_tp(db)

    floor = service._k2_floor_price(db, "SOL")
    assert floor > service._to_pyramid(row).estimated_tp_price
    assert _tp_order(db, row.id).price == floor


def test_second_pass_does_not_duplicate(db, monkeypatch):
    _live(monkeypatch)
    row = _session(db)

    service.sync_resting_tp(db)
    assert service.sync_resting_tp(db) == {"queued": 0, "replaced": 0, "dropped": 0}
    assert db.query(PendingOrder).filter(
        PendingOrder.source_ref == f"pyramid:{row.id}:tp").count() == 1


def test_a_fill_moves_avg_so_the_exit_is_replaced(db, monkeypatch):
    """cancel+replace on avg change — the whole reason the exit cannot be placed once."""
    _live(monkeypatch)
    cancelled: list[str] = []
    monkeypatch.setattr(execution, "cancel_live_order",
                        lambda pair, oid: cancelled.append(oid))
    row = _session(db, avg=10.0, qty=3.0)
    service.sync_resting_tp(db)
    order = _tp_order(db, row.id)
    order.exchange_order_id = "X1"  # already on the book
    order.exchange_status = "open"
    first_price = order.price
    db.commit()

    row.avg_price = 9.0  # a lower wave filled
    row.total_filled_qty = 5.0
    db.commit()

    assert service.sync_resting_tp(db)["replaced"] == 1
    assert cancelled == ["X1"]
    db.refresh(order)
    assert order.price < first_price and order.quantity == 5.0
    assert order.exchange_order_id is None  # re-placed by sync_resting_orders next pass


def test_a_refused_cancel_leaves_the_old_exit_in_place(db, monkeypatch):
    _live(monkeypatch)
    monkeypatch.setattr(execution, "cancel_live_order",
                        lambda pair, oid: (_ for _ in ()).throw(RuntimeError("venue down")))
    row = _session(db, avg=10.0, qty=3.0)
    service.sync_resting_tp(db)
    order = _tp_order(db, row.id)
    order.exchange_order_id = "X1"
    order.exchange_status = "open"
    kept = order.price
    db.commit()

    row.avg_price = 9.0
    db.commit()

    assert service.sync_resting_tp(db)["replaced"] == 0
    db.refresh(order)
    assert order.price == kept and order.exchange_order_id == "X1"


def test_session_without_inventory_has_no_exit(db, monkeypatch):
    _live(monkeypatch)
    row = _session(db, qty=0.0)

    assert service.sync_resting_tp(db)["queued"] == 0
    assert _tp_order(db, row.id) is None


def test_stopped_session_drops_its_exit(db, monkeypatch):
    _live(monkeypatch)
    row = _session(db)
    service.sync_resting_tp(db)

    row.status = models.SESSION_STOPPED
    db.commit()
    assert service.sync_resting_tp(db)["dropped"] == 1
    # Rejected here; sync_resting_orders is what takes it off the exchange.
    assert _tp_order(db, row.id).status == REJECTED


# --- no second, market TP ---------------------------------------------------


def test_manage_open_sessions_does_not_queue_a_market_tp(db, monkeypatch):
    """With the exit resting, a market TP here would sell the same inventory twice."""
    _live(monkeypatch)
    row = _session(db, avg=10.0, qty=3.0)
    monkeypatch.setattr("app.market.get_current_prices", lambda syms: {"SOL": 100.0})

    assert service.manage_open_sessions(db) == []
    assert db.query(PendingOrder).filter(
        PendingOrder.source_ref == f"pyramid:{row.id}:tp").count() == 0
    db.refresh(row)
    assert row.status == models.SESSION_ACTIVE


def test_maker_off_still_queues_the_market_tp(db, monkeypatch):
    """The legacy trigger-then-sell model must be untouched when maker is off."""
    _live(monkeypatch, maker=False)
    row = _session(db, avg=10.0, qty=3.0)
    monkeypatch.setattr("app.market.get_current_prices", lambda syms: {"SOL": 100.0})

    assert service.manage_open_sessions(db) == [row.id]
    order = _tp_order(db, row.id)
    assert order is not None and order.order_type == "MARKET"


def test_fill_that_trips_tp_keeps_the_session_active(db, monkeypatch):
    """`_handle_tp_triggered` must hand the exit to the resting order, not to a market sell."""
    _live(monkeypatch)
    row = _session(db, avg=10.0, qty=3.0)
    row.status = models.SESSION_ACTIVE
    db.commit()

    service._handle_tp_triggered(db, row, {"order": {
        "symbol": "SOL", "side": "SELL", "quantity": 3.0, "price": 0,
        "order_type": "MARKET", "source_ref": f"pyramid:{row.id}:tp",
    }})

    assert row.status == models.SESSION_ACTIVE
    assert db.query(PendingOrder).filter(
        PendingOrder.source_ref == f"pyramid:{row.id}:tp").count() == 0


# --- end to end: queued here, placed by orders --------------------------------


def test_queued_exit_is_placed_by_sync_resting_orders(db, monkeypatch):
    _live(monkeypatch)
    placed: list[dict] = []

    def _place(pair, side, quantity, price, order_type, maker_orders=None, client_order_id=None):
        placed.append({"side": side, "type": order_type, "maker": maker_orders})
        return {"raw_id": "X7", "status": "open", "price": 0.0, "quantity": 0.0, "fee": 0.0}

    monkeypatch.setattr(execution, "place_live_order", _place)
    row = _session(db)

    service.sync_resting_tp(db)
    assert orders.sync_resting_orders(db)["placed"] == 1

    assert placed == [{"side": "SELL", "type": "LIMIT", "maker": True}]
    assert _tp_order(db, row.id).exchange_order_id == "X7"


# --- interaction with the 90s position guard --------------------------------


def test_guard_does_not_force_fill_the_resting_tp(db, monkeypatch):
    """The guard force-fills queued KSS exit SELLs every 90s. The resting TP is a standing
    limit on the exchange, not a protective exit — filling it here would pull it off the book
    and re-place it, and could orphan a live order."""
    _live(monkeypatch)
    approved: list[int] = []
    monkeypatch.setattr(orders, "approve_order",
                        lambda db, oid, reviewer=None: approved.append(oid))
    monkeypatch.setattr("app.market.get_current_prices",
                        lambda syms, force=False: {"SOL": 10.5})
    row = _session(db, avg=10.0, qty=3.0)
    service.sync_resting_tp(db)
    tp = _tp_order(db, row.id)
    tp.exchange_order_id = "X1"
    tp.exchange_status = "open"
    db.commit()

    service.run_position_guard(db)

    assert approved == []
    db.refresh(tp)
    assert tp.status == PENDING and tp.exchange_order_id == "X1"


def test_guard_still_fills_a_protective_exit(db, monkeypatch):
    """The never-gate-exits rule: a queued MARKET stop must still fill immediately."""
    _live(monkeypatch)
    approved: list[int] = []
    monkeypatch.setattr(orders, "approve_order",
                        lambda db, oid, reviewer=None: approved.append(oid))
    monkeypatch.setattr("app.market.get_current_prices",
                        lambda syms, force=False: {"SOL": 10.5})
    row = _session(db, avg=10.0, qty=3.0)
    sl, _ = orders.queue_order(
        db, symbol="SOL", side="SELL", quantity=3.0, price=0.0, order_type="MARKET",
        source="kss", source_ref=f"pyramid:{row.id}:sl",
    )

    service.run_position_guard(db)

    assert approved == [sl.id]


def test_guard_pulls_the_resting_tp_off_the_book_after_a_stop(db, monkeypatch):
    """A hard SL closes the session; its resting exit must leave the exchange at once, not in
    half an hour when the next 30-min cycle happens to run."""
    _live(monkeypatch)
    cancelled: list[str] = []
    monkeypatch.setattr(execution, "cancel_live_order",
                        lambda pair, oid: cancelled.append(oid))
    monkeypatch.setattr(execution, "place_live_order",
                        lambda *a, **k: {"raw_id": "X9", "status": "open",
                                         "price": 0.0, "quantity": 0.0, "fee": 0.0})
    monkeypatch.setattr(orders, "approve_order", lambda db, oid, reviewer=None: None)
    row = _session(db, avg=10.0, qty=3.0)
    row.sl_pct = 10.0
    db.commit()
    service.sync_resting_tp(db)
    tp = _tp_order(db, row.id)
    tp.exchange_order_id = "X1"
    tp.exchange_status = "open"
    db.commit()
    # Price far below the hard-SL floor → _guard_hard_sl stops the session.
    monkeypatch.setattr("app.market.get_current_prices",
                        lambda syms, force=False: {"SOL": 5.0})

    out = service.run_position_guard(db)

    assert out["exited"] == [row.id]
    db.refresh(tp)
    assert tp.status == REJECTED
    assert "X1" in cancelled
