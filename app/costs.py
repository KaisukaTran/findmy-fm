"""
Operating-cost aggregation for the "Chi phí" view.

Totals every cost the bot incurs, sliced by week / month / year in the display TZ:
  * trade fees      — Σ Fill.fee
  * withdrawal cost — Σ (fee + VAT) of recorded Withdrawals (booked only on a real withdrawal)
  * AI cost         — metered Σ OpusCostLedger.billed_cost, split Claude (purpose != grok*)
                      vs Grok (purpose starts "grok"); falls back to the monthly estimate
                      ($25 Claude + $20 Grok) for any period with no metered rows.

Read-only except record_withdrawal(). Mirrors app/pnlcal.py's TZ-aware bucketing — storage
stays naive-UTC; only display buckets shift by settings.tz_offset_hours.
"""

from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Fill, Withdrawal
from app.orchestrator.models import OpusCostLedger

_AVG_MONTH_DAYS = 30.44  # mean Gregorian month — prorates the monthly AI estimate to a bucket
_VALUE_KEYS = ("trade_fees", "withdrawal_fee", "vat", "ai_claude", "ai_grok", "ai_total", "total")


def _offset() -> timedelta:
    return timedelta(hours=settings.tz_offset_hours)


def local_today() -> date:
    return (datetime.utcnow() + _offset()).date()


# --- withdrawal recording (the ONLY write path) -------------------------------


def record_withdrawal(
    db: Session, amount: float, note: str | None = None, exchange: str = "binance"
) -> Withdrawal:
    """Book a withdrawal. fee = (withdrawal_fee_pct + tolerance) × amount; VAT = vat_pct ×
    amount — both frozen at insert so later rate changes never rewrite history."""
    if amount is None or amount <= 0:
        raise ValueError("amount must be positive")
    fee = amount * (settings.withdrawal_fee_pct + settings.withdrawal_fee_tolerance_pct) / 100.0
    vat = amount * settings.vat_pct / 100.0
    w = Withdrawal(amount=float(amount), fee=fee, vat=vat, exchange=exchange, note=note or None)
    db.add(w)
    db.commit()
    db.refresh(w)
    return w


def list_withdrawals(db: Session, limit: int = 50) -> list[Withdrawal]:
    return db.query(Withdrawal).order_by(Withdrawal.id.desc()).limit(limit).all()


# --- period bucketing (mirrors pnlcal) ----------------------------------------


def _period_bounds(period: str, n_back: int, today: date) -> tuple[date, date, str]:
    """Local [start, end] inclusive + a label for the bucket `n_back` periods before now."""
    if period == "year":
        y = today.year - n_back
        return date(y, 1, 1), date(y, 12, 31), str(y)
    if period == "month":
        m0 = today.year * 12 + (today.month - 1) - n_back
        y, m = divmod(m0, 12)
        m += 1
        return date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1]), f"{y}-{m:02d}"
    # week — Monday-first, to match the dashboard's T2..CN week
    monday = today - timedelta(days=today.weekday()) - timedelta(weeks=n_back)
    sunday = monday + timedelta(days=6)
    iso = monday.isocalendar()
    return monday, sunday, f"{iso[0]}-W{iso[1]:02d}"


def _utc_window(start: date, end: date) -> tuple[datetime, datetime]:
    """Half-open UTC window [lo, hi) covering local dates [start, end], widened by the offset."""
    off = _offset()
    lo = datetime(start.year, start.month, start.day) - off
    hi = datetime(end.year, end.month, end.day) + timedelta(days=1) - off
    return lo, hi


def _bucket_costs(db: Session, start: date, end: date) -> dict:
    """All costs that fall in local [start, end] — one row of the series."""
    lo, hi = _utc_window(start, end)
    days = (end - start).days + 1

    trade_fees = float(
        db.execute(
            select(func.coalesce(func.sum(Fill.fee), 0.0)).where(
                Fill.executed_at >= lo, Fill.executed_at < hi
            )
        ).scalar()
        or 0.0
    )

    wf = db.execute(
        select(
            func.coalesce(func.sum(Withdrawal.fee), 0.0),
            func.coalesce(func.sum(Withdrawal.vat), 0.0),
        ).where(Withdrawal.created_at >= lo, Withdrawal.created_at < hi)
    ).one()
    w_fee, w_vat = float(wf[0] or 0.0), float(wf[1] or 0.0)

    # AI: metered first, split by purpose; estimate fallback when the bucket is empty.
    rows = db.execute(
        select(
            OpusCostLedger.purpose, func.coalesce(func.sum(OpusCostLedger.billed_cost), 0.0)
        )
        .where(OpusCostLedger.ts >= lo, OpusCostLedger.ts < hi)
        .group_by(OpusCostLedger.purpose)
    ).all()
    grok = sum(float(c) for p, c in rows if (p or "").startswith("grok"))
    claude = sum(float(c) for p, c in rows if not (p or "").startswith("grok"))
    estimated = (grok + claude) <= 0.0
    if estimated:
        frac = days / _AVG_MONTH_DAYS
        claude = settings.ai_monthly_claude_usd * frac
        grok = settings.ai_monthly_grok_usd * frac
    ai_total = claude + grok

    total = trade_fees + w_fee + w_vat + ai_total
    return {
        "trade_fees": round(trade_fees, 4),
        "withdrawal_fee": round(w_fee, 4),
        "vat": round(w_vat, 4),
        "ai_claude": round(claude, 4),
        "ai_grok": round(grok, 4),
        "ai_total": round(ai_total, 4),
        "ai_estimated": estimated,  # True = monthly-estimate fallback, False = metered actuals
        "total": round(total, 4),
    }


def cost_summary(db: Session, period: str = "month", buckets: int = 12) -> dict:
    """Cost breakdown over the last `buckets` periods (oldest→newest) + current + grand totals.

    period ∈ {week, month, year}. Each series row carries trade_fees / withdrawal_fee / vat /
    ai_claude / ai_grok / ai_total / total + a `label` and `ai_estimated` flag.
    """
    period = period if period in ("week", "month", "year") else "month"
    buckets = max(1, min(buckets, 60))
    today = local_today()
    series = []
    for n in range(buckets - 1, -1, -1):  # oldest first, current last
        start, end, label = _period_bounds(period, n, today)
        b = _bucket_costs(db, start, end)
        b.update(
            {"label": label, "start": start.isoformat(), "end": end.isoformat(), "current": n == 0}
        )
        series.append(b)
    totals = {k: round(sum(s[k] for s in series), 4) for k in _VALUE_KEYS}
    return {
        "period": period,
        "buckets": buckets,
        "series": series,
        "current": series[-1],
        "totals": totals,
    }
