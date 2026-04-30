"""AI decision audit log — persists every AI analysis and action to DB."""

import sqlite3
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Optional


def _db_path() -> str:
    url = os.getenv("DATABASE_URL") or os.getenv("SOT_DATABASE_URL") or "sqlite:///./data/findmy_fm_paper.db"
    return url[len("sqlite:///"):] if url.startswith("sqlite:///") else "./data/findmy_fm_paper.db"


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
    path = _db_path()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as con:
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
        con.commit()
        return cur.lastrowid


def get_decisions(limit: int = 50, symbol: Optional[str] = None) -> list[dict]:
    """Fetch recent AI decisions."""
    path = _db_path()
    if not Path(path).exists():
        return []
    with sqlite3.connect(path) as con:
        con.row_factory = sqlite3.Row
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
    """Aggregate AI decision outcomes for a given date (YYYY-MM-DD)."""
    if date_str is None:
        date_str = datetime.utcnow().strftime("%Y-%m-%d")
    path = _db_path()
    if not Path(path).exists():
        return {"date": date_str, "orders_submitted": 0, "orders_skipped": 0}
    with sqlite3.connect(path) as con:
        submitted = con.execute(
            "SELECT COUNT(*) FROM ai_decision_log WHERE action='ORDER_SUBMITTED' AND created_at LIKE ?",
            (f"{date_str}%",),
        ).fetchone()[0]
        skipped = con.execute(
            "SELECT COUNT(*) FROM ai_decision_log WHERE action='SKIPPED' AND created_at LIKE ?",
            (f"{date_str}%",),
        ).fetchone()[0]
    return {"date": date_str, "orders_submitted": submitted, "orders_skipped": skipped}
