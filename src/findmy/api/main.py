from contextlib import asynccontextmanager
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
import secrets
import asyncio
from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta

from findmy.execution.paper_execution import run_paper_execution
from findmy.strategies import MovingAverageStrategy
from findmy.services.strategy_executor import StrategyExecutor
from services.sot.pending_orders_service import (
    queue_order, get_pending_orders, approve_order, reject_order, count_pending
)
from services.sot.system_state import is_halted, set_halt

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
import time

# v1.0.1: Import observability components
from findmy.api.logging_config import configure_logging, get_logger, get_trace_id
from findmy.api.middleware import RequestLoggingMiddleware
from findmy.api.exception_handlers import register_exception_handlers

# v1.0.1: Configure structured logging FIRST (before any other imports use logging)
configure_logging()
logger = get_logger(__name__)

from findmy.api.sentry_config import init_sentry
init_sentry()

# v0.7.0: Import caching (needed for lifespan)
from services.cache.manager import cache_manager, CacheConfig  # noqa: E402

@asynccontextmanager
async def lifespan(app: FastAPI):
    await cache_manager.init()
    logger.info("Cache manager initialized")
    try:
        app_info.info({"version": "1.0.0"})
    except Exception:
        pass
    logger.info("Application startup complete - v1.0.1 with observability")
    yield
    await cache_manager.clear()
    logger.info("Cache manager shutdown")


# ✅ 1. DECLARE APP FIRST
app = FastAPI(
    title="FINDMY FM – Paper Trading API",
    version="1.0",
    lifespan=lifespan,
)

# v1.0.1: Register centralized exception handlers
register_exception_handlers(app)

# v1.0.1: Add request logging middleware (adds trace_id)
app.add_middleware(RequestLoggingMiddleware)

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

# v0.10.0: Import KSS routes
from src.findmy.kss.routes import router as kss_router

# Include authentication routes
app.include_router(auth_router)

# v0.10.0: Include KSS routes
app.include_router(kss_router)

# CSRF protection middleware
@app.middleware("http")
async def csrf_middleware(request: Request, call_next):
    """
    Sets a csrf_token cookie on GET requests; validates X-CSRF-Token on mutating methods.
    Skips /api/ai/ (Bearer auth), /health, and /metrics.
    """
    mutating_methods = {"POST", "PATCH", "DELETE", "PUT"}
    skip_paths = {"/health", "/metrics"}
    skip_prefixes = ("/api/ai/", "/api/auth/")

    if request.method in mutating_methods:
        path = request.url.path
        if path not in skip_paths and not any(path.startswith(p) for p in skip_prefixes):
            cookie_token = request.cookies.get("csrf_token")
            header_token = request.headers.get("X-CSRF-Token")
            if not cookie_token or not header_token or cookie_token != header_token:
                return JSONResponse(
                    status_code=403,
                    content={"detail": "CSRF token missing or invalid"},
                )

    response = await call_next(request)

    if request.method == "GET" and not request.cookies.get("csrf_token"):
        token = secrets.token_urlsafe(32)
        response.set_cookie(
            "csrf_token",
            token,
            httponly=False,  # must be JS-readable
            samesite="strict",
            secure=False,  # set True behind HTTPS in production
        )

    return response

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

