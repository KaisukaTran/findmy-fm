from fastapi import FastAPI, UploadFile, File, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pathlib import Path
import shutil
import uuid
import os
import asyncio
from typing import Optional
from datetime import datetime, timedelta

from findmy.execution.paper_execution import run_paper_execution

# ✅ 1. DECLARE APP FIRST
app = FastAPI(
    title="FINDMY FM – Paper Trading API",
    version="1.0",
)

# ✅ 2. CONFIGURE TEMPLATES AND STATIC FILES
templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")

# Environment configuration
UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", "data/uploads"))
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB
ALLOWED_MIME_TYPES = {
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# ✅ DASHBOARD ROUTE (root URL)
@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    """
    Render the interactive HTML dashboard for Trade Service & SOT monitoring.
    
    The dashboard displays:
    - System status and health checks
    - Current positions and cost basis
    - Trade history with P&L metrics
    - Summary statistics (realized/unrealized PnL, total invested)
    """
    return templates.TemplateResponse("dashboard.html", {"request": request})


# ✅ 2. THEN USE @app.post
@app.post("/paper-execution")
async def paper_execution(file: UploadFile = File(...)):
    """
    Execute paper trading orders from an Excel file.
    
    Args:
        file: Excel file containing purchase orders (MIME type must be Excel).
    
    Returns:
        JSON response with execution results including orders, trades, and positions.
        
    Raises:
        HTTPException: 400 if file is not Excel, too large, or missing headers.
        HTTPException: 500 if processing fails.
    """
    # Validate MIME type
    if file.content_type not in ALLOWED_MIME_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid file type: {file.content_type}. Only Excel files are supported.",
        )

    # Validate file extension
    if not file.filename or not file.filename.lower().endswith((".xlsx", ".xls")):
        raise HTTPException(
            status_code=400,
            detail="Only Excel files (.xlsx, .xls) are supported",
        )

    # Validate file size
    if file.size and file.size > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=400,
            detail=f"File too large. Maximum size is {MAX_FILE_SIZE / 1024 / 1024:.0f}MB",
        )

    # Generate safe filename with UUID to prevent collisions
    safe_filename = f"{uuid.uuid4()}_{file.filename}"
    saved_path = UPLOAD_DIR / safe_filename

    try:
        # Write file to disk
        with saved_path.open("wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

        # Process the uploaded file
        result = run_paper_execution(str(saved_path))

        return {
            "status": "success",
            "result": result,
        }

    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid Excel file: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Processing error: {str(e)}")
    finally:
        # Clean up uploaded file after processing
        try:
            if saved_path.exists():
                saved_path.unlink()
        except Exception as e:
            # Log cleanup error but don't fail the response
            print(f"Warning: Failed to delete temporary file {saved_path}: {e}")


# ========================
# DASHBOARD ENDPOINTS
# ========================

from services.ts.db import SessionLocal
from services.ts.models import Trade, TradePosition, TradePnL
from services.sot.models import Order
from findmy.services.market_data import get_current_prices, get_unrealized_pnl
from findmy.services.backtesting import run_backtest, BacktestRequest
from sqlalchemy import func
from datetime import datetime
from pydantic import BaseModel
from typing import List


class PositionResponse(BaseModel):
    symbol: str
    quantity: float
    avg_price: float
    total_cost: float
    current_price: Optional[float] = None
    market_value: Optional[float] = None
    unrealized_pnl: Optional[float] = None


class TradeResponse(BaseModel):
    id: int
    symbol: str
    side: str
    entry_qty: float
    entry_price: float
    entry_time: datetime
    exit_qty: Optional[float]
    exit_price: Optional[float]
    exit_time: Optional[datetime]
    status: str
    realized_pnl: Optional[float] = None


class SummaryResponse(BaseModel):
    total_trades: int
    realized_pnl: float
    unrealized_pnl: float
    total_invested: float
    total_market_value: float = 0.0
    total_equity: float = 0.0
    last_trade_time: Optional[datetime] = None
    status: str


@app.get("/api/positions", response_model=List[PositionResponse])
async def get_positions():
    """Get current positions from Trade Service with live market prices and unrealized PnL."""
    db = SessionLocal()
    try:
        try:
            positions = db.query(TradePosition).all()
            if not positions:
                return []
            
            # Fetch current prices for all symbols
            symbols = [p.symbol for p in positions]
            prices = get_current_prices(symbols)
            
            result = []
            for p in positions:
                current_price = prices.get(p.symbol)
                if current_price is not None:
                    market_value = p.quantity * current_price
                    unrealized_pnl = market_value - p.total_cost
                else:
                    market_value = None
                    unrealized_pnl = None
                
                result.append(
                    PositionResponse(
                        symbol=p.symbol,
                        quantity=p.quantity,
                        avg_price=p.avg_entry_price,
                        total_cost=p.total_cost,
                        current_price=current_price,
                        market_value=market_value,
                        unrealized_pnl=unrealized_pnl,
                    )
                )
            return result
        except Exception:
            # Table may not exist yet
            return []
    finally:
        db.close()


@app.get("/api/trades", response_model=List[TradeResponse])
async def get_trades():
    """Get trade history from Trade Service, ordered by timestamp DESC."""
    db = SessionLocal()
    try:
        try:
            trades = db.query(Trade).order_by(Trade.entry_time.desc()).all()
            result = []
            for trade in trades:
                pnl = trade.pnl
                realized_pnl = pnl.realized_pnl if pnl else None
                result.append(
                    TradeResponse(
                        id=trade.id,
                        symbol=trade.symbol,
                        side=trade.side,
                        entry_qty=trade.entry_qty,
                        entry_price=trade.entry_price,
                        entry_time=trade.entry_time,
                        exit_qty=trade.exit_qty,
                        exit_price=trade.exit_price,
                        exit_time=trade.exit_time,
                        status=trade.status,
                        realized_pnl=realized_pnl,
                    )
                )
            return result
        except Exception:
            # Table may not exist yet
            return []
    finally:
        db.close()


@app.get("/api/summary", response_model=SummaryResponse)
async def get_summary():
    """Get PnL summary and trading statistics with market values."""
    db = SessionLocal()
    try:
        try:
            # Total trades
            total_trades = db.query(func.count(Trade.id)).scalar() or 0

            # PnL calculations
            try:
                pnl_records = db.query(TradePnL).all()
                realized_pnl = sum(p.realized_pnl for p in pnl_records) if pnl_records else 0.0
                unrealized_pnl = sum(p.unrealized_pnl for p in pnl_records) if pnl_records else 0.0
            except Exception:
                realized_pnl = 0.0
                unrealized_pnl = 0.0

            # Total invested and market value
            total_invested = 0.0
            total_market_value = 0.0
            try:
                positions = db.query(TradePosition).all()
                total_invested = sum(p.total_cost for p in positions) if positions else 0.0
                
                # Fetch current prices for market value calculation
                if positions:
                    symbols = [p.symbol for p in positions]
                    prices = get_current_prices(symbols)
                    for p in positions:
                        current_price = prices.get(p.symbol)
                        if current_price is not None:
                            total_market_value += p.quantity * current_price
            except Exception:
                total_invested = 0.0
                total_market_value = 0.0

            # Last trade time
            try:
                last_trade = db.query(Trade).order_by(Trade.entry_time.desc()).first()
                last_trade_time = last_trade.entry_time if last_trade else None
            except Exception:
                last_trade_time = None

            # Calculate total equity
            total_equity = total_invested + unrealized_pnl

            return SummaryResponse(
                total_trades=int(total_trades),
                realized_pnl=realized_pnl,
                unrealized_pnl=unrealized_pnl,
                total_invested=total_invested,
                total_market_value=total_market_value,
                total_equity=total_equity,
                last_trade_time=last_trade_time,
                status="✓ Active",
            )
        except Exception:
            # Return empty summary if database is not initialized
            return SummaryResponse(
                total_trades=0,
                realized_pnl=0.0,
                unrealized_pnl=0.0,
                total_invested=0.0,
                total_market_value=0.0,
                total_equity=0.0,
                last_trade_time=None,
                status="✓ Active",
            )
    finally:
        db.close()


# ========================
# BACKTESTING ENDPOINT
# ========================

class BacktestRequestBody(BaseModel):
    """Request body for backtesting."""
    symbols: List[str] = ["BTC", "ETH"]
    start_date: str  # ISO format YYYY-MM-DD
    end_date: str  # ISO format YYYY-MM-DD
    initial_capital: float = 10000.0
    timeframe: str = "1h"


@app.post("/api/backtest")
async def run_backtest_endpoint(request_body: BacktestRequestBody):
    """
    Run a backtest simulation over historical data.
    
    Args:
        request_body: Backtest parameters including symbols, date range, capital, timeframe
    
    Returns:
        BacktestResult with equity curve, trades, and performance metrics
    """
    try:
        # Parse dates
        start_date = datetime.fromisoformat(request_body.start_date)
        end_date = datetime.fromisoformat(request_body.end_date)
        
        # Validate date range
        if start_date >= end_date:
            raise HTTPException(status_code=400, detail="start_date must be before end_date")
        
        if (end_date - start_date).days > 365:
            raise HTTPException(status_code=400, detail="Backtest period cannot exceed 365 days")
        
        # Create request and run backtest
        backtest_request = BacktestRequest(
            symbols=request_body.symbols,
            start_date=start_date,
            end_date=end_date,
            initial_capital=request_body.initial_capital,
            timeframe=request_body.timeframe,
        )
        
        result = run_backtest(backtest_request)
        return result.to_dict()
        
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid date format: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Backtest error: {str(e)}")


# ========================
# WEBSOCKET LIVE UPDATES
# ========================

class ConnectionManager:
    """WebSocket connection manager for broadcasting updates."""
    
    def __init__(self):
        self.active_connections: List[WebSocket] = []
    
    async def connect(self, websocket: WebSocket):
        """Accept and add a new WebSocket connection."""
        await websocket.accept()
        self.active_connections.append(websocket)
    
    def disconnect(self, websocket: WebSocket):
        """Remove a disconnected WebSocket."""
        self.active_connections.remove(websocket)
    
    async def broadcast(self, message: dict):
        """Broadcast message to all connected clients."""
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except Exception:
                # Connection may have closed, will be cleaned up
                pass


manager = ConnectionManager()


@app.websocket("/ws/dashboard")
async def websocket_dashboard(websocket: WebSocket):
    """
    WebSocket endpoint for realtime dashboard updates.
    
    Sends updates every 30 seconds with current positions, summary, and market data.
    """
    await manager.connect(websocket)
    try:
        while True:
            # Wait 30 seconds before sending next update
            await asyncio.sleep(30)
            
            # Fetch fresh data
            db = SessionLocal()
            try:
                # Get positions with current prices
                positions = db.query(TradePosition).all()
                positions_data = []
                symbols = [p.symbol for p in positions]
                prices = get_current_prices(symbols) if symbols else {}
                
                for p in positions:
                    current_price = prices.get(p.symbol)
                    market_value = p.quantity * current_price if current_price else None
                    unrealized_pnl = (
                        market_value - p.total_cost if market_value else None
                    )
                    positions_data.append({
                        "symbol": p.symbol,
                        "quantity": float(p.quantity),
                        "avg_price": float(p.avg_entry_price),
                        "total_cost": float(p.total_cost),
                        "current_price": current_price,
                        "market_value": market_value,
                        "unrealized_pnl": unrealized_pnl,
                    })
                
                # Get summary
                total_trades = db.query(func.count(Trade.id)).scalar() or 0
                
                try:
                    pnl_records = db.query(TradePnL).all()
                    realized_pnl = sum(p.realized_pnl for p in pnl_records) if pnl_records else 0.0
                    unrealized_pnl = sum(p.unrealized_pnl for p in pnl_records) if pnl_records else 0.0
                except Exception:
                    realized_pnl = 0.0
                    unrealized_pnl = 0.0
                
                total_invested = sum(p.total_cost for p in positions) if positions else 0.0
                total_market_value = sum(
                    (prices.get(p.symbol, 0) * p.quantity for p in positions)
                    if positions else []
                )
                total_equity = total_invested + unrealized_pnl
                
                # Create update message
                update = {
                    "type": "dashboard_update",
                    "timestamp": datetime.utcnow().isoformat(),
                    "positions": positions_data,
                    "summary": {
                        "total_trades": int(total_trades),
                        "realized_pnl": float(realized_pnl),
                        "unrealized_pnl": float(unrealized_pnl),
                        "total_invested": float(total_invested),
                        "total_market_value": float(total_market_value),
                        "total_equity": float(total_equity),
                    }
                }
                
                await manager.broadcast(update)
                
            except Exception as e:
                # Log but continue - connection may be updating
                pass
            finally:
                db.close()
    
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception:
        manager.disconnect(websocket)

