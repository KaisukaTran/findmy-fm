"""Session map ("Bản đồ phiên"): every ACTIVE KSS session on one comparable page.

For each open ladder it answers three questions at a glance — how far is the take-profit, how
far is the next rung (and could we even fund it), and is the coin doing worse than BTC since the
session opened. Each row also carries a tiny inline-SVG range bar drawn on a COMMON axis
(-25% .. +25% around the current price) so rows can be compared by eye.

Read-only: it never places, queues or alters an order. Market data is best-effort — a missing
price, a rate hold or a failed BTC fetch degrades the affected cells to ``None`` ("—") and never
raises. The helpers below the "pure" marker take plain data so they are unit-testable offline.
"""

from __future__ import annotations

import logging
from statistics import median

from sqlalchemy.orm import Session, selectinload

from app import execution
from app.clock import utcnow
from app.config import settings
from app.data import candle_cache
from app.kss import chart_data
from app.market import get_current_prices
from app.models import SESSION_ACTIVE, KssSession

logger = logging.getLogger(__name__)

AXIS_PCT = 25.0                 # half-width of the common price axis, in % of the current price
NEAR_TP_PCT = 5.0               # header: "TP within 5%"
NEAR_NEXT_PCT = 3.0             # header: "next rung within 3%"
BTC_TIMEFRAME = "1h"
BTC_MAX_BARS = 1500
_H = 3600
_PENDING = ("pending", "sent")

SVG_W, SVG_H = 260, 22
_PAD = 7.0

SORT_KEYS = ("tp", "next", "upnl", "vsbtc", "age", "invested")
DEFAULT_SORT = ("tp", "asc")
# sort key -> row field (``next`` is ordered by |distance| so ascending = nearest rung first)
_SORT_FIELD = {"tp": "to_tp_pct", "next": "to_next_pct", "upnl": "upnl",
               "vsbtc": "vs_btc", "age": "age_days", "invested": "invested"}


# --- pure helpers ----------------------------------------------------------


def pct(a: float | None, b: float | None) -> float | None:
    """``a / b - 1`` in percent; None when either side is missing or non-positive."""
    if a is None or b is None or a <= 0 or b <= 0:
        return None
    return (a / b - 1.0) * 100.0


def clamp_axis(p: float | None) -> tuple[float | None, int]:
    """Clamp a % distance to the common axis. Returns ``(value, off)``; ``off`` is -1/+1 when the
    value was beyond the left/right edge (drawn as a chevron) and 0 otherwise."""
    if p is None:
        return None, 0
    if p > AXIS_PCT:
        return AXIS_PCT, 1
    if p < -AXIS_PCT:
        return -AXIS_PCT, -1
    return p, 0


def depth_class(filled: int) -> str:
    """CSS class for the rung cell by ladder depth: 1-2 normal, 3-4 amber, 5+ red."""
    if filled >= 5:
        return "depth-bad"
    if filled >= 3:
        return "depth-warn"
    return "depth-ok"


def normalize_sort(sort: str | None, direction: str | None) -> tuple[str, str]:
    """Whitelist the click-to-sort inputs. Unknown key -> the default order; bad dir -> asc."""
    if sort not in SORT_KEYS:
        return DEFAULT_SORT
    return sort, "desc" if direction == "desc" else "asc"


def sort_rows(rows: list[dict], key: str, direction: str) -> list[dict]:
    """Stable sort; rows lacking the value always go last, whichever direction."""
    field = _SORT_FIELD.get(key, "to_tp_pct")

    def val(r: dict) -> float | None:
        v = r.get(field)
        if v is None:
            return None
        return abs(v) if key == "next" else v

    have = [r for r in rows if val(r) is not None]
    missing = [r for r in rows if val(r) is None]
    have.sort(key=val, reverse=(direction == "desc"))
    return have + missing


KEEP_MAX = 500                  # ids honoured from the ``keep`` query parameter
_KEEP_ID_MAX_DIGITS = 12


