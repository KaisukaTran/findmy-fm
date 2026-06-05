"""
Read-side views for the dashboard: positions, trade history, and summary.

These are pure reads derived from fills/positions plus live market prices.
Kept out of the route layer so routes stay thin.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import settings
from app.market import get_current_prices
from app.models import Fill, PendingOrder, Position


def order_source(source_ref: str | None) -> str:
    """Provenance tag for a fill/order from its source_ref (OPUS / KSS / manual / auto)."""
    if not source_ref:
        return "manual"
    if source_ref.startswith("opus:"):
        return "OPUS"
    if source_ref.startswith("pyramid:"):
        return "KSS"
    return "auto"


def _symbol_owners(db: Session) -> dict[str, list[str]]:
    """Map each symbol to who currently manages it: OPUS (watch/ride) and/or KSS (active)."""
    from app.models import SESSION_ACTIVE, KssSession  # local import (avoid heavy coupling)
    from app.orchestrator.models import OPUS_RIDE, OPUS_WATCH, OpusPosition

    owners: dict[str, list[str]] = {}
    for (sym,) in db.query(OpusPosition.symbol).filter(
        OpusPosition.state.in_((OPUS_WATCH, OPUS_RIDE))
    ).distinct():
        owners.setdefault(sym, []).append("OPUS")
    for (sym,) in db.query(KssSession.symbol).filter(
        KssSession.status == SESSION_ACTIVE
    ).distinct():
        owners.setdefault(sym, []).append("KSS")
    return owners


def positions_view(db: Session) -> list[dict]:
    """Open positions enriched with live price, market value and unrealized P&L."""
    positions = db.query(Position).filter(Position.quantity > 0).all()
    if not positions:
        return []
    prices = get_current_prices([p.symbol for p in positions])
    owners = _symbol_owners(db)
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
                "sources": owners.get(p.symbol, []),  # ["OPUS"], ["KSS"], or both
            }
        )
    return rows


def trades_view(db: Session, limit: int = 50) -> list[dict]:
    """Most recent fills (trade history), tagged with their provenance (OPUS/KSS/…)."""
    fills = db.query(Fill).order_by(Fill.executed_at.desc()).limit(limit).all()
    out = []
    for f in fills:
        d = f.to_dict()
        d["source"] = order_source(f.source_ref)
        out.append(d)
    return out


def equity(db: Session) -> float:
    """Live mark-to-market equity = cash + open market value."""
    positions = positions_view(db)
    total_market_value = sum(p["market_value"] for p in positions)
    total_invested = sum(p["total_cost"] for p in positions)
    realized_pnl = float(
        db.query(func.coalesce(func.sum(Fill.realized_pnl), 0.0)).scalar() or 0.0
    )
    cash = settings.account_equity - total_invested + realized_pnl
    return cash + total_market_value


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


def performance_view(db: Session) -> dict:
    """
    Realized-equity curve + win/loss + max drawdown, derived from fills.

    Equity is account_equity + cumulative realized P&L stamped at each fill (a
    "realized equity" curve), with a final point including current unrealized P&L.
    Win/loss counts SELL fills by realized P&L sign.
    """
    fills = db.query(Fill).order_by(Fill.executed_at.asc()).all()
    equity = settings.account_equity
    now_iso = datetime.utcnow().isoformat()
    start_iso = fills[0].executed_at.isoformat() if fills else now_iso
    curve = [equity]
    times = [start_iso]
    realized = 0.0
    wins = losses = 0
    for f in fills:
        realized += f.realized_pnl
        curve.append(equity + realized)
        times.append(f.executed_at.isoformat() if f.executed_at else now_iso)
        if f.side == "SELL":
            if f.realized_pnl > 0:
                wins += 1
            elif f.realized_pnl < 0:
                losses += 1

    summary = summary_view(db)
    final_equity = summary["total_equity"]
    curve.append(final_equity)
    times.append(now_iso)

    # max drawdown (%) over the curve
    peak = curve[0]
    max_dd = 0.0
    for v in curve:
        peak = max(peak, v)
        if peak > 0:
            max_dd = max(max_dd, (peak - v) / peak * 100)

    closed = wins + losses
    return {
        "equity_curve": curve,
        "equity_times": times,
        "realized_pnl": realized,
        "unrealized_pnl": summary["unrealized_pnl"],
        "total_equity": final_equity,
        "wins": wins,
        "losses": losses,
        "win_rate": round(wins / closed * 100, 2) if closed else 0.0,
        "loss_rate": round(losses / closed * 100, 2) if closed else 0.0,
        "max_drawdown_pct": round(max_dd, 2),
    }
