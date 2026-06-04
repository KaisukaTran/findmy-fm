"""
OPUS cost metering + hourly KPI rollup.

Cost truth (requirement #5): every Opus call's token cost is metered and **billed at
`opus_cost_multiplier` (×2)** before it counts against net profit. Net profit per hour =
realized PnL of OPUS positions closed that hour − billed Opus cost. (Paper realized_pnl is
already net of trade fees, so the `fees` column is informational and stays 0 to avoid
double-counting.)
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import settings
from app.orchestrator import service
from app.orchestrator.models import OpusCostLedger, OpusMetricHourly, OpusPosition


def _hour_floor(ts: datetime) -> datetime:
    return datetime(ts.year, ts.month, ts.day, ts.hour)


def raw_cost(input_tokens: int, output_tokens: int) -> float:
    pin = settings.opus_price_in_per_mtok / 1_000_000.0
    pout = settings.opus_price_out_per_mtok / 1_000_000.0
    return input_tokens * pin + output_tokens * pout


def record_cost(
    db: Session,
    input_tokens: int,
    output_tokens: int,
    *,
    purpose: str = "decision",
    request_id: str | None = None,
) -> OpusCostLedger:
    """Meter one Opus call; billed = raw × multiplier (×2). Persists + returns the row."""
    raw = raw_cost(input_tokens, output_tokens)
    billed = raw * settings.opus_cost_multiplier
    row = OpusCostLedger(
        input_tokens=int(input_tokens),
        output_tokens=int(output_tokens),
        raw_cost=raw,
        billed_cost=billed,
        purpose=purpose,
        request_id=request_id,
    )
    db.add(row)
    db.commit()
    return row


def rollup_hour(db: Session, hour: datetime | None = None) -> OpusMetricHourly:
    """Aggregate one hour's OPUS cost + realized PnL into an OpusMetricHourly row (upsert)."""
    hour = _hour_floor(hour or datetime.utcnow())
    nxt = hour + timedelta(hours=1)

    cost = (
        db.query(func.coalesce(func.sum(OpusCostLedger.billed_cost), 0.0))
        .filter(OpusCostLedger.ts >= hour, OpusCostLedger.ts < nxt)
        .scalar()
    ) or 0.0

    closed = (
        db.query(OpusPosition)
        .filter(
            OpusPosition.closed_at.isnot(None),
            OpusPosition.closed_at >= hour,
            OpusPosition.closed_at < nxt,
        )
        .all()
    )
    gross = float(sum(p.realized_pnl or 0.0 for p in closed))  # already net of trade fees
    trades = len(closed)
    wins = sum(1 for p in closed if (p.realized_pnl or 0.0) > 0)

    net = gross - float(cost)
    alloc = service.allocation()
    net_pct = (net / alloc * 100.0) if alloc > 0 else 0.0

    row = (
        db.query(OpusMetricHourly).filter(OpusMetricHourly.hour_ts == hour).one_or_none()
    )
    if row is None:
        row = OpusMetricHourly(hour_ts=hour)
        db.add(row)
    row.gross_pnl = gross
    row.fees = 0.0
    row.opus_cost_billed = float(cost)
    row.net_pnl = net
    row.invested_capital = alloc
    row.net_pct = net_pct
    row.trades = trades
    row.win_trades = wins
    db.commit()
    return row


def rollup_now(db: Session) -> OpusMetricHourly:
    """Refresh the current hour's rollup (cheap; safe to call each tick / on view)."""
    return rollup_hour(db, datetime.utcnow())


def metrics_series(db: Session, hours: int = 48) -> list[OpusMetricHourly]:
    """The last `hours` hourly rollups, oldest→newest (drives the chart)."""
    since = _hour_floor(datetime.utcnow()) - timedelta(hours=hours - 1)
    return (
        db.query(OpusMetricHourly)
        .filter(OpusMetricHourly.hour_ts >= since)
        .order_by(OpusMetricHourly.hour_ts)
        .all()
    )


def target_per_hour() -> float:
    """USD/hour pace implied by the KPI (allocation × target% / 24)."""
    return service.allocation() * (settings.opus_kpi_target_pct / 100.0) / 24.0
