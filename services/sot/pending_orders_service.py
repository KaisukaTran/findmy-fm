"""Pending orders service for manual approval workflow."""

from typing import List, Dict, Any, Optional
from datetime import datetime
import logging

try:
    import ccxt
except ImportError:
    ccxt = None  # type: ignore[assignment]

from services.sot.db import SessionLocal
from services.sot.pending_orders import PendingOrder, PendingOrderStatus
from services.risk import calculate_order_qty, check_all_risks
from src.findmy.config import settings

# v0.10.0: KSS hooks (lazy import to avoid circular deps)
def _get_kss_hooks():
    try:
        from src.findmy.kss.hooks import on_order_approved, on_order_rejected
        return on_order_approved, on_order_rejected
    except ImportError:
        return None, None

logger = logging.getLogger(__name__)


def _write_audit(order, dry_run: bool, request_payload: str,
                 response_payload: str, status: str, error: str = "") -> None:
    """Persist one live-order audit record via raw sqlite (no ORM dependency)."""
    import sqlite3, os
    from pathlib import Path
    url = os.getenv("DATABASE_URL") or os.getenv("SOT_DATABASE_URL") or "sqlite:///./data/findmy_fm_paper.db"
    db_path = url[len("sqlite:///"):] if url.startswith("sqlite:///") else "./data/findmy_fm_paper.db"
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    try:
        with sqlite3.connect(db_path) as con:
            con.execute("""
                INSERT INTO live_orders_audit
                    (pending_order_id, symbol, side, quantity, dry_run,
                     exchange_request, exchange_response, status, error_message)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (order.id, order.symbol, order.side, order.quantity,
                  1 if dry_run else 0,
                  request_payload, response_payload, status, error))
            con.commit()
    except Exception as e:
        logger.error(f"Audit write failed: {e}")


def _execute_live_order(order) -> Optional[str]:
    """
    Send order to Binance. Respects live_trading_dry_run config.

    Returns exchange order ID on success, None on failure.
    Logs and audits every attempt.
    """
    if ccxt is None:
        logger.error("ccxt not installed — live trading blocked. pip install ccxt")
        _write_audit(order, dry_run=True, request_payload="",
                     response_payload="", status="blocked",
                     error="ccxt not installed")
        return None

    dry_run: bool = getattr(settings, "live_trading_dry_run", True)
    exchange_cfg = {
        "apiKey": settings.broker_api_key,
        "secret": (settings.broker_api_secret.get_secret_value()
                   if settings.broker_api_secret else None),
        "sandbox": dry_run,  # True = Binance testnet
    }
    side = "buy" if order.side == "BUY" else "sell"
    req = {"symbol": order.symbol, "side": side, "amount": order.quantity, "dry_run": dry_run}

    try:
        exchange = ccxt.binance(exchange_cfg)
        result = exchange.create_market_order(
            symbol=order.symbol, side=side, amount=order.quantity
        )
        exchange_id = result.get("id", "")
        _write_audit(order, dry_run=dry_run,
                     request_payload=str(req),
                     response_payload=str(result),
                     status="filled")
        mode = "DRY-RUN" if dry_run else "LIVE"
        logger.info(f"[{mode}] Order executed: id={exchange_id} {order.side} {order.quantity} {order.symbol}")
        return exchange_id
    except Exception as e:
        logger.error(f"Live execution failed for order {order.id}: {e}")
        _write_audit(order, dry_run=dry_run,
                     request_payload=str(req),
                     response_payload="",
                     status="error",
                     error=str(e))
        return None


def queue_order(
    symbol: str,
    side: str,
    quantity: Optional[float] = None,
    price: float = 0.0,
    source: str = "manual",
    order_type: str = "MARKET",
    source_ref: Optional[str] = None,
    strategy_name: Optional[str] = None,
    confidence: Optional[float] = None,
    note: Optional[str] = None,
    pips: Optional[float] = None,
) -> tuple[PendingOrder, Optional[str]]:
    """
    Queue an order for manual approval.
    
    All orders must go through this queue before execution.
    
    Args:
        symbol: Asset symbol (e.g., "BTC", "ETH")
        side: Order side ("BUY" or "SELL")
        quantity: Order quantity (auto-calculated from pips if not provided)
        price: Order price
        source: Source of order ("excel", "strategy", "backtest")
        order_type: Order type ("MARKET", "LIMIT", "STOP_LOSS")
        source_ref: Optional reference to source
        strategy_name: Optional strategy name if from strategy
        confidence: Optional signal confidence if from strategy
        note: Optional notes
        pips: Optional number of pips (will calculate quantity)
    
    Returns:
        Tuple of (PendingOrder, risk_violation_note)
        - PendingOrder: The queued order
        - risk_violation_note: None if passed all checks, string with violation reason if failed
    """
    db = SessionLocal()
    try:
        # Calculate quantity from pips if provided
        final_quantity = quantity
        if pips is not None:
            final_quantity = calculate_order_qty(symbol, pips=pips)
        
        if final_quantity is None or final_quantity <= 0:
            raise ValueError(f"Invalid quantity: {final_quantity}")
        
        # Run risk checks
        all_passed, violations = check_all_risks(symbol, final_quantity, db)
        risk_note = None
        if not all_passed:
            risk_note = "; ".join(violations)
            logger.warning(f"Order {symbol} failed risk checks: {risk_note}")
        
        # Create pending order
        pending_order = PendingOrder(
            symbol=symbol,
            side=side,
            quantity=final_quantity,
            price=price,
            order_type=order_type,
            pips=pips,  # Store original pips value
            source=source,
            source_ref=source_ref,
            strategy_name=strategy_name,
            confidence=confidence,
            note=note or risk_note,  # Add risk violation to notes if present
            status=PendingOrderStatus.PENDING,
        )
        
        db.add(pending_order)
        db.commit()
        db.refresh(pending_order)
        
        logger.info(
            f"Queued order: {side} {final_quantity} {symbol} @ {price} "
            f"from {source} (pips={pips})"
        )
        return pending_order, risk_note
    finally:
        db.close()


def get_pending_orders(
    status: Optional[str] = None,
    symbol: Optional[str] = None,
    source: Optional[str] = None,
) -> List[PendingOrder]:
    """
    Get pending orders with optional filters.
    
    Args:
        status: Filter by status ("pending", "approved", "rejected")
        symbol: Filter by symbol
        source: Filter by source ("excel", "strategy", "backtest")
    
    Returns:
        List of PendingOrder objects
    """
    db = SessionLocal()
    try:
        query = db.query(PendingOrder)
        
        if status:
            status_enum = PendingOrderStatus[status.upper()] if isinstance(status, str) else status
            query = query.filter(PendingOrder.status == status_enum)
        
        if symbol:
            query = query.filter(PendingOrder.symbol == symbol)
        
        if source:
            query = query.filter(PendingOrder.source == source)
        
        # Order by created_at DESC (newest first)
        query = query.order_by(PendingOrder.created_at.desc())
        
        return query.all()
    finally:
        db.close()


def approve_order(order_id: int, reviewed_by: str = "user", note: Optional[str] = None) -> PendingOrder:
    """
    Approve a pending order.
    
    Args:
        order_id: ID of pending order to approve
        reviewed_by: User approving the order
        note: Optional approval notes
    
    Returns:
        Updated PendingOrder
    """
    db = SessionLocal()
    try:
        order = db.query(PendingOrder).filter(PendingOrder.id == order_id).first()
        
        if not order:
            raise ValueError(f"Pending order {order_id} not found")

        if order.status != PendingOrderStatus.PENDING:
            raise ValueError(f"Order {order_id} is not pending (status: {order.status.value})")

        # Circuit breaker check before approving
        try:
            from services.trading.circuit_breaker import check as cb_check
            cb = cb_check(order.symbol, order.quantity, order.price)
            if not cb.allowed:
                raise ValueError(
                    f"Circuit breaker blocked order {order_id}: {'; '.join(cb.violations)}"
                )
        except ImportError:
            pass  # circuit breaker not available, proceed

        order.status = PendingOrderStatus.APPROVED
        order.reviewed_at = datetime.utcnow()
        order.reviewed_by = reviewed_by
        if note:
            order.note = note
        
        db.commit()
        db.refresh(order)
        
        # v0.9.0+: Live execution if enabled
        if settings.live_trading:
            live_order_id = _execute_live_order(order)
            if live_order_id:
                order.live_order_id = live_order_id
                db.commit()
        
        logger.info(f"Approved order {order_id}: {order.side} {order.quantity} {order.symbol}")
        
        # v0.10.0: Call KSS hook if this is a KSS order
        on_approved, _ = _get_kss_hooks()
        if on_approved:
            on_approved(order_id, order.source_ref)
        
        return order
    finally:
        db.close()


def reject_order(order_id: int, reviewed_by: str = "user", note: str = "") -> PendingOrder:
    """
    Reject a pending order.
    
    Args:
        order_id: ID of pending order to reject
        reviewed_by: User rejecting the order
        note: Reason for rejection
    
    Returns:
        Updated PendingOrder
    """
    db = SessionLocal()
    try:
        order = db.query(PendingOrder).filter(PendingOrder.id == order_id).first()
        
        if not order:
            raise ValueError(f"Pending order {order_id} not found")
        
        if order.status != PendingOrderStatus.PENDING:
            raise ValueError(f"Order {order_id} is not pending (status: {order.status.value})")
        
        order.status = PendingOrderStatus.REJECTED
        order.reviewed_at = datetime.utcnow()
        order.reviewed_by = reviewed_by
        order.note = note or "User rejected"
        
        db.commit()
        db.refresh(order)
        
        logger.info(f"Rejected order {order_id}: {order.side} {order.quantity} {order.symbol}")
        
        # v0.10.0: Call KSS hook if this is a KSS order
        _, on_rejected = _get_kss_hooks()
        if on_rejected:
            on_rejected(order_id, order.source_ref)
        
        return order
    finally:
        db.close()


def count_pending() -> int:
    """Get count of pending orders."""
    db = SessionLocal()
    try:
        return db.query(PendingOrder).filter(
            PendingOrder.status == PendingOrderStatus.PENDING
        ).count()
    finally:
        db.close()
