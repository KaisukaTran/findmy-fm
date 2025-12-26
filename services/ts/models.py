"""
TS Data Models

Trade Service owns:
1. Trade aggregation (entry → exit)
2. Trade lifecycle (open, closed, partial)
3. Trade performance metrics
4. P&L analysis (realized, unrealized, fees)
5. Trade attribution (strategy, signal source)

All models reference SOT orders via order_id.
TS is read-primarily and derives data from SOT.
"""

from sqlalchemy import (
    Column, Integer, String, Float, DateTime, ForeignKey, Text, Boolean
)
from sqlalchemy.orm import relationship
from datetime import datetime

from .db import Base


class Trade(Base):
    """
    Represents a completed or open trade (entry → exit).
    A trade is an aggregation of related orders (entry + exit).
    """
    __tablename__ = "trades"

    id = Column(Integer, primary_key=True)

    # Reference to entry order in SOT
    entry_order_id = Column(Integer, nullable=False)  # Foreign key to orders.id
    
    # Reference to exit order in SOT (NULL if still open)
    exit_order_id = Column(Integer, nullable=True)  # Foreign key to orders.id

    # Trade metadata
    symbol = Column(String, nullable=False)
    side = Column(String, nullable=False)  # BUY or SELL
    status = Column(String, nullable=False, default="OPEN")  # OPEN, CLOSED, PARTIAL
    
    # Entry details
    entry_qty = Column(Float, nullable=False)
    entry_price = Column(Float, nullable=False)
    entry_time = Column(DateTime, nullable=False)
    
    # Exit details (if trade closed)
    exit_qty = Column(Float, nullable=True)
    exit_price = Column(Float, nullable=True)
    exit_time = Column(DateTime, nullable=True)
    
    # Current position (for partial/open trades)
    current_qty = Column(Float, nullable=False)
    current_price = Column(Float, nullable=True)
    
    # Attribution
    strategy_code = Column(String)
    signal_source = Column(String)  # e.g., "manual", "backtest", "live"
    requested_by = Column(String)
    
    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships (removed because Order is in separate services.sot)
    # Using relationship would require importing Order from SOT which causes circular imports
    # Instead, we store order IDs and fetch via repository pattern
    pnl = relationship("TradePnL", back_populates="trade", uselist=False, cascade="all, delete-orphan")


class TradePnL(Base):
    """
    P&L snapshot for a trade.
    Updated as trade transitions through its lifecycle.
    """
    __tablename__ = "trade_pnl"

    trade_id = Column(Integer, ForeignKey("trades.id"), primary_key=True)

    # Gross P&L (before fees)
    gross_pnl = Column(Float, nullable=False, default=0.0)
    
    # Fees and costs
    total_fees = Column(Float, nullable=False, default=0.0)
    entry_fees = Column(Float, nullable=False, default=0.0)
    exit_fees = Column(Float, nullable=False, default=0.0)
    
    # Net P&L (after fees)
    net_pnl = Column(Float, nullable=False, default=0.0)
    
    # Return metrics
    return_pct = Column(Float, nullable=False, default=0.0)  # (net_pnl / cost_basis) * 100
    
    # Cost basis
    cost_basis = Column(Float, nullable=False, default=0.0)  # entry_qty * entry_price
    
    # Realized vs Unrealized (for open trades)
    realized_pnl = Column(Float, nullable=False, default=0.0)
    unrealized_pnl = Column(Float, nullable=False, default=0.0)
    
    # Risk metrics
    max_profit = Column(Float, nullable=True)  # Best P&L during trade
    max_loss = Column(Float, nullable=True)    # Worst P&L during trade
    max_drawdown = Column(Float, nullable=True) # Max loss / cost_basis
    
    # Duration
    duration_minutes = Column(Integer, nullable=True)  # (exit_time - entry_time).total_seconds() / 60
    
    # Last update
    calculated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    trade = relationship("Trade", back_populates="pnl")


class TradePosition(Base):
    """
    Current position state after each trade execution.
    Used for position reconciliation and tracking.
    """
    __tablename__ = "trade_positions"

    id = Column(Integer, primary_key=True)

    symbol = Column(String, nullable=False)
    
    # Current state
    quantity = Column(Float, nullable=False)
    avg_entry_price = Column(Float, nullable=False)
    
    # History
    total_traded = Column(Float, nullable=False, default=0.0)  # cumulative qty
    total_cost = Column(Float, nullable=False, default=0.0)    # total invested
    
    # Attribution
    strategy_code = Column(String)
    
    # Timestamps
    last_trade_time = Column(DateTime)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class TradePerformance(Base):
    """
    Time-bucketed performance aggregation.
    Used for dashboards and analytics.
    """
    __tablename__ = "trade_performance"

    id = Column(Integer, primary_key=True)

    # Time bucket
    bucket_time = Column(DateTime, nullable=False)  # e.g., daily, hourly
    bucket_type = Column(String, nullable=False)   # "hourly", "daily", "weekly"
    
    # Trades in this bucket
    total_trades = Column(Integer, nullable=False, default=0)
    winning_trades = Column(Integer, nullable=False, default=0)
    losing_trades = Column(Integer, nullable=False, default=0)
    breakeven_trades = Column(Integer, nullable=False, default=0)
    
    # P&L
    total_pnl = Column(Float, nullable=False, default=0.0)
    net_pnl = Column(Float, nullable=False, default=0.0)
    total_fees = Column(Float, nullable=False, default=0.0)
    
    # Win rate
    win_rate = Column(Float, nullable=False, default=0.0)  # winning_trades / total_trades * 100
    
    # Avg trade metrics
    avg_pnl = Column(Float, nullable=False, default=0.0)
    avg_win = Column(Float, nullable=True)
    avg_loss = Column(Float, nullable=True)
    
    # Risk metrics
    max_consecutive_wins = Column(Integer, nullable=True)
    max_consecutive_losses = Column(Integer, nullable=True)
    
    # Created
    calculated_at = Column(DateTime, default=datetime.utcnow)
