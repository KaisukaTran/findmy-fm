"""A `TP_TRIGGERED` session is meant to be transient — `_handle_tp_triggered` returns it to
ACTIVE whenever K-2 defers the sell, and otherwise its resting/market exit is expected to
complete it. Live evidence (EIGEN session 15): its MARKET exit was killed by the resting-tp
stale-row sweep, the orphan sweep rescued the position 11 minutes later, and the session sat
`status='tp_triggered'` for two days afterwards with `positions.EIGEN.quantity == 0.0` — the
guard, the deadline sweep and the ladder watchdog all filter on `status == SESSION_ACTIVE`, so
nothing else ever reconciles it.

`service._reconcile_tp_triggered` (called once per `manage_open_sessions` pass, next to the
ladder watchdog `_rearm_dead_ladders`) closes that gap: a flat ghost (position quantity 0, the
EIGEN shape) is finalised to the same terminal status a normal TP fill uses; a session that
still holds inventory is left alone (an auto-finalise there would abandon real coin) but
audited once so a human notices.
"""

from __future__ import annotations

import json

from app import models
from app.kss import service
from app.models import AuditLog, KssSession, Position


def _session(db, *, status: str = models.SESSION_TP_TRIGGERED, qty: float = 3.0) -> KssSession:
    row = KssSession(
        symbol="EIGEN", entry_price=10.0, distance_pct=2.0, max_waves=5,
        isolated_fund=1000.0, tp_pct=3.0, timeout_x_min=60, gap_y_min=5,
        status=status, current_wave=1, avg_price=10.0, total_filled_qty=qty,
        total_cost=10.0 * qty,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _actions(db, action: str, entity: str) -> list[AuditLog]:
    return (
        db.query(AuditLog)
        .filter(AuditLog.action == action, AuditLog.entity == entity)
        .all()
    )


def test_flat_ghost_is_finalised(db):
    """positions.<symbol>.quantity == 0 while the session is still TP_TRIGGERED: some other
    path (e.g. the orphan sweep) already closed it out. Finalise to the terminal status a
    normal TP completion uses (SESSION_COMPLETED, handle_fill_event's own "tp" branch) — do
    not invent a new status."""
    row = _session(db)
    db.add(Position(symbol="EIGEN", quantity=0.0, avg_entry_price=0.0, total_cost=0.0))
    db.commit()

    service._reconcile_tp_triggered(db)

    db.refresh(row)
    assert row.status == models.SESSION_COMPLETED
    rows = _actions(db, "tp_triggered_reconciled", f"kss:{row.id}")
    assert len(rows) == 1
    detail = json.loads(rows[0].detail or "{}")
    assert detail.get("symbol") == "EIGEN"


def test_flat_ghost_with_no_position_row_at_all_is_finalised(db):
    """No Position row for the symbol at all (never created, or already deleted) is the same
    "flat" shape as quantity == 0 — must not be mistaken for "still holding"."""
    row = _session(db)

    service._reconcile_tp_triggered(db)

    db.refresh(row)
    assert row.status == models.SESSION_COMPLETED


def test_still_holding_session_is_untouched_and_audited_once(db):
    """A TP_TRIGGERED session with real inventory needs a human — never auto-finalise it (that
    would abandon coin the book still thinks it owns). Audited once, deduped across passes."""
    row = _session(db)
    db.add(Position(symbol="EIGEN", quantity=3.0, avg_entry_price=10.0, total_cost=30.0))
    db.commit()

    service._reconcile_tp_triggered(db)
    service._reconcile_tp_triggered(db)
    service._reconcile_tp_triggered(db)

    db.refresh(row)
    assert row.status == models.SESSION_TP_TRIGGERED
    rows = _actions(db, "tp_triggered_stranded", f"kss:{row.id}")
    assert len(rows) == 1


def test_active_session_is_untouched(db):
    row = _session(db, status=models.SESSION_ACTIVE)
    db.add(Position(symbol="EIGEN", quantity=0.0, avg_entry_price=0.0, total_cost=0.0))
    db.commit()

    service._reconcile_tp_triggered(db)

    db.refresh(row)
    assert row.status == models.SESSION_ACTIVE
    assert _actions(db, "tp_triggered_reconciled", f"kss:{row.id}") == []
    assert _actions(db, "tp_triggered_stranded", f"kss:{row.id}") == []


def test_manage_open_sessions_reconciles_even_with_no_active_sessions(db, monkeypatch):
    """The only session in the book can be the stranded TP_TRIGGERED one — manage_open_sessions
    must not skip the reconciliation just because the ACTIVE query came back empty (its early
    `if not active: return []` used to bypass everything below it, including the ladder
    watchdog)."""
    row = _session(db)
    db.add(Position(symbol="EIGEN", quantity=0.0, avg_entry_price=0.0, total_cost=0.0))
    db.commit()

    service.manage_open_sessions(db)

    db.refresh(row)
    assert row.status == models.SESSION_COMPLETED
