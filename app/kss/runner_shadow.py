"""
Runner-shadow: a SHADOW, compute-only measurement of two alternative exits against every real
KSS take-profit (owner-approved 2026-09-26). It never places, cancels or modifies an order —
this module (and its DB glue, kept in this same file) touches only ``fills``/``kss_sessions``
for READS and its own ``runner_shadow`` table for writes. Prices come exclusively from
``app.market.cached_prices`` (never the network) and the whole tick is skipped when
``market.ws_feed_fresh()`` is False, so a stale price can never move a peak.

Two variants, three gaps each (2/3/5%) = 6 shadow rows per real TP fill:

- **V4a** ("what if we hadn't sold"): track the position AS IF the TP never happened. A trailing
  stop starts at the TP price and ratchets up with the market, floored at ``avg×1.03`` (never
  lower than 3% over cost — a deliberately crude proxy for K-2, since this is measurement only).
- **V5** ("what if we bought a runner after"): the real TP sale stands. 90s after the fill, if
  the price is still at/above the TP, buy a small runner sized off the TP's own profit; track
  ITS OWN trailing stop. Below the TP at the 90s mark, or a losing TP, means no runner at all.

``ShadowState``/``step`` below are pure — no DB, no I/O, no clock reads, no randomness — so they
carry almost all of this feature's test coverage. ``sync_new_tp_fills``/``tick``/``summary``
are the only functions that touch the database, and they do so idempotently and defensively:
an exception raised by any of them must never propagate into the exit path (the caller,
``app.scheduler._fast_exit_once``, wraps them in their own try/except after the real exit call
has already completed).
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from app.models import Fill

# --- shared constants --------------------------------------------------------------------

GAPS: tuple[float, ...] = (2.0, 3.0, 5.0)

STATE_WATCH = "watch"
STATE_RUNNER = "runner"
STATE_CLOSED = "closed"
STATE_SKIPPED = "skipped"
_OPEN_STATES = (STATE_WATCH, STATE_RUNNER)

REASON_STOP = "stop"
REASON_DEADLINE = "deadline"
REASON_SKIPPED = "skipped"

# V5 waits this long after the TP fill before deciding whether to buy a runner.
V5_DECISION_DELAY_SEC = 90.0

# V5 runner sizing: runner_usd = min(v0_proceeds, v0_net / (gap% + FEE_FLOOR + EXTRA)) — a fixed
# cushion (round-trip fee ~0.2% + 3% headroom), not a tunable knob, per the owner's spec.
_V5_RISK_FEE_FLOOR = 0.002
_V5_RISK_EXTRA = 0.03

# V4a's trailing stop is never allowed below this multiple of the average cost basis, even if
# the trail would otherwise ratchet lower — a crude, measurement-only echo of K-2.
_V4A_COST_FLOOR_MULT = 1.03

# Runtime-config key holding the ISO timestamp after which a TP fill is eligible for a shadow
# row. Set once, on the first ever call to ``sync_new_tp_fills`` — history before that point is
# deliberately NOT back-filled (there were no live observations for it, so any row built from it
# would be fabricated).
WATERMARK_KEY = "runner_shadow_watermark"

# Fallback session deadline when a fill's session cannot be found (or never had a deadline_at):
# the spec's own fallback, "60 days after the TP fill fill".
_DEADLINE_FALLBACK_DAYS = 60


@dataclass(frozen=True)
class ShadowParams:
    """Fee/slippage inputs to `step` — read from settings once per tick, not per row."""

    maker_fee_pct: float
    taker_fee_pct: float
    slip_pct: float


@dataclass(frozen=True)
class ShadowState:
    """Pure state for one (fill, variant, gap) shadow row. Immutable — `step` returns a new
    instance; nothing here ever mutates in place."""

    variant: str  # "v4a" | "v5"
    gap_pct: float
    avg_price: float
    tp_price: float
    v0_net: float
    v0_proceeds: float
    opened_at: datetime  # == the real TP fill's executed_at
    deadline_at: datetime

    qty: float = 0.0  # V4a: the ORIGINAL TP fill's qty (constant). V5: the RUNNER's own qty
    #                   (0 until/unless a runner is bought).
    state: str = STATE_WATCH
    peak: float = 0.0
    stop: float = 0.0
    entry_price: float = 0.0  # V5 only: the runner's own entry (decision) price
    runner_usd: float = 0.0  # V5 only: dollars committed to the runner
    decision_at: datetime | None = None  # V5 only: when the 90s buy/skip decision was made
    exit_price: float | None = None
    exit_at: datetime | None = None
    exit_reason: str | None = None  # "stop" | "deadline" | "skipped"
    diff_usd: float = 0.0
    net_usd: float = 0.0
    last_price: float = 0.0
    last_seen_at: datetime | None = None
    max_gap_sec: float = 0.0  # largest gap between observations (surfaces app downtime)

    @property
    def is_open(self) -> bool:
        return self.state in _OPEN_STATES


def open_v4a(
    *, gap_pct: float, avg_price: float, qty: float, tp_price: float, v0_net: float,
    v0_proceeds: float, opened_at: datetime, deadline_at: datetime,
) -> ShadowState:
    """A fresh V4a row: peak starts at the TP price itself (the "first observed price" that
    ``step`` will fold in has not happened yet), so the initial stop is already meaningful."""
    peak = tp_price
    stop = max(avg_price * _V4A_COST_FLOOR_MULT, peak * (1 - gap_pct / 100.0))
    return ShadowState(
        variant="v4a", gap_pct=gap_pct, avg_price=avg_price, tp_price=tp_price, v0_net=v0_net,
        v0_proceeds=v0_proceeds, opened_at=opened_at, deadline_at=deadline_at, qty=qty,
        peak=peak, stop=stop,
    )


def open_v5(
    *, gap_pct: float, avg_price: float, qty: float, tp_price: float, v0_net: float,
    v0_proceeds: float, opened_at: datetime, deadline_at: datetime,
) -> ShadowState:
    """A fresh V5 row: the real TP sale already happened (V0 stands); nothing is bought until
    the 90s decision, so `qty` starts at 0 regardless of the fill's own quantity."""
    return ShadowState(
        variant="v5", gap_pct=gap_pct, avg_price=avg_price, tp_price=tp_price, v0_net=v0_net,
        v0_proceeds=v0_proceeds, opened_at=opened_at, deadline_at=deadline_at, qty=0.0,
    )


