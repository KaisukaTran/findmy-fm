"""
KSS (Kai Strategy Service) API Routes.

Provides REST endpoints for:
- Creating pyramid sessions
- Starting/stopping sessions
- Adjusting parameters
- Listing and getting session details
"""

from fastapi import APIRouter, HTTPException, Depends, Query
from typing import Optional, List
from pydantic import BaseModel, Field
from datetime import datetime
import logging

from services.sot.db import SessionLocal
from src.findmy.kss.pyramid import PyramidSession, PyramidSessionStatus
from src.findmy.kss.manager import kss_manager
from src.findmy.kss.repository import KSSRepository
from src.findmy.kss.models import KSSSessionStatus
from services.sot.pending_orders_service import queue_order

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/kss", tags=["KSS"])


# ============================================================
# Request/Response Schemas
# ============================================================

class CreatePyramidRequest(BaseModel):
    """Request schema for creating a pyramid session."""
    symbol: str = Field(..., description="Trading pair symbol (e.g., 'BTC')")
    entry_price: float = Field(..., gt=0, description="Starting price for wave 0")
    distance_pct: float = Field(..., gt=0, lt=100, description="Price decrease % per wave")
    max_waves: int = Field(..., ge=1, le=100, description="Maximum number of waves")
    isolated_fund: float = Field(..., gt=0, description="Fund allocated for this session")
    tp_pct: float = Field(..., gt=0, description="Take profit % above avg price")
    timeout_x_min: float = Field(default=30.0, gt=0, description="Stop if no fill for X minutes")
    gap_y_min: float = Field(default=5.0, ge=0, description="Min time between fills for timeout")
    note: Optional[str] = Field(default=None, description="Optional note for the session")


class AdjustSessionRequest(BaseModel):
    """Request schema for adjusting session parameters."""
    max_waves: Optional[int] = Field(default=None, ge=1, le=100)
    isolated_fund: Optional[float] = Field(default=None, gt=0)
    tp_pct: Optional[float] = Field(default=None, gt=0)
    distance_pct: Optional[float] = Field(default=None, gt=0, lt=100)
    timeout_x_min: Optional[float] = Field(default=None, gt=0)
    gap_y_min: Optional[float] = Field(default=None, ge=0)


class SessionResponse(BaseModel):
    """Response schema for session details."""
    id: int
    symbol: str
    status: str
    entry_price: float
    distance_pct: float
    max_waves: int
    isolated_fund: float
    tp_pct: float
    timeout_x_min: float
    gap_y_min: float
    current_wave: int
    filled_waves_count: int
    pending_waves_count: int
    total_filled_qty: float
    avg_price: float
    total_cost: float
    used_fund: float
    remaining_fund: float
    current_price: float
    estimated_tp_price: float
    unrealized_pnl: float
    unrealized_pnl_pct: float
    start_time: Optional[str]
    last_fill_time: Optional[str]
    created_at: str
    waves: List[dict]
    
    class Config:
        from_attributes = True


class SessionListResponse(BaseModel):
    """Response schema for session list."""
    sessions: List[SessionResponse]
    total: int
    active_count: int
    total_isolated_fund: float


class SummaryResponse(BaseModel):
    """Response schema for KSS summary."""
    total_sessions: int
    active_sessions: int
    total_isolated_fund: float
    total_used_fund: float
    total_unrealized_pnl: float


# ============================================================
# API Endpoints
# ============================================================

@router.post("/sessions", response_model=SessionResponse)
async def create_session(request: CreatePyramidRequest):
    """
    Create a new pyramid DCA session.
    
    The session is created in PENDING state. Call /sessions/{id}/start to begin.
    """
    try:
        # Create in-memory session
        session = kss_manager.create_pyramid_session(
            symbol=request.symbol,
            entry_price=request.entry_price,
            distance_pct=request.distance_pct,
            max_waves=request.max_waves,
            isolated_fund=request.isolated_fund,
            tp_pct=request.tp_pct,
            timeout_x_min=request.timeout_x_min,
            gap_y_min=request.gap_y_min,
        )
        
        # Persist to database
        db = SessionLocal()
        try:
            repo = KSSRepository(db)
            db_session = repo.create_session(
                symbol=request.symbol,
                entry_price=request.entry_price,
                distance_pct=request.distance_pct,
                max_waves=request.max_waves,
                isolated_fund=request.isolated_fund,
                tp_pct=request.tp_pct,
                timeout_x_min=request.timeout_x_min,
                gap_y_min=request.gap_y_min,
                note=request.note,
            )
            # Sync ID
            session.id = db_session.id
            kss_manager._sessions[db_session.id] = session
            if session.id != db_session.id:
                del kss_manager._sessions[session.id]
        finally:
            db.close()
        
        return session.get_status()
        
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to create session: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to create session: {e}")


