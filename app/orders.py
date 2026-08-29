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
            except ValueError:  # insufficient cash / no price — skip, retry next tick
                continue
            approved.append(o.id)
    return approved


def approve_order(db: Session, order_id: int, reviewer: str | None = None) -> Fill:
    """Approve and paper-execute a pending order; fire KSS fill hook if applicable.

    Auto reviewers are blocked when the circuit-breaker freeze is active.
    Human reviewer 'dashboard' is never blocked.
    """
    from app.circuit import AUTO_REVIEWERS  # lazy — circuit imports portfolio which is fine
    if reviewer in AUTO_REVIEWERS and runtime.is_frozen(db):
        raise ValueError(f"automation frozen — {reviewer} blocked")
    order = _get_pending(db, order_id)
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

    # 1.5: this row already rests on the exchange. Reaching here means someone wants it
    # filled NOW (an operator approval, or the position-guard forcing a crash exit), so take
    # the resting order off the book first — a row must never have two live orders. A cancel
    # that fails aborts the placement: placing anyway would double the exposure.
    if order.exchange_order_id:
        execution.cancel_live_order(
            live_provider().pair(order.symbol), str(order.exchange_order_id)
        )
        order.exchange_order_id = None
        order.exchange_status = None

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
    # An entry must fill now, so it is sent WITHOUT post-only even when maker_orders is on —
    # a post-only BUY at the market is rejected outright (-2010) and the session would sit
    # ACTIVE holding nothing. The spread is the price of actually being in the trade; the DCA
    # rungs below still rest and still earn the maker side.
    maker = None if not is_entry_wave(order) else False
    result = execution.place_live_order(
        pair, order.side, order.quantity, order.price, order.order_type,
        maker_orders=maker,
        client_order_id=execution.client_order_id(order.id),  # idempotent placement (1.10)
    )
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

    realized = _update_position(db, order.symbol, order.side, qty, eff, fee)
    fill = Fill(
        pending_order_id=order.id,
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


def _booked_qty_fee(db: Session, pending_order_id: int) -> tuple[float, float]:
    """Sum (quantity, fee) of Fills already recorded for a pending order — the
    idempotency key for live reconciliation, so a fill already booked is never
    double-counted no matter how many times reconcile runs."""
    rows = (
        db.query(Fill.quantity, Fill.fee)
        .filter(Fill.pending_order_id == pending_order_id)
        .all()
    )
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


def _book_delta(db: Session, order: PendingOrder, res: dict) -> bool:
    """Book the *delta* between the venue's cumulative fill and what we already recorded.

    The idempotent core of reconciliation: a NEW→FILLED transition creates exactly one Fill +
    Position update, re-running is a no-op (delta 0), and partial fills accumulate across
    calls. Also stamps the row's exchange status, and marks it EXECUTED once the venue is
    terminal with something filled. Returns True when a Fill was created.

    Shared with ``_cancel_resting``: a cancel has to book what the venue already filled
    BEFORE the exchange link is dropped, because reconcile never looks at an unlinked row.
    """
    status = str(res.get("status") or "").lower()
    cum_filled = float(res.get("filled") or 0.0)
    avg = float(res.get("average") or 0.0)
    cum_fee = float(res.get("fee") or 0.0)

    booked = False
    booked_qty, booked_fee = _booked_qty_fee(db, order.id)
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
        )
        db.add(fill)
        db.flush()
        booked = True
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

    order.exchange_status = status or order.exchange_status
    # A fully/terminally settled order with any fill is done — mark it executed.
    if status in _TERMINAL_EXCHANGE_STATUS and cum_filled > 0:
        order.status = EXECUTED
        order.decided_at = order.decided_at or utcnow()
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
    except Exception:  # exchange/filter error — stays queued, retried next cycle
        logger.exception("resting: placement failed for order %s", order.id)
        return False

    # Post-only rejected = the market is already at/through the rung, so resting there is
    # impossible right now. Leave it queued; the next cycle retries once the book moves away.
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
