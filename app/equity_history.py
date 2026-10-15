"""Equity history ("Dòng thời gian"): hourly mark-to-market snapshots of the whole book.

The book never stored an equity history, and the old Performance curve is rebuilt from fills, i.e.
essentially REALIZED P&L: it only goes up while the account is marked down by the unrealized loss
on the open ladders. This module adds the missing record, display-only (nothing in trading reads
it, no order is placed or altered):

* ``replay``  - PURE reconstruction. Walks fills in time order and, for each hour, values the book
  at the close of that hour's 1h bar. Same numbers ``portfolio.summary_view`` reports:
  ``cash = capital + flows - cost_basis + realized_cum`` (cost basis is average-cost, buy fees
  included, exactly as ``orders._update_position`` keeps it).
* ``backfill`` - fetches the candles, runs ``replay`` and INSERTS rows for hours with no row yet
  (never overwrites a ``live`` row). ``start_backfill`` runs it on a background thread, one at a
  time, with its own DB session.
* ``write_live_snapshot`` - the hourly writer the scheduler calls: one ``live`` row per finished
  hour, from the same summary the dashboard shows. Never raises.
* ``build_series`` - the read-only payload behind ``GET /api/equity-history``.
"""

from __future__ import annotations

import calendar
import logging
import re
import threading
import time
from collections import Counter, defaultdict
from collections.abc import Callable
from datetime import datetime, timedelta

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import audit, execution, portfolio
from app.clock import utcnow
from app.config import settings
from app.market import get_current_prices
from app.models import (
    SESSION_ACTIVE,
    SESSION_PENDING,
    AuditLog,
    Deposit,
    EquitySnapshot,
    Fill,
    KssSession,
)

logger = logging.getLogger(__name__)

HOUR = timedelta(hours=1)
DUST = 1e-12
TIMEFRAME = "1h"
BTC = "BTC"
BATCH = 200
LIVE_MAX_LAG_MIN = 20          # the live writer only stamps an hour it is (almost) on time for
RANGES = {"7": 7, "30": 30}    # `days` query values; anything else means "all"
_SRC_SID = re.compile(r"^pyramid:(\d+):")

Span = tuple[datetime, datetime | None]


# --- helpers ---------------------------------------------------------------


def hour_floor(dt: datetime) -> datetime:
    """The UTC start of the hour containing ``dt`` (naive in, naive out)."""
    return dt.replace(minute=0, second=0, microsecond=0)


def to_epoch(dt: datetime) -> int:
    """Naive-UTC datetime -> UTC epoch seconds."""
    return calendar.timegm(dt.timetuple())


def _g(row, key: str, default=None):
    """Read ``key`` from a dict or an ORM object (replay accepts both)."""
    if isinstance(row, dict):
        return row.get(key, default)
    return getattr(row, key, default)


# --- pure reconstruction ---------------------------------------------------


