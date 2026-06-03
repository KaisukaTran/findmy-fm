"""
Read-side views for the dashboard: positions, trade history, and summary.

These are pure reads derived from fills/positions plus live market prices.
Kept out of the route layer so routes stay thin.
"""

from __future__ import annotations

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import settings
from app.market import get_current_prices
from app.models import Fill, PendingOrder, Position


def positions_view(db: Session) -> list[dict]:
    """Open positions enriched with live price, market value and unrealized P&L."""
    positions = db.query(Position).filter(Position.quantity > 0).all()
    if not positions:
        return []
    prices = get_current_prices([p.symbol for p in positions])
    rows = []
    for p in positions:
        price = prices.get(p.symbol, 0.0)
        market_value = p.quantity * price
        unrealized = market_value - p.total_cost
        rows.append(
            {
                "symbol": p.symbol,
                "quantity": p.quantity,
                "avg_entry_price": p.avg_entry_price,
                "total_cost": p.total_cost,
                "current_price": price,
                "market_value": market_value,
                "unrealized_pnl": unrealized,
                "unrealized_pnl_pct": (unrealized / p.total_cost * 100) if p.total_cost else 0.0,
            }
        )
    return rows


def trades_view(db: Session, limit: int = 50) -> list[dict]:
    """Most recent fills (trade history)."""
    fills = db.query(Fill).order_by(Fill.executed_at.desc()).limit(limit).all()
    return [f.to_dict() for f in fills]


def summary_view(db: Session) -> dict:
    """Portfolio summary: equity, realized/unrealized P&L, counts."""
    positions = positions_view(db)
    total_market_value = sum(p["market_value"] for p in positions)
    total_invested = sum(p["total_cost"] for p in positions)
    unrealized_pnl = sum(p["unrealized_pnl"] for p in positions)

    realized_pnl = float(
        db.query(func.coalesce(func.sum(Fill.realized_pnl), 0.0)).scalar() or 0.0
    )
    total_trades = db.query(func.count(Fill.id)).scalar() or 0
    pending_count = (
        db.query(func.count(PendingOrder.id)).filter(PendingOrder.status == "pending").scalar() or 0
    )

    cash = settings.account_equity - total_invested + realized_pnl
    return {
        "total_trades": int(total_trades),
        "pending_count": int(pending_count),
        "positions_count": len(positions),
        "realized_pnl": realized_pnl,
        "unrealized_pnl": unrealized_pnl,
        "total_invested": total_invested,
        "total_market_value": total_market_value,
        "cash": cash,
        "total_equity": cash + total_market_value,
    }
