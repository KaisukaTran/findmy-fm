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

from app.clock import utcnow
from app.config import settings
from app.db import SessionLocal

log = logging.getLogger(__name__)

_task: asyncio.Task | None = None


def tick(db: Session) -> dict:
    """One OPUS decision cycle. Returns a small summary. Never raises."""
    from app import audit
    from app.orchestrator import brain, ledger, policy, report, service, watch

    if not settings.opus_mode:
        return {"skipped": "off"}

    # 1) Always manage open positions first (3h watch → ride/KSS-rescue), even when capped.
    watch_summary = watch.run(db)

    # 1b) Accountability (P4): daily Telegram report + rolling-7-day auto-freeze. Placed
    # right after watch so both run even when the cost cap below skips the paid decision —
    # accountability must not depend on budget. Each is self-throttled to once/UTC day and
    # never raises (mirrors watch/distill: a reporting bug can't sink the tick).
    report.maybe_daily_report(db)
    report.maybe_auto_freeze(db)

    # 2) Cost-cap gate: pause NEW decisions when the daily Opus budget is spent.
    if service.cost_cap_reached(db):
        ledger.rollup_now(db)
        audit.log(db, "opus", "tick_capped", spend=round(service.spend_today(db), 4))
        return {"skipped": "cost_cap", "watch": watch_summary}

    if not brain.enabled():
        ledger.rollup_now(db)
        return {"skipped": "disabled", "watch": watch_summary}

    # 2b) O-LEARN/L2: reflect on recent outcomes into a few distilled lessons. Its own 6h
    # runtime-timestamp throttle keeps this rare; never raises, so a bad distill can't sink
    # the tick. Placed after the cost-cap/enabled gates so it never runs while capped.
    from app.orchestrator import distill

    distill.distill_lessons(db)

    # 3) Cost-aware decision throttle (O-5): stretch the budget by spacing paid calls.
    #    Position management above already ran every tick — only the decision is throttled.
    from datetime import datetime

    from app import runtime
    last = runtime.get(db, "opus_last_decision_at")
    if last:
        try:
            elapsed_min = (utcnow() - datetime.fromisoformat(last)).total_seconds() / 60.0
        except ValueError:
            elapsed_min = 1e9
        if elapsed_min < service.decision_gap_min(db):
            ledger.rollup_now(db)
            return {"skipped": "throttled", "watch": watch_summary}

    # 3b) Haiku triage (P3): a cheap pre-screen before the paid decision, so most due ticks
    # never call the expensive brain at all. A "no" holds UNLESS the max-gap clock — measured
    # from the last FULL decision, never from a hold — has expired, so a triage bug/bias can't
    # starve the brain forever. opus_last_decision_at is NOT touched on a hold (that clock only
    # advances on a real decision below).
    if settings.opus_triage_enabled:
        from app.orchestrator import triage

        verdict = triage.assess(db)
        if not verdict.get("act", True):
            gap_elapsed_min = 1e9  # never decided yet → treat as fully due, let it through
            if last:
                try:
                    gap_elapsed_min = (utcnow() - datetime.fromisoformat(last)).total_seconds() / 60.0
                except ValueError:
                    gap_elapsed_min = 1e9
            if gap_elapsed_min < settings.opus_max_decision_gap_min:
                ledger.rollup_now(db)
                return {"skipped": "triage_hold", "watch": watch_summary}

    # 4) Decide + route through the sandbox. With Grok on, OPUS and Grok each decide on the
    #    SAME snapshot and consensus.combine() merges them (open=both agree, close=either).
    from app import audit
    from app.orchestrator import consensus, grok

    decision = brain.decide(db)
    billed = decision.get("billed_cost", 0.0)
    intents = decision["intents"] if decision.get("ok") else []
    if grok.enabled():
        g = grok.decide(db)
        billed += g.get("billed_cost", 0.0)
        merged = consensus.combine(intents, g["intents"] if g.get("ok") else [],
                                    solo_open=settings.opus_solo_open)
        audit.log(db, "consensus", "merge", **merged["stats"])
        intents = merged["intents"]
    runtime.set(db, "opus_last_decision_at", utcnow().isoformat())
    applied = policy.apply_intents(db, intents)
    ledger.rollup_now(db)
    return {
        "intents": len(intents),
        "executed": len(applied.get("executed", [])),
        "rejected": len(applied.get("rejected", [])),
        "shadow": applied.get("shadow", settings.opus_shadow),
        "billed_cost": billed,
        "grok": grok.enabled(),
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