def parse_keep(raw: str | None) -> list[int]:
    """Defensive parse of the ``keep`` order hint: positive ints only, de-duplicated, in order,
    at most ``KEEP_MAX``. Everything else (junk, markup, absurdly long numbers) is dropped."""
    if not raw:
        return []
    out: list[int] = []
    seen: set[int] = set()
    for tok in raw[:KEEP_MAX * (_KEEP_ID_MAX_DIGITS + 1)].split(","):
        tok = tok.strip()
        if not tok.isascii() or not tok.isdigit() or len(tok) > _KEEP_ID_MAX_DIGITS:
            continue
        n = int(tok)
        if n <= 0 or n in seen:
            continue
        seen.add(n)
        out.append(n)
        if len(out) >= KEEP_MAX:
            break
    return out


def apply_keep(rows: list[dict], keep: list[int]) -> list[dict]:
    """Order ``rows`` (already in the normal sort order) by their position in ``keep``. Rows the
    reader has not seen yet go last in the normal order; ids no longer active simply drop out."""
    pos = {sid: i for i, sid in enumerate(keep)}
    seen = sorted((r for r in rows if r["id"] in pos), key=lambda r: pos[r["id"]])
    fresh = [r for r in rows if r["id"] not in pos]
    return seen + fresh


def btc_at(bars: list[dict], ts: int) -> float | None:
    """Close of the 1h bar that contains epoch-second ``ts``; None when the series lacks it."""
    start = ts // _H * _H
    for c in bars:
        if int(c["ts"]) // 1000 == start:
            close = float(c["close"])
            return close if close > 0 else None
    return None


def build_row(s: dict, *, price: float | None, btc_now: float | None, btc_open: float | None,
              free_cash: float, now_ts: int) -> dict:
    """One display row from plain session data (see ``session_inputs``) and market numbers."""
    price = price if price and price > 0 else None
    entry, avg, tp = s.get("entry"), s.get("avg") or s.get("entry"), s.get("tp")
    next_price = s.get("next_price") or 0.0
    next_qty = s.get("next_qty") or 0.0
    next_cost = next_price * next_qty if next_price > 0 and next_qty > 0 else None
    qty, invested = s.get("qty") or 0.0, s.get("invested") or 0.0

    upnl = upnl_pct = None
    if price is not None:
        upnl = qty * price - invested if qty > 0 else 0.0
        upnl_pct = upnl / invested * 100.0 if invested > 0 else 0.0

    coin_pct = pct(price, entry)
    btc_pct = pct(btc_now, btc_open)
    created = s.get("created_ts")
    filled = int(s.get("filled") or 0)
    return {
        "id": s["id"], "symbol": s["symbol"], "filled": filled,
        "max_waves": int(s.get("max_waves") or 0), "depth_class": depth_class(filled),
        "entry": entry, "avg": avg, "tp": tp,
        "next_price": next_price if next_cost else None, "next_cost": next_cost,
        "current": price, "invested": invested, "upnl": upnl, "upnl_pct": upnl_pct,
        "to_tp_pct": pct(tp, price) if tp else None,
        "to_next_pct": pct(next_price, price) if next_cost else None,
        "avg_pct": pct(avg, price), "entry_pct": pct(entry, price),
        "coin_pct": coin_pct, "btc_pct": btc_pct,
        "vs_btc": (coin_pct - btc_pct) if coin_pct is not None and btc_pct is not None else None,
        "starved": bool(next_cost is not None and next_cost > free_cash),
        "age_days": max(0.0, (now_ts - created) / 86400.0) if created else None,
    }


def summarize(rows: list[dict], free_cash: float) -> dict:
    """Header-line counters."""
    def near_tp(r: dict) -> bool:
        return r["to_tp_pct"] is not None and r["to_tp_pct"] <= NEAR_TP_PCT

    def near_next(r: dict) -> bool:
        return r["to_next_pct"] is not None and r["to_next_pct"] >= -NEAR_NEXT_PCT

    vs = [r["vs_btc"] for r in rows if r["vs_btc"] is not None]
    return {
        "total": len(rows),
        "near_tp": sum(1 for r in rows if near_tp(r)),
        "near_next": sum(1 for r in rows if near_next(r)),
        "starved": sum(1 for r in rows if r["starved"]),
        "starved_near": sum(1 for r in rows if r["starved"] and near_next(r)),
        "free_cash": free_cash,
        "median_vs_btc": median(vs) if vs else None,
        "near_tp_pct": NEAR_TP_PCT, "near_next_pct": NEAR_NEXT_PCT,
    }


