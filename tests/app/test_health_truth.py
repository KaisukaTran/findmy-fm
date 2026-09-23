"""2026-09-22 split-brain outage + 2026-09-23 cross-check follow-ups.

Original outage: `/health` said "ok" for ~13.7h while a process served :8001 with NO scheduler
running at all (it lost the singleton-lock race during a botched watchdog restart). The old
`stalled` computation only ever looked at loops that had run at least once — a loop that never
started read exactly like a fresh boot, forever.

Cross-check follow-ups pinned here too:
  - EVERY stall reason is gated on `app.scheduler.should_run()` — a deliberately stopped (or
    never-started) scheduler must never read as a stall, from ANY of the checks, including the
    older "ran once, then went quiet" ones.
  - `app.scheduler.should_run()` honours an explicit operator "Scheduler off" even when
    full_auto is still on (settings.scheduler_operator_stopped, persisted).
  - The "never completed a first pass" grace is measured from the SCHEDULER's own start time,
    not process start — turning full_auto on at runtime, long after boot, gets a fresh grace
    window instead of being judged against stale process uptime.
  - The outage-gap notice prints local time and never blocks startup on a slow Telegram.
"""

from __future__ import annotations

import asyncio
import socket
import threading
from datetime import timedelta

import pytest

from app import routes, runtime, scheduler
from app.clock import utcnow
from app.config import settings
from app.db import SessionLocal
from app.main import app as fastapi_app
from app.main import lifespan
from app.models import AuditLog

# --- shared fakes ------------------------------------------------------------------------


def _status(*, running: bool, cycle_at=None, guard_at=None, reconcile_at=None, started_at=None) -> dict:
    return {
        "scheduler_running": running,
        "started_at": started_at,
        "interval_min": settings.scan_interval_min,
        "last_cycle_at": cycle_at,
        "last_guard_at": guard_at,
        "last_reconcile_at": reconcile_at,
        "last_summary": {},
    }


def _ago(seconds: float) -> str:
    return (utcnow() - timedelta(seconds=seconds)).isoformat()


@pytest.fixture(autouse=True)
def _past_boot(monkeypatch):
    """Most cases in this file want to simulate "well past boot" — a process started 5s ago
    is comfortably past a grace of 0 and comfortably within a grace of an hour."""
    monkeypatch.setattr(routes, "_PROCESS_STARTED_AT", utcnow() - timedelta(seconds=5))
    yield


class _SyncThread:
    """Stand-in for `threading.Thread` that runs `target` synchronously on `.start()`.

    app.main's outage-gap notice fires `notify.event` on a real background thread on purpose
    (2026-09-23: a slow Telegram must never delay startup) — which makes its side effects land
    at an unpredictable time relative to the test. Swapping in this stand-in makes the dispatch
    deterministic without changing app.main's fire-and-forget code path at all.
    """

    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}

    def start(self) -> None:
        if self._target is not None:
            self._target(*self._args, **self._kwargs)

    def join(self, timeout=None) -> None:
        pass


@pytest.fixture
def sync_threads(monkeypatch):
    monkeypatch.setattr(threading, "Thread", _SyncThread)
    yield


# --- 1. /health truthfulness ----------------------------------------------------------------


def test_scheduler_should_run_but_isnt_after_grace_is_stalled(monkeypatch):
    monkeypatch.setattr(settings, "scheduler_enabled", True)
    monkeypatch.setattr(settings, "scheduler_not_running_grace_sec", 0.0)
    monkeypatch.setattr(scheduler, "status", lambda: _status(running=False))

    body = routes.health()

    assert body["stalled"] is True
    assert body["stall_reason"] == "scheduler_not_running"


def test_scheduler_not_running_within_grace_is_not_stalled(monkeypatch):
    monkeypatch.setattr(settings, "scheduler_enabled", True)
    monkeypatch.setattr(settings, "scheduler_not_running_grace_sec", 3600.0)
    monkeypatch.setattr(scheduler, "status", lambda: _status(running=False))

    body = routes.health()

    assert body["stalled"] is False
    assert body["stall_reason"] is None


