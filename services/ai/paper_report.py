"""Paper trading performance report and live promotion gate."""

from datetime import datetime, timedelta
from typing import Optional

from src.findmy.config import settings
from ._db import read_connect
from .state import get_paper_start_date


def _compute_trade_metrics(cutoff: str) -> dict:
    """
    Query closed AI agent trades from the TS database for real win-rate/drawdown.
    Returns dict with win_rate, avg_daily_pct, max_drawdown_pct (all may be None if no data).
    """
    try:
        from services.ts.db import SessionLocal
        from services.ts.models import Trade, TradePnL
        from sqlalchemy.orm import joinedload

        session = SessionLocal()
        try:
            closed_trades = (
                session.query(Trade)
                .options(joinedload(Trade.pnl))
                .filter(
                    Trade.status == "CLOSED",
                    Trade.signal_source == "ai_agent",
                    Trade.exit_time >= cutoff,
                )
                .all()
            )

            if not closed_trades:
                return {"win_rate": None, "avg_daily_pct": None, "max_drawdown_pct": None}

            pnls = [t.pnl for t in closed_trades if t.pnl is not None]
            if not pnls:
                return {"win_rate": None, "avg_daily_pct": None, "max_drawdown_pct": None}

            wins = sum(1 for p in pnls if p.net_pnl > 0)
            win_rate = wins / len(pnls)

            avg_daily_pct = sum(p.return_pct for p in pnls) / len(pnls) if pnls else None
            max_drawdown_pct = max((p.max_drawdown or 0.0) for p in pnls) if pnls else None

            return {
                "win_rate": round(win_rate, 4),
                "avg_daily_pct": round(avg_daily_pct, 4) if avg_daily_pct is not None else None,
                "max_drawdown_pct": round(max_drawdown_pct, 4) if max_drawdown_pct is not None else None,
            }
        finally:
            session.close()
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"Trade metrics query failed: {e}")
        return {"win_rate": None, "avg_daily_pct": None, "max_drawdown_pct": None}


def get_paper_report(days: Optional[int] = None) -> dict:
    """
    Compute paper trading performance metrics for the AI agent.
    Uses ai_decision_log + pending_orders to calculate outcomes.
    """
    if days is None:
        days = settings.ai_paper_min_days

    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()

    with read_connect() as con:
        if con is None:
            return _empty_report(days)

        submitted = con.execute(
            "SELECT COUNT(*) FROM ai_decision_log WHERE action='ORDER_SUBMITTED' AND created_at >= ?",
            (cutoff,),
        ).fetchone()[0]

        skipped = con.execute(
            "SELECT COUNT(*) FROM ai_decision_log WHERE action='SKIPPED' AND created_at >= ?",
            (cutoff,),
        ).fetchone()[0]

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
    days_active = max(1, days)

    paper_start = get_paper_start_date()

    trade_metrics = _compute_trade_metrics(cutoff)

    report = {
        "period_days": days,
        "paper_start_date": paper_start,
        "orders_submitted": submitted,
        "orders_skipped": skipped,
        "total_approved_orders": total_orders,
        "avg_orders_per_day": round(submitted / days_active, 2),
        "estimated_win_rate": trade_metrics["win_rate"],
        "estimated_daily_pct": trade_metrics["avg_daily_pct"],
        "max_drawdown_pct": trade_metrics["max_drawdown_pct"],
        "note": (
            "Win rate computed from closed AI trades"
            if trade_metrics["win_rate"] is not None
            else "Win rate pending — no closed AI trades yet in this period"
        ),
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
    """
    Promote AI agent to live trading if eligible.
    Sets ai_agent_state.mode='live'. queue_ai_order() requires both
    settings.live_trading AND mode='live' to actually send to exchange,
    so this is a true runtime gate (no settings reload needed).
    """
    check = check_promotion_eligibility()
    if not check["eligible"]:
        return {"promoted": False, "reasons": check["reasons"]}

    from .state import set_mode
    set_mode("live")

    note = ""
    if not settings.live_trading:
        note = (
            " Note: settings.live_trading is currently False — orders will continue "
            "to run in paper mode. Set LIVE_TRADING=true and restart to enable real exchange execution."
        )

    return {
        "promoted": True,
        "message": "AI mode set to 'live'." + note,
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
        "note": "No AI DB connection — trade metrics unavailable",
    }
