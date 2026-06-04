"""
OPUS scheduler loop — independent of app/scheduler.py (separate cadence/failures).

Each tick: manage existing positions (3h watch → ride/rescue), gate on the daily cost cap,
then ask Opus for intents and route them through the sandbox. Cost-bounded and fail-safe:
a bad tick never kills the loop, and nothing here can exceed a hard cap (see policy.py).
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy.orm import Session

from app.config import settings
from app.db import SessionLocal

log = logging.getLogger(__name__)

_task: asyncio.Task | None = None


def tick(db: Session) -> dict:
    """One OPUS decision cycle. Returns a small summary. Never raises."""
    from app import audit
    from app.orchestrator import brain, ledger, policy, service, watch

    if not settings.opus_mode:
        return {"skipped": "off"}

    # 1) Always manage open positions first (3h watch → ride/KSS-rescue), even when capped.
    watch_summary = watch.run(db)

    # 2) Cost-cap gate: pause NEW decisions when the daily Opus budget is spent.
    if service.cost_cap_reached(db):
        ledger.rollup_now(db)
        audit.log(db, "opus", "tick_capped", spend=round(service.spend_today(db), 4))
        return {"skipped": "cost_cap", "watch": watch_summary}

    if not brain.enabled():
        ledger.rollup_now(db)
        return {"skipped": "disabled", "watch": watch_summary}

    # 3) Cost-aware decision throttle (O-5): stretch the budget by spacing paid calls.
    #    Position management above already ran every tick — only the decision is throttled.
    from datetime import datetime

    from app import runtime
    last = runtime.get(db, "opus_last_decision_at")
    if last:
        try:
            elapsed_min = (datetime.utcnow() - datetime.fromisoformat(last)).total_seconds() / 60.0
        except ValueError:
            elapsed_min = 1e9
        if elapsed_min < service.decision_gap_min(db):
            ledger.rollup_now(db)
            return {"skipped": "throttled", "watch": watch_summary}

    # 4) Decide + route through the sandbox.
    decision = brain.decide(db)
    runtime.set(db, "opus_last_decision_at", datetime.utcnow().isoformat())
    applied = (
        policy.apply_intents(db, decision["intents"])
        if decision.get("ok")
        else {"executed": [], "rejected": []}
    )
    ledger.rollup_now(db)
    return {
        "intents": len(decision.get("intents", [])),
        "executed": len(applied.get("executed", [])),
        "rejected": len(applied.get("rejected", [])),
        "shadow": applied.get("shadow", settings.opus_shadow),
        "billed_cost": decision.get("billed_cost", 0.0),
        "watch": watch_summary,
    }


def _tick_once() -> None:
    db = SessionLocal()
    try:
        tick(db)
    finally:
        db.close()


async def _loop() -> None:
    log.info("OPUS loop started (every %s min)", settings.opus_interval_min)
    while True:
        try:
            await asyncio.to_thread(_tick_once)
        except Exception:  # a bad tick must not kill the loop
            log.exception("OPUS tick failed")
        await asyncio.sleep(max(settings.opus_interval_min, 1) * 60)


def start() -> bool:
    """Start the OPUS loop if not already running. Returns True if it started."""
    global _task
    if _task and not _task.done():
        return False
    _task = asyncio.create_task(_loop())
    return True


def stop() -> bool:
    global _task
    if _task and not _task.done():
        _task.cancel()
        _task = None
        return True
    _task = None
    return False


def is_running() -> bool:
    return bool(_task and not _task.done())