def test_cycle_never_ran_after_grace_is_stalled(monkeypatch):
    """Grace is measured from the SCHEDULER's own start time (started_at), not process start."""
    monkeypatch.setattr(settings, "scheduler_enabled", True)
    monkeypatch.setattr(settings, "health_boot_grace_sec", 0.0)
    monkeypatch.setattr(scheduler, "status", lambda: _status(running=True, started_at=_ago(5)))

    body = routes.health()

    assert body["stalled"] is True
    assert body["stall_reason"] == "cycle_never_ran"


def test_scheduler_just_started_does_not_instantly_report_cycle_never_ran(monkeypatch):
    """2026-09-23 cross-check: turning full_auto on hours into an already-running process must
    get a fresh grace window from the scheduler's OWN start, not be judged against however long
    the process happened to already be up. Process uptime is pinned WAY past the grace here
    (the pre-fix bug: measuring from process start would have reported cycle_never_ran
    instantly) while the scheduler itself only just started."""
    monkeypatch.setattr(routes, "_PROCESS_STARTED_AT", utcnow() - timedelta(hours=5))
    monkeypatch.setattr(settings, "scheduler_enabled", True)
    monkeypatch.setattr(settings, "health_boot_grace_sec", 180.0)
    monkeypatch.setattr(scheduler, "status", lambda: _status(running=True, started_at=_ago(1)))

    body = routes.health()

    assert body["stalled"] is False
    assert body["stall_reason"] is None


def test_guard_never_ran_after_grace_is_stalled(monkeypatch):
    monkeypatch.setattr(settings, "scheduler_enabled", True)
    monkeypatch.setattr(settings, "health_boot_grace_sec", 0.0)
    monkeypatch.setattr(settings, "kss_exit_check_sec", 90)  # guard_should_run() True
    monkeypatch.setattr(
        scheduler, "status",
        lambda: _status(running=True, cycle_at=_ago(1), started_at=_ago(5)),
    )

    body = routes.health()

    assert body["stalled"] is True
    assert body["stall_reason"] == "guard_never_ran"


def test_guard_disabled_never_ran_is_not_a_stall(monkeypatch):
    """kss_exit_check_sec=0 is the deliberate off switch — a guard that never ran because it is
    turned off is not the same failure as one that should be running and isn't."""
    monkeypatch.setattr(settings, "scheduler_enabled", True)
    monkeypatch.setattr(settings, "health_boot_grace_sec", 0.0)
    monkeypatch.setattr(settings, "kss_exit_check_sec", 0)
    monkeypatch.setattr(
        scheduler, "status",
        lambda: _status(running=True, cycle_at=_ago(1), started_at=_ago(5)),
    )

    body = routes.health()

    assert body["stalled"] is False
    assert body["stall_reason"] is None


def test_flags_off_never_stalled_for_scheduler_reasons(monkeypatch):
    """Both scheduler_enabled and full_auto off (deliberate, no automation) — a scheduler that
    "isn't running" is exactly the intended state, never reported as a stall."""
    monkeypatch.setattr(settings, "scheduler_enabled", False)
    monkeypatch.setattr(settings, "full_auto", False)
    monkeypatch.setattr(settings, "scheduler_not_running_grace_sec", 0.0)
    monkeypatch.setattr(scheduler, "status", lambda: _status(running=False))

    body = routes.health()

    assert body["stalled"] is False
    assert body["stall_reason"] is None


def test_existing_cycle_stalled_reason_still_reported_when_should_run(monkeypatch):
    """The pre-existing "ran once, then went quiet" detection keeps working — gated on
    should_run(), same as every other reason (see the next test for the un-gated case)."""
    monkeypatch.setattr(settings, "scheduler_enabled", True)
    monkeypatch.setattr(settings, "scan_interval_min", 15)  # threshold = max(3*15*60, 900)=2700s
    monkeypatch.setattr(settings, "kss_exit_check_sec", 90)
    old = _ago(2701)
    fresh_guard = _ago(1)
    monkeypatch.setattr(
        scheduler, "status",
        lambda: _status(running=True, cycle_at=old, guard_at=fresh_guard, started_at=_ago(3000)),
    )

    body = routes.health()

    assert body["stalled"] is True
    assert body["stall_reason"] == "cycle_stalled"


