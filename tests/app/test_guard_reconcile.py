"""C2: the 90s position-guard reconciles live orders before its exit checks.

`orders.reconcile_live_orders` had exactly one production call site — inside `run_cycle`,
every `scan_interval_min` (15 min by default). The 90s guard (`_guard_once`) therefore sized
hard-SL decisions off `total_filled_qty` that could be up to a whole scan interval stale while
rungs filled on the venue. `_guard_once` now reconciles first, inside the same `_work_lock`,
with any exception swallowed so a reconcile failure never blocks the guard's exit checks.

Fix round A / item 4: (a) a flush/commit-level reconcile failure (not just a bare exception)
leaves the SQLAlchemy session needing a rollback — without one, the guard's OWN first query
raises PendingRollbackError and the entire 90s exit tick dies, repeatedly. (b) the reconcile
pass is bounded by `GUARD_RECONCILE_MAX_ORDERS`: a large tracked-order backlog would otherwise
add tens of seconds of SERIAL fetch_order latency ahead of the hard-SL check under `_work_lock`.
"""

from __future__ import annotations

import pytest

from app import orders, scheduler
from app.kss import service
from app.models import PENDING, PendingOrder


def test_reconcile_runs_before_the_position_guard(db, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(orders, "reconcile_live_orders", lambda db: calls.append("reconcile") or [])
    monkeypatch.setattr(service, "run_position_guard", lambda db: calls.append("guard") or {})

    scheduler._guard_once()

    assert calls == ["reconcile", "guard"], "reconcile must run before the guard's exit checks"


def test_a_reconcile_exception_does_not_stop_the_guard(db, monkeypatch):
    def _boom(db):
        raise RuntimeError("exchange unreachable")

    guard_calls: list[str] = []
    monkeypatch.setattr(orders, "reconcile_live_orders", _boom)
    monkeypatch.setattr(service, "run_position_guard", lambda db: guard_calls.append("guard") or {})

    scheduler._guard_once()  # must not raise

    assert guard_calls == ["guard"], "the guard's exit checks must run regardless"


def test_paper_mode_still_invokes_reconcile_but_it_self_gates(db, monkeypatch):
    """The scheduler must not duplicate reconcile's own live_enabled() gate — it just calls
    it unconditionally and trusts the no-op. Spy on the REAL function (not a stub) so this
    proves reconcile is reached and returns [] on its own, in paper mode."""
    from app import execution

    monkeypatch.setattr(execution, "live_enabled", lambda: False)
    real = orders.reconcile_live_orders
    calls: list[str] = []

    def _spy(db):
        calls.append("reconcile")
        return real(db)

    monkeypatch.setattr(orders, "reconcile_live_orders", _spy)

    scheduler._guard_once()

    assert calls == ["reconcile"], "reconcile must still be invoked in paper mode"


def test_a_reconcile_flush_failure_is_rolled_back_so_the_guard_still_runs(db, monkeypatch):
    """A bare exception (the test above) never dirties the session — a REAL flush-level
    failure does: it leaves the SQLAlchemy session's transaction needing a rollback. Without
    one, `run_position_guard`'s own first query (left un-stubbed here on purpose) would itself
    raise PendingRollbackError and kill this call to `_guard_once` entirely."""
    def _boom(db_inner):
        db_inner.add(PendingOrder(symbol=None, side="BUY", order_type="MARKET",  # NOT NULL violation
                                  quantity=1.0, price=1.0, status=PENDING))
        db_inner.flush()  # raises IntegrityError, dirtying the session's transaction
        raise RuntimeError("unreachable")

    monkeypatch.setattr(orders, "reconcile_live_orders", _boom)

    scheduler._guard_once()  # must not raise — including from run_position_guard's own query


def test_reconcile_skipped_above_the_bound(db, monkeypatch):
    """GUARD_RECONCILE_MAX_ORDERS bounds the serial fetch_order loop ahead of the hard-SL
    check — above it, this tick skips reconcile (run_cycle's own reconcile still covers
    everything) rather than block the guard with tens of seconds of latency."""
    for i in range(scheduler.GUARD_RECONCILE_MAX_ORDERS + 1):
        db.add(PendingOrder(symbol="AAA", side="BUY", order_type="LIMIT", quantity=1.0,
                            price=1.0, status=PENDING, source="kss",
                            source_ref=f"pyramid:1:wave:{i}", exchange_order_id=f"X{i}",
                            exchange_status="open"))
    db.commit()
    calls: list[str] = []
    monkeypatch.setattr(orders, "reconcile_live_orders", lambda db: calls.append("reconcile") or [])
    monkeypatch.setattr(service, "run_position_guard", lambda db: calls.append("guard") or {})

    scheduler._guard_once()

    assert calls == ["guard"], "reconcile must be skipped above the bound; the guard must still run"


def test_reconcile_runs_at_or_below_the_bound(db, monkeypatch):
    for i in range(scheduler.GUARD_RECONCILE_MAX_ORDERS):
        db.add(PendingOrder(symbol="AAA", side="BUY", order_type="LIMIT", quantity=1.0,
                            price=1.0, status=PENDING, source="kss",
                            source_ref=f"pyramid:1:wave:{i}", exchange_order_id=f"X{i}",
                            exchange_status="open"))
    db.commit()
    calls: list[str] = []
    monkeypatch.setattr(orders, "reconcile_live_orders", lambda db: calls.append("reconcile") or [])
    monkeypatch.setattr(service, "run_position_guard", lambda db: calls.append("guard") or {})

    scheduler._guard_once()

    assert calls == ["reconcile", "guard"], "at/below the bound, reconcile must still run"


# --- 2026-09-03 hang hardening: _guard_once stamps its own liveness marker -------------------


def test_guard_once_stamps_last_guard_at_on_success(db, monkeypatch):
    """`/health`'s `guard_seconds_ago`/`stalled` read this — an outside watchdog needs proof
    the 90s guard actually completed a pass, not just that the thread is alive."""
    monkeypatch.setattr(orders, "reconcile_live_orders", lambda db: [])
    monkeypatch.setattr(service, "run_position_guard", lambda db: {})
    scheduler._last_guard_at = None

    scheduler._guard_once()

    assert scheduler._last_guard_at is not None


def test_guard_once_does_not_stamp_last_guard_at_when_the_guard_itself_raises(db, monkeypatch):
    """Mirrors `_last_cycle_at`'s "only on success" semantics — a raised exception must leave
    the marker stale so a genuinely wedged/broken guard is visible as stalled, not hidden."""
    monkeypatch.setattr(orders, "reconcile_live_orders", lambda db: [])

    def _boom(db):
        raise RuntimeError("guard exploded")

    monkeypatch.setattr(service, "run_position_guard", _boom)
    scheduler._last_guard_at = None

    with pytest.raises(RuntimeError):
        scheduler._guard_once()

    assert scheduler._last_guard_at is None
