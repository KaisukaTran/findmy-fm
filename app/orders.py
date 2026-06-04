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
from datetime import datetime

from sqlalchemy.orm import Session

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
    _, violations = check_all_risks(symbol, quantity, ref_price, db)
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


def list_pending(db: Session, status: str | None = None, limit: int = 100) -> list[PendingOrder]:
    """List orders, optionally filtered by status (defaults to pending)."""
    q = db.query(PendingOrder)
    q = q.filter(PendingOrder.status == (status or PENDING))
    return q.order_by(PendingOrder.created_at.desc()).limit(limit).all()


# --- decisions ----------------------------------------------------------


def reject_order(
    db: Session, order_id: int, reason: str = "", reviewer: str | None = None
) -> PendingOrder:
    order = _get_pending(db, order_id)
    order.status = REJECTED
    order.reject_reason = reason
    order.reviewer = reviewer
    order.decided_at = datetime.utcnow()
    db.commit()
    db.refresh(order)
    return order


def auto_fill_due_orders(db: Session) -> list[int]:
    """
    Full-auto: auto-approve pending KSS-sourced orders whose limit the market has
    reached (BUY: price ≤ target, SELL: price ≥ target, MARKET: always due). Only
    touches `source="kss"` orders — manual orders always require human approval.
    Returns the approved order ids.
    """
    pend = (
        db.query(PendingOrder)
        .filter(PendingOrder.status == PENDING, PendingOrder.source == "kss")
        .all()
    )
    if not pend:
        return []
    prices = get_current_prices(list({o.symbol for o in pend}))
    approved: list[int] = []
    for o in pend:
        price = prices.get(o.symbol)
        if price is None:
            continue
        due = (
            o.order_type == "MARKET"
            or (o.side == "BUY" and o.price > 0 and price <= o.price)
            or (o.side == "SELL" and o.price > 0 and price >= o.price)
        )
        if due:
            approve_order(db, o.id, reviewer="auto-trader")
            approved.append(o.id)
    return approved


def approve_order(db: Session, order_id: int, reviewer: str | None = None) -> Fill:
    """Approve and paper-execute a pending order; fire KSS fill hook if applicable."""
    order = _get_pending(db, order_id)
    order.status = APPROVED
    order.reviewer = reviewer
    order.decided_at = datetime.utcnow()
    db.flush()

    fill = _paper_execute(db, order)
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

    return fill


# --- paper execution ----------------------------------------------------


def _paper_execute(db: Session, order: PendingOrder) -> Fill:
    """Simulate a fill with slippage + taker fee and update the position."""
    ref_price = order.price if order.price > 0 else (
        get_current_prices([order.symbol]).get(order.symbol) or 0.0
    )
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
    else:  # SELL
        sell_qty = min(qty, pos.quantity) if pos.quantity > 0 else qty
        cost_basis = pos.avg_entry_price * sell_qty
        proceeds = price * sell_qty - fee
        realized = proceeds - cost_basis
        pos.realized_pnl += realized
        pos.quantity = max(0.0, pos.quantity - sell_qty)
        pos.total_cost = max(0.0, pos.total_cost - cost_basis)
        if pos.quantity == 0:
            pos.avg_entry_price = 0.0
    pos.updated_at = datetime.utcnow()
    db.flush()
    return realized


def _get_pending(db: Session, order_id: int) -> PendingOrder:
    order = db.get(PendingOrder, order_id)
    if order is None:
        raise ValueError(f"Order {order_id} not found")
    if order.status not in (PENDING, APPROVED):
        raise ValueError(f"Order {order_id} is not actionable (status={order.status})")
    return order
