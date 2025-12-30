from fastapi import FastAPI, UploadFile, File, HTTPException, Request
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pathlib import Path
import shutil
import uuid
import os
from typing import Optional

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
from sqlalchemy import func
from datetime import datetime
from pydantic import BaseModel
from typing import List


class PositionResponse(BaseModel):
    symbol: str
    quantity: float
    avg_price: float
    total_cost: float


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
    last_trade_time: Optional[datetime] = None
    status: str


@app.get("/api/positions", response_model=List[PositionResponse])
async def get_positions():
    """Get current positions from Trade Service."""
    db = SessionLocal()
    try:
        try:
            positions = db.query(TradePosition).all()
            return [
                PositionResponse(
                    symbol=p.symbol,
                    quantity=p.quantity,
                    avg_price=p.avg_entry_price,
                    total_cost=p.total_cost,
                )
                for p in positions
            ]
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
    """Get PnL summary and trading statistics."""
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

            # Total invested
            try:
                positions = db.query(TradePosition).all()
                total_invested = sum(p.total_cost for p in positions) if positions else 0.0
            except Exception:
                total_invested = 0.0

            # Last trade time
            try:
                last_trade = db.query(Trade).order_by(Trade.entry_time.desc()).first()
                last_trade_time = last_trade.entry_time if last_trade else None
            except Exception:
                last_trade_time = None

            return SummaryResponse(
                total_trades=int(total_trades),
                realized_pnl=realized_pnl,
                unrealized_pnl=unrealized_pnl,
                total_invested=total_invested,
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
                last_trade_time=None,
                status="✓ Active",
            )
    finally:
        db.close()