def _x(p: float) -> float:
    return _PAD + (p + AXIS_PCT) / (2 * AXIS_PCT) * (SVG_W - 2 * _PAD)


def _marker(kind: str, p: float | None, label: str) -> str:
    """A tick (or, off-scale, an edge chevron) for one level; empty when the level is unknown."""
    clamped, off = clamp_axis(p)
    if clamped is None:
        return ""
    cy = SVG_H / 2
    if off:
        edge = SVG_W - 1.0 if off > 0 else 1.0
        tip = edge
        base = edge - 6.0 * off
        return (f'<polygon class="smap-off smap-{kind}" points="{tip:.1f},{cy:.1f} '
                f'{base:.1f},{cy - 5:.1f} {base:.1f},{cy + 5:.1f}"><title>{label} (ngoài thang)'
                f'</title></polygon>')
    x = _x(clamped)
    if kind == "avg":
        return (f'<circle class="smap-avg" cx="{x:.1f}" cy="{cy:.1f}" r="3.2">'
                f'<title>{label}</title></circle>')
    return (f'<line class="smap-{kind}" x1="{x:.1f}" y1="3" x2="{x:.1f}" y2="{SVG_H - 3}" '
            f'stroke-width="2.4"><title>{label}</title></line>')


def _fmt(p: float | None) -> str:
    return "—" if p is None else f"{p:+.1f}%"


def range_bar_svg(row: dict) -> str:
    """Mini range bar on the common -25%..+25% axis, centred on the current price.

    Orange tick = next rung, orange dot = average price, grey tick = entry, green tick = TP; a
    thin band joins the next rung to the TP. Levels beyond the axis are clamped to the edge and
    drawn as a chevron. Pure presentation attributes + CSS classes: no <style>, no style="".
    """
    cy = SVG_H / 2
    parts = [f'<svg class="smap-svg" viewBox="0 0 {SVG_W} {SVG_H}" width="{SVG_W}" '
             f'height="{SVG_H}" role="img" aria-label="Bản đồ giá của {row["symbol"]}">',
             f'<line class="smap-axis" x1="{_PAD}" y1="{cy}" x2="{SVG_W - _PAD}" y2="{cy}" '
             f'stroke-width="1"/>']
    if row.get("current") is None:
        parts.append("</svg>")
        return "".join(parts)
    lo, hi = row.get("to_next_pct"), row.get("to_tp_pct")
    if lo is not None and hi is not None:
        a, b = clamp_axis(lo)[0], clamp_axis(hi)[0]
        x1, x2 = sorted((_x(a), _x(b)))
        parts.append(f'<rect class="smap-band" x="{x1:.1f}" y="{cy - 3}" '
                     f'width="{max(x2 - x1, 0.5):.1f}" height="6"/>')
    parts.append(f'<line class="smap-now" x1="{_x(0):.1f}" y1="1" x2="{_x(0):.1f}" '
                 f'y2="{SVG_H - 1}" stroke-width="1.5"><title>giá hiện tại</title></line>')
    parts.append(_marker("entry", row.get("entry_pct"), f"giá vào {_fmt(row.get('entry_pct'))}"))
    parts.append(_marker("tp", row.get("to_tp_pct"), f"TP {_fmt(row.get('to_tp_pct'))}"))
    parts.append(_marker("next", row.get("to_next_pct"),
                         f"rung kế {_fmt(row.get('to_next_pct'))}"))
    parts.append(_marker("avg", row.get("avg_pct"), f"giá TB {_fmt(row.get('avg_pct'))}"))
    parts.append("</svg>")
    return "".join(parts)


# --- impure: DB + market ----------------------------------------------------


