"""Stopping a session must take its orders OFF the exchange.

Before the resting model, a stopped session's queued rungs were harmless: they only ever sat
in the approval queue, and nothing would approve them again. Under 1.5 those rungs are REAL
orders resting on the venue, so a session that stops without cancelling them leaves live buy
orders behind — they can fill minutes or hours later, into a position no session manages.

Seen in the first soak: four sessions stopped, and their wave-0 orders were still open on
testnet afterwards (DOT 212388, NEAR 332337, AVAX 826340, ARB 162418).
"""

from __future__ import annotations

from app import models, orders
from app.kss import service as kss
from app.models import PENDING, REJECTED, PendingOrder


def _session(db, **kw) -> models.KssSession:
    defaults = {
        "symbol": "DOT", "entry_price": 0.834, "distance_pct": 2.0, "max_waves": 4,
        "isolated_fund": 150.0, "tp_pct": 3.0, "timeout_x_min": 60, "gap_y_min": 5,
        "status": models.SESSION_ACTIVE, "current_wave": 0,
        "avg_price": 0.0, "total_filled_qty": 0.0,
    }
    defaults.update(kw)
    row = models.KssSession(**defaults)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _rung(db, session_id: int, wave: int, **kw) -> PendingOrder:
    defaults = {
        "symbol": "DOT", "side": "BUY", "order_type": "LIMIT", "quantity": 18.0,
        "price": 0.834, "source": "kss", "source_ref": f"pyramid:{session_id}:wave:{wave}",
        "status": PENDING, "exchange_order_id": "X1", "exchange_status": "open",
    }
    defaults.update(kw)
    order = PendingOrder(**defaults)
    db.add(order)
    db.commit()
    db.refresh(order)
    return order


def test_stopping_a_session_rejects_its_queued_rungs(db):
    row = _session(db)
    entry = _rung(db, row.id, 0)
    dca = _rung(db, row.id, 1, price=0.817, exchange_order_id="X2")

    kss.stop_session(db, row.id, reason="test")

    db.refresh(entry)
    db.refresh(dca)
    assert entry.status == REJECTED, "a live entry order must not outlive its session"
    assert dca.status == REJECTED
    # REJECTED is what sync_resting_orders acts on — it is the signal to cancel on the venue.
    assert entry.exchange_order_id == "X1"  # still linked until the cancel actually lands


def test_a_stopped_session_does_not_touch_another_session_orders(db):
    keep = _session(db, symbol="ETC")
    drop = _session(db)
    mine = _rung(db, keep.id, 1, symbol="ETC", exchange_order_id="K1")
    theirs = _rung(db, drop.id, 1, exchange_order_id="D1")

    kss.stop_session(db, drop.id, reason="test")

    db.refresh(mine)
    db.refresh(theirs)
    assert mine.status == PENDING, "another session's rungs are untouched"
    assert theirs.status == REJECTED


def test_an_executed_rung_is_left_alone(db):
    """A filled entry is history, not something to cancel."""
    row = _session(db, avg_price=0.834, total_filled_qty=18.0)
    done = _rung(db, row.id, 0, status=models.EXECUTED)

    kss.stop_session(db, row.id, reason="test")

    db.refresh(done)
    assert done.status == models.EXECUTED


def test_the_rejected_rung_is_then_cancelled_on_the_venue(db, monkeypatch):
    """End to end: stop -> REJECTED -> sync_resting_orders takes it off the book."""
    from app import execution
    from app.config import settings

    cancelled: list[str] = []

    class _Prov:
        def pair(self, symbol):
            return f"{symbol}/USDT"

    monkeypatch.setattr(execution, "live_enabled", lambda: True)
    monkeypatch.setattr(settings, "maker_orders", True)
    monkeypatch.setattr("app.data.providers.live_provider", lambda: _Prov())
    monkeypatch.setattr(execution, "cancel_live_order",
                        lambda pair, oid: cancelled.append(oid))
    monkeypatch.setattr(execution, "fetch_live_order", lambda pair, oid: {
        "status": "canceled", "filled": 0.0, "average": 0.0, "fee": 0.0, "raw_id": oid,
    })

    row = _session(db)
    _rung(db, row.id, 0)

    kss.stop_session(db, row.id, reason="test")
    counts = orders.sync_resting_orders(db)

    assert counts["cancelled"] == 1
    assert cancelled == ["X1"]
