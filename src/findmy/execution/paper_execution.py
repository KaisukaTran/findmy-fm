"""
FINDMY (FM) - Paper Trading Execution Engine v0.2.0

Features:
- Read Excel input (with or without header)
- Sheet name: "purchase order"
- BUY and SELL orders with position reduction
- Immediate full-fill simulation
- SQLite persistence
- Callable from FastAPI
- Realized PnL calculation on SELL orders

Error Handling:
- Graceful handling of I/O and value errors
- Type-safe numeric conversions
- Proper database session management with context managers
- Oversell prevention with clear error messages
"""

from datetime import datetime
from pathlib import Path
from typing import Tuple, Dict, Any, List
import pandas as pd
import logging

from sqlalchemy import (
    create_engine,
    Column,
    Integer,
    String,
    Numeric,
    DateTime,
    ForeignKey,
    func,
)
from sqlalchemy.orm import declarative_base, sessionmaker, Session

# Configure logging
logger = logging.getLogger(__name__)
import random

# ============================================================
# CONFIG
# ============================================================

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)

DB_PATH = DATA_DIR / "findmy_fm_paper.db"
SHEET_NAME = "purchase order"

# Execution configuration
# Fraction of remaining quantity to fill on each simulated partial fill (0.0-1.0)
# Default to 1.0 (full-fill) for backward compatibility with existing tests and
# behavior; make configurable later if partial-fill testing is desired.
DEFAULT_FILL_PCT = float(1.0)

# Slippage configuration (max percent of price to move)
# Default to 0.0 (no slippage) for backward compatibility; enable later if desired.
DEFAULT_SLIPPAGE_PCT = float(0.0)

# Default fee rates (percentage, e.g. 0.001 = 0.1%)
# Default to 0.0 (no fees) to preserve previous behavior; configurable per-order or globally.
DEFAULT_MAKER_FEE = float(0.0)
DEFAULT_TAKER_FEE = float(0.0)

Base = declarative_base()


# ============================================================
# DATABASE MODELS
# ============================================================

class Order(Base):
    """
    Order model for paper trading.
    
    Attributes:
        id: Primary key
        client_order_id: Unique order identifier from client
        symbol: Trading pair (e.g., BTC/USD)
        side: Order side (BUY or SELL)
        qty: Order quantity (Decimal for precision)
        remaining_qty: Quantity still to be filled (for partial fills)
        price: Order price (Decimal for precision)
        order_type: Order type (MARKET, LIMIT, STOP_LOSS; default MARKET)
        stop_price: Stop price for stop-loss orders (optional)
        status: Order status (NEW, PARTIALLY_FILLED, FILLED, CANCELLED, TRIGGERED)
        maker_fee_rate: Maker fee percentage (default 0.0 = no fees)
        taker_fee_rate: Taker fee percentage (default 0.0 = no fees)
        created_at: Order creation timestamp
        updated_at: Order last update timestamp
    """
    __tablename__ = "orders"

    id = Column(Integer, primary_key=True)
    client_order_id = Column(String, unique=True, nullable=False)
    symbol = Column(String, nullable=False)
    side = Column(String, nullable=False)  # BUY or SELL
    qty = Column(Numeric, nullable=False)
    remaining_qty = Column(Numeric, nullable=False)  # For partial fills
    price = Column(Numeric, nullable=False)
    order_type = Column(String, nullable=False, default="MARKET")  # MARKET, LIMIT, STOP_LOSS
    stop_price = Column(Numeric, nullable=True)  # Stop price for stop-loss orders
    status = Column(String, nullable=False, default="NEW")  # NEW, PARTIALLY_FILLED, FILLED, CANCELLED, TRIGGERED
    maker_fee_rate = Column(Numeric, nullable=False, default=0.0)  # 0% (no fees by default)
    taker_fee_rate = Column(Numeric, nullable=False, default=0.0)  # 0% (no fees by default)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, onupdate=func.now())


class Trade(Base):
    """
    Trade model for executed trades.
    
    Attributes:
        id: Primary key
        order_id: Reference to the order that generated this trade
        symbol: Trading pair (e.g., BTC/USD)
        side: Trade side (BUY or SELL)
        qty: Trade quantity
        price: Original execution price
        effective_price: Price after slippage (actual fill price)
        fees: Trading fees charged on this trade
        slippage_amount: Slippage cost applied to this trade
        ts: Trade execution timestamp
    """
    __tablename__ = "trades"

    id = Column(Integer, primary_key=True)
    order_id = Column(Integer, ForeignKey("orders.id"))
    symbol = Column(String, nullable=False)
    side = Column(String, nullable=False)
    qty = Column(Numeric, nullable=False)
    price = Column(Numeric, nullable=False)
    effective_price = Column(Numeric, nullable=True)  # Price after slippage
    fees = Column(Numeric, nullable=True, default=0.0)  # Trading fees
    slippage_amount = Column(Numeric, nullable=True, default=0.0)  # Slippage cost
    ts = Column(DateTime, server_default=func.now())


