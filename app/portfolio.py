"""
Read-side views for the dashboard: positions, trade history, and summary.

These are pure reads derived from fills/positions plus live market prices.
Kept out of the route layer so routes stay thin.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.clock import utcnow
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
    # Total equity (computed inline — calling equity() here would recurse into positions_view).
    total_mv = sum(p.quantity * prices.get(p.symbol, 0.0) for p in positions)
    total_invested = sum(p.total_cost for p in positions)
    realized = float(db.query(func.coalesce(func.sum(Fill.realized_pnl), 0.0)).scalar() or 0.0)
    equity = (settings.account_equity - total_invested + realized) + total_mv
    eq = equity or 1.0
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
                "market_value_pct": market_value / eq * 100,  # % of total equity
                "unrealized_pnl": unrealized,
                "unrealized_pnl_pct": (unrealized / p.total_cost * 100) if p.total_cost else 0.0,
                "sources": owners.get(p.symbol, []),  # ["OPUS"], ["KSS"], or both
            }
        )
    return rows


_LOSS_CAUSES = {
    "OPUS": "OPUS đóng vị thế lỗ (hard-stop hoặc quyết định của Opus)",
    "KSS-SL": "Cắt lỗ KSS: giá ≤ avg×(1−SL%)",
    "KSS-Trail": "Trailing KSS: giá rớt quá ngưỡng từ đỉnh sau khi đã có lãi",
    "KSS-TP?": "‘Chốt lời’ KSS nhưng LỖ — avg tổng của coin cao hơn giá TP của session "
               "(nhiều session cùng coin chung một vị thế tổng). Cần xem lại.",
    "Khác": "Không rõ nguồn / lệnh thủ công",
}


def _loss_tag(source_ref: str | None) -> str:
    if not source_ref:
        return "Khác"
    if source_ref.startswith("opus:"):
        return "OPUS"
    if source_ref.endswith(":sl"):
        return "KSS-SL"
    if source_ref.endswith(":trailing"):
        return "KSS-Trail"
    if source_ref.endswith(":tp"):
        return "KSS-TP?"
    return "Khác"


def loss_analysis(db: Session, limit: int = 300) -> dict:
    """Every losing fill with its cause, plus breakdowns by cause and by pair (for strategy
    improvement). Read-only; loss = realized_pnl < 0."""
    from app import timefmt

    losses = (
        db.query(Fill)
        .filter(Fill.realized_pnl < 0)
        .order_by(Fill.executed_at.desc())
        .limit(limit)
        .all()
    )
    rows, by_cause, by_pair = [], {}, {}
    for f in losses:
        tag = _loss_tag(f.source_ref)
        loss = float(f.realized_pnl or 0.0)
        rows.append({
            "time": timefmt.local_dt(f.executed_at),
            "symbol": f.symbol,
            "side": f.side,
            "quantity": f.quantity,
            "value": f.quantity * f.price,
            "loss": loss,
            "fee": float(f.fee or 0.0),
            "tag": tag,
            "reason": _LOSS_CAUSES.get(tag, tag),
            "source_ref": f.source_ref or "",
        })
        c = by_cause.setdefault(tag, {"count": 0, "total": 0.0})
        c["count"] += 1
        c["total"] += loss
        p = by_pair.setdefault(f.symbol, {"count": 0, "total": 0.0})
        p["count"] += 1
        p["total"] += loss
    total = sum(r["loss"] for r in rows)
    by_pair_sorted = sorted(by_pair.items(), key=lambda kv: kv[1]["total"])  # worst first
    return {
        "rows": rows,
        "count": len(rows),
        "total": total,
        "by_cause": by_cause,
        "by_pair": by_pair_sorted[:10],
    }


def trades_view(
    db: Session, limit: int = 50, offset: int = 0, side: str | None = None
) -> list[dict]:
    """Most recent fills (trade history), tagged with their provenance (OPUS/KSS/…).

    ``side`` filters to a single direction (``"BUY"``/``"SELL"``); ``None`` returns both."""
    q = db.query(Fill).order_by(Fill.executed_at.desc())
    if side in ("BUY", "SELL"):
        q = q.filter(Fill.side == side)
    fills = q.offset(offset).limit(limit).all()
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
    total_equity = cash + total_market_value
    base = settings.account_equity or 1.0  # % of starting capital for P&L
    eq = total_equity or 1.0
    return {
        "total_trades": int(total_trades),
        "pending_count": int(pending_count),
        "positions_count": len(positions),
        "realized_pnl": realized_pnl,
        "realized_pct": realized_pnl / base * 100,
        "unrealized_pnl": unrealized_pnl,
        "unrealized_pct": (unrealized_pnl / total_invested * 100) if total_invested else 0.0,
        "total_invested": total_invested,
        "total_market_value": total_market_value,
        "market_value_pct": total_market_value / eq * 100,
        "cash": cash,
        "cash_pct": cash / eq * 100,
        "total_equity": total_equity,
    }


# Performance period windows → lookback in hours (None = all-time).
_PERIODS: dict[str, int | None] = {"24h": 24, "7d": 24 * 7, "30d": 24 * 30, "all": None}


def _period_cutoff(period: str) -> datetime | None:
    """UTC cutoff for a period key, or None for all-time / unknown."""
    hours = _PERIODS.get(period)
    return utcnow() - timedelta(hours=hours) if hours else None


def performance_view(db: Session, period: str = "all") -> dict:
    """
    Realized-equity curve + win/loss + drawdown + expectancy, derived from fills.

    Equity is account_equity + cumulative realized P&L stamped at each fill (a
    "realized equity" curve), with a final point including current unrealized P&L.
    Win/loss counts SELL fills by realized P&L sign. When ``period`` restricts the
    window, the curve starts from the equity *as of* the cutoff (realized before it)
    so the line is continuous, and win/loss/expectancy reflect only the window.
    """
    all_fills = db.query(Fill).order_by(Fill.executed_at.asc()).all()
    cutoff = _period_cutoff(period)
    if cutoff is not None:
        before = [f for f in all_fills if f.executed_at and f.executed_at < cutoff]
        fills = [f for f in all_fills if not f.executed_at or f.executed_at >= cutoff]
        realized_before = sum(f.realized_pnl for f in before)
    else:
        fills = all_fills
        realized_before = 0.0

    base = settings.account_equity + realized_before
    now_iso = utcnow().isoformat()
    start_iso = fills[0].executed_at.isoformat() if fills else now_iso
    curve = [base]
    times = [start_iso]
    realized = 0.0
    wins = losses = 0
    win_sum = loss_sum = 0.0
    for f in fills:
        realized += f.realized_pnl
        curve.append(base + realized)
        times.append(f.executed_at.isoformat() if f.executed_at else now_iso)
        if f.side == "SELL":
            if f.realized_pnl > 0:
                wins += 1
                win_sum += f.realized_pnl
            elif f.realized_pnl < 0:
                losses += 1
                loss_sum += f.realized_pnl  # negative

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
    gross_loss = -loss_sum  # positive magnitude
    return {
        "period": period,
        "equity_curve": curve,
        "equity_times": times,
        "realized_pnl": realized,
        "unrealized_pnl": summary["unrealized_pnl"],
        "total_equity": final_equity,
        "wins": wins,
        "losses": losses,
        "closed": closed,
        "win_rate": round(wins / closed * 100, 2) if closed else 0.0,
        "loss_rate": round(losses / closed * 100, 2) if closed else 0.0,
        "max_drawdown_pct": round(max_dd, 2),
        # Per-closed-trade economics (USDT).
        "expectancy": round((win_sum + loss_sum) / closed, 2) if closed else 0.0,
        "avg_win": round(win_sum / wins, 2) if wins else 0.0,
        "avg_loss": round(loss_sum / losses, 2) if losses else 0.0,
        "profit_factor": round(win_sum / gross_loss, 2) if gross_loss > 0 else 0.0,
    }
