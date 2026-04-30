"""AI agent runner — manages the autonomous trading loop."""

import asyncio
import logging
from datetime import datetime
from typing import Optional

from src.findmy.config import settings
from .state import is_running, set_running, get_mode, set_last_action

logger = logging.getLogger(__name__)

# Default liquid symbols to scan; override via WATCHLIST env var or DB config
DEFAULT_WATCHLIST = ["BTC/USDT", "ETH/USDT", "BNB/USDT", "SOL/USDT", "XRP/USDT"]

_loop_task: Optional[asyncio.Task] = None


def _get_watchlist() -> list[str]:
    import os
    env = os.getenv("AI_WATCHLIST", "")
    if env:
        return [s.strip() for s in env.split(",") if s.strip()]
    return DEFAULT_WATCHLIST[: settings.ai_max_symbols]


async def _run_once() -> None:
    """Single iteration: analyze watchlist symbols and submit signals."""
    from services.sot.system_state import is_halted
    if is_halted():
        logger.info("AI agent: emergency halt active, skipping loop")
        set_last_action(f"{datetime.utcnow().isoformat()} HALT_SKIP")
        return

    from .agent import get_agent, submit_ai_order
    from .consultants.registry import get_enabled_consultants
    from .consensus import aggregate_votes

    agent = get_agent()
    consultants = get_enabled_consultants()
    watchlist = _get_watchlist()

    for symbol in watchlist:
        if not is_running():
            break
        try:
            signal = agent.analyze(symbol)
            logger.info(f"AI signal: {symbol} {signal.signal} conf={signal.confidence:.2f}")

            # Gather consultant votes if any
            votes = {}
            if consultants and signal.should_trade():
                for c in consultants:
                    try:
                        vote = c.vote(symbol, signal)
                        votes[c.name] = {"vote": vote.vote, "confidence": vote.confidence, "reasoning": vote.reasoning}
                    except Exception as e:
                        logger.warning(f"Consultant {c.name} error: {e}")

            # Consensus check: if consultants active, require majority agreement
            if votes and not aggregate_votes(signal, votes):
                logger.info(f"Consultants blocked signal for {symbol}: {votes}")
                set_last_action(f"{datetime.utcnow().isoformat()} CONSULTANT_BLOCKED {symbol}")
                continue

            order_id = submit_ai_order(signal, consultant_votes=votes)
            action = f"ORDER:{order_id}" if order_id else "SKIPPED"
            set_last_action(f"{datetime.utcnow().isoformat()} {symbol} {signal.signal} {action}")

        except Exception as e:
            logger.error(f"AI loop error for {symbol}: {e}")
            set_last_action(f"{datetime.utcnow().isoformat()} ERROR {symbol} {e}")


async def _loop() -> None:
    """Continuous agent loop until stopped."""
    logger.info("AI agent loop started")
    while is_running():
        await _run_once()
        interval = settings.ai_loop_interval_seconds
        # Sleep in chunks so we can respond to stop() quickly
        for _ in range(interval):
            if not is_running():
                break
            await asyncio.sleep(1)
    logger.info("AI agent loop stopped")


def start() -> bool:
    """Start the agent loop. Returns False if already running."""
    global _loop_task
    if is_running():
        return False
    set_running(True)
    loop = asyncio.get_event_loop()
    _loop_task = loop.create_task(_loop())
    logger.info("AI agent started")
    return True


def stop() -> bool:
    """Stop the agent loop. Returns False if not running."""
    global _loop_task
    if not is_running():
        return False
    set_running(False)
    if _loop_task and not _loop_task.done():
        _loop_task.cancel()
    _loop_task = None
    logger.info("AI agent stopped")
    return True


def get_status() -> dict:
    """Return current agent status for the API."""
    from .state import get_mode, get_paper_start_date, get_last_action
    from .decision_log import get_daily_ai_pnl
    from services.risk.risk_management import get_account_equity

    equity = 0.0
    try:
        equity = get_account_equity()
    except Exception:
        pass

    daily = get_daily_ai_pnl()
    target_usdt = equity * settings.ai_daily_target_pct / 100

    return {
        "running": is_running(),
        "mode": get_mode(),
        "paper_start_date": get_paper_start_date(),
        "last_action": get_last_action(),
        "today": {
            "orders_submitted": daily["orders_submitted"],
            "orders_skipped": daily["orders_skipped"],
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
