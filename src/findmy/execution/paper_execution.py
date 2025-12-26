"""
FINDMY (FM) - Paper Trading Execution Engine v1

Features:
- Read Excel input (with or without header)
- Sheet name: "purchase order"
- BUY orders only (v1)
- Immediate full-fill simulation
- SQLite persistence
- Callable from FastAPI

Error Handling:
- Graceful handling of I/O and value errors
- Type-safe numeric conversions
- Proper database session management with context managers
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

# ============================================================
# CONFIG
# ============================================================

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)

DB_PATH = DATA_DIR / "findmy_fm_paper.db"
SHEET_NAME = "purchase order"

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
        price: Order price (Decimal for precision)
        status: Order status (NEW, FILLED, CANCELLED)
        created_at: Order creation timestamp
        updated_at: Order last update timestamp
    """
    __tablename__ = "orders"

    id = Column(Integer, primary_key=True)
    client_order_id = Column(String, unique=True, nullable=False)
    symbol = Column(String, nullable=False)
    side = Column(String, nullable=False)  # BUY
    qty = Column(Numeric, nullable=False)
    price = Column(Numeric, nullable=False)
    status = Column(String, nullable=False, default="NEW")
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
        price: Execution price
        ts: Trade execution timestamp
    """
    __tablename__ = "trades"

    id = Column(Integer, primary_key=True)
    order_id = Column(Integer, ForeignKey("orders.id"))
    symbol = Column(String, nullable=False)
    side = Column(String, nullable=False)
    qty = Column(Numeric, nullable=False)
    price = Column(Numeric, nullable=False)
    ts = Column(DateTime, server_default=func.now())


class Position(Base):
    """
    Position model for current holdings.
    
    Attributes:
        id: Primary key
        symbol: Trading pair (e.g., BTC/USD)
        size: Current position size (quantity held)
        avg_price: Average purchase price
        updated_at: Position last update timestamp
    """
    __tablename__ = "positions"

    id = Column(Integer, primary_key=True)
    symbol = Column(String, unique=True, nullable=False)
    size = Column(Numeric, nullable=False)
    avg_price = Column(Numeric, nullable=False)
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

def parse_orders_from_excel(path: str, sheet_name: str = SHEET_NAME) -> pd.DataFrame:
    """
    Parse orders from an Excel file with flexible header support.
    
    Supports:
    - Excel WITH header (Vietnamese / English)
    - Excel WITHOUT header
    - Fallback to positional A,B,C,D if header mismatch
    
    Args:
        path: File path to Excel file
        sheet_name: Name of sheet to read (default: "purchase order")
    
    Returns:
        DataFrame with columns: [client_id, qty, price, symbol]
        
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
        df = df.iloc[:, :4]
        df.columns = ["client_id", "qty", "price", "symbol"]
        return df.dropna(subset=["symbol", "qty"])

    # ---------- CASE 2: HAS HEADER ----------
    df.columns = [str(c).lower().strip() for c in df.columns]

    col_map = {
        "client_id": ["số thứ tự lệnh", "stt", "client_id", "order id"],
        "qty": ["khối lượng mua", "qty", "quantity"],
        "price": ["giá đặt lệnh", "price", "giá"],
        "symbol": ["cặp tiền ảo giao dịch", "symbol", "pair"],
    }

    mapped = {}
    for key, candidates in col_map.items():
        for c in candidates:
            if c in df.columns:
                mapped[key] = c
                break

    # ---------- FALLBACK: HEADER KHÔNG KHỚP ----------
    if len(mapped) < 4:
        df = df.iloc[:, :4]
        df.columns = ["client_id", "qty", "price", "symbol"]
        return df.dropna(subset=["symbol", "qty"])

    # ---------- NORMAL PATH ----------
    clean = pd.DataFrame()
    for k, c in mapped.items():
        clean[k] = df[c]

    return clean.dropna(subset=["symbol", "qty"])


# ============================================================
# EXECUTION LOGIC
# ============================================================