def test_stale_cycle_timestamp_is_not_a_stall_when_scheduler_should_not_run(monkeypatch):
    """2026-09-23 cross-check: after full-auto/scheduler is turned OFF, the stamps from when it
    WAS running (last_cycle_at/last_guard_at) go stale by definition — that must never read as
    a fresh stall. This is the exact scenario the un-gated version of `cycle_stalled` missed."""
    monkeypatch.setattr(settings, "scheduler_enabled", False)
    monkeypatch.setattr(settings, "full_auto", False)
    monkeypatch.setattr(settings, "scan_interval_min", 15)
    old = _ago(2701)
    monkeypatch.setattr(scheduler, "status", lambda: _status(running=True, cycle_at=old))

    body = routes.health()

    assert body["stalled"] is False
    assert body["stall_reason"] is None


def test_stale_guard_timestamp_is_not_a_stall_when_scheduler_should_not_run(monkeypatch):
    monkeypatch.setattr(settings, "scheduler_enabled", False)
    monkeypatch.setattr(settings, "full_auto", False)
    monkeypatch.setattr(settings, "kss_exit_check_sec", 90)
    fresh = _ago(1)
    old_guard = _ago(901)
    monkeypatch.setattr(
        scheduler, "status", lambda: _status(running=True, cycle_at=fresh, guard_at=old_guard)
    )

    body = routes.health()

    assert body["stalled"] is False
    assert body["stall_reason"] is None


def test_reenable_after_long_stop_reads_not_stalled(monkeypatch):
    """Round-2 cross-check repro (ported from the scratch harness): scheduler ran, the operator
    stopped it, then re-enabled full_auto over 3h later (fresh started_at) — the first cycle of
    the NEW run has not landed yet. stop() clears started_at but leaves last_cycle_at/
    last_guard_at exactly where the PREVIOUS run left them (3h old); without `_valid_since`
    filtering those against the new started_at, that stale pair reads as "went quiet" the
    instant the scheduler comes back, and an external watchdog could kill the app mid its very
    first cycle after a legitimate restart."""
    monkeypatch.setattr(routes, "_PROCESS_STARTED_AT", utcnow() - timedelta(hours=5))
    monkeypatch.setattr(settings, "full_auto", True)
    monkeypatch.setattr(settings, "scheduler_operator_stopped", False)
    monkeypatch.setattr(scheduler, "_last_cycle_at", _ago(3 * 3600))
    monkeypatch.setattr(scheduler, "_last_guard_at", _ago(3 * 3600))
    monkeypatch.setattr(scheduler, "_started_at", utcnow().isoformat())
    monkeypatch.setattr(scheduler, "is_running", lambda: True)

    body = routes.health()

    assert body["stalled"] is False, body["stall_reason"]
    assert body["stall_reason"] is None


def test_stale_timestamp_older_than_started_at_is_treated_as_never_ran(monkeypatch):
    """A last_cycle_at from BEFORE the current started_at (the exact leftover `stop()` produces)
    must be discarded, not compared against the stall threshold — it becomes "never ran (yet)
    THIS run", gated by its own start-based grace, rather than "cycle_stalled"."""
    monkeypatch.setattr(settings, "scheduler_enabled", True)
    monkeypatch.setattr(settings, "health_boot_grace_sec", 0.0)
    started = _ago(10)
    stale_before_start = _ago(20)  # older than started_at — a leftover from a previous run
    monkeypatch.setattr(
        scheduler, "status",
        lambda: _status(running=True, cycle_at=stale_before_start, started_at=started),
    )

    body = routes.health()

    assert body["stalled"] is True
    assert body["stall_reason"] == "cycle_never_ran"
    # And NOT the raw, filtered-out timestamp — it must read as though it were never set.
    assert body["last_cycle_at"] is None


# --- 1b. scheduler.should_run() / the operator-stopped override -----------------------------