def replay(
    fills: list,
    start_capital: float,
    flows: list,
    hours: list[datetime],
    price_at: Callable[[str, datetime], float | None],
    session_spans: list[Span],
    btc_at: Callable[[datetime], float | None],
) -> list[dict]:
    """One snapshot dict per hour in ``hours`` (ascending hour STARTS).

    A row for hour H is the state after every fill/flow with ``executed_at < H + 1h``, priced at
    ``price_at(symbol, H)`` (the close of bar H). A symbol with no candle for H carries its last
    known close forward; with none yet it is valued at its last fill price - both counted in the
    row's ``fallbacks`` (and named in ``fallback_syms``). ``fills`` are dicts/objects with
    symbol, side, quantity, price, fee, realized_pnl, executed_at; ``flows`` are
    ``(datetime, signed_amount, ...)``; ``session_spans`` are ``(start, end|None)``.

    Positions use the same average-cost book as ``orders._update_position``: a BUY adds
    ``qty*price + fee`` to the cost, a SELL (clamped to what is held) removes ``avg * qty``.
    ``realized_cum`` sums the fills' own stored ``realized_pnl``. Quantities under ``DUST`` count
    as flat. Each row also carries ``holdings`` ({symbol: qty}) for inspection; it is not stored.
    """
    ordered = sorted(
        ({"symbol": _g(f, "symbol"), "side": str(_g(f, "side", "")).upper(),
          "qty": float(_g(f, "quantity", 0.0) or 0.0), "price": float(_g(f, "price", 0.0) or 0.0),
          "fee": float(_g(f, "fee", 0.0) or 0.0),
          "realized": float(_g(f, "realized_pnl", 0.0) or 0.0), "at": _g(f, "executed_at")}
         for f in fills if _g(f, "executed_at") is not None),
        key=lambda f: f["at"],
    )
    flow_list = sorted(((fl[0], float(fl[1])) for fl in flows), key=lambda x: x[0])

    qty: dict[str, float] = {}
    cost: dict[str, float] = {}
    last_px: dict[str, float] = {}      # last fill price per symbol (valuation of last resort)
    last_close: dict[str, float] = {}   # last candle close seen per symbol
    touched: list[str] = []
    realized_cum = 0.0
    flow_cum = 0.0
    i = j = 0
    rows: list[dict] = []

    for hour in hours:
        end = hour + HOUR
        while i < len(ordered) and ordered[i]["at"] < end:
            f = ordered[i]
            i += 1
            sym = f["symbol"]
            if sym not in qty:
                qty[sym] = 0.0
                cost[sym] = 0.0
                touched.append(sym)
            last_px[sym] = f["price"]
            realized_cum += f["realized"]
            if f["side"] == "BUY":
                qty[sym] += f["qty"]
                cost[sym] += f["qty"] * f["price"] + f["fee"]
            else:
                held = max(qty[sym], 0.0)
                sell = min(f["qty"], held)
                if sell > 0 and held > 0:
                    cost[sym] = max(0.0, cost[sym] - cost[sym] / held * sell)
                    qty[sym] = max(0.0, held - sell)
            if qty[sym] <= DUST:
                qty[sym] = 0.0
                cost[sym] = 0.0
        while j < len(flow_list) and flow_list[j][0] < end:
            flow_cum += flow_list[j][1]
            j += 1

        for sym in touched:                 # keep every traded symbol's last close fresh
            p = price_at(sym, hour)
            if p is not None and p > 0:
                last_close[sym] = float(p)
        market_value = 0.0
        cost_basis = 0.0
        holdings: dict[str, float] = {}
        fb: list[str] = []
        for sym in touched:
            q = qty[sym]
            if q <= DUST:
                continue
            p = price_at(sym, hour)
            if p is None or p <= 0:
                p = last_close.get(sym)
                fb.append(sym)
                if p is None:
                    p = last_px.get(sym, 0.0)
            market_value += q * p
            cost_basis += cost[sym]
            holdings[sym] = q

        cash = start_capital + flow_cum - cost_basis + realized_cum
        active = sum(1 for s, e in session_spans if s <= end and (e is None or end < e))
        btc = btc_at(hour)
        rows.append({
            "ts": hour,
            "equity": cash + market_value,
            "cash": cash,
            "market_value": market_value,
            "cost_basis": cost_basis,
            "unrealized": market_value - cost_basis,
            "realized_cum": realized_cum,
            "active_sessions": active,
            "btc_price": float(btc) if btc is not None and btc > 0 else None,
            "fallbacks": len(fb),
            "fallback_syms": sorted(fb),
            "holdings": holdings,
        })
    return rows


# --- DB-side inputs --------------------------------------------------------