# ✅ HEALTH CHECK (Enhanced v1.0.1)
@app.get("/health")
async def health_check():
    """
    Enhanced health check endpoint with component status.
    
    Checks:
    - API status
    - Database connectivity
    - Cache status
    - Binance API connectivity (optional)
    
    Returns:
        JSON with overall status and component details.
    """
    import time
    import httpx
    
    health_status = {
        "status": "ok",
        "service": "FINDMY FM API",
        "version": "1.0.1",
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "components": {}
    }
    
    # Check database
    try:
        from services.ts.db import get_db, engine
        from sqlalchemy import text
        start = time.perf_counter()
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        db_latency = (time.perf_counter() - start) * 1000
        health_status["components"]["database"] = {
            "status": "ok",
            "latency_ms": round(db_latency, 2)
        }
    except Exception as e:
        health_status["status"] = "degraded"
        health_status["components"]["database"] = {
            "status": "error",
            "error": str(e)
        }
    
    # Check cache
    try:
        cache_ok = cache_manager.l1 is not None
        health_status["components"]["cache"] = {
            "status": "ok" if cache_ok else "unavailable"
        }
    except Exception as e:
        health_status["components"]["cache"] = {
            "status": "error",
            "error": str(e)
        }
    
    # Check Binance API (non-blocking, with timeout)
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            start = time.perf_counter()
            resp = await client.get("https://api.binance.com/api/v3/ping")
            binance_latency = (time.perf_counter() - start) * 1000
            health_status["components"]["binance"] = {
                "status": "ok" if resp.status_code == 200 else "degraded",
                "latency_ms": round(binance_latency, 2)
            }
    except Exception as e:
        health_status["components"]["binance"] = {
            "status": "unavailable",
            "error": "Binance API unreachable"
        }
    
    # Set overall status based on critical components
    if health_status["components"].get("database", {}).get("status") == "error":
        health_status["status"] = "unhealthy"
    
    return health_status


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
):
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
    if is_halted():
        raise HTTPException(status_code=503, detail="System is in emergency halt. Resume trading before approving orders.")
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
    note: str = "",
):
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
    initial_fund: float = 10000.0
    available_fund: float = 10000.0
    fund_utilization_pct: float = 0.0
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
    cached_result = cache_manager.l1.get(cache_key)
    if cached_result is not None:
        cache_hits_total.labels(cache_level="L1", key_pattern="positions").inc()
        return cached_result
    
    try:
        positions = db.query(Position).offset(skip).limit(limit).all()
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


@app.get("/api/trades")
async def get_trades(db: Session = Depends(get_db)):
    """Get trade history from Trade Service, ordered by timestamp DESC."""
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


@app.get("/api/summary", response_model=SummaryResponse)
@limiter.limit(RateLimitConfig.ENDPOINTS["data"])
async def get_summary(request: Request, db: Session = Depends(get_db)):
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

        # Calculate total equity and fund balance
        total_equity = total_invested + unrealized_pnl
        from findmy.config import settings as _app_cfg
        initial_fund = _app_cfg.initial_fund
        available_fund = max(0.0, initial_fund - total_invested)
        fund_utilization_pct = (total_invested / initial_fund * 100) if initial_fund > 0 else 0.0

        result = SummaryResponse(
            total_trades=int(total_trades),
            realized_pnl=realized_pnl,
            unrealized_pnl=unrealized_pnl,
            total_invested=total_invested,
            total_market_value=total_market_value,
            total_equity=total_equity,
            initial_fund=initial_fund,
            available_fund=available_fund,
            fund_utilization_pct=round(fund_utilization_pct, 2),
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
        try:
            from findmy.config import settings as _app_cfg
            _initial = _app_cfg.initial_fund
        except Exception:
            _initial = 10000.0
        return SummaryResponse(
            total_trades=0,
            realized_pnl=0.0,
            unrealized_pnl=0.0,
            total_invested=0.0,
            total_market_value=0.0,
            total_equity=0.0,
            initial_fund=_initial,
            available_fund=_initial,
            fund_utilization_pct=0.0,
            last_trade_time=None,
            status="✓ Active",
        )


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
    request: Request,
    request_body: BacktestRequestBody,
    current_user: dict = Depends(get_current_user),
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
    request: Request,
    request_body: StrategyRequestBody,
    current_user: dict = Depends(get_current_user),
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


# ========================
# EMERGENCY STOP
# ========================

def _require_admin(user: dict) -> None:
    if user.get("role") != "admin":
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail="Admin role required")


@app.post("/api/emergency-stop")
async def emergency_stop(current_user: dict = Depends(get_current_user)):
    """Halt all order approvals across all workers. Admin only."""
    _require_admin(current_user)
    set_halt(True)
    logger.warning(f"EMERGENCY HALT activated by {current_user.get('sub', 'unknown')}")
    return {"status": "halted", "message": "All order approvals are now blocked."}