def test_should_run_reflects_scheduler_enabled_or_full_auto(monkeypatch):
    monkeypatch.setattr(settings, "scheduler_operator_stopped", False)
    monkeypatch.setattr(settings, "scheduler_enabled", False)
    monkeypatch.setattr(settings, "full_auto", False)
    assert scheduler.should_run() is False

    monkeypatch.setattr(settings, "full_auto", True)
    assert scheduler.should_run() is True


def test_operator_stopped_overrides_full_auto(monkeypatch):
    """The exact 2026-09-23 bug: full_auto on, but the operator independently stopped the
    scheduler toggle — should_run() must honour the more specific, more recent operator
    action, not the still-on master switch."""
    monkeypatch.setattr(settings, "full_auto", True)
    monkeypatch.setattr(settings, "scheduler_enabled", False)
    monkeypatch.setattr(settings, "scheduler_operator_stopped", True)

    assert scheduler.should_run() is False


def test_health_does_not_flag_scheduler_not_running_after_an_operator_stop(monkeypatch):
    """End to end: full_auto stays on, operator stopped the scheduler independently — /health
    must not report scheduler_not_running (which would make an external watchdog "fix" the
    operator's own choice)."""
    monkeypatch.setattr(settings, "full_auto", True)
    monkeypatch.setattr(settings, "scheduler_enabled", False)
    monkeypatch.setattr(settings, "scheduler_operator_stopped", True)
    monkeypatch.setattr(settings, "scheduler_not_running_grace_sec", 0.0)
    monkeypatch.setattr(scheduler, "status", lambda: _status(running=False))

    body = routes.health()

    assert body["stalled"] is False
    assert body["stall_reason"] is None


def test_set_scheduler_operator_stopped_persists_and_restores(db):
    """The flag must survive a restart (sync_from_db), or the operator's stop only lasts until
    the next reboot — the same defect KEY_AUTO_TRADE was added to fix, for a different toggle."""
    runtime.set_scheduler_operator_stopped(db, True)
    assert settings.scheduler_operator_stopped is True

    settings.scheduler_operator_stopped = False  # simulate a fresh process
    runtime.sync_from_db(db)
    assert settings.scheduler_operator_stopped is True

    runtime.set_scheduler_operator_stopped(db, False)
    settings.scheduler_operator_stopped = True
    runtime.sync_from_db(db)
    assert settings.scheduler_operator_stopped is False


def test_full_auto_on_clears_a_prior_operator_stop(db, monkeypatch):
    """Turning full-auto ON is a louder, more explicit start command than the standalone
    Scheduler toggle — it must clear an earlier independent stop, or should_run() would stay
    False right after the operator asked, in the loudest way available, for it to run."""
    monkeypatch.setattr(scheduler, "start", lambda: True)  # no real asyncio loop needed here
    runtime.set_scheduler_operator_stopped(db, True)
    assert settings.scheduler_operator_stopped is True

    runtime.full_auto_on(db)

    assert settings.scheduler_operator_stopped is False


def test_api_scheduler_off_persists_the_operator_stop_even_with_full_auto_on(monkeypatch):
    """The literal reported bug: POST /api/scheduler {enabled:false} must not leave
    should_run() true just because full_auto is still persisted on."""
    from fastapi.testclient import TestClient

    monkeypatch.setattr(settings, "full_auto", True)
    monkeypatch.setattr(scheduler, "start", lambda: True)
    monkeypatch.setattr(scheduler, "stop", lambda: True)

    with TestClient(fastapi_app) as c:
        r = c.post("/api/scheduler", json={"enabled": False})
        assert r.status_code == 200

    assert settings.scheduler_operator_stopped is True
    assert scheduler.should_run() is False


# --- 2. fail-fast on a lost singleton lock ---------------------------------------------------


def _boot_lifespan() -> None:
    async def _run():
        async with lifespan(fastapi_app):
            pass

    asyncio.run(_run())


