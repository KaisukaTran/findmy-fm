"""Data for the per-session time x price chart (ladder modal).

Read-only: it joins what the session already recorded (waves, SELL fills) with 1h candles of the
coin and of BTC from the shared in-memory candle cache. It never places or alters an order, and a
market-data failure degrades to an empty candle list plus a reason — it must never 500 the modal.

The pure helpers (`wave_rows`, `avg_steps`, `rel_series`, `trim_candles`) take plain data so they
are unit-testable without a network or a database.
"""

from __future__ import annotations

import calendar
import logging
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app import execution
from app.clock import utcnow
from app.config import settings
from app.data import candle_cache
from app.kss import service
from app.models import Fill

logger = logging.getLogger(__name__)

TIMEFRAME = "1h"
LOOKBACK_BEFORE_OPEN_H = 24     # context shown before the session opened
MAX_CANDLES = 1500
_PENDING = ("pending", "sent")

ERR_RATE_HOLD = "đang bị giới hạn tốc độ sàn, thử lại sau"
ERR_NO_CANDLES = "không tải được nến"


def to_epoch(dt: datetime | str | None) -> int | None:
    """Naive-UTC datetime (or its ISO string) -> UTC epoch seconds; None stays None."""
    if dt is None or dt == "":
        return None
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt)
    return calendar.timegm(dt.timetuple())


def _fmt_utc(ts: int) -> str:
    return (datetime(1970, 1, 1) + timedelta(seconds=ts)).strftime("%d/%m %H:%M")


def wave_rows(waves: list[dict], entry_price: float) -> list[dict]:
    """One display row per wave, ordered by rung number.

    ``gap_pct`` is the target's distance from the PREVIOUS rung's target (None for rung 0 and for
    a defensive rung, whose number is negative); ``from_entry_pct`` is the distance from the
    session's entry price.
    """
    rows: list[dict] = []
    prev_target: float | None = None
    for w in sorted(waves, key=lambda x: x.get("wave_num", 0)):
        n = int(w.get("wave_num", 0))
        target = float(w.get("target_price") or 0.0)
        filled_at = to_epoch(w.get("filled_time"))
        filled_price = float(w.get("filled_price") or 0.0)
        gap = None
        if n >= 1 and prev_target and target > 0:
            gap = (target / prev_target - 1.0) * 100.0
        from_entry = (target / entry_price - 1.0) * 100.0 if entry_price > 0 and target > 0 else None
        rows.append({
            "n": n,
            "target": target,
            "status": w.get("status", ""),
            "filled_qty": float(w.get("filled_qty") or 0.0),
            "filled_price": filled_price if filled_price > 0 else None,
            "filled_at": filled_at,
            "filled_label": _fmt_utc(filled_at) if filled_at is not None else "—",
            "gap_pct": gap,
            "from_entry_pct": from_entry,
        })
        if n >= 0 and target > 0:
            prev_target = target
    return rows


def avg_steps(rows: list[dict], avg_price: float, now_ts: int) -> list[dict]:
    """Running quantity-weighted average price after each filled wave, in fill-time order.

    Times are strictly ascending (a chart requirement): two fills in the same second collapse
    into the later value. The last point is pinned to the session's own ``avg_price`` and a final
    point at ``now_ts`` carries the level to the right edge.
    """
    fills = sorted(
        (r for r in rows
         if r["filled_at"] is not None and r["filled_qty"] > 0 and (r["filled_price"] or 0) > 0),
        key=lambda r: (r["filled_at"], r["n"]),
    )
    qty = cost = 0.0
    out: list[dict] = []
    for r in fills:
        qty += r["filled_qty"]
        cost += r["filled_qty"] * r["filled_price"]
        point = {"time": r["filled_at"], "value": cost / qty}
        if out and out[-1]["time"] == point["time"]:
            out[-1] = point
        else:
            out.append(point)
    if not out:
        return out
    if avg_price > 0:
        out[-1] = {"time": out[-1]["time"], "value": avg_price}
    if now_ts > out[-1]["time"]:
        out.append({"time": now_ts, "value": out[-1]["value"]})
    return out


def trim_candles(raw: list[dict], start_ts: int, end_ts: int) -> list[dict]:
    """Candle dicts (ms timestamps) -> chart bars (epoch seconds), ascending, inside the window."""
    bars: dict[int, dict] = {}
    for c in raw:
        t = int(c["ts"]) // 1000
        if t < start_ts or t > end_ts:
            continue
        bars[t] = {"time": t, "open": float(c["open"]), "high": float(c["high"]),
                   "low": float(c["low"]), "close": float(c["close"])}
    return [bars[t] for t in sorted(bars)]


