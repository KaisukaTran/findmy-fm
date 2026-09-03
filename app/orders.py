"""
Order lifecycle for FINDMY-FM (lean rebuild).

Every order flows through manual approval — nothing executes directly:

    queue_order()  -> pending_orders (status=pending, risk note attached)
    approve_order() -> paper-execute -> Fill + Position update -> status=executed
    reject_order()  -> status=rejected

Paper execution simulates slippage and taker fees. Fills are append-only facts;
Position is the derived running state per symbol.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from app import audit, runtime
from app.clock import utcnow
from app.config import settings
from app.market import get_current_prices
from app.models import (
    APPROVED,
    EXECUTED,
    PENDING,
    REJECTED,
    Fill,
    PendingOrder,
    Position,
)
from app.risk import calculate_order_qty, check_all_risks

logger = logging.getLogger(__name__)


class InsufficientCashError(ValueError):
    """A BUY can't be funded without driving account cash below the floor. Subclasses
    ValueError so existing callers that skip on ValueError (approve_all, the manual-approve
    route → HTTP 400) keep working unchanged."""


# --- placement failure-streak tracker (D2-gap) ----------------------------
#
# `_place_resting` and the live placement call in `_execute` used to swallow every
# placement exception with `logger.exception(...); return False` — a PERSISTENT non-auth
# failure (a filter error, -1013, insufficient balance) produced total silence, because only
# `execution.note_credential_error` alerted, and only for credential errors. This mirrors
# that same alert-cooldown shape (module-level state, one alert per streak, a later success
# clears it) but keyed per PENDING ORDER ID and driven by a failure COUNT rather than a wall
# clock, since "the Nth consecutive failure of the same order" is the signal that matters,
# not "N minutes since the last alert".

_placement_fail_streak: dict[int, int] = {}
_placement_alerted: set[int] = set()

# Only a SUCCESS clears a tracked order id (`_note_placement_success`) — an id that simply
# stops being retried (approved, rejected, its session closed, ...) never leaves the dict on
# its own, so a long-running instance would grow it forever. Cap it.
_PLACEMENT_STREAK_MAX = 500


def _evict_placement_streak_overflow() -> None:
    """Past ``_PLACEMENT_STREAK_MAX`` tracked ids, drop the OLDEST half (insertion order, which
    Python dicts preserve). Cheap and approximate on purpose: a still-failing order id gets a
    fresh entry on its very next attempt regardless, so this can only ever delay a re-alert by
    one failure, never suppress a genuinely stuck one."""
    if len(_placement_fail_streak) <= _PLACEMENT_STREAK_MAX:
        return
    oldest = list(_placement_fail_streak)[: len(_placement_fail_streak) // 2]
    for oid in oldest:
        _placement_fail_streak.pop(oid, None)
        _placement_alerted.discard(oid)


def reset_placement_alert_state() -> None:
    """Clear every tracked placement-failure streak/alert flag (tests; a fresh boot)."""
    _placement_fail_streak.clear()
    _placement_alerted.clear()


def _note_placement_success(order_id: int) -> None:
    """A placement attempt for *order_id* just succeeded — clear its streak so a future run
    of failures starts counting from zero (and can alert again)."""
    _placement_fail_streak.pop(order_id, None)
    _placement_alerted.discard(order_id)


def _note_placement_failure(order: PendingOrder, reason: str) -> None:
    """Record one more CONSECUTIVE placement failure for *order*. Fires exactly one
    ``notify.event('risk', ...)`` on the ``settings.placement_alert_after``-th consecutive
    failure, then stays quiet — the 'alerted' flag (not a timer) is the throttle — until a
    later ``_note_placement_success`` for the same order id clears it, at which point the
    next failing streak can alert again. Alert-only: never raises, never gates anything (same
    contract as ``execution.note_credential_error``).
    """
    try:
        oid = order.id
        streak = _placement_fail_streak.get(oid, 0) + 1
        _placement_fail_streak[oid] = streak
        _evict_placement_streak_overflow()
        threshold = max(settings.placement_alert_after, 1)
        if streak >= threshold and oid not in _placement_alerted:
            _placement_alerted.add(oid)
            from app import notify

            notify.event(
                "risk",
                f"⚠️ Order {oid} ({order.symbol} {order.side}) failed to place {streak} "
                f"times in a row: {reason}. It stays queued and keeps retrying — check for a "
                "persistent problem (filter error, insufficient balance, a bad price/qty, ...).",
            )
    except Exception:  # an alert must never break placement
        logger.debug(
            "_note_placement_failure: alert failed for order %s", getattr(order, "id", "?"),
            exc_info=True,
        )


# --- cash floor (hard guard: account cash may never go negative) ---------


def _free_cash(db: Session) -> float:
    """Real free USDT = starting capital + realized PnL − cost of open positions. This is
    exactly the 'Cash' the portfolio summary shows (portfolio.summary_view)."""
    invested = float(db.query(func.coalesce(func.sum(Position.total_cost), 0.0)).scalar() or 0.0)
    realized = float(db.query(func.coalesce(func.sum(Fill.realized_pnl), 0.0)).scalar() or 0.0)
    return settings.account_equity + realized - invested


def _apply_cash_cap(db: Session, order: PendingOrder) -> None:
    """HARD floor for BUYs: shrink ``order.quantity`` to the qty the available cash can fund
    (partial fill), so a fill can never push cash below ``cash_floor_usd``. Raise
    InsufficientCashError when not even a min-notional slice fits. No-op for SELL (exits are
    never gated) and when no reference price is available (downstream raises 'no price').

    Sizing uses the SAME cost model as the paper fill — ref×(1+slippage)×(1+taker_fee) per unit —
    so the resulting cash is ≥ floor exactly (a tiny epsilon guards float rounding). For live the
    real fill differs slightly, but the exchange independently rejects an over-balance order."""
    if order.side != "BUY":
        return
    free = _free_cash(db) - settings.cash_floor_usd
    ref = order.price if order.price > 0 else (get_current_prices([order.symbol]).get(order.symbol) or 0.0)
    if ref <= 0:
        return  # let _execute raise the explicit "no price" error
    unit_cost = ref * (1 + settings.slippage_pct / 100.0) * (1 + settings.taker_fee_pct / 100.0)
    if unit_cost <= 0:
        return
    affordable = (free / unit_cost) * (1 - 1e-9)  # epsilon: never round up over the floor
    if affordable >= order.quantity:
        return  # full order fits within cash
    if affordable <= 0 or affordable * ref < settings.scan_min_notional:
        raise InsufficientCashError(
            f"thiếu tiền mặt: còn ${free:.2f} (giữ Cash ≥ ${settings.cash_floor_usd:.0f}) — "
            f"không đủ mua {order.symbol}; lệnh bị giữ lại."
        )
    requested = order.quantity
    order.quantity = affordable
    audit.log(db, "orders", "partial_fill_cash", entity=order.symbol,
              requested=round(requested, 8), filled=round(affordable, 8),
              free_cash=round(free, 2), source_ref=order.source_ref)


# --- queue --------------------------------------------------------------


def queue_order(
    db: Session,
    *,
    symbol: str,
    side: str,
    quantity: float | None = None,
    price: float = 0.0,
    pips: float | None = None,
    order_type: str = "LIMIT",
    source: str = "manual",
    source_ref: str | None = None,
    strategy_name: str | None = None,
    note: str | None = None,
) -> tuple[PendingOrder, str | None]:
    """Create a pending order. Returns (order, risk_note). Risk never blocks queuing."""
    side = side.upper()
    if side not in ("BUY", "SELL"):
        raise ValueError(f"Invalid side: {side}")
    if quantity is None:
        if pips is None:
            raise ValueError("Provide either quantity or pips")
        quantity = calculate_order_qty(symbol, pips)
    if quantity <= 0:
        raise ValueError("Quantity must be positive")

    ref_price = price if price > 0 else (get_current_prices([symbol]).get(symbol) or 0.0)
    _, violations = check_all_risks(symbol, quantity, ref_price, db, side=side)
    risk_note = "; ".join(violations) if violations else None

    order = PendingOrder(
        symbol=symbol,
        side=side,
        order_type=order_type,
        quantity=quantity,
        price=price,
        source=source,
        source_ref=source_ref,
        strategy_name=strategy_name,
        note=note,
        risk_note=risk_note,
        status=PENDING,
    )
    db.add(order)
    db.commit()
    db.refresh(order)
    logger.info("Queued order %s: %s %s %s @ %s", order.id, side, quantity, symbol, price)
    return order, risk_note


def list_pending(db: Session, status: str | None = None, limit: int = 100, offset: int = 0) -> list[PendingOrder]:
    """List orders, optionally filtered by status (defaults to pending)."""
    q = db.query(PendingOrder)
    q = q.filter(PendingOrder.status == (status or PENDING))
    return q.order_by(PendingOrder.created_at.desc()).offset(offset).limit(limit).all()


# --- decisions ----------------------------------------------------------


def reject_order(
    db: Session, order_id: int, reason: str = "", reviewer: str | None = None
) -> PendingOrder:
    order = _get_pending(db, order_id)
    order.status = REJECTED
    order.reject_reason = reason
    order.reviewer = reviewer
    order.decided_at = utcnow()
    db.commit()
    db.refresh(order)
    return order


def approve_all(db: Session, reviewer: str | None = "dashboard") -> list[int]:
    """Approve (paper-execute) every currently pending order. Returns approved ids."""
    ids = [o.id for o in db.query(PendingOrder).filter(PendingOrder.status == PENDING).all()]
    done = []
    for oid in ids:
        try:
            approve_order(db, oid, reviewer=reviewer)
            done.append(oid)
        except ValueError:  # e.g. no price available — skip, keep going
            continue
    return done


def reject_all(db: Session, reason: str = "", reviewer: str | None = "dashboard") -> list[int]:
    """Reject every currently pending order. Returns rejected ids."""
    ids = [o.id for o in db.query(PendingOrder).filter(PendingOrder.status == PENDING).all()]
    for oid in ids:
        reject_order(db, oid, reason=reason, reviewer=reviewer)
    return ids


def auto_approve_by_policy(db: Session) -> list[int]:
    """
    AI auto-approval: approve pending orders matching the configured rule —
    source in `autoapprove_sources` AND notional ≤ `autoapprove_max_notional`
    (notional = qty × price, using live price for market orders). Optionally skip
    orders carrying a risk note. Disabled unless `autoapprove_enabled`.
    No-ops when the circuit-breaker is frozen.
    """
    if runtime.is_frozen(db):
        return []
    if not settings.autoapprove_enabled:
        return []
    pend = db.query(PendingOrder).filter(PendingOrder.status == PENDING).all()
    if not pend:
        return []
    market = get_current_prices(list({o.symbol for o in pend}))
    approved: list[int] = []
    for o in pend:
        if o.auto_veto:
            continue
        if o.source not in settings.autoapprove_sources:
            continue
        if settings.autoapprove_require_no_risk and o.risk_note:
            continue
        ref_price = o.price if o.price > 0 else (market.get(o.symbol) or 0.0)
        notional = o.quantity * ref_price
        if ref_price <= 0 or notional > settings.autoapprove_max_notional:
            continue
        # Respect the LIMIT price — never auto-approve a wave whose target isn't reached.
        # (A KSS dip-buy must wait for the actual dip; approving it early defeats the DCA
        #  ladder and overpays. MARKET orders — TP/stop exits — are always due.)
        mkt = market.get(o.symbol) or 0.0
        if o.order_type == "MARKET":
            due = True
        elif o.side == "BUY":
            due = o.price > 0 and 0 < mkt <= o.price
        else:  # SELL
            due = o.price > 0 and mkt >= o.price
        if not due:
            continue
        try:
            approve_order(db, o.id, reviewer="auto-approver")
        except ValueError:  # insufficient cash / no price — skip, retry next tick
            continue
        approved.append(o.id)
    return approved


def session_still_going(db: Session, source_ref: str | None) -> bool:
    """False when a `pyramid:N:*` row belongs to a KSS session that has ended (or vanished).

    The last line of defence against buying into a dead session. `sweep_orphan_waves` retires
    such rungs, but it deliberately skips rows carrying a terminal `exchange_status` to avoid
    stranding a fill only the dead-link reaper can book — and that is EXACTLY the set the
    reaper then releases, later in the same cycle. Once the link is NULL the row is an ordinary
    queued rung again and nothing re-checked whose session it was.

    It happened live: `resting_link_released` on order 4 (ARB) at 06:16:20, then 172 ARB booked
    to `pyramid:4:wave:0` at 06:16:23 — into a session STOPPED for 17 hours. The new
    `EXPIRE_MAKER` self-trade mode feeds it: a stop sweeping our own rung EXPIRES the rung,
    which is a terminal status, so the app could buy back into the fall it just sold to escape.

    Non-pyramid rows always pass, and so does a row whose session cannot be found: a MISSING
    session is `sweep_orphan_waves`'s job (it retires those), and refusing here on a row the
    caller never had a session for would block ordinary flows. What this exists to stop is the
    narrower, observed case — a session that EXISTS and has ENDED.
    """
    ref = str(source_ref or "")
    if not ref.startswith("pyramid:"):
        return True
    try:
        session_id = int(ref.split(":")[1])
    except (IndexError, ValueError):
        return True
    from app.kss.service import (
        SESSION_ACTIVE,
        SESSION_PENDING,
        SESSION_TP_TRIGGERED,
        KssSession,
    )

    row = db.get(KssSession, session_id)
    if row is None:
        return True
    return row.status in (SESSION_ACTIVE, SESSION_PENDING, SESSION_TP_TRIGGERED)


def auto_fill_due_orders(db: Session) -> list[int]:
    """
    Full-auto: auto-approve pending KSS-sourced orders whose limit the market has
    reached (BUY: price ≤ target, SELL: price ≥ target, MARKET: always due). Only
    touches `source="kss"` orders — manual orders always require human approval.
    Returns the approved order ids. No-ops when the circuit-breaker is frozen.
    """
    if runtime.is_frozen(db):
        return []
    pend = (
        db.query(PendingOrder)
        .filter(
            PendingOrder.status == PENDING,
            PendingOrder.source == "kss",
            # An order already resting on the exchange (1.5) is the venue's to fill —
            # approving it here would place a SECOND order for the same rung.
            PendingOrder.exchange_order_id.is_(None),
        )
        .all()
    )
    # Never act on a rung whose session has ended, however it came to be queued again.
    pend = [o for o in pend if session_still_going(db, o.source_ref)]
    if not pend:
        return []
    prices = get_current_prices(list({o.symbol for o in pend}))
    approved: list[int] = []
    for o in pend:
        # Exit SELLs reduce risk — never let a (possibly stale) veto trap them; only a
        # vetoed BUY (new risk) is held back.
        if o.auto_veto and o.side == "BUY":
            continue
        price = prices.get(o.symbol)
        if price is None:
            continue
        due = (
            o.order_type == "MARKET"
            or (o.side == "BUY" and o.price > 0 and price <= o.price)
            or (o.side == "SELL" and o.price > 0 and price >= o.price)
        )
        if due:
            try:
                approve_order(db, o.id, reviewer="auto-trader")
            except Exception as exc:
                # Insufficient cash, no price, or a venue rejection (ccxt raises InvalidOrder,
                # NOT ValueError — that gap let one -1013 order kill the whole scheduler
                # cycle). Skip this one, keep filling the others, retry next tick.
                logger.warning("auto-fill %s order %s skipped (%s: %s)",
                               o.symbol, o.id, type(exc).__name__, exc)
                continue
            approved.append(o.id)
    return approved


def approve_order(db: Session, order_id: int, reviewer: str | None = None) -> Fill:
    """Approve and paper-execute a pending order; fire KSS fill hook if applicable.

    Auto reviewers are blocked when the circuit-breaker freeze is active.
    Human reviewer 'dashboard' is never blocked.
    """
    from app.circuit import AUTO_REVIEWERS  # lazy — circuit imports portfolio which is fine
    order = _get_pending(db, order_id)
    # The freeze stops NEW RISK, never an exit. This check had no side condition, so a frozen
    # breaker blocked automated take-profits, stop-losses and trailing exits too — turning the
    # control that is supposed to protect capital into one that holds a losing position open.
    # Every other gate in this file is already side-aware; this is the chokepoint they all
    # pass through, so it has to be as well.
    if order.side == "BUY" and reviewer in AUTO_REVIEWERS and runtime.is_frozen(db):
        raise ValueError(f"automation frozen — {reviewer} blocked")
    # HARD cash floor: partial-fill a BUY down to available cash (or reject) BEFORE any state
    # change, so cash can never go negative and a reject leaves the order untouched (PENDING).
    _apply_cash_cap(db, order)
    order.status = APPROVED
    order.reviewer = reviewer
    order.decided_at = utcnow()
    db.flush()

    try:
        fill = _execute(db, order)
    except Exception:
        # An order that could not execute must go back to the QUEUE, not sit at APPROVED:
        # sync_resting_orders only ever places PENDING rows, so a rung stranded at APPROVED
        # would never reach the exchange and its session would wait forever. Any exchange
        # link _live_execute already stamped is kept — it is how a placed-but-unfilled order
        # stays tracked.
        order.status = PENDING
        order.reviewer = None
        order.decided_at = None
        db.commit()
        raise
    order.status = EXECUTED
    db.commit()
    db.refresh(fill)

    # Notify KSS strategy of the fill (lazy import to avoid a circular dependency).
    if order.source == "kss" and order.source_ref:
        try:
            from app.kss.service import handle_fill_event

            handle_fill_event(db, order.source_ref, fill.quantity, fill.price)
        except Exception as exc:  # a strategy hook must never corrupt the fill
            logger.exception("KSS fill hook failed for %s: %s", order.source_ref, exc)

    # Best-effort Telegram trade/risk alert — must never break or delay the fill.
    try:
        from app import notify

        notify.fill_alert(fill)
    except Exception:  # network/notify error is non-fatal
        logger.debug("fill_alert failed for fill %s", fill.id)

    return fill


# --- execution dispatch (paper by default; live only when explicitly on) ---


def _execute(db: Session, order: PendingOrder) -> Fill:
    """Route an approved order to live placement when go-live is active, else paper.

    Paper is the default everywhere — live runs ONLY when `execution.live_enabled()`
    (master flag + API keys). See app/execution.py.
    """
    from app import execution

    if execution.live_enabled():
        return _live_execute(db, order)
    return _paper_execute(db, order)


def _live_execute(db: Session, order: PendingOrder) -> Fill:
    """Place a REAL order, re-gating new-exposure BUYs (breaker + notional cap). SELL
    exits are never gated. Raises (never silently papers) if a guard or the exchange fails."""
    from app import execution
    from app.data.providers import live_provider

    # 1.5: under the resting model an ENTRY rung is placed by sync_resting_orders and left on
    # the book, so the synchronous path must not touch it. Sending it here places a post-only
    # order that cannot fill on placement, which then fails the "no fill price" check below —
    # and the venue is left holding an order this row has not been linked to yet. Refuse
    # BEFORE contacting the exchange; the rung stays PENDING and rests on the next cycle.
    # Exits are never refused: a SELL that reduces risk must always be allowed through.
    # Wave 0 is exempt: it is the entry, and it takes (see is_entry_wave).
    if (
        resting_model_active()
        and order.side == "BUY"
        and order.source == "kss"
        and order.order_type == "LIMIT"
        and not order.exchange_order_id
        and not is_entry_wave(order)
    ):
        raise ValueError(
            f"order {order.id} belongs to the resting model — it must rest on the exchange "
            "via sync_resting_orders, not execute synchronously"
        )

    # A7 fix: whether the cancel-resting branch below ran for this call. That branch, by
    # construction, already asked the venue for this order's final state (via _cancel_resting
    # -> _book_delta) — so the probe further down must never re-ask about the SAME
    # clientOrderId afterward: `order.exchange_order_id` is cleared to None on a successful
    # cancel, which would otherwise make the probe's "nothing linked yet" condition always
    # true right after a cancel and hand it back the order JUST cancelled (a zero-fill cancel
    # would then be adopted as a fresh fill; a partial-fill cancel would have its cumulative
    # fill booked a SECOND time on top of what _cancel_resting already booked).
    was_resting = False

    # 1.5: this row already rests on the exchange. Reaching here means someone wants it
    # filled NOW (an operator approval, or the position-guard forcing a crash exit), so take
    # the resting order off the book first — a row must never have two live orders. A cancel
    # that fails aborts the placement: placing anyway would double the exposure.
    if order.exchange_order_id:
        was_resting = True
        # Book whatever it filled BEFORE unlinking. A cancel races the venue, and
        # reconcile_live_orders only looks at linked rows — dropping the link first loses that
        # fill for good. _cancel_resting already does exactly this, so reuse it; a refusal
        # aborts, because placing anyway would double the exposure.
        booked_before, _ = _booked_qty_fee(db, order.id)
        if not _cancel_resting(db, order):
            raise ValueError(
                f"order {order.id}: could not take the resting order off the book — "
                "refusing to place a second one"
            )
        # ...and True does NOT mean "nothing was taken off the book". The venue answering
        # -2011 IS the fill case: _cancel_resting books it and returns True. Placing the full
        # size after that buys the rung twice. Only the part the venue did not fill may go out.
        booked_after, _ = _booked_qty_fee(db, order.id)
        just_filled = booked_after - booked_before
        if just_filled > 1e-9:
            remaining = order.quantity - just_filled
            fill = (
                db.query(Fill)
                .filter(Fill.pending_order_id == order.id)
                .order_by(Fill.id.desc())
                .first()
            )
            if remaining <= 1e-9 or (order.price > 0 and remaining * order.price
                                     < settings.scan_min_notional):
                logger.info(
                    "order %s: the venue had already filled %s of %s — placing nothing more",
                    order.id, just_filled, order.quantity,
                )
                db.commit()
                return fill
            logger.info("order %s: venue filled %s of %s during the cancel — placing the "
                        "remaining %s", order.id, just_filled, order.quantity, remaining)
            order.quantity = remaining

    ref_price = order.price if order.price > 0 else (
        get_current_prices([order.symbol]).get(order.symbol) or 0.0
    )
    if ref_price <= 0:
        raise ValueError(f"No price available to execute {order.symbol}")

    # New exposure (BUY) is gated; exits (SELL) are never blocked.
    if order.side == "BUY":
        if runtime.is_frozen(db):
            raise ValueError("circuit-breaker frozen — live BUY blocked")
        notional = ref_price * order.quantity
        if notional > settings.live_max_order_notional:
            raise ValueError(
                f"live BUY notional {notional:.2f} exceeds cap "
                f"{settings.live_max_order_notional:.2f}"
            )

    pair = live_provider().pair(order.symbol)
    # An entry must actually fill. A post-only BUY at the market is rejected outright (-2010),
    # and even a plain LIMIT only rests when the market ticks up between the scan and the
    # placement — both leave the session ACTIVE holding nothing, which is what the first soak
    # produced. So wave 0 goes out as MARKET: the spread is the price of being in the trade.
    # Every DCA rung below it still rests and still earns the maker side.
    entry = is_entry_wave(order)
    cid = execution.client_order_id(order.id)  # idempotent placement (1.10)

    # A7: probe the venue for this clientOrderId BEFORE placing, catching a lost response after
    # Binance already accepted a PRIOR attempt — see execution.fetch_order_by_client_id for why
    # the reactive duplicate-clientOrderId recovery inside place_live_order cannot catch this for
    # a MARKET order. Skipped entirely when this call just cancelled a resting order above
    # (`was_resting`): that path already asked the venue for this exact order's final state
    # (_cancel_resting -> _book_delta), so re-probing the SAME clientOrderId would hand back the
    # order just cancelled/booked instead of anything new. Also skipped while a rate/order-budget
    # hold is active: a SIGNED probe call during a 418 amplifies an IP ban, and skipping it never
    # gates anything — the fall-through is placement, whose own rate gates are urgent-exempt for
    # a SELL.
    result: dict | None = None
    if (
        execution.live_enabled()
        and not was_resting
        and order.exchange_order_id is None
        and not execution.rate_hold_active()
    ):
        try:
            probed = execution.fetch_order_by_client_id(pair, cid)
        except Exception:
            probed = None
            logger.warning(
                "order %s: idempotency probe failed for clientOrderId %s — placing normally",
                order.id, cid, exc_info=True,
            )
        else:
            if probed is not None:
                probed_status = str(probed.get("status") or "").lower()
                probed_qty = float(probed.get("quantity") or 0.0)
                booked_already, _ = _booked_qty_fee(db, order.id)
                if probed_qty > 1e-12 and booked_already <= 1e-9:
                    # The clean lost-response case: Binance accepted a prior attempt whose
                    # response never reached us, this row has never booked anything against it,
                    # and the venue now reports a real fill quantity — adopt it instead of a
                    # second placement. Credit the ORDERS budget the lost attempt never got to
                    # record (no placement, no fill, ever went through our own tracker for it).
                    result = probed
                    execution.record_order_placed()
                    execution.record_order_filled()
                    logger.info(
                        "order %s: recovered an existing venue order for clientOrderId %s — "
                        "not placing a second one", order.id, cid,
                    )
                elif probed_status in _TERMINAL_EXCHANGE_STATUS and probed_qty <= 1e-12:
                    # Terminal (cancelled/expired/rejected) with nothing filled — nothing to
                    # adopt here; place normally.
                    result = None
                elif probed_status and probed_status not in _TERMINAL_EXCHANGE_STATUS:
                    # Still resting on the venue (open/new) with no clean fill to adopt (either
                    # nothing filled yet, or this row already booked fills against a prior
                    # order). Link this row to it and hand off to reconcile_live_orders rather
                    # than invent a fill price here — mirrors the normal placement link below:
                    # stamped + committed BEFORE raising, so a rollback can never erase a link
                    # to an order that is really out there.
                    if probed.get("raw_id") is not None:
                        order.exchange_order_id = str(probed.get("raw_id"))
                    order.exchange_status = probed_status or None
                    db.commit()
                    raise ValueError(
                        f"order {order.id}: recovered a resting live order; awaiting reconcile"
                    )
                else:
                    # Ambiguous — placing is always recoverable via the venue's own
                    # duplicate-clientOrderId rejection; adopting wrongly is not.
                    result = None
    if result is None:
        # D2-gap: this is the actual venue placement call (as opposed to the gate checks
        # above/below, which are refusals this row will legitimately retry, not exchange
        # failures) — track it the same way `_place_resting` tracks its own placement call,
        # so a PERSISTENT non-auth failure here (filter error, -1013, insufficient balance)
        # is no longer totally silent.
        try:
            result = execution.place_live_order(
                pair, order.side, order.quantity, order.price,
                "MARKET" if entry else order.order_type,
                maker_orders=False if entry else None,
                client_order_id=cid,
            )
        except Exception as exc:
            _note_placement_failure(order, f"{type(exc).__name__}: {exc}")
            raise
    _note_placement_success(order.id)
    # Link the order to its exchange id + last-seen status FIRST, so reconcile_live_orders()
    # (1.4) can pick up any further fills (e.g. a partial that completes later) — and, above
    # all, so an order that reached the venue is never left untracked. This used to happen
    # after the "no fill price" check below, which meant a maker order that rested instead of
    # filling was abandoned on the exchange: nothing to reconcile it, nothing to cancel it.
    # Committed before raising for the same reason — a rollback would erase the link to an
    # order that is really out there.
    if result.get("raw_id") is not None:
        order.exchange_order_id = str(result.get("raw_id"))
    order.exchange_status = str(result.get("status") or "") or None

    eff = float(result.get("price") or 0.0)
    if eff <= 0:
        db.commit()
        raise ValueError("live order returned no fill price")
    qty = float(result.get("quantity") or order.quantity)
    fee = float(result.get("fee") or 0.0)
    # Book what the wallet RECEIVED. On a spot BUY Binance's commission comes out of the coin,
    # so the venue's `filled` overstates our holding by ~0.1% — and the exit that later asks
    # for the booked quantity is rejected -2010, killing the take-profit and the stop-loss in
    # silence. A SELL's commission comes out of the proceeds instead, so its quantity stands.
    if order.side == "BUY":
        qty = max(0.0, qty - float(result.get("fee_base") or 0.0))

    realized = _update_position(db, order.symbol, order.side, qty, eff, fee)
    fill = Fill(
        pending_order_id=order.id,
        # Tag it, or this path keeps minting NULL-keyed fills and a row that reaches a THIRD
        # exchange order re-acquires the netting bug the tagging exists to prevent.
        exchange_order_id=order.exchange_order_id,
        symbol=order.symbol,
        side=order.side,
        quantity=qty,
        price=eff,
        fee=fee,
        slippage=0.0,
        realized_pnl=realized,
        source_ref=order.source_ref,
        strategy_name=order.strategy_name,
    )
    db.add(fill)
    db.flush()
    logger.info(
        "LIVE fill order %s: %s %s %s @ %s", order.id, order.side, qty, order.symbol, eff
    )
    return fill


# --- async live reconciliation (live-readiness 1.4) ---------------------

# ccxt-normalised statuses after which an order will see no further fills.
_TERMINAL_EXCHANGE_STATUS = {"closed", "filled", "canceled", "cancelled", "expired", "rejected"}


def _booked_qty_fee(db: Session, pending_order_id: int,
                    exchange_order_id: str | None = None) -> tuple[float, float]:
    """Sum (quantity, fee) of Fills already recorded — the idempotency key for live
    reconciliation, so a fill already booked is never double-counted however often
    reconcile runs.

    Scoped to ONE exchange order when given. The venue reports a cumulative `filled` per
    order, so comparing it against every fill the pending_order ever had is only equivalent
    while the row has had a single exchange order. After a cancel-and-re-place the old
    order's fills would be netted off the new one's total and real fills silently skipped.
    Rows booked before this column existed have it NULL and are counted for every order,
    which preserves the old behaviour for that history instead of double-booking it."""
    q = db.query(Fill.quantity, Fill.fee).filter(Fill.pending_order_id == pending_order_id)
    if exchange_order_id is not None:
        q = q.filter(
            (Fill.exchange_order_id == str(exchange_order_id)) | (Fill.exchange_order_id.is_(None))
        )
    rows = q.all()
    return (sum(r[0] for r in rows), sum(r[1] for r in rows))


def reconcile_live_orders(db: Session) -> list[int]:
    """Poll resting live orders and book any newly-reported fills (live mode only).

    The async counterpart to synchronous paper execution: a live maker order rests on
    the exchange (status NEW/open) and fills later. Each pass fetches every tracked
    order's status and books the *delta* between the venue's cumulative ``filled`` and
    the quantity we have already recorded as Fills — so a NEW→FILLED transition creates
    exactly one Fill + Position update, and re-running is idempotent (delta 0 → no-op).
    Partial fills accumulate across passes. Paper mode no-ops (``live_enabled()`` False).

    Booking reflects what the exchange already did, so it is NOT gated by the circuit
    breaker (the breaker blocks *new* placement, never the recording of a real fill —
    same invariant as never gating a SELL exit). Returns the ids that booked a fill.
    """
    from app import execution
    from app.data.providers import live_provider

    if not execution.live_enabled():
        return []

    tracked = (
        db.query(PendingOrder)
        .filter(
            PendingOrder.exchange_order_id.isnot(None),
            (PendingOrder.exchange_status.is_(None))
            | (PendingOrder.exchange_status.notin_(_TERMINAL_EXCHANGE_STATUS)),
        )
        .all()
    )
    booked: list[int] = []
    for order in tracked:
        order_id = order.exchange_order_id
        if not order_id:  # guarded by the query filter, but keep the type checker honest
            continue
        try:
            pair = live_provider().pair(order.symbol)
            res = execution.fetch_live_order(pair, order_id)
        except Exception:  # a transient fetch error must not break the cycle — retry next pass
            logger.exception("reconcile: fetch_order failed for %s (order %s)", order.symbol, order.id)
            continue

        if _book_delta(db, order, res):
            booked.append(order.id)
    db.commit()
    return booked


def _venue_fill_time(res: dict) -> datetime:
    """The venue's own fill time from ``res["filled_at_ms"]`` (see ``execution._venue_fill_ms``),
    converted to this codebase's naive-UTC convention (``app.clock.utcnow``). Falls back to
    "now" when the venue reported nothing usable — absent, zero/negative, or more than 24h in
    the FUTURE (clock skew must never write a future fill; a reconcile pass should record "when
    we found out" rather than a lie). Fixes fills being mis-dated by hours to a day after any
    outage or delayed reconcile pass (live evidence: four DCA rungs filled during a 30h outage
    all carried the reconcile pass's own time instead of the venue's 2026-09-02 fill times)."""
    now = utcnow()
    ms = res.get("filled_at_ms")
    if not ms or ms <= 0:
        return now
    try:
        dt = datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).replace(tzinfo=None)
    except (OverflowError, OSError, ValueError):
        return now
    if dt > now + timedelta(hours=24):
        return now
    return dt


def _book_delta(db: Session, order: PendingOrder, res: dict) -> bool:
    """Book the *delta* between the venue's cumulative fill and what we already recorded.

    The idempotent core of reconciliation: a NEW→FILLED transition creates exactly one Fill +
    Position update, re-running is a no-op (delta 0), and partial fills accumulate across
    calls. Also stamps the row's exchange status, and marks it EXECUTED once the venue is
    terminal with something filled. Returns True when a Fill was created.

    Shared with ``_cancel_resting``: a cancel has to book what the venue already filled
    BEFORE the exchange link is dropped, because reconcile never looks at an unlinked row.
    """
    from app import execution

    status = str(res.get("status") or "").lower()
    cum_filled = float(res.get("filled") or 0.0)
    # Net of the base-asset commission on a BUY, for the same reason as the synchronous path:
    # the venue's cumulative `filled` is gross, the wallet received less, and an exit sized on
    # the gross figure is rejected -2010. Under the resting model this is where MOST fills are
    # booked, so getting it wrong here is the common case, not the edge one.
    if order.side == "BUY":
        cum_filled = max(0.0, cum_filled - float(res.get("fee_base") or 0.0))
    avg = float(res.get("average") or 0.0)
    cum_fee = float(res.get("fee") or 0.0)

    def _stamp_exchange_state() -> None:
        # Record the venue status — and EXECUTED once terminal with something filled. Runs
        # BEFORE the KSS fill hook (not after, as this originally did): the hook's deploy-
        # headroom check (_pending_wave_notional) queries rows still PENDING/APPROVED to size
        # the next rung's reservation, and this order must drop OUT of that count the instant
        # its money is booked into total_cost, or it is charged twice — once as total_cost,
        # once as still-pending notional (WLD#13, audit row 1532: headroom computed 48.21
        # instead of the real 124.11). Mirrors approve_order's ordering — the synchronous path
        # never had this bug. The stamp runs immediately before the Fill's own `db.flush()`
        # (same call site, right below) — the two are the SAME unit of work: if booking throws
        # before that flush, neither the stamp nor the Fill were ever sent to the database, and
        # nothing here needs a rollback of its own. Once the flush succeeds, the stamp IS
        # visible to any query on this connection (including the KSS hook's) before the
        # eventual commit — that visibility, not avoidance, is the whole point of the ordering.
        order.exchange_status = status or order.exchange_status
        if status in _TERMINAL_EXCHANGE_STATUS and cum_filled > 0:
            order.status = EXECUTED
            order.decided_at = order.decided_at or utcnow()

    booked = False
    venue_order_id = str(res.get("raw_id") or order.exchange_order_id or "") or None
    booked_qty, booked_fee = _booked_qty_fee(db, order.id, venue_order_id)
    delta = cum_filled - booked_qty
    if delta > 1e-9 and avg > 0:
        delta_fee = max(cum_fee - booked_fee, 0.0)
        realized = _update_position(db, order.symbol, order.side, delta, avg, delta_fee)
        fill = Fill(
            pending_order_id=order.id,
            symbol=order.symbol,
            side=order.side,
            quantity=delta,
            price=avg,
            fee=delta_fee,
            slippage=0.0,
            realized_pnl=realized,
            source_ref=order.source_ref,
            strategy_name=order.strategy_name,
            exchange_order_id=venue_order_id,
            executed_at=_venue_fill_time(res),
        )
        db.add(fill)
        _stamp_exchange_state()  # before the flush + KSS hook — see the note above
        db.flush()
        booked = True
        # A resting maker order was still OUTSTANDING against the ORDERS budget (a cancel never
        # credits it, only a fill does — see the note in app/execution.py). This is where the
        # async fill this module discovers is first known, so it is where the credit belongs —
        # but ONE placement may only ever return ONE unit. A maker order in a thin book fills in
        # several deltas, and crediting each of them would make the tracker believe we have more
        # budget than we do, which is the one direction that ends in a -1015.
        if booked_qty <= 1e-9:
            execution.record_order_filled()
        logger.info(
            "LIVE reconciled fill order %s: %s %s %s @ %s (status=%s)",
            order.id, order.side, delta, order.symbol, avg, status,
        )
        # Advance the KSS ladder on the booked quantity (same hook approve_order fires).
        if order.source == "kss" and order.source_ref:
            try:
                from app.kss.service import handle_fill_event

                handle_fill_event(db, order.source_ref, delta, avg)
            except Exception as exc:  # a strategy hook must never corrupt the fill
                logger.exception("KSS fill hook failed for %s: %s", order.source_ref, exc)
        try:
            from app import notify

            notify.fill_alert(fill)
        except Exception:
            logger.debug("fill_alert failed for fill %s", fill.id)

    if not booked:
        _stamp_exchange_state()  # nothing new to book — still record the venue's state
    return booked


# --- live resting-maker model (live-readiness 1.5) ----------------------


def is_entry_wave(order: PendingOrder) -> bool:
    """True for a KSS session's wave 0 — the ENTRY the session is built on.

    Wave 0 is anchored AT the live market, so it is meant to fill now and cannot be a maker
    order: a BUY at the market crosses the ask and the venue rejects the post-only (-2010).
    It therefore TAKES, while waves 1..n (and the pyramid_up defensive rung) sit BELOW the
    market waiting for a dip — those rest, and still earn the maker side.
    """
    ref = str(order.source_ref or "")
    return order.source == "kss" and ref.endswith(":wave:0")


def resting_model_active() -> bool:
    """True when live orders must REST on the exchange instead of waiting for the market.

    Live **and** ``maker_orders`` only. Paper keeps its synchronous simulated fill, and live
    with maker off keeps the legacy "wait until market reaches the limit, then send a
    marketable order" model — so MAKER_ORDERS is the switch between the two live models and
    turning it off is the way back.
    """
    from app import execution

    return bool(execution.live_enabled() and settings.maker_orders)


def _cancel_resting(db: Session, order: PendingOrder) -> bool:
    """Take *order* off the book, booking anything it filled first, then unlink the row.

    False when the venue refused: the link is then KEPT so the next cycle retries. Dropping
    ``exchange_order_id`` after a failed cancel would orphan a live order nothing tracks.

    A cancel races the venue. Whatever filled before it landed is real, and ``reconcile_live_
    orders`` only looks at rows that still carry a link — so the final status is read and the
    delta booked BEFORE unlinking, or that fill would be lost for good (worst case: a session
    closing on a half-filled take-profit keeps a position it no longer holds). If that read
    fails, the link is kept and the next cycle books it.
    """
    from app import execution
    from app.data.providers import live_provider

    pair = live_provider().pair(order.symbol)
    order_id = str(order.exchange_order_id)
    try:
        execution.cancel_live_order(pair, order_id)
    except Exception as exc:
        # "The venue does not hold this order" is not a failure — it is the fill case.
        if not execution.order_is_gone(exc):
            logger.exception(
                "resting: cancel failed for order %s (exch %s)", order.id, order_id
            )
            return False
        logger.info(
            "resting: venue no longer holds order %s (exch %s) — reading its final status",
            order.id, order_id,
        )

    try:
        _book_delta(db, order, execution.fetch_live_order(pair, order_id))
    except Exception:  # cannot see what filled — keep the link and retry next cycle
        logger.exception(
            "resting: final status read failed for order %s (exch %s)", order.id, order_id
        )
        return False

    order.exchange_order_id = None
    order.exchange_status = None
    return True


def _place_resting(db: Session, order: PendingOrder) -> bool:
    """Place one queued order as a resting post-only LIMIT and link it to the row.

    Re-gates exactly like ``_live_execute``: a BUY is new exposure (Guardian veto, breaker,
    notional cap, cash floor); a SELL exit is never gated. Returns True when the order now
    rests on the exchange. Never raises — a placement failure leaves the order queued for
    the next cycle.
    """
    from app import execution
    from app.data.providers import live_provider

    if order.side == "BUY":
        if order.auto_veto or runtime.is_frozen(db):
            return False
        notional = order.price * order.quantity
        if notional > settings.live_max_order_notional:
            logger.info(
                "resting: order %s notional %.2f exceeds cap %.2f — not placed",
                order.id, notional, settings.live_max_order_notional,
            )
            return False
        try:
            _apply_cash_cap(db, order)  # may shrink the qty to what free cash funds
        except InsufficientCashError:
            return False

    try:
        res = execution.place_live_order(
            live_provider().pair(order.symbol), order.side, order.quantity, order.price,
            "LIMIT", maker_orders=True,
            client_order_id=execution.client_order_id(order.id),  # idempotent (1.10)
        )
    except Exception as exc:  # exchange/filter error — stays queued, retried next cycle
        logger.exception("resting: placement failed for order %s", order.id)
        _note_placement_failure(order, f"{type(exc).__name__}: {exc}")
        return False

    # Post-only rejected = the market is already at/through the rung, so resting there is
    # impossible right now. Leave it queued; the next cycle retries once the book moves away.
    # Does NOT feed the placement-failure streak: a rung priced at/through the book is
    # post-only-rejected EVERY cycle by design until price moves away — that is a normal market
    # condition, not a persistent problem, and the default alert threshold (3) turned it into
    # Telegram risk spam on every session with a rung sitting near the touch. The streak stays
    # reserved for exchange/filter errors (the branch above) and the synchronous placement call
    # in `_live_execute`, where 3 in a row really does mean something is stuck.
    if res.get("status") == "rejected" or res.get("raw_id") is None:
        return False

    order.exchange_order_id = str(res["raw_id"])
    status = str(res.get("status") or "").lower()
    # A venue that already reports terminal (an immediate fill) still needs that fill booked,
    # and reconcile_live_orders skips terminal rows — so leave the status unset for it.
    order.exchange_status = None if status in _TERMINAL_EXCHANGE_STATUS else (status or None)
    audit.log(
        db, "orders", "resting_placed", entity=f"order:{order.id}", symbol=order.symbol,
        side=order.side, qty=round(order.quantity, 8), price=round(order.price, 8),
        exchange_order_id=order.exchange_order_id,
    )
    _note_placement_success(order.id)
    return True


def sync_resting_orders(db: Session) -> dict:
    """Keep the exchange's resting maker orders in step with the local queue (live only).

    The 1.5 model shift: instead of waiting for the market to reach a wave's limit and then
    sending a marketable order, every queued KSS limit is placed on the exchange IN ADVANCE
    and rests there until the venue fills it — ``reconcile_live_orders`` (1.4) books the fill
    and advances the ladder. Each cycle:

      * **cancel** a resting order whose row was rejected, or one that outlived
        ``order_fill_timeout_sec`` (0 = wait forever, the DCA default);
      * **place** every queued KSS limit that is not on the exchange yet.

    A *replace* is therefore a cancel now and a place on the next pass: the local row is the
    record of what was placed, so changing its price/qty is what drives a re-place.

    Only ``source='kss'`` limits rest — manual orders keep human approval, and risk exits
    (SL/trailing/deadline/OPUS-close) are MARKET by design and must never rest. No-op unless
    ``resting_model_active()``. Returns counts for the cycle summary.
    """
    out = {"placed": 0, "cancelled": 0}
    if not resting_model_active():
        return out

    # Release a DEAD link first. An order cancelled outside the app — an operator in the
    # exchange UI, an exchange-side expiry, a cancel-all — leaves the row PENDING with a
    # terminal exchange_status, and every query below (and reconcile, and auto-fill) then
    # excludes it: never re-placed, never cancelled, never reconciled. The session's ladder
    # just silently died while its deploy headroom stayed reserved. Anything it did fill has
    # already been booked by reconcile (which is what set the terminal status), so dropping
    # the link here loses nothing and lets the rung be placed again.
    for order in (
        db.query(PendingOrder)
        .filter(
            PendingOrder.status == PENDING,
            PendingOrder.source == "kss",          # only rows this model placed
            PendingOrder.order_type == "LIMIT",    # a MARKET risk exit is never "resting"
            PendingOrder.exchange_order_id.isnot(None),
            PendingOrder.exchange_status.in_(_TERMINAL_EXCHANGE_STATUS),
        )
        .all()
    ):
        # ASK THE VENUE FIRST. A terminal status is not proof the fill was booked: _live_execute
        # stamps the raw placement status and books nothing when the venue reports no usable
        # price, which leaves PENDING + link + terminal with a REAL fill unrecorded. Releasing
        # that link would discard the fill AND strip the "already resting" guard, so the rung
        # gets bought a second time. Booking first makes both impossible; a fully filled row
        # becomes EXECUTED and drops out of this loop on its own.
        from app import execution
        from app.data.providers import live_provider

        try:
            _book_delta(db, order, execution.fetch_live_order(
                live_provider().pair(order.symbol), str(order.exchange_order_id)))
        except Exception:  # cannot confirm — keep the link and retry next cycle
            logger.exception("resting: could not confirm dead link on order %s", order.id)
            continue
        if order.status != PENDING:
            continue  # it had filled after all; _book_delta marked it EXECUTED
        logger.info("resting: releasing dead exchange link on order %s (%s)",
                    order.id, order.exchange_status)
        audit.log(db, "orders", "resting_link_released", entity=f"order:{order.id}",
                  symbol=order.symbol, exchange_order_id=order.exchange_order_id,
                  exchange_status=order.exchange_status)
        order.exchange_order_id = None
        order.exchange_status = None
    db.commit()  # settle the releases before the loops below query these rows

    linked = (
        db.query(PendingOrder)
        .filter(
            PendingOrder.exchange_order_id.isnot(None),
            (PendingOrder.exchange_status.is_(None))
            | (PendingOrder.exchange_status.notin_(_TERMINAL_EXCHANGE_STATUS)),
        )
        .all()
    )
    timeout = settings.order_fill_timeout_sec
    now = utcnow()
    for order in linked:
        # Only rows this model owns: an order the operator rejected, or a rung that waited
        # past the timeout. APPROVED/EXECUTED rows belong to the synchronous path — a
        # partially filled one of those must keep resting for reconcile to finish it.
        if order.status == REJECTED:
            if _cancel_resting(db, order):
                out["cancelled"] += 1
        elif order.status == PENDING and timeout > 0 and order.created_at is not None:
            if (now - order.created_at).total_seconds() > timeout and _cancel_resting(db, order):
                out["cancelled"] += 1
                # The cancel may have booked a fill that was already sitting at the venue,
                # which marks the row EXECUTED — a rung that FILLED must never be recorded
                # as a timed-out rejection.
                if order.status != PENDING:
                    continue
                order.status = REJECTED
                order.reviewer = "resting-timeout"
                order.decided_at = now
                audit.log(db, "orders", "resting_timeout", entity=f"order:{order.id}",
                          symbol=order.symbol, timeout_sec=timeout)

    # Resting a rung in advance IS the auto-fill of this model, so it follows the same
    # switch: with auto_trade off the operator approves by hand (the synchronous path).
    # Cancellation above is not gated — a rejected or timed-out order must always come off
    # the book.
    if settings.auto_trade:
        due = (
            db.query(PendingOrder)
            .filter(
                PendingOrder.status == PENDING,
                PendingOrder.source == "kss",
                PendingOrder.order_type == "LIMIT",
                PendingOrder.price > 0,
                PendingOrder.exchange_order_id.is_(None),
            )
            .all()
        )
        # Same guard as the auto-fill path: a rung freed by the dead-link reaper must not be
        # re-rested on the venue for a session that has ended.
        due = [o for o in due if session_still_going(db, o.source_ref)]
        for order in due:
            # Wave 0 is the entry and takes (is_entry_wave) — the synchronous path owns it.
            # Resting it as well would put two live orders behind one row, and as a post-only
            # BUY at the market it would only be rejected anyway.
            if is_entry_wave(order):
                continue
            if _place_resting(db, order):
                out["placed"] += 1

    db.commit()
    return out


# --- paper execution ----------------------------------------------------


def _paper_execute(db: Session, order: PendingOrder) -> Fill:
    """Simulate a fill with slippage + taker fee and update the position.

    A LIMIT order fills at the **marketable** price a real exchange would give — never worse
    than the live market: a BUY at ``min(limit, market)`` (so a DCA rung the market has gapped
    BELOW is bought at the current market, not at the now-too-high limit = no overpay), a SELL
    at ``max(limit, market)``. A MARKET order fills at the live price. Offline (no market
    price) falls back to the limit, preserving legacy/offline-test behaviour."""
    mkt = get_current_prices([order.symbol]).get(order.symbol) or 0.0
    if order.price > 0:  # LIMIT — marketable fill, never worse than market
        if mkt > 0:
            ref_price = min(order.price, mkt) if order.side == "BUY" else max(order.price, mkt)
        else:
            ref_price = order.price  # offline fallback = the limit
    else:  # MARKET
        ref_price = mkt
    if ref_price <= 0:
        raise ValueError(f"No price available to execute {order.symbol}")

    slip = settings.slippage_pct / 100.0
    effective = ref_price * (1 + slip) if order.side == "BUY" else ref_price * (1 - slip)
    notional = effective * order.quantity
    fee = notional * settings.taker_fee_pct / 100.0
    slippage_cost = abs(effective - ref_price) * order.quantity

    realized = _update_position(db, order.symbol, order.side, order.quantity, effective, fee)

    fill = Fill(
        pending_order_id=order.id,
        symbol=order.symbol,
        side=order.side,
        quantity=order.quantity,
        price=effective,
        fee=fee,
        slippage=slippage_cost,
        realized_pnl=realized,
        source_ref=order.source_ref,
        strategy_name=order.strategy_name,
    )
    db.add(fill)
    db.flush()
    return fill


def _update_position(
    db: Session, symbol: str, side: str, qty: float, price: float, fee: float
) -> float:
    """Apply a fill to the position. Returns realized P&L (non-zero only on SELL)."""
    pos = db.query(Position).filter(Position.symbol == symbol).one_or_none()
    if pos is None:
        pos = Position(symbol=symbol, quantity=0.0, avg_entry_price=0.0, total_cost=0.0)
        db.add(pos)
        db.flush()

    realized = 0.0
    if side == "BUY":
        new_qty = pos.quantity + qty
        pos.total_cost += qty * price + fee
        pos.quantity = new_qty
        pos.avg_entry_price = pos.total_cost / new_qty if new_qty > 0 else 0.0
    else:  # SELL — never sell more than is held; an empty position books NO proceeds
        # (a stale/duplicate exit hitting a flat position must not invent phantom profit).
        sell_qty = min(qty, max(pos.quantity, 0.0))
        if sell_qty > 0:
            cost_basis = pos.avg_entry_price * sell_qty
            proceeds = price * sell_qty - fee
            realized = proceeds - cost_basis
            pos.realized_pnl += realized
            pos.quantity = max(0.0, pos.quantity - sell_qty)
            pos.total_cost = max(0.0, pos.total_cost - cost_basis)
            if pos.quantity == 0:
                pos.avg_entry_price = 0.0
    pos.updated_at = utcnow()
    db.flush()
    return realized


def _get_pending(db: Session, order_id: int) -> PendingOrder:
    order = db.get(PendingOrder, order_id)
    if order is None:
        raise ValueError(f"Order {order_id} not found")
    if order.status not in (PENDING, APPROVED):
        raise ValueError(f"Order {order_id} is not actionable (status={order.status})")
    return order
