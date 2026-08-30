"""The hard stop-loss is always-on safety, and a queued stop is queued once.

Two findings the KSS and risk audits both landed on.

`run_position_guard` is the 90-second net: the fast hard-SL, crash-detect, and the loop that
force-fills queued exits even while the breaker is frozen. Its own docstring calls the hard SL
"always-on safety, independent of the dynamic-TP toggle" — but the caller only ran it when
`kss_dynamic_tp_enabled` was true, and that flag defaults to False. So on a default install the
only exit check was the 30-minute cycle: exactly the sampling gap that once turned a -15% floor
into a -17.3% realised loss.

And `check_stop` never moved the session out of ACTIVE. The exit SELL is queued, but until it
FILLS the session still looks active and still fully filled, so every following cycle queued
ANOTHER full-size stop order for the same session. Duplicates are clamped on quantity when they
execute, but the fee is charged on the pre-clamp size, and a late-approved stale stop can
overwrite a COMPLETED session back to STOPPED.
"""

from __future__ import annotations

from app import models
from app.config import settings
from app.kss import service as kss
from app.models import PENDING, PendingOrder


def _session(db, **kw) -> models.KssSession:
    defaults = {
        "symbol": "SOL", "entry_price": 100.0, "distance_pct": 2.0, "max_waves": 3,
        "isolated_fund": 300.0, "tp_pct": 3.0, "timeout_x_min": 60, "gap_y_min": 5,
        "status": models.SESSION_ACTIVE, "current_wave": 1, "avg_price": 100.0,
        "total_filled_qty": 1.0, "total_cost": 100.0, "sl_pct": 8.0,
    }
    defaults.update(kw)
    row = models.KssSession(**defaults)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _stops(db, session_id: int) -> list[PendingOrder]:
    return [
        o for o in db.query(PendingOrder).filter(PendingOrder.status == PENDING).all()
        if str(o.source_ref or "").startswith(f"pyramid:{session_id}:")
        and str(o.source_ref).rsplit(":", 1)[-1] in {"sl", "trailing", "trail_sl"}
    ]


def test_a_stop_is_queued_once_not_every_cycle(db, monkeypatch):
    """The stop order is already waiting; re-queueing it every 30 minutes charges a fee on
    quantity that was never sold and can rewrite a finished session's history."""
    row = _session(db)
    monkeypatch.setattr("app.market.get_current_prices", lambda syms: {"SOL": 80.0})  # -20%

    kss.manage_open_sessions(db)
    first = len(_stops(db, row.id))
    kss.manage_open_sessions(db)
    kss.manage_open_sessions(db)

    assert first == 1, "the stop should trigger once"
    assert len(_stops(db, row.id)) == 1, "and must not be re-queued while it waits"


def test_a_session_with_an_exit_in_flight_is_recognised(db, monkeypatch):
    """The session stays ACTIVE until the SELL actually fills — it still holds the position —
    but it must be skipped while its exit is waiting, or the next cycle queues another."""
    row = _session(db)
    monkeypatch.setattr("app.market.get_current_prices", lambda syms: {"SOL": 80.0})
    assert kss._exit_in_flight(db, row.id) is False

    kss.manage_open_sessions(db)

    assert kss._exit_in_flight(db, row.id) is True
    db.refresh(row)
    assert row.status == models.SESSION_ACTIVE, "it still holds the position until the sell fills"


def test_a_session_without_a_stop_is_untouched(db, monkeypatch):
    row = _session(db)
    monkeypatch.setattr("app.market.get_current_prices", lambda syms: {"SOL": 99.0})  # -1%

    kss.manage_open_sessions(db)

    db.refresh(row)
    assert row.status == models.SESSION_ACTIVE
    assert _stops(db, row.id) == []


# --- the guard must not depend on an unrelated toggle -----------------------


def test_the_guard_runs_with_dynamic_tp_off(monkeypatch):
    """The fast hard-SL net is safety, not a feature of the trailing-TP experiment."""
    from app import scheduler

    monkeypatch.setattr(settings, "kss_dynamic_tp_enabled", False)

    assert scheduler.guard_should_run() is True


def test_the_guard_can_still_be_switched_off_deliberately(monkeypatch):
    from app import scheduler

    monkeypatch.setattr(settings, "kss_exit_check_sec", 0)

    assert scheduler.guard_should_run() is False


def test_the_fast_guard_does_not_queue_a_second_stop(db, monkeypatch):
    """The 30-min cycle queues the stop; the 90s guard then runs 20x before it fills. Without
    the in-flight check each tick queued another full-size stop — and turning the guard on by
    default (the other half of this commit) made that far more frequent."""
    row = _session(db)
    monkeypatch.setattr("app.market.get_current_prices", lambda syms: {"SOL": 80.0})
    kss.manage_open_sessions(db)
    assert len(_stops(db, row.id)) == 1

    kss._guard_hard_sl(db, row, 80.0)
    kss._guard_hard_sl(db, row, 79.0)
    db.commit()

    assert len(_stops(db, row.id)) == 1, "the guard must not duplicate a stop already waiting"


def test_the_deadline_sweep_does_not_queue_over_a_waiting_stop(db, monkeypatch):
    from datetime import timedelta

    from app.clock import utcnow

    row = _session(db)
    monkeypatch.setattr("app.market.get_current_prices", lambda syms: {"SOL": 80.0})
    kss.manage_open_sessions(db)
    row.deadline_at = utcnow() - timedelta(days=1)
    db.commit()

    kss.sweep_deadlines(db)

    assert len(_stops(db, row.id)) == 1, "a deadline sell must not stack on a waiting stop"