def rel_series(bars: list[dict], open_ts: int) -> list[dict]:
    """% change of the close vs the close of the first bar at/after ``open_ts`` (that bar = 0.0)."""
    after = [b for b in bars if b["time"] >= open_ts]
    if not after or after[0]["close"] <= 0:
        return []
    base = after[0]["close"]
    return [{"time": b["time"], "value": (b["close"] / base - 1.0) * 100.0} for b in after]


def _fetch_bars(symbol: str, limit: int, start_ts: int, end_ts: int) -> list[dict]:
    from app.scanner import _provider_factory  # lazy: scanner is heavy and imports this layer

    try:
        # The window's first bar (one bar of slack) must be in the cache, else re-fetch in full.
        raw, _hit = candle_cache.get_candles(
            settings.data_exchange, symbol, TIMEFRAME, limit, _provider_factory,
            need_from_ms=(start_ts + 3600) * 1000,
        )
    except Exception as exc:  # noqa: BLE001 — market data must never break the modal
        logger.warning("session chart: candle fetch failed for %s: %s", symbol, exc)
        return []
    return trim_candles(raw or [], start_ts, end_ts)


def _exits(db: Session, session_id: int) -> list[dict]:
    fills = (
        db.query(Fill)
        .filter(Fill.side == "SELL", Fill.source_ref.like(f"pyramid:{session_id}:%"))
        .order_by(Fill.executed_at, Fill.id)
        .all()
    )
    out = []
    for f in fills:
        kind = str(f.source_ref or "").rsplit(":", 1)[-1]
        out.append({
            "time": to_epoch(f.executed_at),
            "price": f.price,
            "realized_pnl": f.realized_pnl,
            "kind": kind if kind in ("tp", "manual_tp") else (kind or "other"),
        })
    return out


def build_chart(db: Session, session_id: int, now: datetime | None = None) -> dict:
    """Assemble the chart payload for one session. Raises ValueError for an unknown id."""
    st = service.ladder_status(db, session_id)
    now_dt = now or utcnow()
    now_ts = to_epoch(now_dt)
    created_ts = to_epoch(st["created_at"])
    entry = float(st["entry_price"] or 0.0)
    avg = float(st["avg_price"] or 0.0)

    rows = wave_rows(st["waves"], entry)
    exits = _exits(db, session_id)
    steps = avg_steps(rows, avg, now_ts)
    if exits and st["status"] != "active" and steps:
        # A finished session's average stops at its last exit rather than running to "now".
        end = max(e["time"] for e in exits if e["time"] is not None)
        steps = [p for p in steps if p["time"] <= end]
        if steps and end > steps[-1]["time"]:
            steps.append({"time": end, "value": steps[-1]["value"]})

    start_ts = created_ts - LOOKBACK_BEFORE_OPEN_H * 3600
    hours = max(1, (now_ts - start_ts) // 3600)
    limit = int(min(MAX_CANDLES, hours + 2))

    symbol = st["symbol"]
    candles: list[dict] = []
    rel: dict = {"coin": [], "btc": []}
    error: str | None = None
    if execution.rate_hold_active():
        error = ERR_RATE_HOLD
    else:
        candles = _fetch_bars(symbol, limit, start_ts, now_ts + 3600)
        if not candles:
            error = ERR_RATE_HOLD if execution.rate_hold_active() else ERR_NO_CANDLES
        else:
            rel["coin"] = rel_series(candles, created_ts)
            if symbol.upper() == "BTC":
                rel["btc"] = list(rel["coin"])
            else:
                btc = _fetch_bars("BTC", limit, start_ts, now_ts + 3600)
                rel["btc"] = rel_series(btc, created_ts)

    sl = float(st.get("sl_price") or 0.0)
    return {
        "id": session_id,
        "symbol": symbol,
        "status": st["status"],
        "entry_price": entry,
        "avg_price": avg,
        "tp_price": float(st.get("estimated_tp_price") or 0.0),
        "sl_price": sl if sl > 0 else None,
        "current_price": float(st.get("current_price") or 0.0),
        "distance_pct": st["distance_pct"],
        "max_waves": st["max_waves"],
        "created_at": created_ts,
        "waves": [
            {k: r[k] for k in ("n", "target", "status", "filled_price", "filled_at",
                               "gap_pct", "from_entry_pct")}
            for r in rows
        ],
        "avg_steps": steps,
        "exits": exits,
        "candles": candles,
        "rel": rel,
        "candles_error": error,
    }
