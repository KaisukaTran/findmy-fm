"""A4: the freeze-immune rescue in `run_position_guard` must reach orphan exits too.

`manage_orphan_positions` queues MARKET SELLs tagged `orphan:tp` / `orphan:sl` for leftover
position quantity no active session owns. While the circuit-breaker is frozen nothing else
fills a PENDING order (`auto_fill_due_orders` and `auto_approve_by_policy` both no-op), so
before this fix an orphan exit could sit PENDING indefinitely — exactly while a losing streak
(the freeze trigger) makes the exit most needed. The rescue loop widened its filter to cover
both `pyramid:%` and `orphan:%`, keyed the resting-TP skip on the `pyramid:` prefix (not the
bare `:tp` suffix), so an `orphan:tp` — a MARKET sweep with no resting leg — is never wrongly
left behind.
"""

from __future__ import annotations

from app import market, orders, runtime
from app.kss import service as kss
from app.models import EXECUTED, PENDING, PendingOrder, Position


def _pos(db, sym, qty, avg):
    db.add(Position(symbol=sym, quantity=qty, avg_entry_price=avg, total_cost=qty * avg))
    db.commit()


def _orphan(db, sym, tag, qty=5.0) -> PendingOrder:
    row = PendingOrder(symbol=sym, side="SELL", quantity=qty, order_type="MARKET",
                        status=PENDING, source="kss", source_ref=f"orphan:{tag}",
                        strategy_name=f"Orphan_{sym}")
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_frozen_breaker_still_rescues_a_pending_orphan_sl(db, monkeypatch):
    monkeypatch.setattr(market, "get_current_prices", lambda s, force=False: {"AAA": 9.0})
    monkeypatch.setattr(orders, "get_current_prices", lambda s: {"AAA": 9.0})
    _pos(db, "AAA", 5.0, 10.0)
    order = _orphan(db, "AAA", "sl")
    runtime.freeze(db, "test")
    assert runtime.is_frozen(db) is True

    kss.run_position_guard(db)

    db.refresh(order)
    assert order.status == EXECUTED, "an orphan SL must be force-filled even while frozen"


def test_frozen_breaker_still_rescues_a_pending_orphan_tp_under_resting_model(db, monkeypatch):
    """`orphan:tp` is a MARKET sweep with no resting leg — the resting-TP skip must not catch
    it just because its source_ref ends in `:tp`."""
    monkeypatch.setattr(market, "get_current_prices", lambda s, force=False: {"BBB": 12.0})
    monkeypatch.setattr(orders, "get_current_prices", lambda s: {"BBB": 12.0})
    monkeypatch.setattr(orders, "resting_model_active", lambda: True)
    monkeypatch.setattr(kss, "sync_resting_tp", lambda db: {"queued": 0, "replaced": 0, "dropped": 0})
    monkeypatch.setattr(orders, "sync_resting_orders", lambda db: {"placed": 0, "cancelled": 0})
    _pos(db, "BBB", 5.0, 10.0)
    order = _orphan(db, "BBB", "tp")
    runtime.freeze(db, "test")

    kss.run_position_guard(db)

    db.refresh(order)
    assert order.status == EXECUTED, "an orphan TP must be force-filled — it is a MARKET order"


def test_a_resting_pyramid_tp_is_still_left_alone(db, monkeypatch):
    """The genuine resting pyramid TP (a STANDING limit order on the exchange) must NOT be
    pulled off the book by this rescue — that is what the resting-TP skip exists for."""
    monkeypatch.setattr(market, "get_current_prices", lambda s, force=False: {"CCC": 12.0})
    monkeypatch.setattr(orders, "resting_model_active", lambda: True)
    monkeypatch.setattr(kss, "sync_resting_tp", lambda db: {"queued": 0, "replaced": 0, "dropped": 0})
    monkeypatch.setattr(orders, "sync_resting_orders", lambda db: {"placed": 0, "cancelled": 0})
    _pos(db, "CCC", 5.0, 10.0)
    order = PendingOrder(symbol="CCC", side="SELL", quantity=5.0, order_type="LIMIT", price=12.5,
                          status=PENDING, source="kss", source_ref="pyramid:99:tp",
                          strategy_name="Pyramid_CCC")
    db.add(order)
    db.commit()
    db.refresh(order)
    runtime.freeze(db, "test")

    kss.run_position_guard(db)

    db.refresh(order)
    assert order.status == PENDING, "a resting pyramid TP must not be force-filled"
