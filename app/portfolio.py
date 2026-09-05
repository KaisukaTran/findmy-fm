"""
Read-side views for the dashboard: positions, trade history, and summary.

These are pure reads derived from fills/positions plus live market prices.
Kept out of the route layer so routes stay thin.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.clock import utcnow
from app.config import settings
from app.market import get_current_prices
from app.models import SESSION_ACTIVE, Fill, KssSession, PendingOrder, Position


def order_source(source_ref: str | None) -> str:
    """Provenance tag for a fill/order from its source_ref (OPUS / KSS / manual / auto)."""
    if not source_ref:
        return "manual"
    if source_ref.startswith("opus:"):
        return "OPUS"
    if source_ref.startswith("pyramid:"):
        return "KSS"
    return "auto"


def _symbol_owners(db: Session) -> dict[str, list[str]]:
    """Map each symbol to who currently manages it: OPUS (watch/ride) and/or KSS (active)."""
    from app.models import SESSION_ACTIVE, KssSession  # local import (avoid heavy coupling)
    from app.orchestrator.models import OPUS_RIDE, OPUS_WATCH, OpusPosition

    owners: dict[str, list[str]] = {}
    for (sym,) in db.query(OpusPosition.symbol).filter(
        OpusPosition.state.in_((OPUS_WATCH, OPUS_RIDE))
    ).distinct():
        owners.setdefault(sym, []).append("OPUS")
    for (sym,) in db.query(KssSession.symbol).filter(
        KssSession.status == SESSION_ACTIVE
    ).distinct():
        owners.setdefault(sym, []).append("KSS")
    return owners


# Columns the Positions table may be sorted by (click a header). Whitelisted so a
# crafted ?sort= can only ever pick one of these dict keys.
POSITION_SORT_KEYS = frozenset(
    {"symbol", "quantity", "avg_entry_price", "current_price", "market_value", "unrealized_pnl"}
)


def positions_view(
    db: Session, sort: str | None = None, direction: str = "asc"
) -> list[dict]:
    """Open positions enriched with live price, market value and unrealized P&L.

    When ``sort`` is one of ``POSITION_SORT_KEYS`` the rows are ordered by that column
    (``direction`` = ``asc``|``desc``); otherwise the natural DB order is kept.
    """
    positions = db.query(Position).filter(Position.quantity > 0).all()
    if not positions:
        return []
    from app import risk  # lazy: risk -> portfolio; avoid an import cycle at load

    prices = get_current_prices([p.symbol for p in positions])
    owners = _symbol_owners(db)
    # Total equity (computed inline — calling equity() here would recurse into positions_view).
    total_mv = sum(p.quantity * prices.get(p.symbol, 0.0) for p in positions)
    total_invested = sum(p.total_cost for p in positions)
    realized = float(db.query(func.coalesce(func.sum(Fill.realized_pnl), 0.0)).scalar() or 0.0)
    equity = (risk.capital_anchor(db) - total_invested + realized) + total_mv
    eq = equity or 1.0
    rows = []
    for p in positions:
        price = prices.get(p.symbol, 0.0)
        market_value = p.quantity * price
        unrealized = market_value - p.total_cost
        rows.append(
            {
                "symbol": p.symbol,
                "quantity": p.quantity,
                "avg_entry_price": p.avg_entry_price,
                "total_cost": p.total_cost,
                "current_price": price,
                "market_value": market_value,
                "market_value_pct": market_value / eq * 100,  # % of total equity
                "unrealized_pnl": unrealized,
                "unrealized_pnl_pct": (unrealized / p.total_cost * 100) if p.total_cost else 0.0,
                "sources": owners.get(p.symbol, []),  # ["OPUS"], ["KSS"], or both
            }
        )
    if sort in POSITION_SORT_KEYS:
        rows.sort(
            key=lambda r: r[sort].lower() if isinstance(r[sort], str) else r[sort],
            reverse=(direction == "desc"),
        )
    return rows


_LOSS_CAUSES = {
    "OPUS": "OPUS đóng vị thế lỗ (hard-stop hoặc quyết định của Opus)",
    "KSS-SL": "Cắt lỗ KSS: giá ≤ avg×(1−SL%)",
    "KSS-Trail": "Trailing KSS: giá rớt quá ngưỡng từ đỉnh sau khi đã có lãi",
    "KSS-TP?": "‘Chốt lời’ KSS nhưng LỖ — avg tổng của coin cao hơn giá TP của session "
               "(nhiều session cùng coin chung một vị thế tổng). Cần xem lại.",
    "Khác": "Không rõ nguồn / lệnh thủ công",
}


def _loss_tag(source_ref: str | None) -> str:
    if not source_ref:
        return "Khác"
    if source_ref.startswith("opus:"):
        return "OPUS"
    if source_ref.endswith(":sl"):
        return "KSS-SL"
    if source_ref.endswith(":trailing"):
        return "KSS-Trail"
    if source_ref.endswith(":tp"):
        return "KSS-TP?"
    return "Khác"


def loss_analysis(db: Session, limit: int = 300) -> dict:
    """Every losing fill with its cause, plus breakdowns by cause and by pair (for strategy
    improvement). Read-only; loss = realized_pnl < 0."""
    from app import timefmt

    losses = (
        db.query(Fill)
        .filter(Fill.realized_pnl < 0)
        .order_by(Fill.executed_at.desc())
        .limit(limit)
        .all()
    )
    rows, by_cause, by_pair = [], {}, {}
    for f in losses:
        tag = _loss_tag(f.source_ref)
        loss = float(f.realized_pnl or 0.0)
        rows.append({
            "time": timefmt.local_dt(f.executed_at),
            "symbol": f.symbol,
            "side": f.side,
            "quantity": f.quantity,
            "value": f.quantity * f.price,
            "loss": loss,
            "fee": float(f.fee or 0.0),
            "tag": tag,
            "reason": _LOSS_CAUSES.get(tag, tag),
            "source_ref": f.source_ref or "",
        })
        c = by_cause.setdefault(tag, {"count": 0, "total": 0.0})
        c["count"] += 1
        c["total"] += loss
        p = by_pair.setdefault(f.symbol, {"count": 0, "total": 0.0})
        p["count"] += 1
        p["total"] += loss
    total = sum(r["loss"] for r in rows)
    by_pair_sorted = sorted(by_pair.items(), key=lambda kv: kv[1]["total"])  # worst first
    return {
        "rows": rows,
        "count": len(rows),
        "total": total,
        "by_cause": by_cause,
        "by_pair": by_pair_sorted[:10],
    }


def trades_view(
    db: Session, limit: int = 50, offset: int = 0, side: str | None = None
) -> list[dict]:
    """Most recent fills (trade history), tagged with their provenance (OPUS/KSS/…).

    ``side`` filters to a single direction (``"BUY"``/``"SELL"``); ``None`` returns both."""
    q = db.query(Fill).order_by(Fill.executed_at.desc())
    if side in ("BUY", "SELL"):
        q = q.filter(Fill.side == side)
    fills = q.offset(offset).limit(limit).all()
    out = []
    for f in fills:
        d = f.to_dict()
        d["source"] = order_source(f.source_ref)
        out.append(d)
    return out


def equity(db: Session) -> float:
    """Live mark-to-market equity = cash + open market value.

    ``cash``'s base is ``risk.capital_anchor(db)`` (Phase 0, docs/capital-scaling-2026-08-23.md
    §2.1) — the real exchange balance on live when opted in, else ``settings.account_equity``
    (paper: always, byte-identical to pre-Phase-0 behaviour).
    """
    from app import risk  # lazy: risk -> portfolio; avoid an import cycle at load

    positions = positions_view(db)
    total_market_value = sum(p["market_value"] for p in positions)
    total_invested = sum(p["total_cost"] for p in positions)
    realized_pnl = float(
        db.query(func.coalesce(func.sum(Fill.realized_pnl), 0.0)).scalar() or 0.0
    )
    cash = risk.capital_anchor(db) - total_invested + realized_pnl
    return cash + total_market_value


def summary_view(db: Session) -> dict:
    """Portfolio summary: equity, realized/unrealized P&L, counts."""
    from app import risk  # lazy: risk -> portfolio; avoid an import cycle at load

    positions = positions_view(db)
    total_market_value = sum(p["market_value"] for p in positions)
    total_invested = sum(p["total_cost"] for p in positions)
    unrealized_pnl = sum(p["unrealized_pnl"] for p in positions)

    realized_pnl = float(
        db.query(func.coalesce(func.sum(Fill.realized_pnl), 0.0)).scalar() or 0.0
    )
    total_trades = db.query(func.count(Fill.id)).scalar() or 0
    pending_count = (
        db.query(func.count(PendingOrder.id)).filter(PendingOrder.status == "pending").scalar() or 0
    )

    cash = risk.capital_anchor(db) - total_invested + realized_pnl
    total_equity = cash + total_market_value
    base = settings.account_equity or 1.0  # % of starting capital for P&L (unadjusted — a
    # historical baseline for the P&L ratio, not a live cash figure; see docs/capital-scaling)
    eq = total_equity or 1.0
    return {
        "total_trades": int(total_trades),
        "pending_count": int(pending_count),
        "positions_count": len(positions),
        "realized_pnl": realized_pnl,
        "realized_pct": realized_pnl / base * 100,
        "unrealized_pnl": unrealized_pnl,
        "unrealized_pct": (unrealized_pnl / total_invested * 100) if total_invested else 0.0,
        "total_invested": total_invested,
        "total_market_value": total_market_value,
        "market_value_pct": total_market_value / eq * 100,
        "cash": cash,
        "cash_pct": cash / eq * 100,
        "total_equity": total_equity,
    }


def _resting_buy_notional(db: Session) -> float:
    """Σ quantity × price over PENDING BUY LIMIT orders — cash already earmarked for a fill
    the venue has not made yet (so it is neither `deployed` nor `free_cash`)."""
    from app.models import PENDING

    total = (
        db.query(func.coalesce(func.sum(PendingOrder.quantity * PendingOrder.price), 0.0))
        .filter(
            PendingOrder.status == PENDING,
            PendingOrder.side == "BUY",
            PendingOrder.order_type == "LIMIT",
        )
        .scalar()
    )
    return float(total or 0.0)


def _snap5(pct: float) -> int:
    """Clamp to [0, 100] and round to the nearest 5% step (the only widths CSS defines)."""
    pct = max(0.0, min(100.0, pct))
    return int(round(pct / 5.0)) * 5


def _capital_bar(
    equity: float, backup: float, free_after_backup: float, resting_buy: float, deployed: float,
) -> list[dict]:
    """Stacked-bar segments for `partials/capital.html` (D1/D5).

    The four values here are disjoint (they must already sum to ``equity`` — the caller is
    responsible for that, e.g. passing ``free_after_backup`` rather than raw ``free_cash``,
    which double-counts ``backup``). Every segment snaps to the nearest 5%, then the residual
    needed to reach exactly 100 is absorbed by the LARGEST segment. Absorbing it in the last
    segment instead would dump up to three roundings (±7.5%) onto ``deployed`` — the one
    number this panel exists to show, and typically the smallest slice, so the distortion
    would land where it does the most damage. The largest segment can carry the same residual
    invisibly. Only classes ``.cap-seg-backup|free|resting|deployed`` and ``.w-0``…``.w-100``
    (5% steps) are ever emitted — no inline ``style=`` (CSP-blocked).
    """
    segments = [
        ("cap-seg-backup", backup),
        ("cap-seg-free", free_after_backup),
        ("cap-seg-resting", resting_buy),
        ("cap-seg-deployed", deployed),
    ]
    bar = [
        {"cls": cls, "step": _snap5((value / equity * 100) if equity else 0.0), "value": value}
        for cls, value in segments
    ]
    if equity <= 0:
        return bar  # nothing to divide: an empty bar, not a bar that is 100% "backup"
    residual = 100 - sum(seg["step"] for seg in bar)
    if residual:
        sink = max(bar, key=lambda seg: seg["value"])
        sink["step"] = max(0, min(100, sink["step"] + residual))
        # Clamping the sink can leave the total off 100 (only when one segment is the whole
        # bar and the residual is negative); re-settle onto whichever segment still has room.
        drift = 100 - sum(seg["step"] for seg in bar)
        for seg in sorted(bar, key=lambda s: -s["value"]):
            if drift == 0:
                break
            room = (100 - seg["step"]) if drift > 0 else -seg["step"]
            take = drift if abs(drift) <= abs(room) else room
            seg["step"] += take
            drift -= take
    return bar


def capital_view(db: Session) -> dict:
    """Capital-utilisation panel: the equity split that shows why only part of the
    account is actually working (docs: measured audit put per-dollar edge near
    1%/day, but only ~29% of capital-days deployed -> ~0.42%/day portfolio return).

    Reuses ``summary_view`` for cash, ``risk.account_equity`` for mark-to-market equity,
    and the scanner's own lend-the-idle-reservation rule (``scanner._session_lock``) for
    what an active session actually locks against the deployable budget — none of that
    is re-derived here.
    """
    from app import risk  # lazy: risk -> portfolio; avoid an import cycle at load
    from app.scanner import _session_lock  # lazy: scanner -> orders -> risk -> portfolio

    active = db.query(KssSession).filter(KssSession.status == SESSION_ACTIVE).all()

    equity = risk.account_equity(db)
    backup = equity * settings.equity_backup_pct / 100
    budget = equity - backup

    deployed = sum(s.total_cost or 0.0 for s in active)
    resting_buy = _resting_buy_notional(db)
    committed = sum(s.isolated_fund or 0.0 for s in active)
    promised = max(committed - deployed - resting_buy, 0.0)

    cash = summary_view(db)["cash"]
    free_cash = max(cash - resting_buy, 0.0)
    # `backup` is a policy claim ON `free_cash`, not a disjoint fourth pot — subtract it so
    # the bar's four segments are disjoint and sum to `equity` (D1).
    free_after_backup = max(free_cash - backup, 0.0)

    locked_book = sum(_session_lock(s) for s in active)
    budget_free = max(budget - locked_book, 0.0)

    working_pct = (deployed + resting_buy) / equity * 100 if equity else 0.0
    committed_pct = committed / equity * 100 if equity else 0.0

    sessions_active = len(active)
    sessions_cap = settings.max_concurrent_sessions
    # The book's own evidence of what a typical session actually needs, instead of the flat
    # `scan_fund` constant (which the real scanner gate doesn't use either — it sizes off
    # `kss_service.projected_ladder_cost`, ~4x smaller on the live book — D2). No network
    # call: this endpoint is polled every 15s and that helper reaches for exchange info.
    typical_need = (committed / sessions_active) if sessions_active else settings.scan_fund
    if sessions_active >= sessions_cap:
        binding = "count"
    elif budget_free < typical_need:
        binding = "budget"
    else:
        binding = "none"

    bar = _capital_bar(equity, backup, free_after_backup, resting_buy, deployed)

    return {
        "equity": equity,
        "backup": backup,
        "budget": budget,
        "deployed": deployed,
        "resting_buy": resting_buy,
        "promised": promised,
        "committed": committed,
        "free_cash": free_cash,
        "free_after_backup": free_after_backup,
        "locked_book": locked_book,
        "budget_free": budget_free,
        "typical_need": typical_need,
        "working_pct": working_pct,
        "committed_pct": committed_pct,
        "sessions_active": sessions_active,
        "sessions_cap": sessions_cap,
        "binding": binding,
        "bar": bar,
    }


def _next_session_start(db: Session, s: KssSession, start: datetime) -> datetime | None:
    """Start time of the next session opened on the same symbol after ``start``, if any.

    Bounds the exit-time fallback (D3) so it can never wander into a later session's
    fills — without this, a stopped session with no own exit fill routinely resolves to
    whatever the NEXT session on that symbol later did, mis-dating it by days.
    """
    order_key = func.coalesce(KssSession.started_at, KssSession.created_at)
    nxt = (
        db.query(KssSession)
        .filter(KssSession.symbol == s.symbol, KssSession.id != s.id, order_key > start)
        .order_by(order_key.asc())
        .first()
    )
    if nxt is None:
        return None
    return nxt.started_at or nxt.created_at


def _session_exit_time(db: Session, s: KssSession, now: datetime) -> datetime | None:
    """When a finished KSS session actually stopped locking capital.

    Prefers the newest ``Fill`` whose order was a SELL for this session (the real exit,
    e.g. TP/SL/trailing/deadline) — NOT ``last_fill_at``, which an exit never updates and
    so understates how long the capital was locked. Falls back to the newest SELL fill for
    the session's symbol inside its own lifetime AND strictly before the next session on
    that symbol started (an ``orphan:`` sweep of this session's leftover inventory lands
    here legitimately) — bounded so it can never resolve to a LATER session's fill (D3).
    ``None`` (counted as ``skipped`` by the caller) if nothing can be found at all.
    """
    exit_fill = (
        db.query(Fill)
        .join(PendingOrder, Fill.pending_order_id == PendingOrder.id)
        .filter(
            PendingOrder.side == "SELL",
            PendingOrder.source_ref.like(f"pyramid:{s.id}:%"),
        )
        .order_by(Fill.executed_at.desc())
        .first()
    )
    if exit_fill is not None:
        return exit_fill.executed_at

    start = s.started_at or s.created_at
    if start is None:
        return None
    next_start = _next_session_start(db, s, start)
    fallback_q = db.query(Fill).filter(
        Fill.symbol == s.symbol,
        Fill.side == "SELL",
        Fill.executed_at >= start,
        Fill.executed_at <= now,
    )
    if next_start is not None:
        fallback_q = fallback_q.filter(Fill.executed_at < next_start)
    fallback = fallback_q.order_by(Fill.executed_at.desc()).first()
    return fallback.executed_at if fallback else None


def _own_exit_time(s_id: int, own_exit: dict[int, datetime]) -> datetime | None:
    """Newest SELL fill tagged exactly ``pyramid:{s_id}:...`` — the good path of D3's
    fallback order, looked up in an already-built map (no DB access)."""
    return own_exit.get(s_id)


def _resolve_exit_time(
    s: KssSession,
    now: datetime,
    own_exit: dict[int, datetime],
    sell_times_by_symbol: dict[str, list[datetime]],
    next_start: datetime | None,
) -> datetime | None:
    """Pure, DB-free re-implementation of ``_session_exit_time``'s resolution order (D3),
    given prebuilt lookups — the core of ``capital_yield_view``'s batched pass (D6)."""
    own = _own_exit_time(s.id, own_exit)
    if own is not None:
        return own

    start = s.started_at or s.created_at
    if start is None:
        return None
    best: datetime | None = None
    for t in sell_times_by_symbol.get(s.symbol, ()):
        if t < start or t > now:
            continue
        if next_start is not None and t >= next_start:
            continue
        if best is None or t > best:
            best = t
    return best