def _bookkeep(state: ShadowState, price: float, now: datetime) -> ShadowState:
    anchor = state.last_seen_at or state.opened_at
    gap_sec = max(0.0, (now - anchor).total_seconds())
    return replace(
        state, last_price=price, last_seen_at=now, max_gap_sec=max(state.max_gap_sec, gap_sec)
    )


def step(state: ShadowState, price: float, now: datetime, params: ShadowParams) -> ShadowState:
    """One observation. Pure: returns a new `ShadowState`; never mutates `state`, touches no
    DB/network. A row already in a terminal state (`closed`/`skipped`) is returned unchanged —
    callers should stop feeding it ticks, but a stray call must be a safe no-op."""
    if not state.is_open:
        return state
    state = _bookkeep(state, price, now)
    if state.variant == "v4a":
        return _step_v4a(state, price, now, params)
    return _step_v5(state, price, now, params)


def mark_to_market(state: ShadowState, params: ShadowParams) -> float:
    """Unrealized `diff_usd` if an OPEN row were closed right now at its own `last_price` —
    dashboard display only. Never mutates state, never used to actually close a row."""
    if not state.is_open:
        return state.diff_usd
    if state.last_price <= 0:
        return 0.0
    if state.variant == "v4a":
        return _v4a_diff(state, state.last_price, params)
    if state.state == STATE_RUNNER:
        return _v5_runner_diff(state, state.last_price, params)
    return 0.0  # still watching for the 90s decision — nothing bought yet


# --- V4a -----------------------------------------------------------------------------------


def _v4a_diff(state: ShadowState, exit_price_raw: float, params: ShadowParams) -> float:
    slip = params.slip_pct / 100.0
    taker = params.taker_fee_pct / 100.0
    maker = params.maker_fee_pct / 100.0
    fill = exit_price_raw * (1 - slip)
    return state.qty * (fill * (1 - taker) - state.tp_price * (1 - maker))


def _step_v4a(state: ShadowState, price: float, now: datetime, params: ShadowParams) -> ShadowState:
    gap = state.gap_pct / 100.0
    peak = max(state.peak, price)
    stop = max(state.avg_price * _V4A_COST_FLOOR_MULT, peak * (1 - gap))
    if price <= stop:
        return _close_v4a(state, price, now, REASON_STOP, peak, stop, params)
    state = replace(state, peak=peak, stop=stop)
    if now >= state.deadline_at:
        return _close_v4a(state, price, now, REASON_DEADLINE, peak, stop, params)
    return state