def test_lifespan_raises_when_scheduler_should_run_but_lock_is_lost(monkeypatch):
    monkeypatch.setattr(settings, "scheduler_enabled", True)
    monkeypatch.setattr(settings, "scheduler_lock_fail_fast", True)
    monkeypatch.setattr(scheduler, "_lock_sock", None)

    # Hold the configured lock port ourselves, simulating another process owning it.
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    port = holder.getsockname()[1]
    monkeypatch.setattr(settings, "scheduler_lock_port", port)

    try:
        with pytest.raises(RuntimeError, match="singleton lock"):
            _boot_lifespan()
    finally:
        holder.close()
        scheduler.stop()


def test_lifespan_does_not_raise_when_fail_fast_disabled(monkeypatch):
    """False restores the old silent-twin behaviour deliberately — an operator opt-out, not
    the default."""
    monkeypatch.setattr(settings, "scheduler_enabled", True)
    monkeypatch.setattr(settings, "scheduler_lock_fail_fast", False)
    monkeypatch.setattr(scheduler, "_lock_sock", None)

    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    port = holder.getsockname()[1]
    monkeypatch.setattr(settings, "scheduler_lock_port", port)

    try:
        _boot_lifespan()  # must not raise
    finally:
        holder.close()
        scheduler.stop()


def test_lifespan_notifies_best_effort_before_raising(monkeypatch):
    monkeypatch.setattr(settings, "scheduler_enabled", True)
    monkeypatch.setattr(settings, "scheduler_lock_fail_fast", True)
    monkeypatch.setattr(scheduler, "_lock_sock", None)
    calls = []
    monkeypatch.setattr("app.notify.event", lambda kind, text, **kw: calls.append((kind, text)))

    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    port = holder.getsockname()[1]
    monkeypatch.setattr(settings, "scheduler_lock_port", port)

    try:
        with pytest.raises(RuntimeError):
            _boot_lifespan()
    finally:
        holder.close()
        scheduler.stop()

    assert len(calls) == 1
    assert calls[0][0] == "risk"


def test_lifespan_notify_failure_never_blocks_the_raise(monkeypatch):
    """A dead Telegram/Discord must never turn a fail-fast startup into a silent success."""
    monkeypatch.setattr(settings, "scheduler_enabled", True)
    monkeypatch.setattr(settings, "scheduler_lock_fail_fast", True)
    monkeypatch.setattr(scheduler, "_lock_sock", None)
    monkeypatch.setattr("app.notify.event", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("dead bot")))

    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    port = holder.getsockname()[1]
    monkeypatch.setattr(settings, "scheduler_lock_port", port)

    try:
        with pytest.raises(RuntimeError, match="singleton lock"):
            _boot_lifespan()
    finally:
        holder.close()
        scheduler.stop()


# --- 3. outage notice on a real gap ----------------------------------------------------------


def _seed_last_activity(gap: timedelta) -> None:
    db = SessionLocal()
    try:
        db.add(AuditLog(actor="scheduler", action="cycle", created_at=utcnow() - gap))
        db.commit()
    finally:
        db.close()


def test_outage_notice_fires_once_above_threshold(monkeypatch, sync_threads):
    monkeypatch.setattr(settings, "outage_notice_min", 10.0)
    monkeypatch.setattr(settings, "scheduler_enabled", False)
    monkeypatch.setattr(settings, "full_auto", False)
    _seed_last_activity(timedelta(hours=13, minutes=41))
    calls = []
    monkeypatch.setattr("app.notify.event", lambda kind, text, **kw: calls.append((kind, text)))

    _boot_lifespan()

    assert len(calls) == 1
    assert calls[0][0] == "risk"
    assert "13 giờ" in calls[0][1] and "41 phút" in calls[0][1]

    db = SessionLocal()
    try:
        row = (
            db.query(AuditLog)
            .filter_by(action="app_restarted_after_gap")
            .order_by(AuditLog.id.desc())
            .first()
        )
        assert row is not None
    finally:
        db.close()