def session_spans(db: Session) -> list[Span]:
    """``(start, end|None)`` per started session. A finished session ends at its last SELL fill
    (fallback: ``last_fill_at``, then ``created_at``); an ``active`` one is still open."""
    last_sell: dict[int, datetime] = {}
    for ref, at in (
        db.query(Fill.source_ref, Fill.executed_at)
        .filter(Fill.side == "SELL", Fill.source_ref.like("pyramid:%"))
        .all()
    ):
        m = _SRC_SID.match(ref or "")
        if m and at is not None:
            sid = int(m.group(1))
            if sid not in last_sell or at > last_sell[sid]:
                last_sell[sid] = at
    spans: list[Span] = []
    for s in db.query(KssSession).filter(KssSession.status != SESSION_PENDING).all():
        if s.created_at is None:
            continue
        if s.status == SESSION_ACTIVE:
            end = None
        else:
            end = last_sell.get(s.id) or s.last_fill_at or s.created_at
        spans.append((s.created_at, end))
    return spans


def snapshot_count(db: Session) -> int:
    """How many hourly snapshots exist (0 -> the timeline shows the "run the backfill" hint)."""
    return int(db.query(EquitySnapshot).count())


def active_session_count(db: Session) -> int:
    """Sessions currently ``active`` (the "đang mở N phiên" number)."""
    return int(db.query(KssSession).filter(KssSession.status == SESSION_ACTIVE).count())


# --- backfill --------------------------------------------------------------

_run_lock = threading.Lock()          # one backfill at a time
_status_lock = threading.Lock()
_status: dict = {"state": "idle"}


def backfill_status() -> dict:
    """A copy of the last/current backfill status: ``idle|running|done|error`` plus counts."""
    with _status_lock:
        return dict(_status)


def _set_status(**kw) -> None:
    with _status_lock:
        _status.update(kw)


class BackfillBusy(RuntimeError):
    """A backfill is already running."""


def _candle_hour(ts_ms: int) -> datetime:
    return datetime(1970, 1, 1) + timedelta(milliseconds=int(ts_ms))


def _provider():
    from app import scanner  # lazy: scanner is heavy; looked up at call time so tests can patch it

    return scanner._provider_factory(settings.data_exchange)


