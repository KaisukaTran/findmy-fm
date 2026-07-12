"""P0.4 — duplicate-wave race guard (see memory loss-cases.md / findmy-audit-2026-07).

The manual DCA+ (queue_next_wave) and the auto-chain (handle_fill_event) could both queue the
SAME wave_num for a session. Historically this double-bought a rung (session 42/C: wave 13 filled
twice, ~$49.9k) and left an orphan lot the session bookkeeping never saw, while _wave_row's
.one_or_none() then raised MultipleResultsFound and crashed the fill hook.

Fix: (1) UNIQUE(session_id, wave_num) DB constraint, (2) an idempotency guard in
_queue_wave_if_above_sl, (3) _wave_row tolerates legacy dups via .first().
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from app.kss import service
from app.models import (
    SESSION_ACTIVE,
    AuditLog,
    KssSession,
    KssWave,
    PendingOrder,
)


def _session(db, **kw):
    d = {"symbol": "AAA", "entry_price": 100.0, "distance_pct": 1.5, "max_waves": 6,
         "isolated_fund": 1000.0, "tp_pct": 4.0, "timeout_x_min": 43200.0, "gap_y_min": 0.0,
         "status": SESSION_ACTIVE, "current_wave": 2, "avg_price": 100.0,
         "total_filled_qty": 10.0, "total_cost": 1000.0, "sl_pct": 8.0}
    d.update(kw)
    row = KssSession(**d)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_unique_wave_constraint_blocks_dup_insert(db):
    row = _session(db)
    db.add(KssWave(session_id=row.id, wave_num=3, quantity=1.0, target_price=95.0))
    db.commit()
    db.add(KssWave(session_id=row.id, wave_num=3, quantity=2.0, target_price=94.0))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_queue_wave_skips_when_already_queued(db, monkeypatch):
    """_queue_wave_if_above_sl returns False + audits and queues NOTHING when the wave exists."""
    monkeypatch.setattr(
        "app.market.get_current_prices", lambda syms, force=False: {"AAA": 95.0})
    row = _session(db)
    # A wave 3 row already exists (e.g. a manual DCA+ queued it).
    db.add(KssWave(session_id=row.id, wave_num=3, quantity=1.0, target_price=95.0))
    db.commit()
    pending_before = db.query(PendingOrder).count()

    py = service._to_pyramid(row)
    order = {"symbol": "AAA", "side": "BUY", "quantity": 1.0, "price": 95.0,
             "order_type": "LIMIT", "source_ref": f"pyramid:{row.id}:wave:3"}
    ok = service._queue_wave_if_above_sl(db, py, row.id, "AAA", order)
    db.commit()

    assert ok is False
    assert db.query(PendingOrder).count() == pending_before  # no new order
    assert db.query(KssWave).filter_by(session_id=row.id, wave_num=3).count() == 1  # no dup
    assert db.query(AuditLog).filter_by(action="wave_already_queued").count() == 1


def test_wave_row_returns_the_row_without_crashing(db):
    row = _session(db)
    db.add(KssWave(session_id=row.id, wave_num=4, quantity=1.0, target_price=90.0))
    db.commit()
    got = service._wave_row(db, row.id, 4)
    assert got is not None and got.wave_num == 4
    assert service._wave_row(db, row.id, 99) is None
