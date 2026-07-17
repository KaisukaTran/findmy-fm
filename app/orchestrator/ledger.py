"""
OPUS cost metering + hourly KPI rollup.

Cost truth (requirement #5): every Opus call's token cost is metered and **billed at
`opus_cost_multiplier` (×2)** before it counts against net profit. Net profit per hour =
realized PnL of OPUS positions closed that hour − billed Opus cost. (Paper realized_pnl is
already net of trade fees, so the `fees` column is informational and stays 0 to avoid
double-counting.)
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.clock import utcnow
from app.config import settings
from app.orchestrator import service
from app.orchestrator.models import (
    OPUS_OWN_PURPOSES,
    OpusCostLedger,
    OpusMetricHourly,
    OpusPosition,
)


def _hour_floor(ts: datetime) -> datetime:
    return datetime(ts.year, ts.month, ts.day, ts.hour)


def raw_cost(
    input_tokens: int,
    output_tokens: int,
    price_in: float | None = None,
    price_out: float | None = None,
    *,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> float:
    """Raw (unbilled) USD cost of one call. Cache read = 0.1x input price; cache write
    (5-minute ephemeral, the kind brain.py uses) = 1.25x input price — Anthropic's
    prompt-caching multipliers, applied on top of the plain input price."""
    pin = (settings.opus_price_in_per_mtok if price_in is None else price_in) / 1_000_000.0
    pout = (settings.opus_price_out_per_mtok if price_out is None else price_out) / 1_000_000.0
    cache_read_cost = cache_read_tokens * pin * 0.1
    cache_write_cost = cache_write_tokens * pin * 1.25
    return input_tokens * pin + output_tokens * pout + cache_read_cost + cache_write_cost


def record_cost(
    db: Session,
    input_tokens: int,
    output_tokens: int,
    *,
    purpose: str = "decision",
    request_id: str | None = None,
    price_in: float | None = None,
    price_out: float | None = None,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> OpusCostLedger:
    """Meter one agent call; billed = raw × multiplier (×2). Persists + returns the row.
    Pass price_in/out to meter a non-Opus agent (e.g. Grok) at its own price."""
    raw = raw_cost(
        input_tokens,
        output_tokens,
        price_in,
        price_out,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
    )
    billed = raw * settings.opus_cost_multiplier
    row = OpusCostLedger(
        input_tokens=int(input_tokens),
        output_tokens=int(output_tokens),
        cache_read_tokens=int(cache_read_tokens),
        cache_write_tokens=int(cache_write_tokens),
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
    hour = _hour_floor(hour or utcnow())
    nxt = hour + timedelta(hours=1)

    cost = (
        db.query(func.coalesce(func.sum(OpusCostLedger.billed_cost), 0.0))
        .filter(OpusCostLedger.ts >= hour, OpusCostLedger.ts < nxt)
        .filter(OpusCostLedger.purpose.in_(OPUS_OWN_PURPOSES))
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
    return rollup_hour(db, utcnow())


def metrics_series(db: Session, hours: int = 48) -> list[OpusMetricHourly]:
    """The last `hours` hourly rollups, oldest→newest (drives the chart)."""
    since = _hour_floor(utcnow()) - timedelta(hours=hours - 1)
    return (
        db.query(OpusMetricHourly)
        .filter(OpusMetricHourly.hour_ts >= since)
        .order_by(OpusMetricHourly.hour_ts)
        .all()
    )


def _day_bounds(day: date) -> tuple[datetime, datetime]:
    lo = datetime(day.year, day.month, day.day)
    return lo, lo + timedelta(days=1)


def engine_daily_pct(db: Session, day: date) -> float | None:
    """P5 benchmark: the rule-based engine's realized PnL that UTC day as a % of its
    CURRENT equity (equity now, not equity-that-day — an honest approximation, flagged in
    the UI tooltip). This is the "what if the capital had stayed with the engine" yardstick
    the trial gate compares OPUS against. None when the engine has no equity."""
    from app.models import Fill

    equity = service.rulebased_equity(db)
    if equity <= 0:
        return None
    lo, hi = _day_bounds(day)
    realized = (
        db.query(func.coalesce(func.sum(Fill.realized_pnl), 0.0))
        .filter(
            Fill.side == "SELL",
            Fill.strategy_name != "OPUS",
            Fill.executed_at >= lo,
            Fill.executed_at < hi,
        )
        .scalar()
    ) or 0.0
    return float(realized) / equity * 100.0


def rollup_day(db: Session, day: date) -> dict:
    """Aggregate one UTC calendar day's hourly rollups into a single dict (pure query — no
    new table; sums the already-computed `opus_metrics_hourly` rows for that day)."""
    lo, hi = _day_bounds(day)
    rows = (
        db.query(OpusMetricHourly)
        .filter(OpusMetricHourly.hour_ts >= lo, OpusMetricHourly.hour_ts < hi)
        .all()
    )
    gross = float(sum(r.gross_pnl or 0.0 for r in rows))
    cost = float(sum(r.opus_cost_billed or 0.0 for r in rows))
    net = gross - cost
    alloc = service.allocation()
    net_pct = (net / alloc * 100.0) if alloc > 0 else 0.0
    return {
        "day": day.isoformat(),
        "gross": gross,
        "cost": cost,
        "net": net,
        "net_pct": net_pct,
        "trades": sum(r.trades or 0 for r in rows),
        "win_trades": sum(r.win_trades or 0 for r in rows),
        "engine_pct": engine_daily_pct(db, day),
    }


def daily_series(db: Session, days: int = 14) -> list[dict]:
    """The last `days` UTC calendar days, oldest→newest (drives the OPUS daily table)."""
    today = utcnow().date()
    return [rollup_day(db, today - timedelta(days=i)) for i in range(days - 1, -1, -1)]


def target_per_hour() -> float:
    """USD/hour pace implied by the KPI (allocation × target% / 24)."""
    return service.allocation() * (settings.opus_kpi_target_pct / 100.0) / 24.0