def backfill(db: Session, since: datetime | None = None, now: datetime | None = None) -> dict:
    """Reconstruct the missing hourly rows (first fill .. last complete hour) and INSERT them.

    Idempotent: hours that already have a row are skipped (a ``live`` row is never touched) and,
    with nothing missing, no candle is fetched. Candles come from the exchange (the research DB
    stops at 2026-07-31); a rate hold stops the fetching and the run reports ``aborted`` - rows
    whose valuation would have needed a symbol that was never fetched are NOT written (they
    would be wrong and never corrected), everything else is kept. Returns the audit counts."""
    t0 = time.monotonic()
    now = now or utcnow()
    result = {"hours_written": 0, "hours_missing": 0, "symbols_fetched": 0,
              "symbols_missing": [], "fallback_hours": 0, "fallback_by_symbol": {},
              "aborted": None, "duration_s": 0.0}

    fills = db.query(Fill).order_by(Fill.executed_at, Fill.id).all()
    if not fills:
        result["aborted"] = "no_fills"
        return _finish(db, result, t0)
    last_h = hour_floor(now) - HOUR                      # last COMPLETE hour
    first_h = hour_floor(fills[0].executed_at)
    if since is not None:
        first_h = max(first_h, hour_floor(since))
    all_hours = []
    h = first_h
    while h <= last_h:
        all_hours.append(h)
        h += HOUR
    existing = {t for (t,) in db.query(EquitySnapshot.ts).filter(EquitySnapshot.ts >= first_h)}
    hours = [h for h in all_hours if h not in existing]
    result["hours_missing"] = len(hours)
    if not hours:
        return _finish(db, result, t0)

    # --- candles: BTC first, then each traded symbol (from its first fill, or the first gap)
    first_fill: dict[str, datetime] = {}
    for f in fills:
        first_fill.setdefault(f.symbol, f.executed_at)
    symbols = [BTC] + sorted(s for s in first_fill if s != BTC)
    closes: dict[str, dict[datetime, float]] = {}
    unfetched: set[str] = set()
    missing: list[str] = []
    provider = None
    total = len(symbols)
    for idx, sym in enumerate(symbols):
        if execution.rate_hold_active():
            result["aborted"] = "rate_hold"
            unfetched.update(symbols[idx:])
            break
        start = hours[0] if sym == BTC else max(hours[0], hour_floor(first_fill[sym]))
        limit = int((hour_floor(now) - start) / HOUR) + 2
        try:
            provider = provider or _provider()
            candles = provider.get_ohlcv(sym, TIMEFRAME, limit)
        except Exception as exc:  # noqa: BLE001 - one bad symbol must not end the run
            logger.warning("equity backfill: candles failed for %s: %s", sym, exc)
            candles = []
        if not candles and execution.rate_hold_active():
            result["aborted"] = "rate_hold"
            unfetched.update(symbols[idx:])
            break
        if candles:
            closes[sym] = {_candle_hour(c["ts"]): float(c["close"]) for c in candles}
            result["symbols_fetched"] += 1
        else:
            missing.append(sym)
        _set_status(progress={"done": idx + 1, "total": total})
    result["symbols_missing"] = missing

    # --- replay + insert
    def price_at(sym: str, hour: datetime) -> float | None:
        return closes.get(sym, {}).get(hour)

    def btc_at(hour: datetime) -> float | None:
        return closes.get(BTC, {}).get(hour)

    flows = portfolio._capital_flows(db)
    rows = replay(fills, settings.account_equity, flows, hours, price_at, session_spans(db), btc_at)
    if BTC in unfetched:
        rows = []
    elif unfetched:
        rows = [r for r in rows if not (set(r["fallback_syms"]) & unfetched)]
    result["fallback_hours"] = sum(1 for r in rows if r["fallbacks"])
    # which symbols were valued without their own candle, and for how many hours (top 10)
    fb_count = Counter(s for r in rows for s in r["fallback_syms"])
    result["fallback_by_symbol"] = dict(fb_count.most_common(10))
    result["hours_written"] = _insert_rows(db, rows)
    return _finish(db, result, t0)


def _insert_rows(db: Session, rows: list[dict]) -> int:
    """INSERT rows whose ``ts`` has no snapshot yet, committing in batches. A concurrent writer
    that wins a ts (UNIQUE) just means that row is skipped."""
    written = 0
    for k in range(0, len(rows), BATCH):
        batch = rows[k:k + BATCH]
        have = {t for (t,) in db.query(EquitySnapshot.ts).filter(
            EquitySnapshot.ts.in_([r["ts"] for r in batch]))}
        fresh = [r for r in batch if r["ts"] not in have]
        try:
            for r in fresh:
                db.add(_to_model(r, "backfill"))
            db.commit()
            written += len(fresh)
        except IntegrityError:
            db.rollback()
            for r in fresh:                      # a live row raced us: insert one by one
                try:
                    db.add(_to_model(r, "backfill"))
                    db.commit()
                    written += 1
                except IntegrityError:
                    db.rollback()
    return written


def _to_model(r: dict, source: str) -> EquitySnapshot:
    return EquitySnapshot(
        ts=r["ts"], equity=r["equity"], cash=r["cash"], market_value=r["market_value"],
        cost_basis=r["cost_basis"], unrealized=r["unrealized"], realized_cum=r["realized_cum"],
        active_sessions=int(r["active_sessions"]), btc_price=r["btc_price"], source=source,
    )


def _finish(db: Session, result: dict, t0: float) -> dict:
    result["duration_s"] = round(time.monotonic() - t0, 2)
    try:
        audit.log(db, "equity-history", "equity_backfill", None, **result)
        db.commit()
    except Exception:  # noqa: BLE001 - the audit row is a courtesy; the rows are already in
        logger.exception("equity backfill: audit row failed")
        db.rollback()
    return result


