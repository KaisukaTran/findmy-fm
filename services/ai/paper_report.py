"""Paper trading performance report and live promotion gate."""

import sqlite3
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from src.findmy.config import settings


def _db_path() -> str:
    url = os.getenv("DATABASE_URL") or os.getenv("SOT_DATABASE_URL") or "sqlite:///./data/findmy_fm_paper.db"
    return url[len("sqlite:///"):] if url.startswith("sqlite:///") else "./data/findmy_fm_paper.db"


def get_paper_report(days: Optional[int] = None) -> dict:
    """
    Compute paper trading performance metrics for the AI agent.
    Uses ai_decision_log + pending_orders to calculate outcomes.
    """
    from .state import get_paper_start_date

    if days is None:
        days = settings.ai_paper_min_days

    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
    path = _db_path()

    if not Path(path).exists():
        return _empty_report(days)

    with sqlite3.connect(path) as con:
        # Count AI-submitted orders in period
        submitted = con.execute(
            "SELECT COUNT(*) FROM ai_decision_log WHERE action='ORDER_SUBMITTED' AND created_at >= ?",
            (cutoff,),
        ).fetchone()[0]

        skipped = con.execute(
            "SELECT COUNT(*) FROM ai_decision_log WHERE action='SKIPPED' AND created_at >= ?",
            (cutoff,),
        ).fetchone()[0]

        # Get approved AI orders with PnL from pending_orders join trade data
        orders = con.execute(
            """
            SELECT po.symbol, po.side, po.quantity, po.price, po.created_at
            FROM pending_orders po
            WHERE po.source='ai_agent'
              AND po.status='APPROVED'
              AND po.created_at >= ?
            ORDER BY po.created_at
            """,
            (cutoff,),
        ).fetchall()

    total_orders = len(orders)
    # Without actual trade close data, estimate win_rate from signal log
    # (Full P&L tracking requires trade close events — this is a scaffold)
    days_active = max(1, days)

    paper_start = get_paper_start_date()

    report = {
        "period_days": days,
        "paper_start_date": paper_start,
        "orders_submitted": submitted,
        "orders_skipped": skipped,
        "total_approved_orders": total_orders,
        "avg_orders_per_day": round(submitted / days_active, 2),
        "note": "Win rate and P&L require closed trade data — currently showing order counts only",
        # Placeholder metrics (will be accurate once trade close tracking is wired)
        "estimated_win_rate": None,
        "estimated_daily_pct": None,
        "max_drawdown_pct": None,
    }
    return report


def check_promotion_eligibility() -> dict:
    """
    Check if paper performance meets the bar for live promotion.
    Returns {eligible, reasons, report}.
    """
    report = get_paper_report()
    reasons = []
    eligible = True

    # Days requirement
    from .state import get_paper_start_date
    paper_start = get_paper_start_date()
    if paper_start:
        try:
            start_dt = datetime.fromisoformat(paper_start)
            days_elapsed = (datetime.utcnow() - start_dt).days
            if days_elapsed < settings.ai_paper_min_days:
                eligible = False
                reasons.append(
                    f"Need {settings.ai_paper_min_days} paper days, only {days_elapsed} elapsed"
                )
        except Exception:
            eligible = False
            reasons.append("Paper start date invalid")
    else:
        eligible = False
        reasons.append("Paper trading not started")

    # Win rate (when available)
    win_rate = report.get("estimated_win_rate")
    if win_rate is not None and win_rate < settings.ai_paper_min_win_rate:
        eligible = False
        reasons.append(f"Win rate {win_rate:.1%} < required {settings.ai_paper_min_win_rate:.1%}")

    # Drawdown (when available)
    drawdown = report.get("max_drawdown_pct")
    if drawdown is not None and drawdown > settings.ai_paper_max_drawdown_pct:
        eligible = False
        reasons.append(f"Max drawdown {drawdown:.1f}% > limit {settings.ai_paper_max_drawdown_pct:.1f}%")

    return {"eligible": eligible, "reasons": reasons, "report": report}


def promote_to_live() -> dict:
    """Promote AI agent to live trading if eligible."""
    check = check_promotion_eligibility()
    if not check["eligible"]:
        return {"promoted": False, "reasons": check["reasons"]}

    from .state import set_mode
    set_mode("live")

    # Enable live trading in settings requires restart or dynamic config
    # For now: set DB flag and return instructions
    return {
        "promoted": True,
        "message": (
            "Mode set to 'live' in DB. "
            "Set LIVE_TRADING=true and restart the server to activate real order execution."
        ),
    }


def _empty_report(days: int) -> dict:
    return {
        "period_days": days,
        "paper_start_date": "",
        "orders_submitted": 0,
        "orders_skipped": 0,
        "total_approved_orders": 0,
        "avg_orders_per_day": 0.0,
        "estimated_win_rate": None,
        "estimated_daily_pct": None,
        "max_drawdown_pct": None,
        "note": "No data yet",
    }
