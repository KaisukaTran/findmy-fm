"""
KSS (Kai Strategy Service) Database Models.

Defines SQLAlchemy models for persisting KSS sessions and waves.

Tables:
- kss_sessions: Pyramid DCA sessions
- kss_waves: Individual waves within sessions
"""

from sqlalchemy import (
    Column, Integer, String, Float, DateTime, ForeignKey, Text, Index, Enum
)
from sqlalchemy.orm import relationship
from datetime import datetime
from enum import Enum as PyEnum

from services.sot.db import Base


class KSSSessionStatus(PyEnum):
    """Status enum for KSS sessions."""
    PENDING = "pending"
    ACTIVE = "active"
    STOPPED = "stopped"
    COMPLETED = "completed"
    TP_TRIGGERED = "tp_triggered"


class KSSWaveStatus(PyEnum):
    """Status enum for KSS waves."""
    PENDING = "pending"
    SENT = "sent"
    FILLED = "filled"
    CANCELLED = "cancelled"


class KSSSession(Base):
    """
    KSS Pyramid DCA Session.
    
    Stores session parameters and state for pyramid DCA strategy.
    """
    __tablename__ = "kss_sessions"
    
    __table_args__ = (
        Index('ix_kss_sessions_symbol', 'symbol'),
        Index('ix_kss_sessions_status', 'status'),
        Index('ix_kss_sessions_created_at', 'created_at'),
    )
    
    id = Column(Integer, primary_key=True)
    
    # Strategy type (for future expansion)
    strategy_type = Column(String, nullable=False, default="pyramid")
    
    # Session parameters
    symbol = Column(String, nullable=False)
    entry_price = Column(Float, nullable=False)
    distance_pct = Column(Float, nullable=False)
    max_waves = Column(Integer, nullable=False)
    isolated_fund = Column(Float, nullable=False)
    tp_pct = Column(Float, nullable=False)
    timeout_x_min = Column(Float, nullable=False)
    gap_y_min = Column(Float, nullable=False)
    
    # Session state
    status = Column(Enum(KSSSessionStatus), nullable=False, default=KSSSessionStatus.PENDING)
    current_wave = Column(Integer, nullable=False, default=0)
    
    # Calculated values
    avg_price = Column(Float, nullable=False, default=0.0)
    total_filled_qty = Column(Float, nullable=False, default=0.0)
    total_cost = Column(Float, nullable=False, default=0.0)
    
    # Timestamps
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    started_at = Column(DateTime, nullable=True)
    last_fill_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    
    # User/notes
    created_by = Column(String, nullable=True)
    note = Column(Text, nullable=True)
    
    # Relationship to waves
    waves = relationship("KSSWave", back_populates="session", cascade="all, delete-orphan")
    
    def to_dict(self):
        """Convert to dictionary for API response."""
        return {
            "id": self.id,
            "strategy_type": self.strategy_type,
            "symbol": self.symbol,
            "entry_price": self.entry_price,
            "distance_pct": self.distance_pct,
            "max_waves": self.max_waves,
            "isolated_fund": self.isolated_fund,
            "tp_pct": self.tp_pct,
            "timeout_x_min": self.timeout_x_min,
            "gap_y_min": self.gap_y_min,
            "status": self.status.value if isinstance(self.status, PyEnum) else self.status,
            "current_wave": self.current_wave,
            "avg_price": self.avg_price,
            "total_filled_qty": self.total_filled_qty,
            "total_cost": self.total_cost,
            "used_fund": self.total_cost,
            "remaining_fund": max(0, self.isolated_fund - self.total_cost),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "last_fill_at": self.last_fill_at.isoformat() if self.last_fill_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "created_by": self.created_by,
            "note": self.note,
            "waves": [w.to_dict() for w in self.waves] if self.waves else [],
        }


class KSSWave(Base):
    """
    Individual wave within a KSS session.
    
    Tracks each wave's parameters, status, and fill information.
    """
    __tablename__ = "kss_waves"
    
    __table_args__ = (
        Index('ix_kss_waves_session_id', 'session_id'),
        Index('ix_kss_waves_status', 'status'),
        Index('ix_kss_waves_pending_order_id', 'pending_order_id'),
    )
    
    id = Column(Integer, primary_key=True)
    
    # Foreign key to session
    session_id = Column(Integer, ForeignKey("kss_sessions.id"), nullable=False)
    
    # Wave parameters
    wave_num = Column(Integer, nullable=False)
    quantity = Column(Float, nullable=False)
    target_price = Column(Float, nullable=False)
    
    # Status
    status = Column(Enum(KSSWaveStatus), nullable=False, default=KSSWaveStatus.PENDING)
    
    # Fill info
    filled_qty = Column(Float, nullable=True)
    filled_price = Column(Float, nullable=True)
    filled_at = Column(DateTime, nullable=True)
    
    # Link to pending order
    pending_order_id = Column(Integer, nullable=True)
    
    # Timestamps
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    sent_at = Column(DateTime, nullable=True)
    
    # Relationship back to session
    session = relationship("KSSSession", back_populates="waves")
    
    def to_dict(self):
        """Convert to dictionary for API response."""
        return {
            "id": self.id,
            "session_id": self.session_id,
            "wave_num": self.wave_num,
            "quantity": self.quantity,
            "target_price": self.target_price,
            "status": self.status.value if isinstance(self.status, PyEnum) else self.status,
            "filled_qty": self.filled_qty,
            "filled_price": self.filled_price,
            "filled_at": self.filled_at.isoformat() if self.filled_at else None,
            "pending_order_id": self.pending_order_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "sent_at": self.sent_at.isoformat() if self.sent_at else None,
        }
