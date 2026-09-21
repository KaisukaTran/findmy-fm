"""Market-wide crash watch: tell Kai when most of the universe is falling at once.

WHY THIS EXISTS, AND WHY IT ONLY WATCHES

Kai asked for two things on 2026-09-21: warn me when the market collapses, and stop the bot
buying deeper rungs until I confirm. The second half was built into the offline simulator and
measured across 1,500 portfolio runs at three capital levels -- and it lost, every time, on
every threshold tried:

    $5,000 account, 20 seeds, reserve gate, pessimistic bound
      no brake            median $18,503   max drawdown 31%
      halt on -20%/70%    median $10,332   max drawdown 39%
      halt on -12%/70%    median  $5,512   max drawdown 49%

The brake made drawdown WORSE at every capital level, which is not a tuning accident: in a DCA
ladder, buying into the fall IS the recovery mechanism. Refusing the deeper rungs freezes the
position at its highest average with no path back, so the unrealised loss simply stays. Kai
dropped the halt the same day and kept this -- the watching half, which touches no order.

So: this module NEVER blocks, cancels, delays or resizes anything. It reads candles the scanner
already fetched and sends a message. If it ever grows a code path that touches an order, that is
a bug, and `tests/app/test_crash_watch.py` should be the thing that catches it.

WHAT IT MEASURES, AND HOW THE NUMBERS WERE PICKED

Breadth of a FAST fall: the share of the scanned universe whose latest bar traded at least
`crash_alert_drop_pct` below the PREVIOUS bar's high. Two earlier definitions were measured and
discarded:

  * Depth of our own ladders ("30% of sessions past 75% of their rungs", Kai's original). At
    $5,000 the book holds a MEDIAN OF 2 open sessions, so "30% of sessions" is one unlucky coin.
    It also fires far too late: rung 22 of 30 means price already fell 57.6% and the session
    already spent $4,098. Across three years it would have fired on exactly one day.
  * Drawdown from a 24-bar high. Most crypto sits below its 24-day high most of the time, so
    that version was engaged 61-98% of the time.

Calibrated on the top-100-by-volume universe the scanner actually sees, 2023-08 -> 2026-07:

    drop >= 20% in one bar, breadth >=      50%    60%    70%
    alert days in three years                25     20     17
    2025-10-10 (the real crash)            96.3% fires  fires  fires
    2026-01-31 (the second one)            64.7% fires  fires  MISSED
    2025-10-14 (an ordinary down day)       2.5% quiet  quiet  quiet

Hence the defaults: 20% and 60%, about seven alerts a year, catching both measured crashes and
silent on ordinary weakness.

THE LIMIT, STATED PLAINLY: the scanner warms DAILY candles, so this sees the in-progress day.
Measured hour by hour, the 2025-10-10 crash gave roughly one hour between "most of the market is
down 11%" and "most of the market is down 57%" -- everything below rung 22 filled inside that
hour. An alert on a daily bar can tell Kai it is happening; it cannot give him a day's notice,
because there was never a day's notice to give.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app import audit, notify, runtime
from app.clock import utcnow
from app.config import settings

KEY_LAST_ALERT = "crash_alert_last_at"


def breadth(candles_by_symbol: dict[str, list[dict]], drop_pct: float) -> tuple[float, int, int]:
    """Share of symbols whose latest bar traded `drop_pct` below the previous bar's high.

    Returns ``(breadth_pct, hits, measured)``. A symbol with fewer than two bars, or a
    non-positive previous high, is skipped rather than counted as calm -- a missing symbol must
    never dilute the breadth downward and turn a real crash into a quiet reading.
    """
    hits = measured = 0
    threshold = 1 - drop_pct / 100.0
    for bars in candles_by_symbol.values():
        if not bars or len(bars) < 2:
            continue
        prev_high = bars[-2].get("high") or 0.0
        low = bars[-1].get("low") or 0.0
        if prev_high <= 0 or low <= 0:
            continue
        measured += 1
        if low <= prev_high * threshold:
            hits += 1
    return (100.0 * hits / measured if measured else 0.0), hits, measured


def evaluate(db: Session, candles_by_symbol: dict[str, list[dict]]) -> dict | None:
    """Check the universe and alert if it is falling broadly. Returns the reading, or None.

    Called from the scanner's off-lock candle prefetch, so it costs no extra exchange weight:
    it reuses exactly the candles that were just warmed for the scan.
    """
    if not settings.crash_alert_enabled or settings.crash_alert_breadth_pct <= 0:
        return None
    pct, hits, measured = breadth(candles_by_symbol, settings.crash_alert_drop_pct)
    # A handful of symbols cannot carry a breadth statistic — this is the same sample-size trap
    # that killed the session-depth version of this rule (a $5,000 book holds 2 sessions, so
    # "30% of them" was one coin). Stay silent rather than alert on noise.
    if measured < settings.crash_alert_min_symbols:
        return None
    reading = {"breadth_pct": round(pct, 2), "hits": hits, "measured": measured,
               "drop_pct": settings.crash_alert_drop_pct}
    if pct < settings.crash_alert_breadth_pct:
        return reading

    last = runtime.get(db, KEY_LAST_ALERT)
    now = utcnow()
    if last:
        try:
            from datetime import datetime
            elapsed_min = (now - datetime.fromisoformat(last)).total_seconds() / 60.0
        except (TypeError, ValueError):
            elapsed_min = float("inf")   # unreadable stamp: alert rather than stay silent
        if elapsed_min < settings.crash_alert_cooldown_min:
            reading["throttled"] = True
            return reading

    runtime.set(db, KEY_LAST_ALERT, now.isoformat())
    audit.log(db, "crash_watch", "market_wide_drop", entity="universe", **reading)
    db.commit()
    # kind="risk" bypasses the Telegram master mute and is never throttled by notify itself —
    # the cooldown above is the only throttle, so a muted phone still gets this one.
    notify.event(
        "risk",
        f"⚠️ Thị trường rơi diện rộng: {hits}/{measured} coin "
        f"({pct:.0f}%) đã rơi ≥{settings.crash_alert_drop_pct:.0f}% trong phiên này.\n"
        f"Bot KHÔNG tự dừng mua — thang DCA vẫn chạy (đo được: chặn nó làm sụt sâu tệ hơn). "
        f"Nếu muốn dừng, hãy đóng băng thủ công trên bảng điều khiển.",
    )
    reading["alerted"] = True
    return reading
