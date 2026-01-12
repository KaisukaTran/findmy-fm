"""
KSS Fill Event Hook.

Integrates KSS with the execution engine to handle fill events.
When an order is filled, this hook routes the event to the appropriate KSS session.
"""

from typing import Optional, Dict, Any
import logging

from services.sot.db import SessionLocal
from src.findmy.kss.manager import kss_manager
from src.findmy.kss.repository import KSSRepository
from src.findmy.kss.models import KSSSessionStatus, KSSWaveStatus
from services.sot.pending_orders_service import queue_order

logger = logging.getLogger(__name__)


def handle_fill_event(
    pending_order_id: int,
    filled_qty: float,
    filled_price: float,
    source_ref: Optional[str] = None,
    current_market_price: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """
    Handle a fill event from the execution engine.
    
    This function is called when an order is filled. It:
    1. Checks if the order belongs to a KSS session (via source_ref)
    2. Routes the fill to the appropriate session
    3. Handles any resulting actions (next wave, TP, stop)
    
    Args:
        pending_order_id: ID of the filled pending order
        filled_qty: Actual filled quantity
        filled_price: Actual fill price
        source_ref: Source reference (e.g., "pyramid:1:wave:0")
        current_market_price: Current market price for TP check
    
    Returns:
        Dict with action taken, or None if not a KSS order
    """
    if not source_ref:
        return None
    
    if not source_ref.startswith("pyramid:"):
        return None
    
    logger.info(f"KSS fill event: order={pending_order_id}, ref={source_ref}")
    
    # Route to KSS manager
    result = kss_manager.on_fill(
        source_ref=source_ref,
        filled_qty=filled_qty,
        filled_price=filled_price,
        current_market_price=current_market_price,
    )
    
    if not result:
        return None
    
    # Extract session ID from source_ref
    try:
        parts = source_ref.split(":")
        session_id = int(parts[1])
    except (ValueError, IndexError):
        logger.error(f"Failed to parse session ID from {source_ref}")
        return result
    
    # Persist state to DB
    db = SessionLocal()
    try:
        repo = KSSRepository(db)
        session = kss_manager.get_session(session_id)
        
        if session:
            # Update session state
            repo.update_session_state(
                session_id=session_id,
                current_wave=session.current_wave,
                avg_price=session.avg_price,
                total_filled_qty=session.total_filled_qty,
                total_cost=session.total_cost,
                last_fill_at=session.last_fill_time,
            )
            
            # Update wave as filled
            wave = repo.get_wave_by_order_id(pending_order_id)
            if wave:
                repo.update_wave_filled(wave.id, filled_qty, filled_price)
        
        # Handle action from KSS
        action = result.get("action")
        
        if action == "next_wave":
            # Queue next wave
            order_dict = result.get("order")
            if order_dict:
                pending_order, risk_note = queue_order(
                    symbol=order_dict["symbol"],
                    side=order_dict["side"],
                    quantity=order_dict["quantity"],
                    price=order_dict["price"],
                    source=order_dict["source"],
                    source_ref=order_dict["source_ref"],
                    strategy_name=order_dict.get("strategy_name"),
                    note=order_dict.get("note"),
                    order_type=order_dict.get("order_type", "LIMIT"),
                )
                
                # Create wave in DB
                wave_num = session.current_wave if session else 0
                wave_db = repo.create_wave(
                    session_id=session_id,
                    wave_num=wave_num,
                    quantity=order_dict["quantity"],
                    target_price=order_dict["price"],
                    pending_order_id=pending_order.id,
                )
                repo.update_wave_sent(wave_db.id, pending_order.id)
                
                result["pending_order_id"] = pending_order.id
                result["risk_note"] = risk_note
                
                logger.info(f"KSS queued next wave {wave_num} for session {session_id}")
        
        elif action == "tp_triggered":
            # Queue TP order
            order_dict = result.get("order")
            if order_dict:
                pending_order, risk_note = queue_order(
                    symbol=order_dict["symbol"],
                    side=order_dict["side"],
                    quantity=order_dict["quantity"],
                    price=order_dict.get("price", 0),
                    source=order_dict["source"],
                    source_ref=order_dict["source_ref"],
                    strategy_name=order_dict.get("strategy_name"),
                    note=order_dict.get("note"),
                    order_type=order_dict.get("order_type", "MARKET"),
                )
                
                result["pending_order_id"] = pending_order.id
                result["risk_note"] = risk_note
                
                # Update session status
                repo.update_session_status(session_id, KSSSessionStatus.TP_TRIGGERED)
                
                logger.info(f"KSS TP triggered for session {session_id}")
        
        elif action == "stopped":
            repo.update_session_status(session_id, KSSSessionStatus.STOPPED)
            logger.info(f"KSS session {session_id} stopped (timeout)")
        
        elif action == "completed":
            repo.update_session_status(session_id, KSSSessionStatus.COMPLETED)
            logger.info(f"KSS session {session_id} completed")
    
    except Exception as e:
        logger.error(f"Error persisting KSS fill event: {e}")
    finally:
        db.close()
    
    return result


def on_order_approved(pending_order_id: int, source_ref: Optional[str] = None) -> None:
    """
    Hook called when an order is approved.
    
    For KSS orders, this updates the wave status to 'sent'.
    """
    if not source_ref or not source_ref.startswith("pyramid:"):
        return
    
    db = SessionLocal()
    try:
        repo = KSSRepository(db)
        wave = repo.get_wave_by_order_id(pending_order_id)
        if wave and wave.status != KSSWaveStatus.SENT:
            repo.update_wave_sent(wave.id, pending_order_id)
    finally:
        db.close()


def on_order_rejected(pending_order_id: int, source_ref: Optional[str] = None) -> None:
    """
    Hook called when an order is rejected.
    
    For KSS orders, this cancels the wave and may stop the session.
    """
    if not source_ref or not source_ref.startswith("pyramid:"):
        return
    
    try:
        parts = source_ref.split(":")
        session_id = int(parts[1])
    except (ValueError, IndexError):
        return
    
    db = SessionLocal()
    try:
        repo = KSSRepository(db)
        wave = repo.get_wave_by_order_id(pending_order_id)
        if wave:
            repo.update_wave_cancelled(wave.id)
        
        # Stop the session if a wave is rejected
        session = kss_manager.get_session(session_id)
        if session:
            session.stop("wave_rejected")
            repo.update_session_status(session_id, KSSSessionStatus.STOPPED)
            logger.info(f"KSS session {session_id} stopped due to wave rejection")
    finally:
        db.close()
