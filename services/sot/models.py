from sqlalchemy import (
    Column, Integer, String, Float, DateTime, ForeignKey, Text, Index
)
from sqlalchemy.orm import relationship
from datetime import datetime

from .db import Base
class OrderRequest(Base):
    """Order request - v0.7.0: Added indexes for faster queries."""
    __tablename__ = "order_requests"
    
    # v0.7.0: Indexes for performance
    __table_args__ = (
        Index('ix_order_requests_symbol', 'symbol'),
        Index('ix_order_requests_requested_at', 'requested_at'),
    )

    id = Column(Integer, primary_key=True)

    source = Column(String, nullable=False)
    source_ref = Column(String)

    symbol = Column(String, nullable=False)
    side = Column(String, nullable=False)
    order_type = Column(String, nullable=False)

    quantity = Column(Float, nullable=False)
    price = Column(Float)

    strategy_code = Column(String)
    requested_by = Column(String)
    requested_at = Column(DateTime, default=datetime.utcnow)

    raw_payload = Column(Text)

    orders = relationship("Order", back_populates="order_request")
class Order(Base):
    """Order - v0.7.0: Added indexes on status, created_at, symbol."""
    __tablename__ = "orders"
    
    # v0.7.0: Indexes for performance
    __table_args__ = (
        Index('ix_orders_status', 'status'),
        Index('ix_orders_created_at', 'created_at'),
        Index('ix_orders_order_request_id', 'order_request_id'),
    )

    id = Column(Integer, primary_key=True)

    order_request_id = Column(
        Integer,
        ForeignKey("order_requests.id"),
        nullable=False
    )

    exchange = Column(String, nullable=False)
    exchange_order_id = Column(String)
    client_order_id = Column(String)

    position_id = Column(String)

    status = Column(String, nullable=False)
    time_in_force = Column(String)

    reduce_only = Column(Integer, default=0)
    post_only = Column(Integer, default=0)

    sent_at = Column(DateTime)
    filled_at = Column(DateTime)

    avg_price = Column(Float)
    executed_qty = Column(Float)

    error_message = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)

    order_request = relationship(
        "OrderRequest",
        back_populates="orders"
    )

    events = relationship("OrderEvent", back_populates="order")
    fills = relationship("OrderFill", back_populates="order")
class OrderEvent(Base):
    __tablename__ = "order_events"

    id = Column(Integer, primary_key=True)

    order_id = Column(
        Integer,
        ForeignKey("orders.id"),
        nullable=False
    )

    event_type = Column(String, nullable=False)
    event_time = Column(DateTime, default=datetime.utcnow)

    payload = Column(Text)

    order = relationship("Order", back_populates="events")
class OrderFill(Base):
    __tablename__ = "order_fills"

    id = Column(Integer, primary_key=True)

    order_id = Column(
        Integer,
        ForeignKey("orders.id"),
        nullable=False
    )

    fill_price = Column(Float, nullable=False)
    fill_qty = Column(Float, nullable=False)

    fee_amount = Column(Float)
    fee_asset = Column(String)

    liquidity = Column(String)
    filled_at = Column(DateTime)

    order = relationship("Order", back_populates="fills")
class OrderCost(Base):
    __tablename__ = "order_costs"

    order_id = Column(
        Integer,
        ForeignKey("orders.id"),
        primary_key=True
    )

    total_fee = Column(Float)
    fee_asset = Column(String)
    commission_rate = Column(Float)

    order = relationship("Order")
class OrderPnl(Base):
    __tablename__ = "order_pnl"

    order_id = Column(
        Integer,
        ForeignKey("orders.id"),
        primary_key=True
    )

    realized_pnl = Column(Float)
    unrealized_pnl = Column(Float)
    cost_basis = Column(Float)

    calculated_at = Column(DateTime, default=datetime.utcnow)

    order = relationship("Order")