@app.post("/api/emergency-resume")
async def emergency_resume(current_user: dict = Depends(get_current_user)):
    """Resume normal operations. Admin only."""
    _require_admin(current_user)
    set_halt(False)
    logger.warning(f"EMERGENCY HALT cleared by {current_user.get('sub', 'unknown')}")
    return {"status": "active", "message": "Order approvals resumed."}


@app.get("/api/system/status")
async def system_status():
    """Get current system halt state (public read, DB-backed)."""
    return {"emergency_halt": is_halted()}


@app.get("/api/system/circuit-status")
async def circuit_status():
    """Current circuit-breaker thresholds and live order-rate (public read)."""
    try:
        from src.findmy.config import settings as _cfg
        from services.trading.circuit_breaker import MAX_ORDERS_PER_MINUTE, _conn
        with _conn() as con:
            row = con.execute("""
                SELECT COUNT(*) as cnt FROM pending_orders
                WHERE created_at >= datetime('now', '-1 minute')
                  AND status IN ('pending', 'approved')
            """).fetchone()
        rate = int(row["cnt"]) if row else 0
        return {
            "max_position_size_pct": _cfg.max_position_size_pct,
            "max_daily_loss_pct": _cfg.max_daily_loss_pct,
            "max_orders_per_minute": MAX_ORDERS_PER_MINUTE,
            "orders_last_minute": rate,
        }
    except Exception as e:
        return {"error": str(e)}


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


# ========================
# AI AGENT ROUTES
# ========================

@app.post("/api/ai/start")
@limiter.limit("10/minute")
async def ai_start(request: Request, current_user: dict = Depends(get_current_user)):
    """Start the autonomous AI trading agent loop. Admin only."""
    _require_admin(current_user)
    from services.ai import agent_runner
    result = await agent_runner.start()
    if not result["started"]:
        raise HTTPException(status_code=409, detail=result.get("error", "Could not start"))
    from services.ai.state import get_paper_start_date, set_paper_start_date
    if not get_paper_start_date():
        set_paper_start_date(datetime.utcnow().strftime("%Y-%m-%d"))
    return {"status": "started", "mode": agent_runner.get_status()["mode"]}


@app.post("/api/ai/stop")
@limiter.limit("10/minute")
async def ai_stop(request: Request, current_user: dict = Depends(get_current_user)):
    """Stop the autonomous AI trading agent loop. Admin only."""
    _require_admin(current_user)
    from services.ai import agent_runner
    result = await agent_runner.stop()
    if not result["stopped"]:
        raise HTTPException(status_code=409, detail=result.get("error", "Could not stop"))
    return {"status": "stopped"}


@app.get("/api/ai/status")
@limiter.limit("60/minute")
async def ai_status(request: Request, current_user: dict = Depends(get_current_user)):
    """Get current AI agent status, config, and today's activity."""
    from services.ai import agent_runner
    return agent_runner.get_status()


@app.get("/api/ai/decisions")
@limiter.limit("30/minute")
async def ai_decisions(
    request: Request,
    limit: int = 50,
    symbol: str = None,
    current_user: dict = Depends(get_current_user),
):
    """List recent AI trading decisions with reasoning."""
    from services.ai.decision_log import get_decisions
    return get_decisions(limit=min(limit, 200), symbol=symbol)


@app.get("/api/ai/paper-report")
@limiter.limit("10/minute")
async def ai_paper_report(
    request: Request,
    days: int = None,
    current_user: dict = Depends(get_current_user),
):
    """Get AI paper trading performance report."""
    from services.ai.paper_report import get_paper_report
    from src.findmy.config import settings
    return get_paper_report(days=days or settings.ai_paper_min_days)


@app.post("/api/ai/promote-to-live")
@limiter.limit("5/minute")
async def ai_promote_to_live(request: Request, current_user: dict = Depends(get_current_user)):
    """Promote AI agent from paper to live trading if performance gate passes. Admin only."""
    _require_admin(current_user)
    from services.ai.paper_report import promote_to_live
    result = promote_to_live()
    if not result["promoted"]:
        raise HTTPException(status_code=400, detail={"eligible": False, "reasons": result["reasons"]})
    return result


