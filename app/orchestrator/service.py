"""
OPUS orchestrator service — capital envelope + state aggregation.

Phase O-0: read-only helpers (mode/allocation/spend/KPI). The KPI and deployed figures
are zero until later phases populate the tables. Capital is isolated: OPUS sees
`opus_allocation_usd`; the rule-based engine sees `equity - allocation` (disjoint envelopes).
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from app import portfolio
from app.config import settings
from app.orchestrator.models import (
    OPUS_CLOSED,
    OPUS_RIDE,
    OPUS_WATCH,
    OpusCostLedger,
    OpusMetricHourly,
    OpusPosition,
)


def allocation() -> float:
    """Capital envelope reserved for OPUS mode (the KPI denominator)."""
    return float(settings.opus_allocation_usd)


def rulebased_equity(db: Session) -> float:
    """Equity the rule-based engine may use = total MTM equity minus the OPUS envelope."""
    return max(0.0, portfolio.equity(db) - allocation())


def open_positions(db: Session) -> list[OpusPosition]:
    """All non-closed OPUS positions (watch/ride/rescue) — for display."""
    return (
        db.query(OpusPosition)
        .filter(OpusPosition.state != OPUS_CLOSED)
        .order_by(OpusPosition.opened_at.desc())
        .all()
    )


def managed_positions(db: Session) -> list[OpusPosition]:
    """Positions OPUS still actively manages (watch/ride). Rescued ones moved to KSS books."""
    return (
        db.query(OpusPosition)
        .filter(OpusPosition.state.in_((OPUS_WATCH, OPUS_RIDE)))
        .order_by(OpusPosition.opened_at.desc())
        .all()
    )


def deployed(db: Session) -> float:
    """USD under OPUS control (watch/ride only; rescued capital belongs to KSS)."""
    rows = managed_positions(db)
    return sum((p.qty or 0.0) * (p.avg_price or p.entry_price or 0.0) for p in rows)


def _utc_day_start() -> datetime:
    now = datetime.utcnow()
    return datetime(now.year, now.month, now.day)


def spend_today(db: Session) -> float:
    """Billed (x2) Opus cost accrued since 00:00 UTC today."""
    total = (
        db.query(func.coalesce(func.sum(OpusCostLedger.billed_cost), 0.0))
        .filter(OpusCostLedger.ts >= _utc_day_start())
        .scalar()
    )
    return float(total or 0.0)


def net_pnl_24h(db: Session) -> float:
    """Net profit over the last 24h from hourly rollups (gross - fees - billed Opus cost)."""
    since = datetime.utcnow() - timedelta(hours=24)
    total = (
        db.query(func.coalesce(func.sum(OpusMetricHourly.net_pnl), 0.0))
        .filter(OpusMetricHourly.hour_ts >= since)
        .scalar()
    )
    return float(total or 0.0)


def kpi_24h_pct(db: Session) -> float:
    """Rolling-24h net profit as a %% of the OPUS allocation (the 1%/24h KPI)."""
    alloc = allocation()
    if alloc <= 0:
        return 0.0
    return net_pnl_24h(db) / alloc * 100.0


def realized_pnl(db: Session) -> float:
    """Total realized P&L of OPUS-closed positions."""
    total = (
        db.query(func.coalesce(func.sum(OpusPosition.realized_pnl), 0.0)).scalar()
    )
    return float(total or 0.0)


def unrealized_pnl(db: Session) -> float:
    """Mark-to-market P&L of currently OPUS-managed (watch/ride) positions."""
    from app.market import get_current_prices

    rows = managed_positions(db)
    if not rows:
        return 0.0
    prices = get_current_prices(sorted({p.symbol for p in rows}))
    total = 0.0
    for p in rows:
        px = prices.get(p.symbol)
        if px:
            total += (px - (p.avg_price or p.entry_price or 0.0)) * (p.qty or 0.0)
    return total


def cost_cap_reached(db: Session) -> bool:
    return spend_today(db) >= settings.opus_daily_cost_cap_usd


def spend_ratio(db: Session) -> float:
    """Fraction of today's Opus budget already spent (0..1+)."""
    cap = settings.opus_daily_cost_cap_usd
    return (spend_today(db) / cap) if cap > 0 else 1.0


def behind_pace(db: Session) -> bool:
    """True when the rolling-24h KPI is below the target — Opus should turn MORE selective."""
    return kpi_24h_pct(db) < settings.opus_kpi_target_pct


def decision_gap_min(db: Session) -> float:
    """
    Minimum minutes between costly Opus decisions (O-5 cost-aware backoff). Position
    management (watch.py) is NOT throttled — only the paid decision call is. Stretch the
    remaining budget by doubling the gap once 70% of the daily cap is spent.
    """
    base = float(settings.opus_interval_min)
    return base * 2.0 if spend_ratio(db) >= 0.7 else base


def pacing(db: Session) -> dict:
    """Cost/KPI pacing signals fed to Opus so it self-regulates within the cage."""
    return {
        "kpi_pct": round(kpi_24h_pct(db), 3),
        "target_pct": settings.opus_kpi_target_pct,
        "behind_pace": behind_pace(db),
        "spend_ratio": round(spend_ratio(db), 3),
    }


def state(db: Session) -> dict:
    """Compact OPUS state for the API + dashboard."""
    alloc = allocation()
    dep = deployed(db)
    spent = spend_today(db)
    rpnl = realized_pnl(db)
    upnl = unrealized_pnl(db)
    total_pnl = rpnl + upnl - spent  # net of billed Opus cost
    return {
        "mode": settings.opus_mode,
        "shadow": settings.opus_shadow,
        "allocation_usd": alloc,
        "deployed_usd": dep,
        "free_usd": max(0.0, alloc - dep),
        "realized_pnl": rpnl,
        "unrealized_pnl": upnl,
        "total_pnl": total_pnl,
        "pnl_pct": (total_pnl / alloc * 100) if alloc > 0 else 0.0,
        "open_positions": len(managed_positions(db)),
        "spend_today_usd": spent,
        "daily_cost_cap_usd": settings.opus_daily_cost_cap_usd,
        "cost_cap_reached": spent >= settings.opus_daily_cost_cap_usd,
        "kpi_24h_pct": kpi_24h_pct(db),
        "kpi_target_pct": settings.opus_kpi_target_pct,
        "interval_min": settings.opus_interval_min,
        "grok_enabled": settings.grok_enabled,
        "grok_active": bool(settings.grok_enabled and settings.xai_api_key.get_secret_value()),
        "grok_role": settings.grok_role,
    }
