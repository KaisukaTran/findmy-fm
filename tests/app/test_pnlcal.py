"""
Phase 4 PnL-calendar aggregation tests.

Fixtures place each fill at *local noon* (UTC noon minus the tz offset) so its
calendar date is unambiguous under any ``tz_offset_hours``. Every assertion is a
hand-sum of the inserted realized PnL.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from app import pnlcal
from app.config import settings
from app.models import Fill


def _add_fill(db, *, local_day: date, pnl: float, side: str = "SELL", symbol: str = "BTC") -> None:
    utc = datetime(local_day.year, local_day.month, local_day.day, 12, 0, 0) - timedelta(
        hours=settings.tz_offset_hours
    )
    db.add(
        Fill(symbol=symbol, side=side, quantity=1.0, price=100.0, fee=0.0,
             realized_pnl=pnl, executed_at=utc)
    )
    db.commit()


def test_realized_by_day_hand_sum(db):
    d = date(2026, 6, 10)
    _add_fill(db, local_day=d, pnl=10.0)
    _add_fill(db, local_day=d, pnl=-4.0)
    _add_fill(db, local_day=date(2026, 6, 11), pnl=7.5)
    by_day = pnlcal.realized_by_day(db, date(2026, 6, 1), date(2026, 6, 30))
    assert by_day[d]["pnl"] == 6.0          # 10 - 4
    assert by_day[d]["closed"] == 2
    assert by_day[d]["wins"] == 1
    assert by_day[d]["losses"] == 1
    assert by_day[date(2026, 6, 11)]["pnl"] == 7.5


def test_month_view_total_matches_sum(db):
    for day, pnl in [(3, 5.0), (3, 5.0), (15, -8.0), (28, 20.0)]:
        _add_fill(db, local_day=date(2026, 6, day), pnl=pnl)
    # a fill in the prior month must NOT leak into June's total
    _add_fill(db, local_day=date(2026, 5, 31), pnl=999.0)
    mv = pnlcal.month_view(db, 2026, 6)
    assert mv["total"] == 22.0          # 5 + 5 - 8 + 20
    assert mv["closed"] == 4
    assert mv["wins"] == 3
    # per-week subtotals (in-month only) reconstruct the month total
    assert round(sum(w["pnl"] for w in mv["weeks"]), 2) == 22.0


def test_month_view_in_month_flag(db):
    mv = pnlcal.month_view(db, 2026, 6)
    for wk in mv["weeks"]:
        for c in wk["cells"]:
            d = date.fromisoformat(c["date"])
            assert c["in_month"] == (d.month == 6 and d.year == 2026)


def test_year_view_buckets_by_month(db):
    _add_fill(db, local_day=date(2026, 1, 5), pnl=12.0)
    _add_fill(db, local_day=date(2026, 6, 9), pnl=-3.0)
    _add_fill(db, local_day=date(2026, 6, 20), pnl=8.0)
    yv = pnlcal.year_view(db, 2026)
    jan = next(m for m in yv["months"] if m["month"] == 1)
    jun = next(m for m in yv["months"] if m["month"] == 6)
    assert jan["pnl"] == 12.0
    assert jun["pnl"] == 5.0             # -3 + 8
    assert yv["total"] == 17.0


def test_day_fills_returns_only_that_day(db):
    d = date(2026, 6, 10)
    _add_fill(db, local_day=d, pnl=10.0)
    _add_fill(db, local_day=date(2026, 6, 11), pnl=7.5)
    fills = pnlcal.day_fills(db, d)
    assert len(fills) == 1
    assert fills[0].realized_pnl == 10.0


def test_buy_fill_counts_as_fill_not_closed(db):
    d = date(2026, 6, 10)
    _add_fill(db, local_day=d, pnl=0.0, side="BUY")
    by_day = pnlcal.realized_by_day(db, d, d)
    assert by_day[d]["fills"] == 1
    assert by_day[d]["closed"] == 0
    assert by_day[d]["pnl"] == 0.0


def test_calendar_view_defaults_to_current_local_month(db):
    today = pnlcal.local_today()
    cv = pnlcal.calendar_view(db)
    assert cv["view"] == "month"
    assert cv["year"] == today.year
    assert cv["month"] == today.month


def test_calendar_view_year_dispatch(db):
    cv = pnlcal.calendar_view(db, view="year", year=2026)
    assert cv["view"] == "year"
    assert len(cv["months"]) == 12