# ── Consultant registry ───────────────────────────────────────────────────────

@app.get("/api/ai/consultants")
@limiter.limit("30/minute")
async def list_consultants(request: Request, current_user: dict = Depends(get_current_user)):
    """List all registered AI consultant agents."""
    from services.ai.consultants.registry import list_consultants as _list
    return _list()


@app.post("/api/ai/consultants")
@limiter.limit("10/minute")
async def add_consultant(
    request: Request,
    body: dict,
    current_user: dict = Depends(get_current_user),
):
    """
    Register a new AI consultant agent. Admin only.
    Body: {name, type: 'technical'|'llm', config: {}, enabled: true}
    """
    _require_admin(current_user)
    from services.ai.consultants.registry import add_consultant as _add, DuplicateConsultantError
    name = body.get("name")
    type_ = body.get("type", "llm")
    config = body.get("config", {})
    enabled = body.get("enabled", True)
    if not name:
        raise HTTPException(status_code=422, detail="name is required")
    try:
        return _add(name, type_, config, enabled)
    except DuplicateConsultantError as e:
        raise HTTPException(status_code=409, detail=str(e))


@app.patch("/api/ai/consultants/{consultant_id}/toggle")
@limiter.limit("10/minute")
async def toggle_consultant(
    request: Request,
    consultant_id: int,
    body: dict,
    current_user: dict = Depends(get_current_user),
):
    """Enable or disable a consultant agent. Admin only."""
    _require_admin(current_user)
    from services.ai.consultants.registry import toggle_consultant as _toggle
    enabled = bool(body.get("enabled", True))
    ok = _toggle(consultant_id, enabled)
    if not ok:
        raise HTTPException(status_code=404, detail="Consultant not found")
    return {"id": consultant_id, "enabled": enabled}


@app.delete("/api/ai/consultants/{consultant_id}")
@limiter.limit("10/minute")
async def delete_consultant(
    request: Request,
    consultant_id: int,
    current_user: dict = Depends(get_current_user),
):
    """Remove a consultant agent. Admin only."""
    _require_admin(current_user)
    from services.ai.consultants.registry import remove_consultant as _remove
    ok = _remove(consultant_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Consultant not found")
    return {"deleted": True, "id": consultant_id}


@app.post("/api/ai/inject-signal")
async def inject_ai_signal(
    payload: dict,
    current_user: dict = Depends(get_current_user),
):
    """Inject a synthetic AI signal to test consensus without Claude API calls. Admin only."""
    _require_admin(current_user)

    from services.ai.agent import TradingSignal, submit_ai_order
    from services.ai.consultants.registry import get_enabled_consultants
    from services.ai.consensus import aggregate_votes

    symbol = payload.get("symbol", "BTC/USDT")
    signal_type = payload.get("signal", "HOLD").upper()
    confidence = float(payload.get("confidence", 0.5))
    reasoning = payload.get("reasoning", "Manual injection for testing")

    signal = TradingSignal(
        symbol=symbol,
        signal=signal_type,
        confidence=confidence,
        reasoning=reasoning,
        suggested_price=payload.get("suggested_price"),
        suggested_quantity_usdt=payload.get("suggested_quantity_usdt"),
    )

    consultants = get_enabled_consultants()
    votes = {}
    for c in consultants:
        try:
            vote = await asyncio.to_thread(c.vote, symbol, signal)
            votes[c.name] = {"vote": vote.vote, "confidence": vote.confidence, "reasoning": vote.reasoning}
        except Exception as e:
            votes[c.name] = {"error": str(e)}

    consensus_passed = True
    if votes:
        consensus_passed = aggregate_votes(signal, votes)

    order_id = None
    if consensus_passed:
        order_id = await asyncio.to_thread(submit_ai_order, signal, votes)

    return {
        "symbol": symbol,
        "signal": signal_type,
        "confidence": confidence,
        "votes": votes,
        "consensus_passed": consensus_passed,
        "order_id": order_id,
    }
