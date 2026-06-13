"""
Realized-PnL calendar aggregation (Phase 4).

Groups executed ``Fill`` rows' ``realized_pnl`` into day / week / month buckets in
the **display timezone** (UTC + ``settings.tz_offset_hours``) for the server-rendered
PnL calendar. Read-only; pure Python bucketing over a single indexed query (no network).

Realized PnL lives on SELL fills (a BUY fill carries ``realized_pnl == 0``), so a day's
realized PnL is simply ``Σ fill.realized_pnl`` for fills whose *local* date is that day.
"""

from __future__ import annotations

import calendar
from collections import defaultdict
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Fill

# Monday-first calendar to match the dashboard's Vietnamese week (T2..CN).
_CAL = calendar.Calendar(firstweekday=0)


def _offset() -> timedelta:
    return timedelta(hours=settings.tz_offset_hours)


def local_today() -> date:
    return (datetime.utcnow() + _offset()).date()


def _local_date(dt: datetime) -> date:
    """Calendar date of a naive-UTC fill timestamp, in the display zone."""
    return (dt + _offset()).date()


def _window_fills(db: Session, start: date, end: date) -> list[Fill]:
    """Fills whose *local* date falls in [start, end] (inclusive).

    Query a UTC window widened by the tz offset on both ends, then filter precisely by
    local date — the offset can push a fill across the UTC midnight boundary.
    """
    off = _offset()
    lo = datetime(start.year, start.month, start.day) - off
    hi = datetime(end.year, end.month, end.day) + timedelta(days=1) - off
    rows = (
        db.execute(
            select(Fill)
            .where(Fill.executed_at >= lo, Fill.executed_at < hi)
            .order_by(Fill.executed_at)
        )
        .scalars()
        .all()
    )
    return [f for f in rows if start <= _local_date(f.executed_at) <= end]


def _agg(fills: list[Fill]) -> dict:
    """Summarise a bucket of fills. ``closed`` counts realising (non-zero PnL) fills."""
    pnl = sum(f.realized_pnl for f in fills)
    closed = [f for f in fills if f.realized_pnl != 0.0]
    return {
        "pnl": round(pnl, 2),
        "fills": len(fills),
        "closed": len(closed),
        "wins": sum(1 for f in closed if f.realized_pnl > 0),
        "losses": sum(1 for f in closed if f.realized_pnl < 0),
    }


def realized_by_day(db: Session, start: date, end: date) -> dict[date, dict]:
    """Map each local date in [start, end] that has fills to its aggregate."""
    buckets: dict[date, list[Fill]] = defaultdict(list)
    for f in _window_fills(db, start, end):
        buckets[_local_date(f.executed_at)].append(f)
    return {d: _agg(fs) for d, fs in buckets.items()}


def day_fills(db: Session, day: date) -> list[Fill]:
    """All fills executed on ``day`` (local date), oldest first — for the drilldown."""
    return _window_fills(db, day, day)


def month_view(db: Session, year: int, month: int) -> dict:
    """Month grid of day cells (Mon-first weeks) + per-week and month subtotals."""
    last_dom = calendar.monthrange(year, month)[1]
    first, last = date(year, month, 1), date(year, month, last_dom)
    weeks_dates = _CAL.monthdatescalendar(year, month)  # weeks of 7 date objects
    by_day = realized_by_day(db, weeks_dates[0][0], weeks_dates[-1][-1])
    today = local_today()

    weeks = []
    for wk in weeks_dates:
        cells, wk_pnl, wk_closed = [], 0.0, 0
        for d in wk:
            a = by_day.get(d)
            in_month = d.month == month and d.year == year
            if a and in_month:
                wk_pnl += a["pnl"]
                wk_closed += a["closed"]
            cells.append(
                {
                    "date": d.isoformat(),
                    "day": d.day,
                    "in_month": in_month,
                    "today": d == today,
                    "pnl": a["pnl"] if a else 0.0,
                    "closed": a["closed"] if a else 0,
                    "has": bool(a and a["closed"]),
                }
            )
        weeks.append({"cells": cells, "pnl": round(wk_pnl, 2), "closed": wk_closed})

    in_month_aggs = [a for d, a in by_day.items() if first <= d <= last]
    total = round(sum(a["pnl"] for a in in_month_aggs), 2)
    closed = sum(a["closed"] for a in in_month_aggs)
    wins = sum(a["wins"] for a in in_month_aggs)

    prev_m = first - timedelta(days=1)
    next_m = last + timedelta(days=1)
    return {
        "view": "month",
        "year": year,
        "month": month,
        "month_name": f"{year}-{month:02d}",
        "weekday_labels": ["T2", "T3", "T4", "T5", "T6", "T7", "CN"],
        "weeks": weeks,
        "total": total,
        "closed": closed,
        "wins": wins,
        "win_rate": round(wins / closed * 100, 1) if closed else 0.0,
        "prev": {"year": prev_m.year, "month": prev_m.month},
        "next": {"year": next_m.year, "month": next_m.month},
    }


def year_view(db: Session, year: int) -> dict:
    """12-month summary for ``year`` — the month-granularity toggle."""
    fills = _window_fills(db, date(year, 1, 1), date(year, 12, 31))
    by_month: dict[int, list[Fill]] = defaultdict(list)
    for f in fills:
        by_month[_local_date(f.executed_at).month].append(f)
    months = []
    for m in range(1, 13):
        a = _agg(by_month.get(m, []))
        months.append({"month": m, "name": f"Th{m:02d}", **a})
    total = round(sum(mo["pnl"] for mo in months), 2)
    closed = sum(mo["closed"] for mo in months)
    wins = sum(mo["wins"] for mo in months)
    return {
        "view": "year",
        "year": year,
        "months": months,
        "total": total,
        "closed": closed,
        "wins": wins,
        "win_rate": round(wins / closed * 100, 1) if closed else 0.0,
        "prev": {"year": year - 1},
        "next": {"year": year + 1},
    }


def calendar_view(
    db: Session,
    *,
    view: str = "month",
    year: int | None = None,
    month: int | None = None,
) -> dict:
    """Entry point for the route: dispatch to the month or year aggregation.

    Defaults to the current local month. Invalid month falls back to today's month.
    """
    today = local_today()
    year = year or today.year
    if view == "year":
        return year_view(db, year)
    month = month if (month and 1 <= month <= 12) else today.month
    return month_view(db, year, month)
