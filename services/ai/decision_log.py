"""AI decision audit log — persists every AI analysis and action to DB."""

import json
from datetime import datetime
from typing import Optional

from ._db import connect, read_connect


def log_decision(
    symbol: str,
    signal: str,
    confidence: float,
    reasoning: str,
    action: str,
    pending_order_id: Optional[int] = None,
    consultant_votes: Optional[dict] = None,
    market_context: Optional[dict] = None,
) -> int:
    """Insert one AI decision record. Returns inserted row id."""
    with connect() as con:
        cur = con.execute(
            """
            INSERT INTO ai_decision_log
                (symbol, signal, confidence, reasoning, action,
                 pending_order_id, consultant_votes, market_context, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                symbol, signal, round(confidence, 4), reasoning, action,
                pending_order_id,
                json.dumps(consultant_votes or {}),
                json.dumps(market_context or {}),
                datetime.utcnow().isoformat(),
            ),
        )
        return cur.lastrowid


def log_trade_close(
    trade_id: int,
    symbol: str,
    side: str,
    entry_price: float,
    exit_price: float,
    net_pnl: float,
    return_pct: float,
    pending_order_id: Optional[int] = None,
) -> int:
    """Record trade close event in ai_decision_log for paper report computation."""
    action = f"TRADE_CLOSED:{'WIN' if net_pnl > 0 else 'LOSS'}:{trade_id}"
    reasoning = (
        f"Trade {trade_id} closed: {side} {symbol} "
        f"entry={entry_price:.4f} exit={exit_price:.4f} "
        f"net_pnl={net_pnl:.4f} return={return_pct:.2f}%"
    )
    return log_decision(
        symbol=symbol,
        signal=side,
        confidence=1.0,
        reasoning=reasoning,
        action=action,
        pending_order_id=pending_order_id,
        market_context={"trade_id": trade_id, "net_pnl": net_pnl, "return_pct": return_pct},
    )


def get_decisions(limit: int = 50, symbol: Optional[str] = None) -> list[dict]:
    with read_connect() as con:
        if con is None:
            return []
        if symbol:
            rows = con.execute(
                "SELECT * FROM ai_decision_log WHERE symbol=? ORDER BY created_at DESC LIMIT ?",
                (symbol, limit),
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT * FROM ai_decision_log ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]


def get_daily_ai_pnl(date_str: Optional[str] = None) -> dict:
    if date_str is None:
        date_str = datetime.utcnow().strftime("%Y-%m-%d")
    with read_connect() as con:
        if con is None:
            return {"date": date_str, "orders_submitted": 0, "orders_skipped": 0}
        submitted = con.execute(
            "SELECT COUNT(*) FROM ai_decision_log WHERE action='ORDER_SUBMITTED' AND created_at LIKE ?",
            (f"{date_str}%",),
        ).fetchone()[0]
        skipped = con.execute(
            "SELECT COUNT(*) FROM ai_decision_log WHERE action='SKIPPED' AND created_at LIKE ?",
            (f"{date_str}%",),
        ).fetchone()[0]
    return {"date": date_str, "orders_submitted": submitted, "orders_skipped": skipped}


def sum_daily_ai_spend_usdt(date_str: Optional[str] = None) -> float:
    """Sum approved AI orders' notional value (qty * price) for the given day."""
    if date_str is None:
        date_str = datetime.utcnow().strftime("%Y-%m-%d")
    with read_connect() as con:
        if con is None:
            return 0.0
        # Status names use SQLAlchemy Enum default (uppercase)
        row = con.execute(
            """
            SELECT COALESCE(SUM(quantity * price), 0)
            FROM pending_orders
            WHERE source = 'ai_agent'
              AND status = 'APPROVED'
              AND created_at LIKE ?
            """,
            (f"{date_str}%",),
        ).fetchone()
        return float(row[0] or 0.0)
