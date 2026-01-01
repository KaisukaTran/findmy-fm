from fastapi import FastAPI, UploadFile, File, HTTPException, Request, WebSocket, WebSocketDisconnect, Depends
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from pathlib import Path
import shutil
import uuid
import os
import asyncio
from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta

from findmy.execution.paper_execution import run_paper_execution
from findmy.strategies import MovingAverageStrategy
from findmy.services.strategy_executor import StrategyExecutor
from services.sot.pending_orders_service import (
    queue_order, get_pending_orders, approve_order, reject_order, count_pending
)

# v0.7.0: Import security middleware
from findmy.api.security import (
    limiter,
    CORS_CONFIG,
    SECURITY_HEADERS,
    get_current_user,
    RateLimitConfig,
)
from findmy.api.auth_routes import router as auth_router

# v0.7.0: Import Prometheus metrics
from prometheus_fastapi_instrumentator import Instrumentator
from findmy.api.metrics import (
    trades_total, trades_pnl_total, positions_active, positions_total_value,
    cache_hits_total, cache_misses_total, cache_size_bytes, cache_entries,
    orders_pending_total, orders_approved_total, orders_rejected_total,
    order_processing_time_seconds, db_queries_total, db_query_duration_seconds,
    app_info, MetricsSnapshot, track_api_request, track_db_query
)
import logging
import time

# ✅ 1. DECLARE APP FIRST
app = FastAPI(
    title="FINDMY FM – Paper Trading API",
    version="1.0",
)

# v0.7.0: Add Prometheus metrics instrumentator
Instrumentator().instrument(app).expose(app)

# v0.7.0: Add security middleware
app.state.limiter = limiter

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_CONFIG["allow_origins"],
    allow_credentials=CORS_CONFIG["allow_credentials"],
    allow_methods=CORS_CONFIG["allow_methods"],
    allow_headers=CORS_CONFIG["allow_headers"],
)

# Add trusted host middleware for security
app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=["localhost", "127.0.0.1", "testserver", "yourdomain.com"]  # testserver for tests
)

# v0.7.0: Add security headers to all responses
@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    """Add security headers to all responses."""
    response = await call_next(request)
    for header, value in SECURITY_HEADERS.items():
        response.headers[header] = value
    return response

# v0.7.0: Import caching
from services.cache.manager import cache_manager, CacheConfig

# Include authentication routes
app.include_router(auth_router)

# v0.7.0: Initialize caching on startup
@app.on_event("startup")
async def startup_event():
    """Initialize caching and metrics on application startup."""
    await cache_manager.init()
    logger = __import__("logging").getLogger(__name__)
    logger.info("Cache manager initialized")
    
    # v0.7.0: Initialize application info metric
    app_info.info({"version": "0.7.0"})
    logger.info("Application metrics initialized - v0.7.0")

@app.on_event("shutdown")
async def shutdown_event():
    """Cleanup on shutdown."""
    await cache_manager.clear()
    logger = __import__("logging").getLogger(__name__)
    logger.info("Cache manager shutdown")

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

