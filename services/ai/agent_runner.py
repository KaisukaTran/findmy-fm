"""AI agent runner — manages the autonomous trading loop."""

import asyncio
import logging
import os
from datetime import datetime
from typing import Optional

from src.findmy.config import settings
from .state import (
    is_running, set_running, cas_set_running,
    get_mode, set_last_action, get_paper_start_date, get_last_action,
)

logger = logging.getLogger(__name__)

DEFAULT_WATCHLIST = ["BTC/USDT", "ETH/USDT", "BNB/USDT", "SOL/USDT", "XRP/USDT"]

_loop_task: Optional[asyncio.Task] = None
_start_lock = asyncio.Lock()


def _get_watchlist() -> list[str]:
    env = os.getenv("AI_WATCHLIST", "")
    if env:
        symbols = [s.strip() for s in env.split(",") if s.strip()]
    else:
        symbols = DEFAULT_WATCHLIST
    return symbols[: settings.ai_max_symbols]


def _check_dependencies() -> Optional[str]:
    """Return error string if AI agent cannot start, None if ready."""
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return "anthropic SDK not installed (pip install anthropic)"
    if settings.anthropic_api_key is None:
        return "ANTHROPIC_API_KEY not configured"
    return None


async def _run_once() -> None:
    """Single iteration: analyze watchlist symbols and submit signals."""
    from services.sot.system_state import is_halted
    from .agent import get_agent, submit_ai_order
    from .consultants.registry import get_enabled_consultants
    from .consensus import aggregate_votes

    if is_halted():
        logger.info("AI agent: emergency halt active, skipping loop")
        set_last_action(f"{datetime.utcnow().isoformat()} HALT_SKIP")
        return

    agent = get_agent()
    consultants = get_enabled_consultants()
    watchlist = _get_watchlist()

    for symbol in watchlist:
        if not is_running():
            break
        try:
            # Run blocking Anthropic SDK call in a thread to avoid blocking event loop
            signal = await asyncio.to_thread(agent.analyze, symbol)
            logger.info(f"AI signal: {symbol} {signal.signal} conf={signal.confidence:.2f}")

            votes = {}
            if consultants and signal.should_trade():
                for c in consultants:
                    try:
                        vote = await asyncio.to_thread(c.vote, symbol, signal)
                        votes[c.name] = {
                            "vote": vote.vote,
                            "confidence": vote.confidence,
                            "reasoning": vote.reasoning,
                        }
                    except Exception as e:
                        logger.warning(f"Consultant {c.name} error: {e}")

            if votes and not aggregate_votes(signal, votes):
                logger.info(f"Consultants blocked signal for {symbol}: {votes}")
                set_last_action(f"{datetime.utcnow().isoformat()} CONSULTANT_BLOCKED {symbol}")
                continue

            order_id = await asyncio.to_thread(submit_ai_order, signal, votes)
            action = f"ORDER:{order_id}" if order_id else "SKIPPED"
            set_last_action(f"{datetime.utcnow().isoformat()} {symbol} {signal.signal} {action}")

        except Exception as e:
            logger.error(f"AI loop error for {symbol}: {e}", exc_info=True)
            set_last_action(f"{datetime.utcnow().isoformat()} ERROR {symbol} {e}")


async def _loop() -> None:
    """Continuous agent loop until stopped."""
    logger.info("AI agent loop started")
    try:
        while is_running():
            try:
                await _run_once()
            except Exception as e:
                # Per-iteration safety net — never let a single failure kill the loop
                logger.error(f"AI loop iteration failed: {e}", exc_info=True)
                set_last_action(f"{datetime.utcnow().isoformat()} LOOP_ERROR {e}")

            interval = settings.ai_loop_interval_seconds
            for _ in range(interval):
                if not is_running():
                    break
                await asyncio.sleep(1)
    except asyncio.CancelledError:
        logger.info("AI agent loop cancelled")
        raise
    except Exception as e:
        logger.error(f"AI agent loop crashed: {e}", exc_info=True)
    finally:
        # Always clear the running flag so /api/ai/start can recover
        set_running(False)
        logger.info("AI agent loop stopped")


async def start() -> dict:
    """Start the agent loop. Returns {started, error?}."""
    global _loop_task
    err = _check_dependencies()
    if err:
        return {"started": False, "error": err}

    async with _start_lock:
        # Check task liveness first
        if _loop_task and not _loop_task.done():
            return {"started": False, "error": "AI agent is already running"}

        # CAS to flip the DB flag from false → true
        if not cas_set_running(expected=False, target=True):
            return {"started": False, "error": "AI agent is already running (state contention)"}

        _loop_task = asyncio.create_task(_loop())
        logger.info("AI agent started")
        return {"started": True}


async def stop() -> dict:
    """Stop the agent loop. Returns {stopped, error?}."""
    global _loop_task
    async with _start_lock:
        if not is_running() and (not _loop_task or _loop_task.done()):
            return {"stopped": False, "error": "AI agent is not running"}

        set_running(False)
        if _loop_task and not _loop_task.done():
            _loop_task.cancel()
            try:
                await asyncio.wait_for(_loop_task, timeout=10.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
        _loop_task = None
        logger.info("AI agent stopped")
        return {"stopped": True}


def get_status() -> dict:
    """Return current agent status for the API."""
    from .decision_log import get_daily_ai_pnl, sum_daily_ai_spend_usdt
    from services.risk.risk_management import get_account_equity

    equity = 0.0
    try:
        equity = get_account_equity()
    except Exception:
        pass

    daily = get_daily_ai_pnl()
    target_usdt = equity * settings.ai_daily_target_pct / 100
    spent = sum_daily_ai_spend_usdt()

    return {
        "running": is_running(),
        "mode": get_mode(),
        "paper_start_date": get_paper_start_date(),
        "last_action": get_last_action(),
        "today": {
            "orders_submitted": daily["orders_submitted"],
            "orders_skipped": daily["orders_skipped"],
            "spent_usdt": round(spent, 2),
            "target_usdt": round(target_usdt, 2),
            "target_pct": settings.ai_daily_target_pct,
        },
        "config": {
            "model": settings.ai_model,
            "confidence_threshold": settings.ai_confidence_threshold,
            "max_spend_usdt": settings.ai_max_spend_usdt,
            "loop_interval_seconds": settings.ai_loop_interval_seconds,
            "watchlist": _get_watchlist(),
        },
    }
