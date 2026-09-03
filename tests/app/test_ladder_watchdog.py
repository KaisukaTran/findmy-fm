"""Fix round A / item 7 — dead ladders never self-heal.

Rung queueing happens ONLY inside `handle_fill_event` (fill-driven) and manual
`queue_next_wave`. A session whose fill-time queue attempt was refused (insufficient fund at
the time, a transient error) is therefore dead FOREVER even after funds free up — verified
live: after a fund-restoring migration, two full cycles ran with the ladder still dead, zero
rungs queued.

`service._rearm_dead_ladders` (called once per `manage_open_sessions` pass) re-attempts the
next rung through the SAME guarded path a fill uses (`_queue_wave_if_above_sl`), for every
ACTIVE `dca_down` session with room left in `max_waves` and nothing already queued for the next
wave. Never touches `pyramid_up` or a session that already has a pending rung.
"""

from __future__ import annotations

from app import market
from app.kss import service
from app.models import (
    APPROVED,
    PENDING,
    SESSION_ACTIVE,
    AuditLog,
    KssSession,
    PendingOrder,
)


def _session(db, **kw) -> KssSession:
    defaults = {
        "symbol": "AAA", "entry_price": 100.0, "distance_pct": 2.0, "max_waves": 4,
        "isolated_fund": 1_000_000.0, "tp_pct": 50.0, "timeout_x_min": 43200.0, "gap_y_min": 0.0,
        "status": SESSION_ACTIVE, "current_wave": 0, "avg_price": 100.0,
        "total_filled_qty": 1.0, "total_cost": 100.0, "sl_pct": 0.0, "strategy_mode": "dca_down",
    }
    defaults.update(kw)
    row = KssSession(**defaults)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _wave1_cost(row: KssSession) -> float:
    """The real next-rung cost from the frozen ladder math, so the test's affordability numbers
    never depend on the live `kss_first_wave_usd` setting."""
    py = service._to_pyramid(row)
    w = py.generate_wave(row.current_wave + 1)
    return w.quantity * w.target_price


def _pending_waves(db, session_id: int):
    return (
        db.query(PendingOrder)
        .filter(PendingOrder.source_ref.like(f"pyramid:{session_id}:wave:%"))
        .all()
    )


def test_dead_ladder_with_funds_available_gets_rearmed(db, monkeypatch):
    monkeypatch.setattr(market, "get_current_prices", lambda syms, force=False: {})
    row = _session(db)
    row.isolated_fund = row.total_cost + _wave1_cost(row) * 2  # comfortably affordable
    db.commit()

    result = service.manage_open_sessions(db)

    assert result == []  # no TP/SL trigger — just the rearm
    waves = _pending_waves(db, row.id)
    assert len(waves) == 1, "the dead ladder must be re-armed with the next rung"
    assert waves[0].side == "BUY"
    audits = db.query(AuditLog).filter(AuditLog.action == "ladder_rearmed",
                                       AuditLog.entity == f"kss:{row.id}").all()
    assert len(audits) == 1


def test_dead_ladder_rearmed_only_once_across_passes(db, monkeypatch):
    """Once re-armed, the freshly-queued PENDING wave IS the reason the next pass leaves it
    alone — no double-queueing."""
    monkeypatch.setattr(market, "get_current_prices", lambda syms, force=False: {})
    row = _session(db)
    row.isolated_fund = row.total_cost + _wave1_cost(row) * 2
    db.commit()

    service.manage_open_sessions(db)
    service.manage_open_sessions(db)
    service.manage_open_sessions(db)

    assert len(_pending_waves(db, row.id)) == 1


def test_insufficient_funds_queues_nothing_and_does_not_spam_the_audit(db, monkeypatch):
    """The rung is genuinely unaffordable (isolated_fund exhausted) — nothing is queued, and
    the EXISTING insufficient_fund audit dedupe (per session+wave) must not grow across passes."""
    monkeypatch.setattr(market, "get_current_prices", lambda syms, force=False: {})
    row = _session(db)
    row.isolated_fund = row.total_cost + _wave1_cost(row) * 0.5  # not enough
    db.commit()

    service.manage_open_sessions(db)
    service.manage_open_sessions(db)
    service.manage_open_sessions(db)

    assert _pending_waves(db, row.id) == []
    audits = db.query(AuditLog).filter(AuditLog.action == "insufficient_fund",
                                       AuditLog.entity == f"kss:{row.id}").all()
    assert len(audits) == 1, "must not duplicate the insufficient_fund audit across passes"