def upsert_order(session: Session, client_order_id: str, symbol: str, qty: float, price: float) -> Tuple[Order, bool]:
    """
    Insert or retrieve an order by client_order_id.
    
    Args:
        session: SQLAlchemy session
        client_order_id: Unique order identifier
        symbol: Trading pair (e.g., BTC/USD)
        qty: Order quantity
        price: Order price
    
    Returns:
        Tuple of (Order object, is_new: bool). is_new is True if order was created.
        
    Raises:
        ValueError: If numeric conversion fails
    """
    order = session.query(Order).filter_by(
        client_order_id=str(client_order_id)
    ).one_or_none()

    if order:
        return order, False

    try:
        qty = float(qty)
        price = float(price)
    except (ValueError, TypeError) as e:
        raise ValueError(f"Invalid numeric values: qty={qty}, price={price}. Error: {str(e)}")

    order = Order(
        client_order_id=str(client_order_id),
        symbol=str(symbol).strip(),
        side="BUY",
        qty=qty,
        price=price,
        status="NEW",
        created_at=datetime.utcnow(),
    )
    session.add(order)
    session.commit()
    return order, True


def simulate_fill(session: Session, order: Order) -> Tuple[bool, Dict[str, Any]]:
    """
    Simulate order fill and update positions.
    
    Args:
        session: SQLAlchemy session
        order: Order object to fill
    
    Returns:
        Tuple of (success: bool, trade_data: dict). trade_data contains trade details.
        
    Raises:
        ValueError: If numeric conversion fails
    """
    if order.status == "FILLED":
        return False, {}

    try:
        qty = float(order.qty)
        price = float(order.price)
    except (ValueError, TypeError) as e:
        logger.error(f"Failed to convert numeric values for order {order.id}: {str(e)}")
        raise ValueError(f"Invalid order numeric values: {str(e)}")

    trade = Trade(
        order_id=order.id,
        symbol=order.symbol,
        side=order.side,
        qty=qty,
        price=price,
        ts=datetime.utcnow(),
    )
    session.add(trade)

    order.status = "FILLED"
    order.updated_at = datetime.utcnow()

    pos = session.query(Position).filter_by(symbol=order.symbol).one_or_none()

    if pos is None:
        pos = Position(
            symbol=order.symbol,
            size=qty,
            avg_price=price,
            updated_at=datetime.utcnow(),
        )
        session.add(pos)
    else:
        old_size = float(pos.size)
        old_avg = float(pos.avg_price)
        new_size = old_size + qty
        new_avg = ((old_size * old_avg) + (qty * price)) / new_size

        pos.size = new_size
        pos.avg_price = new_avg
        pos.updated_at = datetime.utcnow()

    session.commit()
    return True, {
        "trade_id": trade.id,
        "symbol": order.symbol,
        "qty": qty,
        "price": price,
    }


# ============================================================
# PUBLIC API (CALLABLE BY FASTAPI)
# ============================================================

def run_paper_execution(excel_path: str) -> Dict[str, Any]:
    """
    Execute paper trading orders from an Excel file.
    
    Args:
        excel_path: Path to Excel file containing orders
    
    Returns:
        Dictionary with execution results:
        {
            "orders": int,
            "trades": int,
            "positions": List[dict]
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

    with SessionFactory() as session:
        for idx, r in df_orders.iterrows():
            try:
                order, _ = upsert_order(
                    session=session,
                    client_order_id=r["client_id"],
                    symbol=str(r["symbol"]).strip(),
                    qty=float(r["qty"]),
                    price=float(r["price"]),
                )

                success, trade_data = simulate_fill(session, order)
                if success:
                    trade_count += 1

            except (ValueError, TypeError) as e:
                error_rows.append({"row": idx + 2, "error": str(e)})
                logger.warning(f"Skipped row {idx + 2}: {str(e)}")
                continue
            except Exception as e:
                logger.error(f"Unexpected error at row {idx + 2}: {str(e)}")
                raise

        # Use raw SQL to avoid deprecated pd.read_sql_table
        positions_df = pd.read_sql("SELECT * FROM positions", engine)

    return {
        "orders": len(df_orders),
        "trades": trade_count,
        "positions": positions_df.to_dict(orient="records"),
        "errors": error_rows if error_rows else None,
    }
