"""
FINDMY (FM) - Paper Trading Execution Engine v1

Features:
- Read Excel input (with or without header)
- Sheet name: "purchase order"
- BUY orders only (v1)
- Immediate full-fill simulation
- SQLite persistence
- Callable from FastAPI
"""

from datetime import datetime
from pathlib import Path
import pandas as pd

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
from sqlalchemy.orm import declarative_base, sessionmaker

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
    __tablename__ = "trades"

    id = Column(Integer, primary_key=True)
    order_id = Column(Integer, ForeignKey("orders.id"))
    symbol = Column(String, nullable=False)
    side = Column(String, nullable=False)
    qty = Column(Numeric, nullable=False)
    price = Column(Numeric, nullable=False)
    ts = Column(DateTime, server_default=func.now())


class Position(Base):
    __tablename__ = "positions"

    id = Column(Integer, primary_key=True)
    symbol = Column(String, unique=True, nullable=False)
    size = Column(Numeric, nullable=False)
    avg_price = Column(Numeric, nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


# ============================================================
# DB SETUP
# ============================================================

def setup_db():
    engine = create_engine(f"sqlite:///{DB_PATH}", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return engine, Session


# ============================================================
# EXCEL PARSER (ROBUST)
# ============================================================

def parse_orders_from_excel(path: str, sheet_name: str = SHEET_NAME) -> pd.DataFrame:
    """
    Supports:
    - Excel WITH header (Vietnamese / English)
    - Excel WITHOUT header
    - Fallback to positional A,B,C,D if header mismatch
    """

    sheets = pd.read_excel(path, sheet_name=None, engine="openpyxl")

    df = None
    for name, sheet in sheets.items():
        if str(name).lower().strip() == sheet_name:
            df = sheet
            break

    if df is None:
        raise ValueError(f"Sheet '{sheet_name}' not found")

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

def upsert_order(session, client_order_id, symbol, qty, price):
    order = session.query(Order).filter_by(
        client_order_id=str(client_order_id)
    ).one_or_none()

    if order:
        return order, False

    order = Order(
        client_order_id=str(client_order_id),
        symbol=symbol,
        side="BUY",
        qty=qty,
        price=price,
        status="NEW",
        created_at=datetime.utcnow(),
    )
    session.add(order)
    session.commit()
    return order, True


def simulate_fill(session, order: Order):
    if order.status == "FILLED":
        return None

    trade = Trade(
        order_id=order.id,
        symbol=order.symbol,
        side=order.side,
        qty=order.qty,
        price=order.price,
        ts=datetime.utcnow(),
    )
    session.add(trade)

    order.status = "FILLED"
    order.updated_at = datetime.utcnow()

    pos = session.query(Position).filter_by(symbol=order.symbol).one_or_none()

    qty = float(order.qty)
    price = float(order.price)

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
    return trade


# ============================================================
# PUBLIC API (CALLABLE BY FASTAPI)
# ============================================================

def run_paper_execution(excel_path: str) -> dict:
    engine, Session = setup_db()
    session = Session()

    df_orders = parse_orders_from_excel(excel_path)

    trade_count = 0

    for _, r in df_orders.iterrows():
        order, _ = upsert_order(
            session=session,
            client_order_id=r["client_id"],
            symbol=str(r["symbol"]).strip(),
            qty=r["qty"],
            price=r["price"],
        )

        if simulate_fill(session, order):
            trade_count += 1

    positions_df = pd.read_sql_table("positions", engine)

    return {
        "orders": len(df_orders),
        "trades": trade_count,
        "positions": positions_df.to_dict(orient="records"),
    }