def test_a_rung_that_would_sit_below_sl_is_skipped_via_the_existing_path(db, monkeypatch):
    """The below-SL skip lives in `_queue_wave_if_above_sl` (shared with the fill-driven
    chain) — the watchdog must route through it, not bypass it. sl_pct=1 -> floor 99; the
    anchored wave-1 price (98, no live price -> falls back to entry*(1-2%)) sits at/below it."""
    monkeypatch.setattr(market, "get_current_prices", lambda syms, force=False: {})
    row = _session(db, sl_pct=1.0)
    row.isolated_fund = row.total_cost + _wave1_cost(row) * 2  # affordable — SL must be the gate
    db.commit()

    service.manage_open_sessions(db)

    assert _pending_waves(db, row.id) == []
    audits = db.query(AuditLog).filter(AuditLog.action == "wave_below_sl",
                                       AuditLog.entity == f"kss:{row.id}").all()
    assert len(audits) == 1


def test_pyramid_up_session_is_never_touched(db, monkeypatch):
    monkeypatch.setattr(market, "get_current_prices", lambda syms, force=False: {})
    row = _session(db, strategy_mode="pyramid_up")

    service.manage_open_sessions(db)

    assert _pending_waves(db, row.id) == []
    assert db.query(AuditLog).filter(AuditLog.action == "ladder_rearmed").count() == 0


def test_a_session_with_an_already_pending_rung_is_left_alone(db, monkeypatch):
    monkeypatch.setattr(market, "get_current_prices", lambda syms, force=False: {})
    row = _session(db)
    db.add(PendingOrder(symbol="AAA", side="BUY", order_type="LIMIT", quantity=1.0, price=98.0,
                        status=PENDING, source="kss", source_ref=f"pyramid:{row.id}:wave:1"))
    db.commit()

    service.manage_open_sessions(db)

    assert len(_pending_waves(db, row.id)) == 1  # unchanged — still just the one already there
    assert db.query(AuditLog).filter(AuditLog.action == "ladder_rearmed").count() == 0


def test_an_approved_pending_rung_also_counts_as_alive(db, monkeypatch):
    monkeypatch.setattr(market, "get_current_prices", lambda syms, force=False: {})
    row = _session(db)
    db.add(PendingOrder(symbol="AAA", side="BUY", order_type="LIMIT", quantity=1.0, price=98.0,
                        status=APPROVED, source="kss", source_ref=f"pyramid:{row.id}:wave:1"))
    db.commit()

    service.manage_open_sessions(db)

    assert len(_pending_waves(db, row.id)) == 1
    assert db.query(AuditLog).filter(AuditLog.action == "ladder_rearmed").count() == 0


def test_a_full_ladder_is_never_rearmed(db, monkeypatch):
    monkeypatch.setattr(market, "get_current_prices", lambda syms, force=False: {})
    row = _session(db, current_wave=3, max_waves=4)

    service.manage_open_sessions(db)

    assert _pending_waves(db, row.id) == []
    assert db.query(AuditLog).filter(AuditLog.action == "ladder_rearmed").count() == 0


def test_watchdog_skips_a_trail_armed_session(db, monkeypatch):
    """An armed dynamic exit DELIBERATELY dropped its ladder (service: "committing to
    trail-up -> drop the DCA ladder") — the watchdog must treat that as intentional state,
    never as a dead ladder to re-arm (found live: NEAR#17, trail_active=1, rung cancelled by
    the arming path, watchdog then tried to queue wave 2)."""
    monkeypatch.setattr(market, "get_current_prices", lambda syms, force=False: {})
    row = _session(db, trail_active=True)
    row.isolated_fund = row.total_cost + _wave1_cost(row) * 2  # affordable — only the arm blocks
    db.commit()

    service._rearm_dead_ladders(db)

    assert _pending_waves(db, row.id) == []
    audits = db.query(AuditLog).filter(AuditLog.action == "ladder_rearmed").all()
    assert audits == []