@router.post("/sessions/{session_id}/start")
async def start_session(session_id: int):
    """
    Start a pyramid session by sending wave 0 to pending queue.
    
    Returns the queued order details.
    """
    session = kss_manager.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail=f"Session {session_id} not found")
    
    if session.status != PyramidSessionStatus.PENDING:
        raise HTTPException(
            status_code=400, 
            detail=f"Session {session_id} already started (status={session.status.value})"
        )
    
    # Start session (generates wave 0)
    order_dict = session.start()
    if not order_dict:
        raise HTTPException(status_code=400, detail="Failed to start session")
    
    # Queue wave 0 to pending orders
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
    
    # Update wave with pending order ID
    if session.waves:
        session.waves[0].pending_order_id = pending_order.id
    
    # Update DB
    db = SessionLocal()
    try:
        repo = KSSRepository(db)
        repo.update_session_status(session_id, KSSSessionStatus.ACTIVE)
        if session.waves:
            wave_db = repo.create_wave(
                session_id=session_id,
                wave_num=0,
                quantity=order_dict["quantity"],
                target_price=order_dict["price"],
                pending_order_id=pending_order.id,
            )
            repo.update_wave_sent(wave_db.id, pending_order.id)
    finally:
        db.close()
    
    return {
        "message": f"Session {session_id} started",
        "wave_0_queued": True,
        "pending_order_id": pending_order.id,
        "risk_note": risk_note,
        "order": {
            "symbol": order_dict["symbol"],
            "side": order_dict["side"],
            "quantity": order_dict["quantity"],
            "price": order_dict["price"],
        },
    }


@router.post("/sessions/{session_id}/stop")
async def stop_session(session_id: int, reason: str = "manual"):
    """Stop an active pyramid session."""
    session = kss_manager.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail=f"Session {session_id} not found")
    
    if session.status != PyramidSessionStatus.ACTIVE:
        raise HTTPException(
            status_code=400,
            detail=f"Session {session_id} not active (status={session.status.value})"
        )
    
    session.stop(reason)
    
    # Update DB
    db = SessionLocal()
    try:
        repo = KSSRepository(db)
        repo.update_session_status(session_id, KSSSessionStatus.STOPPED)
    finally:
        db.close()
    
    return {
        "message": f"Session {session_id} stopped",
        "reason": reason,
        "status": session.get_status(),
    }


@router.patch("/sessions/{session_id}")
async def adjust_session(session_id: int, request: AdjustSessionRequest):
    """
    Adjust session parameters while running.
    
    Only certain parameters can be adjusted:
    - max_waves: Can increase/decrease (but not below current wave)
    - isolated_fund: Can increase (adding more fund)
    - tp_pct, distance_pct, timeout_x_min, gap_y_min: Can adjust freely
    """
    session = kss_manager.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail=f"Session {session_id} not found")
    
    changes = session.adjust_params(
        max_waves=request.max_waves,
        isolated_fund=request.isolated_fund,
        tp_pct=request.tp_pct,
        distance_pct=request.distance_pct,
        timeout_x_min=request.timeout_x_min,
        gap_y_min=request.gap_y_min,
    )
    
    if not changes:
        raise HTTPException(status_code=400, detail="No valid changes applied")
    
    # Update DB
    db = SessionLocal()
    try:
        repo = KSSRepository(db)
        repo.update_session_params(session_id, **changes)
    finally:
        db.close()
    
    return {
        "message": f"Session {session_id} adjusted",
        "changes": changes,
        "status": session.get_status(),
    }


@router.get("/sessions/{session_id}", response_model=SessionResponse)
async def get_session(session_id: int):
    """Get detailed status of a session."""
    session = kss_manager.get_session(session_id)
    if not session:
        # Try loading from DB
        db = SessionLocal()
        try:
            repo = KSSRepository(db)
            db_session = repo.get_session(session_id)
            if not db_session:
                raise HTTPException(status_code=404, detail=f"Session {session_id} not found")
            
            # Load into manager
            session = repo.db_to_pyramid_session(db_session)
            kss_manager._sessions[session_id] = session
        finally:
            db.close()
    
    return session.get_status()


