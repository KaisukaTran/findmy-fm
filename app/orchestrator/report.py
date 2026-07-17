"""
OPUS accountability: daily Telegram/Discord report + rolling-7-day auto-freeze
(docs/opus-3pct-plan.md §2, Phase P4).

Both entry points are called once per `loop.tick`, right after `watch.run(db)`, so
accountability keeps running even on ticks where the cost cap or decision throttle
skips the paid brain call. Each function is fully defensive (try/except + log.warning)
so a reporting bug can never sink the tick — mirrors how watch/distill are treated.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from app import audit, notify, runtime
from app.clock import utcnow
from app.config import settings
from app.orchestrator import ledger, service
from app.orchestrator.models import OPUS_CLOSED, OpusMetricHourly, OpusPosition

log = logging.getLogger(__name__)

# Throttle keys — both checks are "once per UTC calendar day", so a 5-min tick cadence
# doesn't spam a report or flap the freeze decision.
_KEY_LAST_DAILY_REPORT = "opus_last_daily_report_date"
_KEY_LAST_FREEZE_CHECK = "opus_last_freeze_check_date"


def maybe_daily_report(db: Session) -> bool:
    """Send a compact end-of-day accountability report once per UTC day, covering
    YESTERDAY's activity. Returns True iff a report was actually sent.

    First-ever call (no stored date, e.g. a brand-new deployment) just seeds the key
    without sending — there is no "yesterday" worth reporting on since OPUS may not
    even have been running. Never raises.
    """
    try:
        today = utcnow().date()
        today_str = today.isoformat()
        stored = runtime.get(db, _KEY_LAST_DAILY_REPORT)
        if stored == today_str:
            return False  # already reported today

        first_ever = stored is None
        runtime.set(db, _KEY_LAST_DAILY_REPORT, today_str)
        if first_ever:
            return False  # don't spam a report for a day OPUS wasn't necessarily running

        yesterday = today - timedelta(days=1)
        day = ledger.rollup_day(db, yesterday)
        had_activity = day["trades"] > 0 or day["cost"] > 0 or day["net"] != 0
        if not had_activity:
            return False  # nothing happened yesterday — stay quiet, key already bumped

        trades = day["trades"]
        win_trades = day["win_trades"]
        wr = (win_trades / trades * 100.0) if trades > 0 else 0.0
        cost = day["cost"]
        coverage = f"{day['net'] / cost:.1f}×" if cost > 0 else "—"
        n_open = len(service.managed_positions(db))
        alloc = service.allocation()

        text = (
            f"📊 OPUS ngày {day['day']}: net ${day['net']:+.2f} "
            f"({day['net_pct']:+.2f}%/vốn, mục tiêu {settings.opus_kpi_target_pct:.0f}%)\n"
            f"gross ${day['gross']:+.2f} · phí API ${cost:.2f} · net/phí = {coverage}\n"
            f"lệnh đóng: {trades} · thắng: {win_trades} ({wr:.0f}%)\n"
            f"vị thế đang mở: {n_open} · vốn giao: ${alloc:.0f}"
        )
        notify.send(text)
        return True
    except Exception as exc:  # noqa: BLE001 — a bad report must never sink the tick
        log.warning("OPUS daily report failed: %s", type(exc).__name__)
        return False


def maybe_auto_freeze(db: Session) -> bool:
    """Safety brake: auto-disable OPUS when the rolling `opus_freeze_window_days` net is
    negative across at least `opus_freeze_min_closed` closed positions. Evaluated at most
    once per UTC day (a freeze decision doesn't need 5-min granularity and must not flap).
    Returns True iff it just froze OPUS. Never raises.
    """
    try:
        if not settings.opus_auto_freeze_enabled or not settings.opus_mode:
            return False

        today_str = utcnow().date().isoformat()
        if runtime.get(db, _KEY_LAST_FREEZE_CHECK) == today_str:
            return False  # already evaluated today
        runtime.set(db, _KEY_LAST_FREEZE_CHECK, today_str)

        window_days = settings.opus_freeze_window_days
        cutoff = utcnow() - timedelta(days=window_days)

        net = float(
            db.query(func.coalesce(func.sum(OpusMetricHourly.net_pnl), 0.0))
            .filter(OpusMetricHourly.hour_ts >= cutoff)
            .scalar()
            or 0.0
        )
        closed_count = (
            db.query(OpusPosition)
            .filter(
                OpusPosition.state == OPUS_CLOSED,
                OpusPosition.closed_at.isnot(None),
                OpusPosition.closed_at >= cutoff,
            )
            .count()
        )

        if net < 0 and closed_count >= settings.opus_freeze_min_closed:
            runtime.opus_mode_off(db)  # persists + commits
            audit.log(
                db, "opus", "auto_freeze",
                net=round(net, 2), closed=closed_count, window_days=window_days,
            )
            db.commit()  # audit.log only flushes
            notify.send(
                f"🧊 OPUS TỰ ĐÓNG BĂNG: net {window_days} ngày = ${net:+.2f} (<0) qua "
                f"{closed_count} lệnh đóng — opus_mode đã TẮT. Bật lại thủ công trên tab "
                "OPUS sau khi xem xét."
            )
            return True
        return False
    except Exception as exc:  # noqa: BLE001 — a bad freeze check must never sink the tick
        log.warning("OPUS auto-freeze check failed: %s", type(exc).__name__)
        return False
