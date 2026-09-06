"""
TS API Routes

REST endpoints for Trade Service operations.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import datetime

from services.ts.service import TSService
from services.ts.db import get_db

router = APIRouter(prefix="/api/v1/ts", tags=["TS (Trade Service)"])


# ==================
# Request/Response Models
# ==================

class OpenTradeRequest(BaseModel):
    """Request to open a new trade."""
    entry_order_id: int = Field(..., description="SOT Order ID for entry")
    symbol: str = Field(..., description="Trading symbol (e.g., AAPL)")
    side: str = Field(..., description="BUY or SELL")
    entry_qty: float = Field(..., description="Entry quantity")
    entry_price: float = Field(..., description="Entry price per unit")
    strategy_code: Optional[str] = Field(None, description="Strategy identifier")
    signal_source: Optional[str] = Field(None, description="Signal source (e.g., backtest)")
    requested_by: Optional[str] = Field(None, description="User/system that requested trade")


class CloseTradeRequest(BaseModel):
    """Request to close a trade."""
    exit_order_id: int = Field(..., description="SOT Order ID for exit")
    exit_qty: float = Field(..., description="Exit quantity")
    exit_price: float = Field(..., description="Exit price per unit")


class TradeResponse(BaseModel):
    """Trade data response."""
    id: int
    symbol: str
    side: str
    status: str
    entry_qty: float
    entry_price: float
    entry_time: str
    exit_qty: Optional[float]
    exit_price: Optional[float]
    exit_time: Optional[str]
    current_qty: float
    strategy_code: Optional[str]
    signal_source: Optional[str]
    pnl: Optional[dict]

    class Config:
        from_attributes = True


class TradeListResponse(BaseModel):
    """Trade list item response."""
    id: int
    symbol: str
    side: str
    status: str
    entry_price: float
    exit_price: Optional[float]
    current_qty: float
    strategy_code: Optional[str]
    net_pnl: float
    return_pct: float


class TradePnLResponse(BaseModel):
    """Trade P&L response."""
    trade_id: int
    gross_pnl: float
    total_fees: float
    net_pnl: float
    return_pct: float
    cost_basis: float
    realized_pnl: float
    unrealized_pnl: float
    duration_minutes: Optional[int]
    calculated_at: str


class PositionResponse(BaseModel):
    """Position response."""
    symbol: str
    quantity: float
    avg_entry_price: float
    total_traded: float
    total_cost: float
    strategy_code: Optional[str]
    last_trade_time: Optional[str]


class TotalPnLResponse(BaseModel):
    """Total P&L response."""
    total_realized_pnl: float
    calculated_at: str


# ==================
# Trade Endpoints
# ==================

@router.post("/trades/open", response_model=dict)
def open_trade(
    request: OpenTradeRequest,
    db: Session = Depends(get_db),
):
    """
    Open a new trade (entry order placed).
    
    Creates a new trade record in TS when entry order fills in SOT.
    """
    try:
        ts_service = TSService(db)
        trade_id = ts_service.open_trade(
            entry_order_id=request.entry_order_id,
            symbol=request.symbol,
            side=request.side,
            entry_qty=request.entry_qty,
            entry_price=request.entry_price,
            strategy_code=request.strategy_code,
            signal_source=request.signal_source,
            requested_by=request.requested_by,
        )
        
        return {
            "status": "success",
            "trade_id": trade_id,
            "message": f"Trade {trade_id} opened",
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/trades/{trade_id}/close", response_model=dict)
def close_trade(
    trade_id: int,
    request: CloseTradeRequest,
    db: Session = Depends(get_db),
):
    """
    Close or partially close a trade (exit order placed).
    
    Records exit and calculates P&L.
    """
    try:
        ts_service = TSService(db)
        result = ts_service.close_trade(
            trade_id,
            exit_order_id=request.exit_order_id,
            exit_qty=request.exit_qty,
            exit_price=request.exit_price,
        )
        
        return {
            "status": "success",
            "data": result,
        }
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/trades/{trade_id}", response_model=TradeResponse)
def get_trade(
    trade_id: int,
    db: Session = Depends(get_db),
):
    """Get trade details with P&L."""
    try:
        ts_service = TSService(db)
        trade_data = ts_service.get_trade(trade_id)
        return TradeResponse(**trade_data)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/trades", response_model=List[TradeListResponse])
def list_trades(
    symbol: Optional[str] = Query(None, description="Filter by symbol"),
    status: Optional[str] = Query(None, description="Filter by status (OPEN, CLOSED, PARTIAL)"),
    strategy_code: Optional[str] = Query(None, description="Filter by strategy code"),
    limit: int = Query(100, ge=1, le=1000, description="Result limit"),
    offset: int = Query(0, ge=0, description="Result offset"),
    db: Session = Depends(get_db),
):
    """
    List trades with optional filters.
    
    **Parameters:**
    - `symbol`: Trading symbol (e.g., AAPL)
    - `status`: Trade status (OPEN, CLOSED, PARTIAL)
    - `strategy_code`: Strategy identifier
    - `limit`: Number of results (default 100, max 1000)
    - `offset`: Pagination offset (default 0)
    """
    try:
        ts_service = TSService(db)
        trades = ts_service.list_trades(
            symbol=symbol,
            status=status,
            strategy_code=strategy_code,
            limit=limit,
            offset=offset,
        )
        return [TradeListResponse(**trade) for trade in trades]
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


# ==================
# P&L Endpoints
# ==================

@router.get("/trades/{trade_id}/pnl", response_model=TradePnLResponse)
def get_trade_pnl(
    trade_id: int,
    db: Session = Depends(get_db),
):
    """Get P&L snapshot for a trade."""
    try:
        ts_service = TSService(db)
        pnl_data = ts_service.get_trade_pnl(trade_id)
        return TradePnLResponse(**pnl_data)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/pnl/total", response_model=TotalPnLResponse)
def get_total_pnl(db: Session = Depends(get_db)):
    """Get total realized P&L across all closed trades."""
    try:
        ts_service = TSService(db)
        pnl_data = ts_service.get_total_pnl()
        return TotalPnLResponse(**pnl_data)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


# ==================
# Position Endpoints
# ==================

@router.get("/positions/{symbol}", response_model=PositionResponse)
def get_position(
    symbol: str,
    strategy_code: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    """
    Get current position for a symbol.
    
    **Parameters:**
    - `symbol`: Trading symbol (e.g., AAPL)
    - `strategy_code`: Optional strategy filter
    """
    try:
        ts_service = TSService(db)
        pos_data = ts_service.get_position(symbol, strategy_code)
        return PositionResponse(**pos_data)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/positions", response_model=List[PositionResponse])
def list_positions(db: Session = Depends(get_db)):
    """
    List all open positions.
    
    Returns only positions with non-zero quantity.
    """
    try:
        ts_service = TSService(db)
        positions = ts_service.list_positions()
        return [PositionResponse(**pos) for pos in positions]
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


# ==================
# Health Check
# ==================

@router.get("/health")
def health_check():
    """TS service health check."""
    return {
        "status": "ok",
        "service": "TS (Trade Service)",
        "version": "0.1.0",
        "timestamp": datetime.utcnow().isoformat(),
    }