@router.get("/sessions", response_model=SessionListResponse)
async def list_sessions(
    status: Optional[str] = Query(None, description="Filter by status"),
    symbol: Optional[str] = Query(None, description="Filter by symbol"),
    limit: int = Query(50, ge=1, le=200),
):
    """List all sessions with optional filters."""
    # Convert status string to enum if provided
    status_enum = None
    if status:
        try:
            status_enum = PyramidSessionStatus[status.upper()]
        except KeyError:
            raise HTTPException(status_code=400, detail=f"Invalid status: {status}")
    
    sessions = kss_manager.list_sessions(status=status_enum, symbol=symbol)[:limit]
    
    # Also load from DB for any not in memory
    db = SessionLocal()
    try:
        repo = KSSRepository(db)
        db_sessions = repo.get_sessions(
            status=KSSSessionStatus[status.upper()] if status else None,
            symbol=symbol,
            limit=limit,
        )
        
        # Merge DB sessions not in memory
        memory_ids = {s["id"] for s in sessions}
        for db_session in db_sessions:
            if db_session.id not in memory_ids:
                session = repo.db_to_pyramid_session(db_session)
                kss_manager._sessions[db_session.id] = session
                sessions.append(session.get_status())
    finally:
        db.close()
    
    # Calculate summary
    active_sessions = [s for s in sessions if s["status"] == "active"]
    
    return {
        "sessions": sessions[:limit],
        "total": len(sessions),
        "active_count": len(active_sessions),
        "total_isolated_fund": sum(s["isolated_fund"] for s in active_sessions),
    }


@router.get("/summary", response_model=SummaryResponse)
async def get_summary():
    """Get KSS summary statistics for dashboard."""
    summary = kss_manager.get_summary()
    return summary


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: int):
    """
    Delete a session (only if not active).
    
    Removes from both memory and database.
    """
    session = kss_manager.get_session(session_id)
    if session and session.status == PyramidSessionStatus.ACTIVE:
        raise HTTPException(
            status_code=400,
            detail="Cannot delete active session. Stop it first."
        )
    
    # Remove from memory
    if session_id in kss_manager._sessions:
        del kss_manager._sessions[session_id]
    
    # Remove from DB
    db = SessionLocal()
    try:
        repo = KSSRepository(db)
        deleted = repo.delete_session(session_id)
        if not deleted and not session:
            raise HTTPException(status_code=404, detail=f"Session {session_id} not found")
    finally:
        db.close()
    
    return {"message": f"Session {session_id} deleted"}


@router.post("/sessions/{session_id}/check-tp")
async def check_tp(session_id: int, current_price: Optional[float] = None):
    """
    Manually check if TP condition is met.
    
    Useful for testing or forcing TP check outside of fill events.
    """
    session = kss_manager.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail=f"Session {session_id} not found")
    
    if session.total_filled_qty <= 0:
        return {"tp_triggered": False, "message": "No filled quantity yet"}
    
    # Get current price if not provided
    if current_price is None:
        from src.findmy.services.market_data import get_current_prices
        prices = get_current_prices([session.symbol])
        current_price = prices.get(session.symbol, 0)
    
    if current_price <= 0:
        return {"tp_triggered": False, "message": "Could not get current price"}
    
    tp_price = session.estimated_tp_price
    tp_triggered = current_price >= tp_price
    
    result = {
        "tp_triggered": tp_triggered,
        "current_price": current_price,
        "avg_price": session.avg_price,
        "tp_price": tp_price,
        "tp_pct": session.tp_pct,
    }
    
    if tp_triggered:
        # Execute TP
        tp_result = session.check_tp(current_price)
        if tp_result and tp_result.get("order"):
            order_dict = tp_result["order"]
            pending_order, _ = queue_order(
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
            result["tp_order_queued"] = True
            result["pending_order_id"] = pending_order.id
            
            # Update DB
            db = SessionLocal()
            try:
                repo = KSSRepository(db)
                repo.update_session_status(session_id, KSSSessionStatus.TP_TRIGGERED)
            finally:
                db.close()
    
    return result