class Position(Base):
    """
    Position model for current holdings.
    
    Attributes:
        id: Primary key
        symbol: Trading pair (e.g., BTC/USD)
        size: Current position size (quantity held)
        avg_price: Average purchase price
        realized_pnl: Cumulative realized P&L from closed positions
        updated_at: Position last update timestamp
    """
    __tablename__ = "positions"

    id = Column(Integer, primary_key=True)
    symbol = Column(String, unique=True, nullable=False)
    size = Column(Numeric, nullable=False)
    avg_price = Column(Numeric, nullable=False)
    realized_pnl = Column(Numeric, nullable=False, default=0.0)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


# ============================================================
# DB SETUP
# ============================================================

def setup_db() -> Tuple[Any, sessionmaker]:
    """
    Initialize database and return engine and session factory.
    
    Returns:
        Tuple of (SQLAlchemy engine, sessionmaker factory)
        
    Raises:
        Exception: If database initialization fails
    """
    engine = create_engine(f"sqlite:///{DB_PATH}", future=True)
    Base.metadata.create_all(engine)
    SessionFactory = sessionmaker(bind=engine)
    return engine, SessionFactory


# ============================================================
# EXCEL PARSER (ROBUST)
# ============================================================

def detect_order_side(side_value: Any) -> str:
    """
    Detect order side (BUY or SELL) from a cell value.
    
    Supports:
    - English: "BUY", "SELL" (case-insensitive)
    - Vietnamese: "MUA" (buy), "BÁN" (sell) (case-insensitive)
    
    Args:
        side_value: Raw cell value (str, None, or other type)
    
    Returns:
        "BUY" or "SELL" (defaults to "BUY" if not recognized)
    """
    if side_value is None or (isinstance(side_value, float) and pd.isna(side_value)):
        return "BUY"
    
    side_str = str(side_value).strip().upper()
    
    # Check for SELL indicators
    if side_str in ("SELL", "BÁN"):
        return "SELL"
    
    # Default to BUY for anything else
    return "BUY"


def parse_orders_from_excel(path: str, sheet_name: str = SHEET_NAME) -> pd.DataFrame:
    """
    Parse orders from an Excel file with flexible header support.
    
    Supports:
    - Excel WITH header (Vietnamese / English)
    - Excel WITHOUT header
    - Fallback to positional A,B,C,D,E if header mismatch
    - Optional 5th column for order side (BUY/SELL)
    
    Args:
        path: File path to Excel file
        sheet_name: Name of sheet to read (default: "purchase order")
    
    Returns:
        DataFrame with columns: [client_id, qty, price, symbol, side]
        where side defaults to "BUY" if not specified
        
    Raises:
        ValueError: If sheet not found or data is invalid
        IOError: If file cannot be read
    """
    try:
        sheets = pd.read_excel(path, sheet_name=None, engine="openpyxl")
    except Exception as e:
        raise IOError(f"Failed to read Excel file: {str(e)}")

    df = None
    for name, sheet in sheets.items():
        if str(name).lower().strip() == sheet_name:
            df = sheet
            break

    if df is None:
        raise ValueError(f"Sheet '{sheet_name}' not found in Excel file")

    # ---------- CASE 1: NO HEADER ----------
    if all(isinstance(c, int) for c in df.columns):
        # Support 4 or 5 columns (with optional side)
        if len(df.columns) >= 5:
            df = df.iloc[:, :5]
            df.columns = ["client_id", "qty", "price", "symbol", "side"]
            df["side"] = df["side"].apply(detect_order_side)
        else:
            df = df.iloc[:, :4]
            df.columns = ["client_id", "qty", "price", "symbol"]
            df["side"] = "BUY"
        return df.dropna(subset=["symbol", "qty"])

    # ---------- CASE 2: HAS HEADER ----------
    df.columns = [str(c).lower().strip() for c in df.columns]

    col_map = {
        "client_id": ["order id", "stt", "client_id"],
        "qty": ["quantity", "qty"],
        "price": ["price"],
        "symbol": ["trading pair", "symbol", "pair"],
        "side": ["side", "order side", "direction"],
    }

    mapped = {}
    for key, candidates in col_map.items():
        for c in candidates:
            if c in df.columns:
                mapped[key] = c
                break

    # ---------- FALLBACK: HEADER MISMATCH ----------
    if len(mapped) < 4:
        # If less than 4 required columns, try positional fallback
        if len(df.columns) >= 5:
            df = df.iloc[:, :5]
            df.columns = ["client_id", "qty", "price", "symbol", "side"]
            df["side"] = df["side"].apply(detect_order_side)
        else:
            df = df.iloc[:, :4]
            df.columns = ["client_id", "qty", "price", "symbol"]
            df["side"] = "BUY"
        return df.dropna(subset=["symbol", "qty"])

    # ---------- NORMAL PATH ----------
    clean = pd.DataFrame()
    for k in ["client_id", "qty", "price", "symbol"]:
        if k in mapped:
            clean[k] = df[mapped[k]]
    
    # Convert client_id to string
    clean["client_id"] = clean["client_id"].astype(str)
    
    # Add side column with detection
    if "side" in mapped:
        clean["side"] = df[mapped["side"]].apply(detect_order_side)
    else:
        clean["side"] = "BUY"

    return clean.dropna(subset=["symbol", "qty"])


