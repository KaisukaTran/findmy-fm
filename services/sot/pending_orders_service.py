"""Pending orders service for manual approval workflow."""

from typing import List, Dict, Any, Optional
from datetime import datetime
import logging

from services.sot.db import SessionLocal
from services.sot.pending_orders import PendingOrder, PendingOrderStatus

logger = logging.getLogger(__name__)


def queue_order(
    symbol: str,
    side: str,
    quantity: float,
    price: float,
    source: str,
    order_type: str = "MARKET",
    source_ref: Optional[str] = None,
    strategy_name: Optional[str] = None,
    confidence: Optional[float] = None,
    note: Optional[str] = None,
) -> PendingOrder:
    """
    Queue an order for manual approval.
    
    All orders must go through this queue before execution.
    
    Args:
        symbol: Asset symbol (e.g., "BTC", "ETH")
        side: Order side ("BUY" or "SELL")
        quantity: Order quantity
        price: Order price
        source: Source of order ("excel", "strategy", "backtest")
        order_type: Order type ("MARKET", "LIMIT", "STOP_LOSS")
        source_ref: Optional reference to source
        strategy_name: Optional strategy name if from strategy
        confidence: Optional signal confidence if from strategy
        note: Optional notes
    
    Returns:
        PendingOrder object
    """
    db = SessionLocal()
    try:
        pending_order = PendingOrder(
            symbol=symbol,
            side=side,
            quantity=quantity,
            price=price,
            order_type=order_type,
            source=source,
            source_ref=source_ref,
            strategy_name=strategy_name,
            confidence=confidence,
            note=note,
            status=PendingOrderStatus.PENDING,
        )
        
        db.add(pending_order)
        db.commit()
        db.refresh(pending_order)
        
        logger.info(f"Queued order: {side} {quantity} {symbol} @ {price} from {source}")
        return pending_order
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
        
        order.status = PendingOrderStatus.APPROVED
        order.reviewed_at = datetime.utcnow()
        order.reviewed_by = reviewed_by
        if note:
            order.note = note
        
        db.commit()
        db.refresh(order)
        
        logger.info(f"Approved order {order_id}: {order.side} {order.quantity} {order.symbol}")
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
