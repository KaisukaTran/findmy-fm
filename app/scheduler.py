"""
Background scheduler — drives autonomous operation.

Each cycle: close overdue sessions → check TP on open sessions → scan the
universe (auto-opens sessions in full-auto) → auto-fill KSS orders whose limit
the market reached (full-auto only). Off by default; toggled via settings /
the /api/scheduler endpoint. Everything it does is audit-logged downstream.

`run_cycle(db)` is the synchronous unit of work (unit-testable); the async loop
just calls it on an interval.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import threading

from sqlalchemy.orm import Session

from app import audit, orders, scanner
from app.config import settings
from app.db import SessionLocal
from app.kss import service

logger = logging.getLogger(__name__)

_task: asyncio.Task | None = None
_guard_task: asyncio.Task | None = None
# Serialize the 30-min cycle and the fast position-guard so only one DB writer runs at a time
# (prevents a guard exit racing manage_open_sessions on the same session → no double-sell).
_work_lock = threading.Lock()
_last_cycle_at: str | None = None
_last_summary: dict = {}

# Cross-process singleton lock. Two app processes each running this scan loop race
# scanner._can_open (separate DB transactions) and blow past max_concurrent_sessions
# (observed 8–9 active vs a cap of 5). A localhost-only socket is a process-wide mutex:
# only ONE process can bind it, and the OS frees it automatically on exit (no stale lock
# files). 8801 = 8000 (app) + a fixed offset reserved for this lock.
_SINGLETON_PORT = 8801
_lock_sock: socket.socket | None = None


def _acquire_singleton_lock(port: int = _SINGLETON_PORT) -> bool:
    """True if this process is the scheduler singleton; False if another already holds it.
    Idempotent: a process that already holds the lock returns True. (`port` is overridable
    for tests.)"""
    global _lock_sock
    if _lock_sock is not None:
        return True
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))  # no SO_REUSEADDR — a 2nd bind MUST fail
        s.listen(1)
    except OSError:
        s.close()
        return False
    _lock_sock = s
    return True


def _release_singleton_lock() -> None:
    global _lock_sock
    if _lock_sock is not None:
        _lock_sock.close()
        _lock_sock = None


def status() -> dict:
    """Lightweight scheduler status for the header badge / /api/automation."""
    return {
        "scheduler_running": is_running(),
        "interval_min": settings.scan_interval_min,
        "last_cycle_at": _last_cycle_at,
        "last_summary": _last_summary,
    }


def _run_periodic(db: Session) -> tuple[int, bool]:
    """Phase C: time-gated per-pair hyperopt + ML retrain. Never raises."""
    from datetime import datetime

    hyperopt_runs = 0
    ml_trained = False
    try:
        from app import hyperopt, ml, runtime
        now = datetime.utcnow()

        def _due(key: str, hours: float) -> bool:
            last = runtime.get(db, key)
            if not last:
                return True
            try:
                return (now - datetime.fromisoformat(last)).total_seconds() >= hours * 3600
            except ValueError:
                return True

        if settings.hyperopt_enabled and _due("hyperopt_last_at", settings.hyperopt_interval_hours):
            for sym in settings.watchlist:
                if hyperopt.run_for(db, sym) is not None:
                    hyperopt_runs += 1
            runtime.set(db, "hyperopt_last_at", now.isoformat())
        if settings.ml_enabled and _due("ml_last_at", settings.ml_retrain_hours):
            ml_trained = ml.train(db) is not None
            runtime.set(db, "ml_last_at", now.isoformat())
    except Exception:  # periodic tuning must never kill the cycle
        logger.exception("phase-c periodic tasks failed")
    return hyperopt_runs, ml_trained


def run_cycle(db: Session) -> dict:
    """One scheduler cycle. Returns a small summary (counts), not data dumps."""
    global _last_cycle_at, _last_summary
    from datetime import datetime, timedelta

    from app import circuit, guardian, notify
    from app.models import PENDING, PendingOrder
    # Live-only: book fills of resting maker orders the exchange filled since last cycle,
    # BEFORE TP/scan run so sessions/positions reflect reality. No-op on paper.
    reconciled = orders.reconcile_live_orders(db)
    closed = service.sweep_deadlines(db)
    tp = service.manage_open_sessions(db)
    service.manage_orphan_positions(db)  # TP/SL leftover positions no session/OPUS covers
    scan: dict
    try:
        scan = scanner.run_scan(db)
    except scanner.ScanInProgress:
        # A manual /api/scan is mid-flight; skip this cycle's scan rather than collide on the
        # SQLite writer. The rest of the cycle (TP, breaker, auto-fill) still runs.
        logger.info("run_cycle: scan already in progress, skipping scan this cycle")
        scan = {"scan_id": None, "candidates": []}
    breaker = circuit.evaluate(db)
    frozen = breaker["frozen"]

    # Veto TTL: expire stale Guardian vetoes so a transient veto can't permanently
    # deadlock a KSS DCA wave whose limit price has since been reached. Cleared orders
    # become auto-eligible again and are re-reviewed below (if the Guardian is on) — if
    # still unsafe they get re-vetoed with a fresh timestamp. Runs unconditionally
    # (even when frozen / Guardian off) so a stuck veto always drains. Legacy rows with
    # no timestamp are treated as already expired.
    veto_expired = 0
    ttl = settings.guardian_veto_ttl_min
    if ttl > 0:
        cutoff = datetime.utcnow() - timedelta(minutes=ttl)
        stale = (
            db.query(PendingOrder)
            .filter(
                PendingOrder.status == PENDING,
                PendingOrder.auto_veto == True,  # noqa: E712
                (PendingOrder.auto_veto_at == None) | (PendingOrder.auto_veto_at < cutoff),  # noqa: E711
            )
            .all()
        )
        for order in stale:
            order.auto_veto = False
            order.auto_veto_reason = None
            order.auto_veto_at = None
            audit.log(db, "guardian", "veto_expired", entity=f"order:{order.id}",
                      symbol=order.symbol)
            veto_expired += 1

    # Guardian review: veto any auto-eligible orders the LLM deems unsafe.
    guardian_vetoes = 0
    if not frozen and guardian.enabled():
        _eligible_sources = list(set(settings.autoapprove_sources) | {"kss"})
        pend = (
            db.query(PendingOrder)
            .filter(
                PendingOrder.status == PENDING,
                PendingOrder.auto_veto == False,  # noqa: E712
                PendingOrder.source.in_(_eligible_sources),
                # Guardian only screens NEW risk (BUYs). Exits (SELLs) reduce risk and must
                # never be vetoed — vetoing a take-profit/stop traps capital (drawdown).
                PendingOrder.side == "BUY",
            )
            .all()
        )
        if pend:
            vetoes = guardian.review(pend)
            for oid, reason in vetoes.items():
                order = db.get(PendingOrder, oid)
                if order is not None:
                    order.auto_veto = True
                    order.auto_veto_reason = reason
                    order.auto_veto_at = datetime.utcnow()
                    audit.log(db, "guardian", "veto", entity=f"order:{oid}", reason=reason)
                    notify.event("risk", f"⛔ Guardian vetoed order {oid} ({order.symbol}): {reason}")
                    guardian_vetoes += 1

    # Phase C: periodic per-pair hyperopt + ML retrain (time-gated, never blocks).
    hyperopt_runs, ml_trained = _run_periodic(db)

    # Defense-in-depth: short-circuit the auto branches when frozen. The callees
    # also self-guard, but gating here makes the breaker's intent explicit.
    filled = orders.auto_fill_due_orders(db) if settings.auto_trade and not frozen else []
    auto_approved = [] if frozen else orders.auto_approve_by_policy(db)  # self-guards on autoapprove_enabled
    audit.log(db, "scheduler", "cycle", deadlines_closed=len(closed), tp_queued=len(tp),
              candidates=len(scan["candidates"]), auto_filled=len(filled),
              auto_approved=len(auto_approved), reconciled=len(reconciled), frozen=frozen,
              guardian_vetoes=guardian_vetoes, veto_expired=veto_expired,
              hyperopt_runs=hyperopt_runs, ml_trained=ml_trained)
    db.commit()
    # Periodic Telegram digest (no-op unless telegram_digest_hours>0 and the interval elapsed).
    try:
        notify.maybe_send_digest(db)
    except Exception:
        logger.debug("maybe_send_digest failed")
    summary = {
        "deadlines_closed": closed,
        "tp_queued": tp,
        "scan_id": scan["scan_id"],
        "auto_filled": filled,
        "auto_approved": auto_approved,
        "reconciled": reconciled,
        "frozen": frozen,
        "guardian_vetoes": guardian_vetoes,
        "veto_expired": veto_expired,
        "hyperopt_runs": hyperopt_runs,
        "ml_trained": ml_trained,
    }
    _last_cycle_at = datetime.utcnow().isoformat()
    _last_summary = {k: (len(v) if isinstance(v, list) else v) for k, v in summary.items()}
    return summary


def _cycle_once() -> None:
    db = SessionLocal()
    try:
        with _work_lock:  # never run concurrently with the fast guard
            run_cycle(db)
    finally:
        db.close()


def _guard_once() -> None:
    db = SessionLocal()
    try:
        with _work_lock:  # serialize with the 30-min cycle
            service.run_position_guard(db)
    finally:
        db.close()


async def _loop() -> None:
    logger.info("scheduler started (every %s min)", settings.scan_interval_min)
    while True:
        try:
            # Offload the blocking, network-heavy cycle to a thread so the event
            # loop (and the API) stays responsive.
            await asyncio.to_thread(_cycle_once)
        except Exception:  # a bad cycle must not kill the loop
            logger.exception("scheduler cycle failed")
        await asyncio.sleep(max(settings.scan_interval_min, 1) * 60)


async def _guard_loop() -> None:
    """Fast, lightweight exit guard — decoupled from the 30-min cycle so a trailing stop is checked
    every ``kss_exit_check_sec`` (not every 30 min). No-op unless dynamic trailing is enabled."""
    logger.info("position-guard started (every %ss)", settings.kss_exit_check_sec)
    while True:
        try:
            if settings.kss_dynamic_tp_enabled:
                await asyncio.to_thread(_guard_once)
        except Exception:  # a bad guard tick must not kill the loop
            logger.exception("position-guard tick failed")
        await asyncio.sleep(max(settings.kss_exit_check_sec, 5))


def start() -> bool:
    """Start the background loop if not already running. Returns True if started.

    Refuses to start (returns False) when another app process already holds the
    singleton lock — only one process may run the scan loop, or the two race
    scanner._can_open and overshoot max_concurrent_sessions."""
    global _task
    if _task and not _task.done():
        return False
    if not _acquire_singleton_lock(settings.scheduler_lock_port):
        logger.warning(
            "scheduler NOT started — another app instance already holds the singleton lock "
            "(127.0.0.1:%d). Run a single process per lock port, or give a parallel instance a "
            "distinct scheduler_lock_port.", settings.scheduler_lock_port,
        )
        return False
    settings.scheduler_enabled = True
    _task = asyncio.create_task(_loop())
    global _guard_task
    if not (_guard_task and not _guard_task.done()):
        _guard_task = asyncio.create_task(_guard_loop())
    return True


def stop() -> bool:
    """Stop the background loop. Returns True if a running task was cancelled."""
    global _task, _guard_task
    settings.scheduler_enabled = False
    cancelled = False
    if _task and not _task.done():
        _task.cancel()
        cancelled = True
    _task = None
    if _guard_task and not _guard_task.done():
        _guard_task.cancel()
    _guard_task = None
    _release_singleton_lock()  # free the lock so the same process can restart cleanly
    return cancelled


def is_running() -> bool:
    return bool(_task and not _task.done())