def capital_yield_view(db: Session, window_days: int = 7) -> dict:
    """Trailing-window realized yield per dollar-day of locked capital.

    For every KSS session that was ever active (status != pending), integrates its lock
    value (``scanner._session_lock``) over the hours it was active inside the window —
    ``now`` for a still-ACTIVE session, its exit-fill time for a finished one (see
    ``_session_exit_time`` / ``_resolve_exit_time`` for the same D3-bounded resolution
    order, applied here from prebuilt lookups so this stays O(1) queries — D6). Kept
    separate from ``capital_view`` so it can be tested on its own.

    ``realized_pnl_window`` is restricted to fills whose order is KSS-originated
    (``pyramid:`` or ``orphan:``) so the numerator covers the same book as the
    denominator (``locked_dollar_days`` only ever counts KSS sessions) — a manual or OPUS
    fill must not inflate the KSS-only yield ratio (D4).
    """
    from app import risk  # lazy: risk -> portfolio; avoid an import cycle at load
    from app.models import SESSION_PENDING
    from app.scanner import _session_lock  # lazy: scanner -> orders -> risk -> portfolio

    now = utcnow()
    window_start = now - timedelta(days=window_days)

    # One query for every session (any status — pending sessions still matter as
    # same-symbol ordering bounds for D3) instead of one per session (D6).
    all_sessions = db.query(KssSession).all()
    sessions = [s for s in all_sessions if s.status != SESSION_PENDING]

    by_symbol_sessions: dict[str, list[KssSession]] = {}
    for sess in all_sessions:
        by_symbol_sessions.setdefault(sess.symbol, []).append(sess)
    for lst in by_symbol_sessions.values():
        lst.sort(key=lambda x: x.started_at or x.created_at or window_start)

    # One query for every SELL fill instead of one/two per session (D6). `Fill.source_ref`
    # is copied from the order at fill time, so no join to `pending_orders` is needed.
    sell_fills = (
        db.query(Fill.symbol, Fill.source_ref, Fill.executed_at)
        .filter(Fill.side == "SELL")
        .all()
    )
    own_exit: dict[int, datetime] = {}
    sell_times_by_symbol: dict[str, list[datetime]] = {}
    for symbol, source_ref, executed_at in sell_fills:
        sell_times_by_symbol.setdefault(symbol, []).append(executed_at)
        if source_ref and source_ref.startswith("pyramid:"):
            parts = source_ref.split(":")
            if len(parts) >= 2:
                try:
                    sid = int(parts[1])
                except ValueError:
                    sid = None
                if sid is not None and (sid not in own_exit or executed_at > own_exit[sid]):
                    own_exit[sid] = executed_at

    locked_dollar_days = 0.0
    skipped = 0
    for s in sessions:
        start = s.started_at or s.created_at
        if start is None:
            continue
        if s.status == SESSION_ACTIVE:
            end = now
        else:
            same_symbol = by_symbol_sessions.get(s.symbol, [])
            idx = next((i for i, x in enumerate(same_symbol) if x.id == s.id), None)
            next_start = None
            if idx is not None:
                for nxt in same_symbol[idx + 1:]:
                    cand = nxt.started_at or nxt.created_at
                    if cand is not None and cand > start:
                        next_start = cand
                        break
            end = _resolve_exit_time(s, now, own_exit, sell_times_by_symbol, next_start)
            if end is None:
                skipped += 1
                continue
        overlap_start = max(start, window_start)
        overlap_end = min(end, now)
        if overlap_end <= overlap_start:
            continue
        hours = (overlap_end - overlap_start).total_seconds() / 3600.0
        locked_dollar_days += _session_lock(s) * hours / 24.0

    realized_pnl_window = float(
        db.query(func.coalesce(func.sum(Fill.realized_pnl), 0.0))
        .filter(
            Fill.executed_at >= window_start,
            Fill.executed_at <= now,
            or_(Fill.source_ref.like("pyramid:%"), Fill.source_ref.like("orphan:%")),
        )
        .scalar()
        or 0.0
    )

    pct_per_locked_dollar_day = (
        realized_pnl_window / locked_dollar_days * 100 if locked_dollar_days > 0 else None
    )
    equity = risk.account_equity(db)
    utilisation_pct = (
        locked_dollar_days / (equity * window_days) * 100 if equity and window_days else 0.0
    )

    return {
        "window_days": window_days,
        "locked_dollar_days": locked_dollar_days,
        "realized_pnl_window": realized_pnl_window,
        "pct_per_locked_dollar_day": pct_per_locked_dollar_day,
        "utilisation_pct": utilisation_pct,
        "skipped": skipped,
    }