def test_outage_notice_uses_local_time_for_the_last_activity_timestamp(monkeypatch, sync_threads):
    """2026-09-23 cross-check: the printed 'từ HH:MM' used to be raw UTC (created_at is stored
    naive-UTC) in a Vietnamese message — shift it to the display zone first."""
    from app import timefmt

    monkeypatch.setattr(settings, "outage_notice_min", 10.0)
    monkeypatch.setattr(settings, "scheduler_enabled", False)
    monkeypatch.setattr(settings, "full_auto", False)
    monkeypatch.setattr(settings, "tz_offset_hours", 7.0)
    last_utc = utcnow() - timedelta(hours=2)
    db = SessionLocal()
    try:
        db.add(AuditLog(actor="scheduler", action="cycle", created_at=last_utc))
        db.commit()
    finally:
        db.close()
    calls = []
    monkeypatch.setattr("app.notify.event", lambda kind, text, **kw: calls.append((kind, text)))

    _boot_lifespan()

    expected_local = timefmt.to_local(last_utc).strftime("%H:%M %d/%m")
    assert len(calls) == 1
    assert expected_local in calls[0][1]
    # And it must NOT be the raw UTC string (would only coincide by fluke at tz_offset 0).
    assert last_utc.strftime("%H:%M %d/%m") not in calls[0][1] or settings.tz_offset_hours == 0


def test_outage_notice_dispatch_does_not_block_startup(monkeypatch):
    """The notify call runs on a background thread (daemon=True), not inline — proven here by
    NOT installing the sync-thread stand-in: a real thread is started and `main.lifespan`
    returns without waiting for it (and without the target having necessarily run yet)."""
    monkeypatch.setattr(settings, "outage_notice_min", 10.0)
    monkeypatch.setattr(settings, "scheduler_enabled", False)
    monkeypatch.setattr(settings, "full_auto", False)
    _seed_last_activity(timedelta(hours=1))
    release = threading.Event()
    started = threading.Event()

    def _slow_notify(kind, text, **kw):
        started.set()
        release.wait(timeout=5)

    monkeypatch.setattr("app.notify.event", _slow_notify)

    _boot_lifespan()  # must return promptly even though _slow_notify is still blocked

    assert started.wait(timeout=2), "background notify never started"
    release.set()  # let the background thread finish so it does not leak past the test


def test_outage_notice_silent_below_threshold(monkeypatch, sync_threads):
    monkeypatch.setattr(settings, "outage_notice_min", 10.0)
    monkeypatch.setattr(settings, "scheduler_enabled", False)
    monkeypatch.setattr(settings, "full_auto", False)
    _seed_last_activity(timedelta(minutes=2))
    calls = []
    monkeypatch.setattr("app.notify.event", lambda kind, text, **kw: calls.append((kind, text)))

    _boot_lifespan()

    assert calls == []
    db = SessionLocal()
    try:
        row = db.query(AuditLog).filter_by(action="app_restarted_after_gap").first()
        assert row is None
    finally:
        db.close()


def test_outage_notice_silent_with_no_prior_activity(monkeypatch, sync_threads):
    """An empty audit_log (brand-new DB) has no gap to measure — must not notify or raise."""
    monkeypatch.setattr(settings, "outage_notice_min", 10.0)
    monkeypatch.setattr(settings, "scheduler_enabled", False)
    monkeypatch.setattr(settings, "full_auto", False)
    calls = []
    monkeypatch.setattr("app.notify.event", lambda kind, text, **kw: calls.append((kind, text)))

    _boot_lifespan()  # must not raise

    assert calls == []


def test_outage_notice_exception_is_swallowed(monkeypatch, sync_threads):
    monkeypatch.setattr(settings, "outage_notice_min", 10.0)
    monkeypatch.setattr(settings, "scheduler_enabled", False)
    monkeypatch.setattr(settings, "full_auto", False)
    _seed_last_activity(timedelta(hours=1))
    monkeypatch.setattr(
        "app.notify.event", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("notify down"))
    )

    _boot_lifespan()  # must not raise despite notify blowing up


def test_timestamp_helpers_never_raise_on_mixed_naive_and_aware_stamps():
    """Round-3 cross-check: comparing an aware stamp with naive utcnow() raised TypeError (not
    ValueError), which would 500 /health and make the watchdog restart a healthy app."""
    from app import routes

    aware = "2026-09-23T06:00:00+00:00"
    naive = "2026-09-23T05:00:00"
    assert routes._seconds_ago(aware) is None
    assert routes._valid_since(aware, naive) == aware
