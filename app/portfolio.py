"""
Read-side views for the dashboard: positions, trade history, and summary.

These are pure reads derived from fills/positions plus live market prices.
Kept out of the route layer so routes stay thin.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.clock import utcnow
from app.config import settings
from app.market import get_current_prices
from app.models import (
    SESSION_ACTIVE,
    AuditLog,
    Deposit,
    Fill,
    KssSession,
    PendingOrder,
    Position,
    Withdrawal,
)


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
    # `realized_pct` is a capital-weighted ROI (profit / total capital ever contributed), NOT a
    # time-weighted return: it does not account for WHEN each deposit landed relative to the
    # P&L, only how much was put in. Chosen over a unit-NAV time-weighted return because it is
    # a one-line change instead of restructuring the fill-by-fill curve with deposit timestamps
    # woven in — deposits are rare ($500-1,000/month), so the extra precision is not worth the
    # invasiveness. Leaving deposits OUT of the base (the old behaviour) would have done the
    # opposite of hiding profit as a deposit: it would OVERSTATE every future % figure, because
    # profit earned on newly deposited capital gets compared against the original, smaller base
    # as if it all came from the first dollar in.
    base = settings.account_equity + risk.total_deposited(db) or 1.0
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


def _rung_starved_last_hour(db: Session) -> dict:
    """Rungs currently flagged cash-starved (audit ``orders.rung_starved`` — refused outright or
    trimmed by ``_apply_cash_cap``) in the last hour, for a single row on the capital panel.

    Deduped by (session, wave), not raw row count: the alert can legitimately repeat for the
    SAME rung within the hour when ``rung_starved_alert_min`` is set below 60, and counting each
    repeat separately would overstate how many rungs are actually stuck (one rung re-alerting
    three times must still read as one rung, at its most recent $ shortfall)."""
    since = utcnow() - timedelta(hours=1)
    rows = (
        db.query(AuditLog)
        .filter(AuditLog.action == "rung_starved", AuditLog.created_at >= since)
        .all()
    )
    latest: dict[tuple, float] = {}
    for row in rows:
        try:
            detail = json.loads(row.detail) if row.detail else {}
        except (TypeError, ValueError):
            continue
        latest[(row.entity, detail.get("wave"))] = float(detail.get("needed_usd") or 0.0)
    return {"count": len(latest), "needed_usd": sum(latest.values())}


def capital_view(db: Session) -> dict:
    """Capital-utilisation panel: the equity split that shows why only part of the
    account is actually working (docs: measured audit put per-dollar edge near
    1%/day, but only ~29% of capital-days deployed -> ~0.42%/day portfolio return).

    Reuses ``summary_view`` for cash, ``risk.account_equity`` for mark-to-market equity,
    and the scanner's own reserve-gate lock rule (``scanner._session_lock`` — Fix A2,
    2026-09-21: cash already spent plus the untouched ``ladder_coverage_pct`` pre-booking) for
    what an active session actually locks against the deployable budget — none of that
    is re-derived here.
    """
    from app import risk  # lazy: risk -> portfolio; avoid an import cycle at load
    from app.scanner import (  # lazy: scanner -> orders -> risk -> portfolio
        _session_lock,
        effective_max_sessions,
    )

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
    # The cap the scanner actually enforces — derived from capital when session_cover_rungs > 0.
    sessions_cap, sessions_cap_why = effective_max_sessions(db)
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
    rung_starved = _rung_starved_last_hour(db)

    return {
        "equity": equity,
        "base_equity": settings.account_equity,  # config constant, unaffected by deposits
        "total_deposited": risk.total_deposited(db),
        "capital_anchor": risk.capital_anchor(db),  # base + deposits (- withdrawals on live)
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
        "sessions_cap_why": sessions_cap_why,
        "binding": binding,
        "bar": bar,
        "rung_starved": rung_starved,
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


def _capital_flows(db: Session) -> list[tuple[datetime, float, float | None]]:
    """Chronological (timestamp, signed USD amount, equity_before) capital-flow events — the
    same additive decomposition ``risk.capital_anchor`` layers on top of
    ``settings.account_equity``: deposits always count; a withdrawal counts only where the
    anchor actually subtracts it (live, ``use_exchange_balance`` off). Empty when the anchor is
    the real exchange balance (live + ``use_exchange_balance``) — that balance is not
    decomposable into base + flows (see ``risk.capital_anchor``'s docstring); left unmodeled
    there rather than compounding its own separate, pre-existing double-count risk.

    ``equity_before`` is the mark-to-market total equity snapshot taken at record time
    (``Deposit``/``Withdrawal.equity_before`` — see their docstrings); ``None`` for any row
    inserted before that column existed, or for a withdrawal on a DB that hasn't picked up the
    ``app/db.py`` ALTER yet.
    """
    if settings.live_trading and settings.use_exchange_balance:
        return []
    flows: list[tuple[datetime, float, float | None]] = [
        (d.created_at, d.amount, d.equity_before)
        for d in db.query(Deposit).all()
        if d.created_at is not None
    ]
    if settings.live_trading:  # use_exchange_balance is False here (handled above)
        flows += [
            (w.created_at, -w.amount, w.equity_before)
            for w in db.query(Withdrawal).all()
            if w.created_at is not None
        ]
    flows.sort(key=lambda f: f[0])
    return flows


def _nav_walk(db: Session) -> tuple[list[dict], float, float]:
    """Chronological unit-NAV walk over the WHOLE history of realized fills and capital flows
    (``_capital_flows``), each applied at its OWN timestamp — modeled like a mutual fund: a
    flow buys/redeems units at the NAV just before it lands, so BY CONSTRUCTION it can never
    move NAV/unit, only realized P&L can.

    This is what makes drawdown flow-safe at every point in history, not just "right now": a
    deposit can never look like recovery, a withdrawal can never look like a fresh drawdown.
    Measured bug this replaces: dividing a real 10% loss by a peak that excluded 12 months of
    $1,000 deposits read as a 27.14% drawdown — enough to falsely freeze the circuit breaker.

    A flow prices its UNITS off ``equity_before`` (the TRUE mark-to-market equity snapshotted
    at record time — see ``_capital_flows``) when present, instead of the running ``equity``
    tracker below, which only ever accumulates REALIZED fills. Without this, a deposit made
    while a position sits underwater (SL=0 means that loss is almost always unrealized) would
    price its units at a NAV that doesn't know about the loss yet, diluting it.

    The tracker itself is DELIBERATELY NEVER re-based to ``equity_before`` — an earlier version
    of this fix did (``equity = equity_before + value``), and an adversarial review caught the
    consequence: that bakes the position's UNREALIZED P&L into the realized-only tracker, so
    when the position later closes, its realized P&L is counted a SECOND time (inflating
    ``max_drawdown_pct``), or a later recovery is never reflected (understating it). The tracker
    stays exactly what its name says — realized equity + cumulative flow amounts, nothing
    else — through every event; only the one-off unit count at a flow is priced off the truer
    number. Every intermediate NAV point (fill or flow alike) is plainly ``tracker / units``;
    only the FINAL point (built by the caller) is ``true mark-to-market / units`` — the tracker
    catches back up to the true total on its own, for free, the moment a position's P&L is
    actually realized.

    Edge case: ``equity_before`` (or the realized-only fallback) at or below zero can't price a
    NAV by division. Treated as a wipeout, not silently skipped: the point for THIS flow records
    ``nav=0.0`` (a full loss relative to any positive peak — max_drawdown correctly reads 100%),
    then NAV is re-seeded to 1.0 with units equal to the post-flow dollar total, so subsequent
    points measure performance from this recovery instead of carrying a corrupt (zero/negative)
    unit count forward. (This reset path is a distinct, narrow case — the book was already at or
    below zero — and does re-base to the true total, since there is no valid realized-only value
    to preserve through a wipeout.)

    Returns ``(points, equity, units)``: ``points`` is one ``{"t", "equity", "nav"}`` dict per
    event in chronological order; ``equity``/``units`` are the running totals after the last
    one. The caller folds in today's mark-to-market point itself (needs ``summary_view``,
    which would recurse if computed in here).
    """
    fills = db.query(Fill).order_by(Fill.executed_at.asc()).all()
    events: list[tuple[datetime, str, float, float | None]] = [
        (f.executed_at or utcnow(), "fill", f.realized_pnl, None) for f in fills
    ]
    events += [(t, "flow", amount, eq_before) for t, amount, eq_before in _capital_flows(db)]
    # Tie-break same-instant events flow-before-fill. Timestamps come from the wall clock at
    # insert time, whose resolution is coarser than a tight test loop (measured: 12 sequential
    # commits landing on the SAME microsecond value) — real usage is a human recording ~1
    # deposit a month, so a genuine tie is a clock-resolution artifact, never two real
    # simultaneous events. Deposit-before-fill on a tie is also the SAFE direction: it can only
    # ever make drawdown look better (the deposit counts sooner), never hide a real one.
    events.sort(key=lambda e: (e[0], 0 if e[1] == "flow" else 1))

    equity = settings.account_equity
    units = settings.account_equity if settings.account_equity > 0 else 1.0
    points: list[dict] = []
    for t, kind, value, eq_before in events:
        if kind == "flow":
            raw_nav = (eq_before / units) if (eq_before is not None and units) else (
                (equity / units) if units else 1.0
            )
            if raw_nav <= 0:
                point_nav = 0.0  # full loss relative to any positive peak — see docstring
                post_flow_equity = (eq_before if eq_before is not None else equity) + value
                equity = post_flow_equity
                units = post_flow_equity if post_flow_equity > 0 else 1.0
            else:
                units += value / raw_nav  # units priced at the TRUE nav when known
                equity += value           # tracker: realized-only + this flow — NEVER rebased
                # The flow's OWN point uses `raw_nav` directly — the one instant its true value
                # is actually known — rather than the post-update `equity/units`, which can
                # drift from it once the tracker stops matching true equity: bounded (a mediant
                # of the prior ratio and raw_nav, so it can only read BETWEEN them) for a
                # deposit, but an unbounded EXTRAPOLATION beyond both for a withdrawal —
                # measured, a withdrawal made while a position was underwater inflated a later
                # `current_drawdown_pct` from a true 10.0% to 11.67% before this line existed.
                point_nav = raw_nav
        else:
            equity += value
            point_nav = (equity / units) if units else 0.0
        points.append({"t": t, "equity": equity, "nav": point_nav})
    return points, equity, units


def performance_view(db: Session, period: str = "all") -> dict:
    """
    Equity curve (dollars) + win/loss + drawdown + expectancy.

    The dollar ``equity_curve`` stamps every realized fill AND every capital flow (a deposit,
    or a withdrawal where ``risk.capital_anchor`` subtracts it — see ``_capital_flows``) at its
    own timestamp, ending in a final point with today's mark-to-market unrealized P&L.

    Drawdown (``max_drawdown_pct``/``current_drawdown_pct``) is read off a PARALLEL unit-NAV
    series (``_nav_walk``) instead of the dollar curve: a flow changes units, never NAV/unit,
    so it can neither hide a real drawdown nor manufacture a fake one. For ``period="all"``
    (what the circuit breaker actually reads — ``circuit.metrics`` calls this with no period)
    the NAV walk spans the FULL history, so the peak is the true all-time high. For a
    restricted period the peak/drawdown are LOCAL to that window (seeded from whatever NAV the
    book already had at the cutoff, then tracked only from there forward) — a deliberate choice
    ported unchanged from the pre-NAV-walk code, which reset its own peak the same way; only
    win/loss/expectancy are otherwise scoped to the window's own fills.
    """
    all_fills = db.query(Fill).order_by(Fill.executed_at.asc()).all()
    cutoff = _period_cutoff(period)
    fills = (
        [f for f in all_fills if not f.executed_at or f.executed_at >= cutoff]
        if cutoff is not None
        else all_fills
    )

    now = utcnow()
    walk_points, _run_equity, run_units = _nav_walk(db)
    summary = summary_view(db)
    final_equity = summary["total_equity"]
    final_nav = (final_equity / run_units) if run_units else 0.0
    walk_points = walk_points + [{"t": now, "equity": final_equity, "nav": final_nav}]

    if cutoff is not None:
        seed_points = [p for p in walk_points if p["t"] < cutoff]
        window_points = [p for p in walk_points if p["t"] >= cutoff]
        last_seed = seed_points[-1] if seed_points else None
        seed = {
            # The seed's own timestamp is the first REAL event in the window when there is one
            # (a flat "value carried in" point at that same instant, just before it) — not the
            # bare cutoff boundary, which read as a synthetic point nothing actually happened at.
            "t": window_points[0]["t"] if window_points else cutoff,
            "equity": last_seed["equity"] if last_seed else settings.account_equity,
            "nav": last_seed["nav"] if last_seed else 1.0,
        }
    else:
        window_points = walk_points
        # Earliest of ALL events (fills AND flows) — a deposit can predate the first fill, and
        # seeding from `all_fills[0]` alone would then put a later-looking seed BEFORE an
        # earlier-timestamped deposit point once sorted (or, unsorted, an out-of-order first
        # entry — "time goes backwards" on the chart).
        seed = {"t": window_points[0]["t"] if window_points else now,
                "equity": settings.account_equity, "nav": 1.0}

    # Sorted by timestamp (stable — a tie keeps `seed` first, the "value just before" reading):
    # `window_points` is already chronological, but `seed` computed from the OTHER branch's
    # cutoff/first fill is not guaranteed to sort first once a flow's own timestamp is considered.
    curve_points = sorted([seed] + window_points, key=lambda p: p["t"])
    curve = [p["equity"] for p in curve_points]
    nav_curve = [p["nav"] for p in curve_points]
    times = [p["t"].isoformat() for p in curve_points]

    realized = 0.0
    wins = losses = 0
    win_sum = loss_sum = 0.0
    for f in fills:
        realized += f.realized_pnl
        if f.side == "SELL":
            if f.realized_pnl > 0:
                wins += 1
                win_sum += f.realized_pnl
            elif f.realized_pnl < 0:
                losses += 1
                loss_sum += f.realized_pnl  # negative

    # Two different numbers, and the difference matters. `max_dd` is the WORST dip the curve
    # ever took — a historical statistic that can only grow. `current_dd` is how far below the
    # running peak the account sits RIGHT NOW, and it falls back towards 0 as it recovers.
    # The circuit breaker needs the second: gating on the first means one bad day freezes
    # trading forever, because the reason to stay frozen can never clear.
    peak = nav_curve[0] if nav_curve else 1.0
    max_dd = 0.0
    for v in nav_curve:
        peak = max(peak, v)
        if peak > 0:
            max_dd = max(max_dd, (peak - v) / peak * 100)
    current_dd = (peak - nav_curve[-1]) / peak * 100 if peak > 0 and nav_curve else 0.0

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