# ✅ HEALTH CHECK
@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "ok", "service": "FINDMY FM API"}


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
@limiter.limit(RateLimitConfig.ENDPOINTS["trading"])
async def paper_execution(
    request: Request,
    current_user: dict = Depends(get_current_user),
    file: UploadFile = File(...),
    """
    Execute paper trading orders from an Excel file.
    
    Args:
        file: Excel file containing purchase orders (MIME type must be Excel).
    
    v0.7.0: Rate limited to prevent abuse
    
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
        # ValueError from Excel parsing
        raise HTTPException(status_code=400, detail=f"Invalid Excel file: {str(e)}")
    except Exception as e:
        # Check if it's a zip file error (malformed Excel)
        error_msg = str(e).lower()
        if "zip" in error_msg or "excel" in error_msg or "openpyxl" in error_msg:
            raise HTTPException(status_code=400, detail=f"Invalid Excel file: {str(e)}")
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
# PENDING ORDERS ENDPOINTS
# ========================

@app.get("/api/pending")
async def list_pending_orders(status: Optional[str] = None, symbol: Optional[str] = None):
    """
    List all pending orders awaiting user approval.
    
    Query parameters:
    - status: Filter by status ("pending", "approved", "rejected")
    - symbol: Filter by symbol (e.g., "BTC", "ETH")
    
    Returns:
        List of pending orders with all details
    
    v0.7.0: Metrics - Tracks pending order count
    """
    try:
        # If no status filter, default to "pending" only
        if not status:
            status = "pending"
        
        pending = get_pending_orders(status=status, symbol=symbol)
        
        # v0.7.0: Update pending orders metric
        if status == "pending":
            orders_pending_total._value.set(len(pending))
        
        return [order.to_dict() for order in pending]
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch pending orders: {str(e)}")


@app.post("/api/pending/approve/{order_id}")
@limiter.limit(RateLimitConfig.ENDPOINTS["trading"])
async def approve_pending_order(
    request: Request,
    order_id: int,
    current_user: dict = Depends(get_current_user),
    note: Optional[str] = None
):
    """
    Approve a pending order for execution.
    
    Path parameters:
    - order_id: ID of pending order to approve
    
    v0.7.0: Rate limited to prevent abuse
    Metrics: Tracks approved orders and processing time
    
    Query parameters:
    - note: Optional approval notes
    
    Returns:
        Updated pending order
    """
    try:
        start_time = time.time()
        order = approve_order(order_id, reviewed_by="user", note=note)
        
        # v0.7.0: Track order approval metrics
        symbol = getattr(order, 'symbol', 'UNKNOWN')
        orders_approved_total.labels(symbol=symbol).inc()
        processing_time = time.time() - start_time
        order_processing_time_seconds.labels(status="approved").observe(processing_time)
        
        return {
            "status": "approved",
            "order": order.to_dict(),
            "message": f"Order {order_id} approved for execution"
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to approve order: {str(e)}")


@app.post("/api/pending/reject/{order_id}")
@limiter.limit(RateLimitConfig.ENDPOINTS["trading"])
async def reject_pending_order(
    request: Request,
    order_id: int,
    """
    Reject a pending order.
    
    Path parameters:
    - order_id: ID of pending order to reject
    
    Query parameters:
    - note: Reason for rejection
    
    v0.7.0: Rate limited to prevent abuse
    Metrics: Tracks rejected orders and processing time
    
    Returns:
        Updated pending order
    """
    try:
        start_time = time.time()
        order = reject_order(order_id, reviewed_by="user", note=note)
        
        # v0.7.0: Track order rejection metrics
        symbol = getattr(order, 'symbol', 'UNKNOWN')
        orders_rejected_total.labels(symbol=symbol).inc()
        processing_time = time.time() - start_time
        order_processing_time_seconds.labels(status="rejected").observe(processing_time)
        
        return {
            "status": "rejected",
            "order": order.to_dict(),
            "message": f"Order {order_id} rejected"
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to reject order: {str(e)}")


# ========================
# DASHBOARD ENDPOINTS
# ========================

from services.ts.db import get_db
from services.ts.models import Trade, TradePosition, TradePnL
from services.sot.models import Order
from findmy.services.market_data import get_current_prices, get_unrealized_pnl
from findmy.services.backtesting import run_backtest, BacktestRequest
from sqlalchemy import func
from sqlalchemy.orm import Session
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
@limiter.limit(RateLimitConfig.ENDPOINTS["data"])
async def get_positions(
    request: Request,
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db)
):
    """
    Get current positions from Trade Service with live market prices and unrealized PnL.
    
    v0.7.0: Cached for 30s for 90% faster reads on repeated requests.
    Metrics: Tracks cache hits, position count, and total position value.
    """
    # Try cache first
    cache_key = f"positions:skip{skip}:limit{limit}"
    cached_result = cache_manager.l1.get(cache
            if not positions:
                return []
            
            # Fetch current prices for all symbols
            symbols = [p.symbol for p in positions]
            prices = get_current_prices(symbols)
            
            result = []
            total_value = 0.0
            for p in positions:
                current_price = prices.get(p.symbol)
                if current_price is not None:
                    market_value = p.quantity * current_price
                    unrealized_pnl = market_value - p.total_cost
                    total_value += market_value
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
            
            # v0.7.0: Update position metrics
            positions_active.labels(symbol="all")._value.set(len(result))
            if total_value > 0:
                positions_total_value.labels(currency="USD")._value.set(total_value)
            
            # Cache the result for 30s
            cache_manager.l1.set(cache_key, result, CacheConfig.TTL_POSITIONS)
            return result
        except Exception:
            # Table may not exist yet
            return []
    finally:
        db.close()


@app.get("/api/trades",
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
@limiter.limit(RateLimitConfig.ENDPOINTS["data"])
async def get_summary(request: Request):
    """
    Get PnL summary and trading statistics with market values.
    
    v0.7.0: Cached for 10s for instant dashboard loads.
    Metrics: Tracks cache hits, realized/unrealized PnL, total position value.
    """
    # Try cache first (very hot endpoint)
    cache_key = "summary:all"
    cached_result = cache_manager.l1.get(cache_key)
    if cached_result is not None:
        cache_hits_total.labels(cache_level="L1", key_pattern="summary").inc()
        return cached_result
    
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

            result = SummaryResponse(
                total_trades=int(total_trades),
                realized_pnl=realized_pnl,
                unrealized_pnl=unrealized_pnl,
                total_invested=total_invested,
                total_market_value=total_market_value,
                total_equity=total_equity,
                last_trade_time=last_trade_time,
                status="✓ Active",
            )
            
            # v0.7.0: Update PnL metrics
            if realized_pnl != 0:
                trades_pnl_total.labels(symbol="all").observe(realized_pnl)
            if total_market_value > 0:
                positions_total_value.labels(currency="USD")._value.set(total_market_value)
            
            # Cache for 10s (very hot endpoint)
            cache_manager.l1.set(cache_key, result, CacheConfig.TTL_SUMMARY)
            return result
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
    strategy_type: Optional[str] = None  # "moving_average" or None for basic backtest
    strategy_config: Optional[Dict[str, Any]] = None  # Strategy-specific config


@app.post("/api/backtest")
@limiter.limit(RateLimitConfig.ENDPOINTS["trading"])
async def run_backtest_endpoint(
    current_user: dict = Depends(get_current_user),
    request_body: BacktestRequestBody
):
    """
    Run a backtest simulation over historical data.
    
    Supports two modes:
    1. Basic backtest: Simple equity curve without strategy (traditional backtesting)
    2. Strategy backtest: Run trading strategy over historical data with signal tracking
    
    Args:
        request_body: Backtest parameters including symbols, date range, capital, timeframe
                     Optional: strategy_type and strategy_config for strategy backtesting
    
    Returns:
        BacktestResult with equity curve, trades, performance metrics
        If strategy provided, also includes signals and strategy-specific metrics
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
        
        # Check if strategy backtest is requested
        if request_body.strategy_type:
            from findmy.services.strategy_backtest import StrategyBacktester
            
            # Create strategy instance
            if request_body.strategy_type.lower() == "moving_average":
                strategy = MovingAverageStrategy(
                    symbols=request_body.symbols,
                    config=request_body.strategy_config
                )
            else:
                raise HTTPException(
                    status_code=400,
                    detail=f"Unknown strategy type: {request_body.strategy_type}. Available: moving_average"
                )
            
            # Run strategy backtest
            backtester = StrategyBacktester(strategy)
            result = backtester.run(
                start_date=start_date,
                end_date=end_date,
                initial_capital=request_body.initial_capital,
                timeframe=request_body.timeframe
            )
            return result.to_dict()
        else:
            # Run basic backtest (original behavior)
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
# STRATEGY EXECUTION
# ========================

class StrategyRequestBody(BaseModel):
    """Request body for /api/run-strategy endpoint."""
    strategy_type: str  # "moving_average", etc.
    symbols: List[str] = ["BTC", "ETH"]
    start_date: str  # ISO format YYYY-MM-DD
    end_date: str  # ISO format YYYY-MM-DD
    timeframe: str = "1h"
    config: Optional[Dict[str, Any]] = None  # Strategy-specific config


@app.post("/api/run-strategy")
@limiter.limit(RateLimitConfig.ENDPOINTS["trading"])
async def run_strategy_endpoint(
    current_user: dict = Depends(get_current_user),
    request_body: StrategyRequestBody
):
    """
    Run a trading strategy to generate signals and convert them to orders.
    
    Strategies available:
    - moving_average: Moving Average Crossover (fast MA > slow MA = BUY)
    
    Args:
        request_body: Strategy parameters including type, symbols, date range, config
    
    Returns:
        Dictionary with generated signals and orders ready for execution
        
    Example:
        {
            "strategy": "moving_average",
            "symbols": ["BTC", "ETH"],
            "start_date": "2024-01-01",
            "end_date": "2024-01-31",
            "config": {"fast_period": 9, "slow_period": 21}
        }
    """
    try:
        # Parse dates
        start_date = datetime.fromisoformat(request_body.start_date)
        end_date = datetime.fromisoformat(request_body.end_date)
        
        # Validate date range
        if start_date >= end_date:
            raise HTTPException(status_code=400, detail="start_date must be before end_date")
        
        if (end_date - start_date).days > 365:
            raise HTTPException(status_code=400, detail="Strategy period cannot exceed 365 days")
        
        # Create strategy based on type
        if request_body.strategy_type.lower() == "moving_average":
            strategy = MovingAverageStrategy(
                symbols=request_body.symbols,
                config=request_body.config
            )
        else:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown strategy type: {request_body.strategy_type}. Available: moving_average"
            )
        
        # Execute strategy
        executor = StrategyExecutor(strategy)
        result = executor.run(
            start_date=start_date,
            end_date=end_date,
            timeframe=request_body.timeframe
        )
        
        return result
        
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid date format: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Strategy execution error: {str(e)}")


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