# Performance period windows → lookback in hours (None = all-time).
_PERIODS: dict[str, int | None] = {"24h": 24, "7d": 24 * 7, "30d": 24 * 30, "all": None}


def _period_cutoff(period: str) -> datetime | None:
    """UTC cutoff for a period key, or None for all-time / unknown."""
    hours = _PERIODS.get(period)
    return utcnow() - timedelta(hours=hours) if hours else None


def performance_view(db: Session, period: str = "all") -> dict:
    """
    Realized-equity curve + win/loss + drawdown + expectancy, derived from fills.

    Equity is account_equity + cumulative realized P&L stamped at each fill (a
    "realized equity" curve), with a final point including current unrealized P&L.
    Win/loss counts SELL fills by realized P&L sign. When ``period`` restricts the
    window, the curve starts from the equity *as of* the cutoff (realized before it)
    so the line is continuous, and win/loss/expectancy reflect only the window.
    """
    all_fills = db.query(Fill).order_by(Fill.executed_at.asc()).all()
    cutoff = _period_cutoff(period)
    if cutoff is not None:
        before = [f for f in all_fills if f.executed_at and f.executed_at < cutoff]
        fills = [f for f in all_fills if not f.executed_at or f.executed_at >= cutoff]
        realized_before = sum(f.realized_pnl for f in before)
    else:
        fills = all_fills
        realized_before = 0.0

    base = settings.account_equity + realized_before
    now_iso = utcnow().isoformat()
    start_iso = fills[0].executed_at.isoformat() if fills else now_iso
    curve = [base]
    times = [start_iso]
    realized = 0.0
    wins = losses = 0
    win_sum = loss_sum = 0.0
    for f in fills:
        realized += f.realized_pnl
        curve.append(base + realized)
        times.append(f.executed_at.isoformat() if f.executed_at else now_iso)
        if f.side == "SELL":
            if f.realized_pnl > 0:
                wins += 1
                win_sum += f.realized_pnl
            elif f.realized_pnl < 0:
                losses += 1
                loss_sum += f.realized_pnl  # negative

    summary = summary_view(db)
    final_equity = summary["total_equity"]
    curve.append(final_equity)
    times.append(now_iso)

    # Two different numbers, and the difference matters. `max_dd` is the WORST dip the curve
    # ever took — a historical statistic that can only grow. `current_dd` is how far below the
    # running peak the account sits RIGHT NOW, and it falls back towards 0 as it recovers.
    # The circuit breaker needs the second: gating on the first means one bad day freezes
    # trading forever, because the reason to stay frozen can never clear.
    peak = curve[0]
    max_dd = 0.0
    for v in curve:
        peak = max(peak, v)
        if peak > 0:
            max_dd = max(max_dd, (peak - v) / peak * 100)
    current_dd = (peak - curve[-1]) / peak * 100 if peak > 0 and curve else 0.0

    closed = wins + losses
    gross_loss = -loss_sum  # positive magnitude
    return {
        "period": period,
        "equity_curve": curve,
        "equity_times": times,
        "realized_pnl": realized,
        "unrealized_pnl": summary["unrealized_pnl"],
        "total_equity": final_equity,
        "wins": wins,
        "losses": losses,
        "closed": closed,
        "win_rate": round(wins / closed * 100, 2) if closed else 0.0,
        "loss_rate": round(losses / closed * 100, 2) if closed else 0.0,
        "max_drawdown_pct": round(max_dd, 2),
        "current_drawdown_pct": round(max(current_dd, 0.0), 2),
        # Per-closed-trade economics (USDT).
        "expectancy": round((win_sum + loss_sum) / closed, 2) if closed else 0.0,
        "avg_win": round(win_sum / wins, 2) if wins else 0.0,
        "avg_loss": round(loss_sum / losses, 2) if losses else 0.0,
        "profit_factor": round(win_sum / gross_loss, 2) if gross_loss > 0 else 0.0,
    }