def start_backfill(since: datetime | None = None) -> dict:
    """Start ``backfill`` on a daemon thread with its own session. Raises ``BackfillBusy`` when
    one is already running. Returns the (running) status."""
    if not _run_lock.acquire(blocking=False):
        raise BackfillBusy("a backfill is already running")
    _set_status(state="running", started_at=utcnow().isoformat(), finished_at=None, error=None,
                result=None, progress={"done": 0, "total": 0})

    def _run() -> None:
        from app.db import SessionLocal

        db = SessionLocal()
        try:
            res = backfill(db, since)
            _set_status(state="done", finished_at=utcnow().isoformat(), result=res)
        except Exception as exc:  # noqa: BLE001
            logger.exception("equity backfill failed")
            _set_status(state="error", finished_at=utcnow().isoformat(), error=str(exc)[:300])
        finally:
            db.close()
            _run_lock.release()

    try:
        threading.Thread(target=_run, name="equity-backfill", daemon=True).start()
    except Exception:
        _run_lock.release()
        raise
    return backfill_status()


# --- live hourly writer ----------------------------------------------------


def write_live_snapshot(db: Session, now: datetime | None = None,
                        max_lag_min: float = LIVE_MAX_LAG_MIN) -> EquitySnapshot | None:
    """Insert the ``live`` row for the hour that just finished, if missing. Uses the same summary
    the dashboard shows. Returns the row, or None when nothing was written (already there, too
    late for that hour - the backfill covers it - a position without a price, or any error).
    Never raises, never touches an order."""
    try:
        now = now or utcnow()
        hour = hour_floor(now) - HOUR
        if now - (hour + HOUR) > timedelta(minutes=max_lag_min):
            return None
        if db.query(EquitySnapshot.id).filter(EquitySnapshot.ts == hour).first():
            return None
        if any(p["current_price"] <= 0 for p in portfolio.positions_view(db)):
            logger.warning("equity snapshot skipped: an open position has no price")
            return None                     # a 0 price would book a bogus equity dip
        s = portfolio.summary_view(db)
        btc = get_current_prices([BTC]).get(BTC)
        row = EquitySnapshot(
            ts=hour, equity=s["total_equity"], cash=s["cash"], market_value=s["total_market_value"],
            cost_basis=s["total_invested"], unrealized=s["unrealized_pnl"],
            realized_cum=s["realized_pnl"], active_sessions=active_session_count(db),
            btc_price=float(btc) if btc else None, source="live",
        )
        db.add(row)
        db.commit()
        return row
    except IntegrityError:
        db.rollback()                        # lost a race with the backfill: the row exists
        return None
    except Exception:  # noqa: BLE001 - must never break the scheduler
        logger.exception("equity snapshot failed")
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return None


# --- read side -------------------------------------------------------------


def _capital_at(start: float, deposits: list[tuple[datetime, float]], end: datetime) -> float:
    return start + sum(a for t, a in deposits if t < end)