# ============================================================
# EXECUTION LOGIC
# ============================================================

def upsert_order(
    session: Session,
    client_order_id: str,
    symbol: str,
    qty: float,
    price: float,
    side: str = "BUY",
    order_type: str = "MARKET",
    stop_price: float = None,
) -> Tuple[Order, bool]:
    """
    Insert or retrieve an order by client_order_id.
    
    Args:
        session: SQLAlchemy session
        client_order_id: Unique order identifier
        symbol: Trading pair (e.g., BTC/USD)
        qty: Order quantity
        price: Order price
        side: Order side ("BUY" or "SELL", defaults to "BUY")
        order_type: Order type ("MARKET", "LIMIT", "STOP_LOSS"; defaults to "MARKET")
        stop_price: Stop price for stop-loss orders (optional)
    
    Returns:
        Tuple of (Order object, is_new: bool). is_new is True if order was created.
        
    Raises:
        ValueError: If numeric conversion fails or side is invalid
    """
    order = session.query(Order).filter_by(
        client_order_id=str(client_order_id)
    ).one_or_none()

    if order:
        return order, False

    try:
        qty = float(qty)
        price = float(price)
        if stop_price is not None:
            stop_price = float(stop_price)
    except (ValueError, TypeError) as e:
        raise ValueError(f"Invalid numeric values: qty={qty}, price={price}, stop_price={stop_price}. Error: {str(e)}")

    # Validate side
    side = str(side).strip().upper()
    if side not in ("BUY", "SELL"):
        raise ValueError(f"Invalid order side: {side}. Must be 'BUY' or 'SELL'")

    # Validate order type
    order_type = str(order_type).strip().upper()
    if order_type not in ("MARKET", "LIMIT", "STOP_LOSS"):
        raise ValueError(f"Invalid order type: {order_type}. Must be 'MARKET', 'LIMIT', or 'STOP_LOSS'")

    order = Order(
        client_order_id=str(client_order_id),
        symbol=str(symbol).strip(),
        side=side,
        qty=qty,
        remaining_qty=qty,
        price=price,
        order_type=order_type,
        stop_price=stop_price,
        status="NEW",
        created_at=datetime.utcnow(),
    )
    session.add(order)
    session.commit()
    return order, True