def _close_v4a(
    state: ShadowState, price: float, now: datetime, reason: str, peak: float, stop: float,
    params: ShadowParams,
) -> ShadowState:
    slip = params.slip_pct / 100.0
    fill = price * (1 - slip)
    diff = _v4a_diff(state, price, params)
    return replace(
        state, state=STATE_CLOSED, peak=peak, stop=stop, exit_price=fill, exit_at=now,
        exit_reason=reason, diff_usd=diff, net_usd=state.v0_net + diff,
    )


# --- V5 --------------------------------------------------------------------------------------


def _v5_runner_diff(state: ShadowState, exit_price_raw: float, params: ShadowParams) -> float:
    slip = params.slip_pct / 100.0
    taker = params.taker_fee_pct / 100.0
    exit_price = exit_price_raw * (1 - slip)
    proceeds = state.qty * exit_price * (1 - taker)
    return proceeds - state.runner_usd


def _step_v5(state: ShadowState, price: float, now: datetime, params: ShadowParams) -> ShadowState:
    if state.state == STATE_WATCH:
        return _step_v5_watch(state, price, now, params)
    return _step_v5_runner(state, price, now, params)


def _skip_v5(state: ShadowState, now: datetime, reason: str) -> ShadowState:
    return replace(
        state, state=STATE_SKIPPED, decision_at=now, exit_at=now, exit_reason=reason,
        diff_usd=0.0, net_usd=state.v0_net,
    )


def _step_v5_watch(state: ShadowState, price: float, now: datetime, params: ShadowParams) -> ShadowState:
    decision_due = now >= state.opened_at + timedelta(seconds=V5_DECISION_DELAY_SEC)
    if not decision_due:
        if now >= state.deadline_at:
            # The deadline arrived before the 90s decision ever ran (an outage swallowed the
            # window, or a very short deadline) — nothing was ever bought, so there is no
            # position to close; record it as skipped rather than fabricate an exit.
            return _skip_v5(state, now, REASON_DEADLINE)
        return state
    if state.v0_net <= 0 or price < state.tp_price:
        return _skip_v5(state, now, REASON_SKIPPED)
    gap_frac = state.gap_pct / 100.0
    risk_frac = gap_frac + _V5_RISK_FEE_FLOOR + _V5_RISK_EXTRA
    runner_usd = min(state.v0_proceeds, state.v0_net / risk_frac) if risk_frac > 0 else 0.0
    slip = params.slip_pct / 100.0
    taker = params.taker_fee_pct / 100.0
    cost_per_unit = price * (1 + slip) * (1 + taker)
    qty = runner_usd / cost_per_unit if cost_per_unit > 0 else 0.0
    return replace(
        state, state=STATE_RUNNER, decision_at=now, entry_price=price, runner_usd=runner_usd,
        qty=qty, peak=price, stop=price * (1 - gap_frac),
    )


def _step_v5_runner(state: ShadowState, price: float, now: datetime, params: ShadowParams) -> ShadowState:
    gap = state.gap_pct / 100.0
    peak = max(state.peak, price)
    stop = peak * (1 - gap)
    if price <= stop:
        return _close_v5_runner(state, price, now, REASON_STOP, peak, stop, params)
    state = replace(state, peak=peak, stop=stop)
    if now >= state.deadline_at:
        return _close_v5_runner(state, price, now, REASON_DEADLINE, peak, stop, params)
    return state


def _close_v5_runner(
    state: ShadowState, price: float, now: datetime, reason: str, peak: float, stop: float,
    params: ShadowParams,
) -> ShadowState:
    slip = params.slip_pct / 100.0
    exit_price = price * (1 - slip)
    diff = _v5_runner_diff(state, price, params)
    return replace(
        state, state=STATE_CLOSED, peak=peak, stop=stop, exit_price=exit_price, exit_at=now,
        exit_reason=reason, diff_usd=diff, net_usd=state.v0_net + diff,
    )


# ==============================================================================================
# DB glue — the only functions in this file that touch the database. Every one of them is safe
# to call repeatedly (idempotent) and every one is meant to be wrapped by the caller in its own
# try/except (see app.scheduler._fast_exit_once) so a bug here can never reach the exit path.
# ==============================================================================================


def _session_id_from_tp_ref(source_ref: str | None) -> int | None:
    """``pyramid:{id}:tp`` -> id, or None if it doesn't parse."""
    if not source_ref:
        return None
    parts = source_ref.split(":")
    if len(parts) != 3 or parts[0] != "pyramid" or parts[2] != "tp":
        return None
    try:
        return int(parts[1])
    except ValueError:
        return None