def session_inputs(row: KssSession) -> dict:
    """Plain-data view of one session row: ladder levels, next pending rung, cost basis."""
    from app.kss.service import _to_pyramid  # lazy: service pulls the whole trading stack

    py = _to_pyramid(row)
    pend = sorted((w for w in py.waves if w.status in _PENDING), key=lambda w: w.wave_num)
    nxt = pend[0] if pend else None
    return {
        "id": row.id, "symbol": row.symbol,
        "filled": sum(1 for w in py.waves if w.status == "filled"),
        "max_waves": row.max_waves, "entry": float(row.entry_price or 0.0),
        "avg": float(row.avg_price or 0.0), "tp": float(py.estimated_tp_price or 0.0),
        "next_price": float(nxt.target_price) if nxt else 0.0,
        "next_qty": float(nxt.quantity) if nxt else 0.0,
        "qty": float(row.total_filled_qty or 0.0), "invested": float(row.total_cost or 0.0),
        "created_ts": chart_data.to_epoch(row.created_at),
    }


def _btc_bars(oldest_ts: int, now_ts: int) -> list[dict]:
    """ONE BTC 1h fetch covering the oldest open session; [] on rate hold or any failure."""
    if execution.rate_hold_active():
        return []
    from app.scanner import _provider_factory  # lazy: scanner is heavy and imports this layer

    # Never ask for more than the cap: a series that cannot reach back would be treated as a cold
    # miss and re-fetched on every poll. Sessions older than the cap simply show "—".
    need_ts = max(oldest_ts, now_ts - (BTC_MAX_BARS - 3) * _H)
    limit = int(min(BTC_MAX_BARS, max(2, (now_ts - need_ts) // _H + 2)))
    try:
        raw, _hit = candle_cache.get_candles(
            settings.data_exchange, "BTC", BTC_TIMEFRAME, limit, _provider_factory,
            need_from_ms=need_ts * 1000,
        )
    except Exception as exc:  # noqa: BLE001 — market data must never break the panel
        logger.warning("session map: BTC candle fetch failed: %s", exc)
        return []
    return list(raw or [])


def _free_cash(db: Session) -> float:
    from app.kss.service import _idle_deployable  # the same "cash" the summary shows

    try:
        return float(_idle_deployable(db))
    except Exception as exc:  # noqa: BLE001
        logger.warning("session map: free cash unavailable: %s", exc)
        return 0.0


def build_map(db: Session, sort: str | None = None, direction: str | None = None,
              keep: list[int] | None = None) -> dict:
    """Assemble the panel context for every ACTIVE session. Never raises on market-data errors.

    ``keep`` (the ids the reader currently sees, in display order) freezes the row order across
    an auto-refresh; ``reorder_pending`` then says the fresh sort would order them differently."""
    key, dirn = normalize_sort(sort, direction)
    now_ts = chart_data.to_epoch(utcnow())
    rows_db = (
        db.query(KssSession)
        .options(selectinload(KssSession.waves))
        .filter(KssSession.status == SESSION_ACTIVE)
        .all()
    )
    inputs = [session_inputs(r) for r in rows_db]
    free_cash = _free_cash(db) if inputs else 0.0

    prices: dict[str, float] = {}
    btc_bars: list[dict] = []
    if inputs:
        try:
            prices = get_current_prices(sorted({i["symbol"] for i in inputs} | {"BTC"})) or {}
        except Exception as exc:  # noqa: BLE001
            logger.warning("session map: price fetch failed: %s", exc)
        btc_bars = _btc_bars(min(i["created_ts"] for i in inputs), now_ts)
    btc_now = prices.get("BTC") or (float(btc_bars[-1]["close"]) if btc_bars else None)

    rows = [
        build_row(i, price=prices.get(i["symbol"]), btc_now=btc_now,
                  btc_open=btc_at(btc_bars, i["created_ts"]) if btc_bars else None,
                  free_cash=free_cash, now_ts=now_ts)
        for i in inputs
    ]
    rows = sort_rows(rows, key, dirn)
    reorder_pending = False
    if keep:
        shown = apply_keep(rows, keep)
        reorder_pending = [r["id"] for r in shown] != [r["id"] for r in rows]
        rows = shown
    for r in rows:
        r["svg"] = range_bar_svg(r)
    return {"rows": rows, "summary": summarize(rows, free_cash), "sort": key, "dir": dirn,
            "axis_pct": int(AXIS_PCT), "reorder_pending": reorder_pending}