def simulate_fill(session: Session, order: Order) -> Tuple[bool, Dict[str, Any]]:
    """
    Simulate order fill and update positions.
    
    For BUY orders:
    - Increases position size
    - Updates average cost basis
    
    For SELL orders:
    - Reduces position size
    - Calculates realized P&L based on cost basis
    - Prevents overselling
    
    Args:
        session: SQLAlchemy session
        order: Order object to fill
    
    Returns:
        Tuple of (success: bool, trade_data: dict). trade_data contains trade details
        including realized_pnl for SELL orders.
        
    Raises:
        ValueError: If numeric conversion fails or position insufficient for SELL
    """
    if order.status == "FILLED":
        return False, {}

    try:
        qty = float(order.qty)
        price = float(order.price)
    except (ValueError, TypeError) as e:
        logger.error(f"Failed to convert numeric values for order {order.id}: {str(e)}")
        raise ValueError(f"Invalid order numeric values: {str(e)}")

    # Fetch or create position
    pos = session.query(Position).filter_by(symbol=order.symbol).one_or_none()

    # Determine remaining quantity on order (supports older rows)
    remaining = float(getattr(order, "remaining_qty", None) or qty)

    # Determine fill quantity for this simulation (partial fill)
    fill_qty = round(remaining * DEFAULT_FILL_PCT, 8)
    if fill_qty <= 0:
        fill_qty = remaining
    if fill_qty > remaining:
        fill_qty = remaining

    # Compute slippage: adverse to the side (BUY pays higher, SELL receives lower)
    # Only apply slippage if DEFAULT_SLIPPAGE_PCT > 0
    if DEFAULT_SLIPPAGE_PCT > 0:
        slip_raw = random.uniform(0, DEFAULT_SLIPPAGE_PCT)
        if order.side == "BUY":
            slippage_pct = slip_raw
        else:
            slippage_pct = -slip_raw
    else:
        slippage_pct = 0.0
    
    effective_price = price * (1 + slippage_pct)
    slippage_amount = (effective_price - price) * fill_qty

    # Determine fee rate (prefer order-level taker fee, fallback to default)
    fee_rate = float(getattr(order, "taker_fee_rate", DEFAULT_TAKER_FEE) or DEFAULT_TAKER_FEE)
    fees = effective_price * fill_qty * fee_rate

    # ============================================================
    # SELL: ensure position sufficiency for this partial fill
    # ============================================================
    if order.side == "SELL":
        if pos is None or float(pos.size) < fill_qty:
            current_size = float(pos.size) if pos else 0.0
            raise ValueError(
                f"Insufficient position for SELL: requested {fill_qty}, current position {current_size} for {order.symbol}"
            )

        old_size = float(pos.size)
        old_avg = float(pos.avg_price)

        # Realized PnL uses effective_price and deducts fees
        realized_pnl = (effective_price - old_avg) * fill_qty - fees
        cost_basis = old_avg * fill_qty

        trade = Trade(
            order_id=order.id,
            symbol=order.symbol,
            side="SELL",
            qty=fill_qty,
            price=price,
            effective_price=effective_price,
            fees=fees,
            slippage_amount=slippage_amount,
            ts=datetime.utcnow(),
        )
        session.add(trade)

        # Update position
        new_size = old_size - fill_qty
        pos.size = new_size
        pos.realized_pnl = float(pos.realized_pnl) + realized_pnl
        if new_size == 0:
            pos.avg_price = 0
        pos.updated_at = datetime.utcnow()

        # Update order remaining qty and status
        order.remaining_qty = remaining - fill_qty
        order.updated_at = datetime.utcnow()
        order.status = "PARTIALLY_FILLED" if order.remaining_qty > 0 else "FILLED"

        session.commit()

        return True, {
            "trade_id": trade.id,
            "symbol": order.symbol,
            "side": "SELL",
            "qty": fill_qty,
            "filled_qty": fill_qty,
            "remaining_qty": float(order.remaining_qty),
            "price": price,
            "effective_price": effective_price,
            "fees": fees,
            "slippage_amount": slippage_amount,
            "cost_basis": cost_basis,
            "realized_pnl": realized_pnl,
            "position_remaining": new_size,
        }

    # ============================================================
    # BUY: apply partial fill and update position incrementally
    # ============================================================
    # Compute new position values using effective_price
    trade = Trade(
        order_id=order.id,
        symbol=order.symbol,
        side="BUY",
        qty=fill_qty,
        price=price,
        effective_price=effective_price,
        fees=fees,
        slippage_amount=slippage_amount,
        ts=datetime.utcnow(),
    )
    session.add(trade)

    # Update or create position
    if pos is None:
        pos = Position(
            symbol=order.symbol,
            size=fill_qty,
            avg_price=effective_price,
            realized_pnl=0.0,
            updated_at=datetime.utcnow(),
        )
        session.add(pos)
    else:
        old_size = float(pos.size)
        old_avg = float(pos.avg_price)
        new_size = old_size + fill_qty
        new_avg = ((old_size * old_avg) + (fill_qty * effective_price)) / new_size
        pos.size = new_size
        pos.avg_price = new_avg
        pos.updated_at = datetime.utcnow()

    # Update order remaining qty and status
    order.remaining_qty = remaining - fill_qty
    order.updated_at = datetime.utcnow()
    order.status = "PARTIALLY_FILLED" if order.remaining_qty > 0 else "FILLED"

    session.commit()

    return True, {
        "trade_id": trade.id,
        "symbol": order.symbol,
        "side": "BUY",
        "qty": fill_qty,
        "filled_qty": fill_qty,
        "remaining_qty": float(order.remaining_qty),
        "price": price,
        "effective_price": effective_price,
        "fees": fees,
        "slippage_amount": slippage_amount,
        "position_size": float(pos.size),
    }


