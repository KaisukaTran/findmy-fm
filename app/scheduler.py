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

from sqlalchemy.orm import Session

from app import audit, orders, scanner
from app.config import settings
from app.db import SessionLocal
from app.kss import service

logger = logging.getLogger(__name__)

_task: asyncio.Task | None = None
_last_cycle_at: str | None = None
_last_summary: dict = {}


def status() -> dict:
    """Lightweight scheduler status for the header badge / /api/automation."""
    return {
        "scheduler_running": is_running(),
        "interval_min": settings.scan_interval_min,
        "last_cycle_at": _last_cycle_at,
        "last_summary": _last_summary,
    }


def run_cycle(db: Session) -> dict:
    """One scheduler cycle. Returns a small summary (counts), not data dumps."""
    global _last_cycle_at, _last_summary
    from datetime import datetime

    from app import circuit
    closed = service.sweep_deadlines(db)
    tp = service.manage_open_sessions(db)
    scan = scanner.run_scan(db)
    breaker = circuit.evaluate(db)
    frozen = breaker["frozen"]
    # Defense-in-depth: short-circuit the auto branches when frozen. The callees
    # also self-guard, but gating here makes the breaker's intent explicit.
    filled = orders.auto_fill_due_orders(db) if settings.auto_trade and not frozen else []
    auto_approved = [] if frozen else orders.auto_approve_by_policy(db)  # self-guards on autoapprove_enabled
    audit.log(db, "scheduler", "cycle", deadlines_closed=len(closed), tp_queued=len(tp),
              candidates=len(scan["candidates"]), auto_filled=len(filled),
              auto_approved=len(auto_approved), frozen=frozen)
    db.commit()
    summary = {
        "deadlines_closed": closed,
        "tp_queued": tp,
        "scan_id": scan["scan_id"],
        "auto_filled": filled,
        "auto_approved": auto_approved,
        "frozen": frozen,
    }
    _last_cycle_at = datetime.utcnow().isoformat()
    _last_summary = {k: (len(v) if isinstance(v, list) else v) for k, v in summary.items()}
    return summary


def _cycle_once() -> None:
    db = SessionLocal()
    try:
        run_cycle(db)
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


def start() -> bool:
    """Start the background loop if not already running. Returns True if started."""
    global _task
    if _task and not _task.done():
        return False
    settings.scheduler_enabled = True
    _task = asyncio.create_task(_loop())
    return True


def stop() -> bool:
    """Stop the background loop. Returns True if a running task was cancelled."""
    global _task
    settings.scheduler_enabled = False
    if _task and not _task.done():
        _task.cancel()
        _task = None
        return True
    _task = None
    return False


def is_running() -> bool:
    return bool(_task and not _task.done())
