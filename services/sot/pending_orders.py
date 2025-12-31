"""Pending Orders Model for manual approval workflow."""

from sqlalchemy import Column, Integer, String, Float, DateTime, Enum, Text
from datetime import datetime
from enum import Enum as PyEnum

from services.sot.db import Base


class PendingOrderStatus(PyEnum):
    """Status enum for pending orders."""
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class PendingOrder(Base):
    """
    Represents an order awaiting manual user approval.
    
    All orders (from Excel upload, strategy signals, backtest) are saved here first.
    User must approve/reject before order is sent to execution engine.
    """
    __tablename__ = "pending_orders"

    id = Column(Integer, primary_key=True)

    # Order details (mirror of Order model fields)
    symbol = Column(String, nullable=False)
    side = Column(String, nullable=False)  # "BUY" or "SELL"
    quantity = Column(Float, nullable=False)
    price = Column(Float, nullable=False)
    order_type = Column(String, nullable=False, default="MARKET")  # MARKET, LIMIT, STOP_LOSS
    
    # Source tracking
    source = Column(String, nullable=False)  # "excel", "strategy", "backtest"
    source_ref = Column(String, nullable=True)  # Reference to source (e.g., file ID, strategy name)
    
    # Approval workflow
    status = Column(Enum(PendingOrderStatus), nullable=False, default=PendingOrderStatus.PENDING)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    reviewed_at = Column(DateTime, nullable=True)  # When approved/rejected
    reviewed_by = Column(String, nullable=True)  # User who approved/rejected
    
    # Optional metadata
    note = Column(Text, nullable=True)  # Reason for rejection or approval notes
    strategy_name = Column(String, nullable=True)  # If from strategy source
    confidence = Column(Float, nullable=True)  # Signal confidence if from strategy
    
    def to_dict(self):
        """Convert to dictionary for JSON response."""
        return {
            "id": self.id,
            "symbol": self.symbol,
            "side": self.side,
            "quantity": self.quantity,
            "price": self.price,
            "order_type": self.order_type,
            "source": self.source,
            "source_ref": self.source_ref,
            "status": self.status.value if isinstance(self.status, PyEnum) else self.status,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "reviewed_at": self.reviewed_at.isoformat() if self.reviewed_at else None,
            "reviewed_by": self.reviewed_by,
            "note": self.note,
            "strategy_name": self.strategy_name,
            "confidence": self.confidence,
        }