# ============================================================
# STOP-LOSS ORDER MANAGEMENT
# ============================================================

def check_and_trigger_stoploss(
    session: Session,
    current_prices: Dict[str, float],
) -> List[Dict[str, Any]]:
    """
    Check pending stop-loss orders and trigger them if conditions are met.
    
    Args:
        session: SQLAlchemy session
        current_prices: Dict mapping symbol to current price
    
    Returns:
        List of triggered stop-loss orders with trade data
    """
    triggered_orders = []
    
    # Find all pending stop-loss orders
    pending_stops = session.query(Order).filter(
        Order.order_type == "STOP_LOSS",
        Order.status == "NEW"
    ).all()
    
    for order in pending_stops:
        current_price = current_prices.get(order.symbol)
        if current_price is None:
            continue
        
        stop_price = float(order.stop_price)
        
        # Check if stop condition is met (price at or below stop_price for SELL stops)
        # For now, only support SELL stop-loss (common use case)
        if order.side == "SELL" and float(current_price) <= stop_price:
            # Trigger the order by converting stop_price to execution price
            order.price = current_price
            order.status = "TRIGGERED"
            order.updated_at = datetime.utcnow()
            session.commit()
            
            # Execute the triggered order
            success, trade_data = simulate_fill(session, order)
            if success:
                trade_data["triggered_at_price"] = float(current_price)
                triggered_orders.append(trade_data)
    
    return triggered_orders


# ============================================================
# PUBLIC API (CALLABLE BY FASTAPI)
# ============================================================

def run_paper_execution(excel_path: str) -> Dict[str, Any]:
    """
    Execute paper trading orders from an Excel file.
    
    Processes both BUY and SELL orders:
    - BUY orders: accumulate positions
    - SELL orders: reduce positions and calculate realized PnL
    
    Args:
        excel_path: Path to Excel file containing orders
    
    Returns:
        Dictionary with execution results:
        {
            "orders": int,                    # Total orders processed
            "trades": int,                    # Total trades executed
            "summary": dict,                  # Aggregated metrics (total_fees, total_slippage, total_realized_pnl)
            "positions": List[dict],          # Final positions with realized PnL
            "errors": List[dict] or None      # Any row-level errors
        }
        
    Raises:
        IOError: If Excel file cannot be read
        ValueError: If order data is invalid
        Exception: For other processing errors
    """
    engine, SessionFactory = setup_db()

    try:
        df_orders = parse_orders_from_excel(excel_path)
    except (IOError, ValueError) as e:
        logger.error(f"Failed to parse Excel file: {str(e)}")
        raise

    trade_count = 0
    error_rows = []
    total_fees = 0.0
    total_slippage = 0.0

    with SessionFactory() as session:
        for idx, r in df_orders.iterrows():
            try:
                side = r.get("side", "BUY") if isinstance(r, dict) else getattr(r, "side", "BUY")
                
                order, _ = upsert_order(
                    session=session,
                    client_order_id=r["client_id"],
                    symbol=str(r["symbol"]).strip(),
                    qty=float(r["qty"]),
                    price=float(r["price"]),
                    side=side,
                )

                success, trade_data = simulate_fill(session, order)
                if success:
                    trade_count += 1
                    # Accumulate fees and slippage from each trade
                    total_fees += float(trade_data.get("fees", 0.0))
                    total_slippage += abs(float(trade_data.get("slippage_amount", 0.0)))

            except (ValueError, TypeError) as e:
                error_rows.append({"row": idx + 2, "error": str(e)})
                logger.warning(f"Skipped row {idx + 2}: {str(e)}")
                continue
            except Exception as e:
                logger.error(f"Unexpected error at row {idx + 2}: {str(e)}")
                raise

        # Fetch final positions and aggregate realized PnL
        positions_df = pd.read_sql("SELECT * FROM positions", engine)
        total_realized_pnl = float(positions_df["realized_pnl"].sum()) if len(positions_df) > 0 else 0.0

    return {
        "orders": len(df_orders),
        "trades": trade_count,
        "summary": {
            "total_fees": round(total_fees, 8),
            "total_slippage": round(total_slippage, 8),
            "total_realized_pnl": round(total_realized_pnl, 8),
        },
        "positions": positions_df.to_dict(orient="records"),
        "errors": error_rows if error_rows else None,
    }
