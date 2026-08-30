"""A filled take-profit must close the session AND clear its ladder off the exchange.

Under the legacy model this was safe by accident: `manage_open_sessions` queued the TP and
cancelled the ladder in the same breath. Under the 1.5 resting model an ACTIVE session
deliberately has one resting SELL and N resting BUY rungs on the book at once — and the `:tp`
branch of `handle_fill_event` was the only terminal path that did not cancel the rungs.

So when the venue filled the exit, the DCA rungs stayed live. Binance locks their quote
notional (free USDT drains), and if the market later dips to one it BUYS into a position with
no session, no ladder and no take-profit — the orphan pattern the loss-cases file already
blames for a big share of realised losses.

The second half is worse: the branch set COMPLETED without checking how much actually filled.
A PARTIAL fill of a resting maker order — the normal case in a thin alt book — completed the
session and abandoned the rest of the position with no managed exit.
"""

from __future__ import annotations

from app import models
from app.kss import service as kss
from app.models import PENDING, REJECTED, PendingOrder


def _session(db, *, qty=3.0, avg=10.0) -> models.KssSession:
    row = models.KssSession(
        symbol="SOL", entry_price=avg, distance_pct=2.0, max_waves=4, isolated_fund=150.0,
        tp_pct=3.0, timeout_x_min=60, gap_y_min=5, status=models.SESSION_ACTIVE,
        current_wave=1, avg_price=avg, total_filled_qty=qty, total_cost=qty * avg,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _rung(db, session_id: int, wave: int, **kw) -> PendingOrder:
    defaults = {
        "symbol": "SOL", "side": "BUY", "order_type": "LIMIT", "quantity": 1.0,
        "price": 9.8, "source": "kss", "source_ref": f"pyramid:{session_id}:wave:{wave}",
        "status": PENDING, "exchange_order_id": f"EX{wave}", "exchange_status": "open",
    }
    defaults.update(kw)
    o = PendingOrder(**defaults)
    db.add(o)
    db.commit()
    db.refresh(o)
    return o


def test_a_full_tp_fill_takes_the_remaining_rungs_off_the_book(db):
    row = _session(db, qty=3.0)
    rung1, rung2 = _rung(db, row.id, 1), _rung(db, row.id, 2)

    kss.handle_fill_event(db, f"pyramid:{row.id}:tp", 3.0, 10.4)

    db.refresh(row)
    db.refresh(rung1)
    db.refresh(rung2)
    assert row.status == models.SESSION_COMPLETED
    # REJECTED is the signal sync_resting_orders acts on to cancel them at the venue.
    assert rung1.status == REJECTED, "a completed session must not leave live BUYs behind"
    assert rung2.status == REJECTED


def test_a_partial_tp_fill_does_not_complete_the_session(db):
    """0.4 of 3.0 filled must leave 2.6 still managed, with its ladder intact."""
    row = _session(db, qty=3.0)
    rung = _rung(db, row.id, 1)

    kss.handle_fill_event(db, f"pyramid:{row.id}:tp", 0.4, 10.4)

    db.refresh(row)
    db.refresh(rung)
    assert row.status == models.SESSION_ACTIVE, "most of the position is still held"
    assert rung.status == PENDING, "the ladder still belongs to a live session"


def test_a_partial_that_finishes_the_position_completes_it(db):
    row = _session(db, qty=3.0)

    kss.handle_fill_event(db, f"pyramid:{row.id}:tp", 1.0, 10.4)
    db.refresh(row)
    assert row.status == models.SESSION_ACTIVE

    kss.handle_fill_event(db, f"pyramid:{row.id}:tp", 2.0, 10.4)  # 1.0 + 2.0 = the whole 3.0

    db.refresh(row)
    assert row.status == models.SESSION_COMPLETED


def test_another_session_ladder_is_untouched(db):
    keep = _session(db)
    done = _session(db)
    mine = _rung(db, keep.id, 1)
    theirs = _rung(db, done.id, 1)

    kss.handle_fill_event(db, f"pyramid:{done.id}:tp", 3.0, 10.4)

    db.refresh(mine)
    db.refresh(theirs)
    assert mine.status == PENDING
    assert theirs.status == REJECTED