def build_series(db: Session, days: str = "30", now: datetime | None = None) -> dict:
    """Payload for ``GET /api/equity-history``. Series are ``[{time, value}]`` in UTC epoch
    seconds; percentages are against the capital contributed by then (start capital + deposits),
    so a deposit is not a gain. ``btc_pct`` is against the first snapshot overall, so every % line
    shares one base. ``daily`` bars sit at the first snapshot of their UTC day (00:00 for a full
    day) so the chart's index-based time scale gains no extra slots."""
    now = now or utcnow()
    span = RANGES.get(days)
    q = db.query(EquitySnapshot).order_by(EquitySnapshot.ts)
    if span:
        q = q.filter(EquitySnapshot.ts >= hour_floor(now) - timedelta(days=span))
    snaps = q.all()
    first_btc = (
        db.query(EquitySnapshot.btc_price)
        .filter(EquitySnapshot.btc_price.isnot(None), EquitySnapshot.btc_price > 0)
        .order_by(EquitySnapshot.ts).limit(1).scalar()
    )
    deposits = [(d.created_at, d.amount) for d in db.query(Deposit).all() if d.created_at]
    start = settings.account_equity

    out: dict[str, list] = {k: [] for k in (
        "equity_pct", "realized_pct", "btc_pct", "cash_pct", "active_sessions", "equity", "cash")}
    for s in snaps:
        t = to_epoch(s.ts)
        cap = _capital_at(start, deposits, s.ts + HOUR) or 1.0
        out["equity_pct"].append({"time": t, "value": (s.equity - cap) / cap * 100})
        out["realized_pct"].append({"time": t, "value": s.realized_cum / cap * 100})
        out["cash_pct"].append({"time": t, "value": (s.cash / s.equity * 100) if s.equity else 0.0})
        out["active_sessions"].append({"time": t, "value": s.active_sessions})
        out["equity"].append({"time": t, "value": s.equity})
        out["cash"].append({"time": t, "value": s.cash})
        if first_btc and s.btc_price:
            out["btc_pct"].append({"time": t, "value": (s.btc_price / first_btc - 1) * 100})

    return {
        "days": days if span else "all",
        "count": len(snaps),
        "start_capital": start,
        "first_ts": to_epoch(snaps[0].ts) if snaps else None,
        "last_ts": to_epoch(snaps[-1].ts) if snaps else None,
        **out,
        "daily": _daily_events(db, snaps),
    }


def _daily_events(db: Session, snaps: list[EquitySnapshot]) -> dict:
    """Per UTC day: TP sells, rung fills and cash-starved rungs, keyed to that day's first
    snapshot time. Days with no snapshot in range are dropped."""
    empty = {"tp": [], "rungs": [], "starved": []}
    if not snaps:
        return empty
    day_time: dict[str, int] = {}
    for s in snaps:                                  # ascending: first snapshot per day wins
        day_time.setdefault(s.ts.strftime("%Y-%m-%d"), to_epoch(s.ts))
    since = snaps[0].ts
    tp: dict[str, int] = defaultdict(int)
    rungs: dict[str, int] = defaultdict(int)
    starved: dict[str, int] = defaultdict(int)
    for side, ref, at in (
        db.query(Fill.side, Fill.source_ref, Fill.executed_at).filter(Fill.executed_at >= since)
    ):
        ref = ref or ""
        day = at.strftime("%Y-%m-%d")
        if side == "SELL" and (ref.endswith(":tp") or ref.endswith(":manual_tp")):
            tp[day] += 1
        elif side == "BUY" and ":wave:" in ref:
            rungs[day] += 1
    for (at,) in db.query(AuditLog.created_at).filter(
        AuditLog.action == "rung_starved", AuditLog.created_at >= since
    ):
        starved[at.strftime("%Y-%m-%d")] += 1

    def series(counts: dict[str, int]) -> list[dict]:
        return [{"time": day_time[d], "value": n} for d, n in sorted(counts.items())
                if d in day_time and n > 0]

    return {"tp": series(tp), "rungs": series(rungs), "starved": series(starved)}


def curve_for_period(db: Session, period: str, now: datetime | None = None,
                     live_equity: float | None = None) -> tuple[list[float], list[str]] | None:
    """Mark-to-market equity curve ``(values, iso_times)`` for the Performance panel, or None
    when there are fewer than 2 snapshots in the period (the panel then keeps its fills-based
    curve). A last point at ``now`` carries the live equity so the curve ends on the card."""
    now = now or utcnow()
    hours = portfolio._PERIODS.get(period)
    q = db.query(EquitySnapshot).order_by(EquitySnapshot.ts)
    if hours:
        q = q.filter(EquitySnapshot.ts >= now - timedelta(hours=hours))
    snaps = q.all()
    if len(snaps) < 2:
        return None
    values = [s.equity for s in snaps]
    times = [s.ts.isoformat() for s in snaps]
    if live_equity is not None:
        values.append(float(live_equity))
        times.append(now.isoformat())
    return values, times