def _row_from_state(fill: Fill, session_id: int | None, state: ShadowState):
    from app.models import RunnerShadow

    return RunnerShadow(
        fill_id=fill.id, session_id=session_id, symbol=fill.symbol, variant=state.variant,
        gap_pct=state.gap_pct, avg_price=state.avg_price, qty=state.qty, tp_price=state.tp_price,
        v0_net=state.v0_net, v0_proceeds=state.v0_proceeds, opened_at=state.opened_at,
        decision_at=state.decision_at, state=state.state, peak=state.peak, stop=state.stop,
        entry_price=state.entry_price, runner_usd=state.runner_usd, exit_price=state.exit_price,
        exit_at=state.exit_at, exit_reason=state.exit_reason, diff_usd=state.diff_usd,
        net_usd=state.net_usd, last_price=state.last_price, last_seen_at=state.last_seen_at,
        max_gap_sec=state.max_gap_sec, deadline_at=state.deadline_at,
    )


def _state_from_row(row) -> ShadowState:
    return ShadowState(
        variant=row.variant, gap_pct=row.gap_pct, avg_price=row.avg_price, tp_price=row.tp_price,
        v0_net=row.v0_net, v0_proceeds=row.v0_proceeds, opened_at=row.opened_at,
        deadline_at=row.deadline_at, qty=row.qty, state=row.state, peak=row.peak, stop=row.stop,
        entry_price=row.entry_price, runner_usd=row.runner_usd, decision_at=row.decision_at,
        exit_price=row.exit_price, exit_at=row.exit_at, exit_reason=row.exit_reason,
        diff_usd=row.diff_usd, net_usd=row.net_usd, last_price=row.last_price,
        last_seen_at=row.last_seen_at, max_gap_sec=row.max_gap_sec,
    )


def _apply_state(row, state: ShadowState) -> None:
    row.qty = state.qty
    row.state = state.state
    row.peak = state.peak
    row.stop = state.stop
    row.entry_price = state.entry_price
    row.runner_usd = state.runner_usd
    row.decision_at = state.decision_at
    row.exit_price = state.exit_price
    row.exit_at = state.exit_at
    row.exit_reason = state.exit_reason
    row.diff_usd = state.diff_usd
    row.net_usd = state.net_usd
    row.last_price = state.last_price
    row.last_seen_at = state.last_seen_at
    row.max_gap_sec = state.max_gap_sec


def _shadow_params() -> ShadowParams:
    from app.config import settings

    return ShadowParams(
        maker_fee_pct=settings.maker_fee_pct, taker_fee_pct=settings.taker_fee_pct,
        slip_pct=settings.runner_shadow_slip_pct,
    )


def sync_new_tp_fills(db: Session) -> int:
    """Create the 6 shadow rows (v4a x {2,3,5} + v5 x {2,3,5}) for every real KSS take-profit
    SELL fill not yet covered. Idempotent: skips fills that already have shadow rows. The very
    first call ever made (no stored watermark) does not back-fill history — it records "now" as
    the watermark and returns 0, so no row is ever built from a fill this feature never actually
    observed live. Returns the number of rows created."""
    from app import runtime
    from app.clock import utcnow
    from app.models import Fill, KssSession

    watermark_raw = runtime.get(db, WATERMARK_KEY)
    if watermark_raw is None:
        runtime.set(db, WATERMARK_KEY, utcnow().isoformat())
        return 0
    watermark = datetime.fromisoformat(watermark_raw)

    fills = (
        db.query(Fill)
        .filter(Fill.source_ref.like("pyramid:%:tp"), Fill.side == "SELL",
                Fill.executed_at > watermark)
        .order_by(Fill.id.asc())
        .all()
    )
    if not fills:
        return 0

    from app.models import RunnerShadow

    already = {
        fid for (fid,) in db.query(RunnerShadow.fill_id)
        .filter(RunnerShadow.fill_id.in_([f.id for f in fills]))
        .distinct()
    }
    created = 0
    for fill in fills:
        if fill.id in already:
            continue
        session_id = _session_id_from_tp_ref(fill.source_ref)
        session = db.get(KssSession, session_id) if session_id is not None else None
        if session is None:
            continue  # no session to read avg_price/deadline_at from — nothing trustworthy to build
        avg = session.avg_price
        deadline_at = session.deadline_at or (fill.executed_at + timedelta(days=_DEADLINE_FALLBACK_DAYS))
        v0_net = fill.realized_pnl
        v0_proceeds = fill.quantity * fill.price * (1 - _shadow_params().maker_fee_pct / 100.0)
        for gap in GAPS:
            v4a = open_v4a(gap_pct=gap, avg_price=avg, qty=fill.quantity, tp_price=fill.price,
                            v0_net=v0_net, v0_proceeds=v0_proceeds, opened_at=fill.executed_at,
                            deadline_at=deadline_at)
            db.add(_row_from_state(fill, session_id, v4a))
            v5 = open_v5(gap_pct=gap, avg_price=avg, qty=fill.quantity, tp_price=fill.price,
                         v0_net=v0_net, v0_proceeds=v0_proceeds, opened_at=fill.executed_at,
                         deadline_at=deadline_at)
            db.add(_row_from_state(fill, session_id, v5))
            created += 2
    db.commit()
    return created


