"""Circuit breaker for live/paper order execution.

Checks three thresholds before allowing an order to execute:
  1. Position size ≤ max_position_size_pct % of initial_fund
  2. Daily P&L > -max_daily_loss_pct % of initial_fund
  3. Orders-per-minute ≤ MAX_ORDERS_PER_MINUTE (default 10)

All thresholds are config-driven via src/findmy/config.settings.
"""

from __future__ import annotations

import logging
import sqlite3
import os
from datetime import datetime, timezone
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

MAX_ORDERS_PER_MINUTE: int = int(os.getenv("CB_MAX_ORDERS_PER_MINUTE", "10"))


def _db_path() -> str:
    url = (
        os.getenv("DATABASE_URL")
        or os.getenv("SOT_DATABASE_URL")
        or "sqlite:///./data/findmy_fm_paper.db"
    )
    return url[len("sqlite:///"):] if url.startswith("sqlite:///") else "./data/findmy_fm_paper.db"


def _conn() -> sqlite3.Connection:
    con = sqlite3.connect(_db_path())
    con.row_factory = sqlite3.Row
    return con


@dataclass
class CircuitBreakerResult:
    allowed: bool
    violations: list[str] = field(default_factory=list)


def check(symbol: str, quantity: float, price: float) -> CircuitBreakerResult:
    """
    Evaluate all circuit-breaker rules for an incoming order.

    Returns CircuitBreakerResult(allowed=True) when all rules pass,
    or allowed=False with a list of violation messages.
    """
    try:
        from src.findmy.config import settings
    except Exception:
        return CircuitBreakerResult(allowed=True)  # config unavailable, pass-through

    violations: list[str] = []
    order_value = quantity * price

    # ── Rule 1: Position size ─────────────────────────────────────────────
    max_order_usd = settings.initial_fund * settings.max_position_size_pct / 100.0
    if order_value > max_order_usd:
        violations.append(
            f"Position size ${order_value:.2f} exceeds limit "
            f"${max_order_usd:.2f} ({settings.max_position_size_pct}% of fund)"
        )

    # ── Rule 2: Daily loss limit ──────────────────────────────────────────
    try:
        today = datetime.now(timezone.utc).date().isoformat()
        with _conn() as con:
            row = con.execute("""
                SELECT COALESCE(SUM(tp.realized_pnl), 0.0) as daily_pnl
                FROM trade_pnl tp
                JOIN trades t ON t.id = tp.trade_id
                WHERE date(t.exit_time) = ?
            """, (today,)).fetchone()
        daily_pnl = float(row["daily_pnl"]) if row else 0.0
        max_loss = -abs(settings.initial_fund * settings.max_daily_loss_pct / 100.0)
        if daily_pnl < max_loss:
            violations.append(
                f"Daily P&L ${daily_pnl:.2f} exceeds max loss "
                f"${max_loss:.2f} ({settings.max_daily_loss_pct}% of fund)"
            )
    except Exception as e:
        logger.warning(f"Circuit breaker could not check daily P&L: {e}")

    # ── Rule 3: Order rate (orders per minute) ────────────────────────────
    try:
        with _conn() as con:
            row = con.execute("""
                SELECT COUNT(*) as cnt FROM pending_orders
                WHERE created_at >= datetime('now', '-1 minute')
                  AND status IN ('pending', 'approved')
            """).fetchone()
        order_rate = int(row["cnt"]) if row else 0
        if order_rate >= MAX_ORDERS_PER_MINUTE:
            violations.append(
                f"Order rate {order_rate}/min reached limit {MAX_ORDERS_PER_MINUTE}/min"
            )
    except Exception as e:
        logger.warning(f"Circuit breaker could not check order rate: {e}")

    if violations:
        for v in violations:
            logger.warning(f"CIRCUIT BREAKER: {v}")

    return CircuitBreakerResult(allowed=len(violations) == 0, violations=violations)