def forget_watermark(db: Session) -> None:
    """Knob OFF: drop the watermark (if any) so re-enabling re-seeds it to "now". A TP that
    fills while the shadow is off was never observed live — back-filling it later would open
    its rows with a first observation minutes-to-days late (V5 would even "decide" at that
    unrelated moment). Rows already open are untouched; their `max_gap_sec` records the pause.
    One primary-key read per call; writes only when a watermark actually exists."""
    from app.models import RuntimeConfig

    row = db.get(RuntimeConfig, WATERMARK_KEY)
    if row is not None:
        db.delete(row)
        db.commit()


def open_symbols(db: Session) -> list[str]:
    """Distinct symbols with at least one OPEN (watch/runner) shadow row — the exact set `tick`
    needs prices for."""
    from app.models import RunnerShadow

    rows = db.query(RunnerShadow.symbol).filter(RunnerShadow.state.in_(_OPEN_STATES)).distinct()
    return [s for (s,) in rows]


def tick(db: Session, prices: dict[str, float], now: datetime) -> int:
    """Advance every OPEN shadow row one observation using `prices` (an already-warm price map —
    this function performs no I/O of its own) and `now`. No-op, zero rows touched, no commit, when
    `market.ws_feed_fresh()` is False: a stale feed must never move a peak. Returns the number of
    rows updated."""
    from app.market import ws_feed_fresh
    from app.models import RunnerShadow

    if not ws_feed_fresh():
        return 0
    rows = db.query(RunnerShadow).filter(RunnerShadow.state.in_(_OPEN_STATES)).all()
    if not rows:
        return 0
    params = _shadow_params()
    updated = 0
    for row in rows:
        price = prices.get(row.symbol)
        if not price:
            continue
        new_state = step(_state_from_row(row), price, now, params)
        _apply_state(row, new_state)
        updated += 1
    if updated:
        db.commit()
    return updated


def summary(db: Session, recent_limit: int = 50) -> dict:
    """Per variant×gap: N closed / open / skipped, sum/mean realized diff, % worse than V0,
    worst/best, and the OPEN rows' unrealized diff marked-to-market separately. Plus the most
    recent `recent_limit` rows (any state) as dicts, newest first."""
    from app.models import RunnerShadow

    rows = db.query(RunnerShadow).all()
    params = _shadow_params()
    buckets = []
    for variant, gap in itertools.product(("v4a", "v5"), GAPS):
        subset = [r for r in rows if r.variant == variant and r.gap_pct == gap]
        closed = [r for r in subset if r.state == STATE_CLOSED]
        open_rows = [r for r in subset if r.state in _OPEN_STATES]
        skipped = [r for r in subset if r.state == STATE_SKIPPED]
        diffs = [r.diff_usd for r in closed]
        mtm = [mark_to_market(_state_from_row(r), params) for r in open_rows]
        buckets.append({
            "variant": variant,
            "gap_pct": gap,
            "n_closed": len(closed),
            "n_open": len(open_rows),
            "n_skipped": len(skipped),
            "sum_diff_usd": sum(diffs),
            "mean_diff_usd": (sum(diffs) / len(diffs)) if diffs else 0.0,
            "pct_worse_than_v0": (100.0 * sum(1 for d in diffs if d < 0) / len(diffs)) if diffs else 0.0,
            "worst_usd": min(diffs) if diffs else 0.0,
            "best_usd": max(diffs) if diffs else 0.0,
            "open_mtm_sum_usd": sum(mtm),
            "open_mtm_mean_usd": (sum(mtm) / len(mtm)) if mtm else 0.0,
        })
    recent = sorted(rows, key=lambda r: r.id, reverse=True)[:recent_limit]
    recent_dicts = []
    for r in recent:
        d = r.to_dict()
        # The stored diff/net stay 0 until a row closes; the table promises an ESTIMATE for an
        # open row, so hand it the mark-to-market (== the realized diff once closed/skipped).
        est = mark_to_market(_state_from_row(r), params)
        d["diff_est_usd"] = est
        d["net_est_usd"] = r.v0_net + est
        recent_dicts.append(d)
    return {"buckets": buckets, "recent": recent_dicts}
