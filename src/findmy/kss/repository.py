"""
KSS Database Repository.

Data access layer for KSS sessions and waves.
Handles CRUD operations and queries.
"""

from typing import List, Optional
from datetime import datetime
import logging

from sqlalchemy.orm import Session

from src.findmy.kss.models import KSSSession, KSSWave, KSSSessionStatus, KSSWaveStatus
from src.findmy.kss.pyramid import PyramidSession, PyramidSessionStatus, WaveInfo

logger = logging.getLogger(__name__)


class KSSRepository:
    """Repository for KSS database operations."""
    
    def __init__(self, db: Session):
        """Initialize with database session."""
        self.db = db
    
    # ============================================================
    # Session CRUD
    # ============================================================
    
    def create_session(
        self,
        symbol: str,
        entry_price: float,
        distance_pct: float,
        max_waves: int,
        isolated_fund: float,
        tp_pct: float,
        timeout_x_min: float,
        gap_y_min: float,
        created_by: Optional[str] = None,
        note: Optional[str] = None,
    ) -> KSSSession:
        """Create a new KSS session in database."""
        session = KSSSession(
            strategy_type="pyramid",
            symbol=symbol,
            entry_price=entry_price,
            distance_pct=distance_pct,
            max_waves=max_waves,
            isolated_fund=isolated_fund,
            tp_pct=tp_pct,
            timeout_x_min=timeout_x_min,
            gap_y_min=gap_y_min,
            status=KSSSessionStatus.PENDING,
            current_wave=0,
            avg_price=0.0,
            total_filled_qty=0.0,
            total_cost=0.0,
            created_by=created_by,
            note=note,
        )
        
        self.db.add(session)
        self.db.commit()
        self.db.refresh(session)
        
        logger.info(f"Created KSS session {session.id}: {symbol} @ {entry_price}")
        return session
    
    def get_session(self, session_id: int) -> Optional[KSSSession]:
        """Get session by ID."""
        return self.db.query(KSSSession).filter(KSSSession.id == session_id).first()
    
    def get_sessions(
        self,
        status: Optional[KSSSessionStatus] = None,
        symbol: Optional[str] = None,
        limit: int = 100,
    ) -> List[KSSSession]:
        """Get sessions with optional filters."""
        query = self.db.query(KSSSession)
        
        if status:
            query = query.filter(KSSSession.status == status)
        if symbol:
            query = query.filter(KSSSession.symbol == symbol)
        
        query = query.order_by(KSSSession.created_at.desc()).limit(limit)
        return query.all()
    
    def get_active_sessions(self) -> List[KSSSession]:
        """Get all active sessions."""
        return self.get_sessions(status=KSSSessionStatus.ACTIVE)
    
    def update_session_status(
        self,
        session_id: int,
        status: KSSSessionStatus,
    ) -> Optional[KSSSession]:
        """Update session status."""
        session = self.get_session(session_id)
        if not session:
            return None
        
        session.status = status
        
        if status == KSSSessionStatus.ACTIVE and not session.started_at:
            session.started_at = datetime.utcnow()
        elif status in (KSSSessionStatus.COMPLETED, KSSSessionStatus.STOPPED, KSSSessionStatus.TP_TRIGGERED):
            session.completed_at = datetime.utcnow()
        
        self.db.commit()
        self.db.refresh(session)
        
        logger.info(f"Updated KSS session {session_id} status to {status.value}")
        return session
    
    def update_session_state(
        self,
        session_id: int,
        current_wave: Optional[int] = None,
        avg_price: Optional[float] = None,
        total_filled_qty: Optional[float] = None,
        total_cost: Optional[float] = None,
        last_fill_at: Optional[datetime] = None,
    ) -> Optional[KSSSession]:
        """Update session calculated state."""
        session = self.get_session(session_id)
        if not session:
            return None
        
        if current_wave is not None:
            session.current_wave = current_wave
        if avg_price is not None:
            session.avg_price = avg_price
        if total_filled_qty is not None:
            session.total_filled_qty = total_filled_qty
        if total_cost is not None:
            session.total_cost = total_cost
        if last_fill_at is not None:
            session.last_fill_at = last_fill_at
        
        self.db.commit()
        self.db.refresh(session)
        return session
    
    def update_session_params(
        self,
        session_id: int,
        max_waves: Optional[int] = None,
        isolated_fund: Optional[float] = None,
        tp_pct: Optional[float] = None,
        distance_pct: Optional[float] = None,
        timeout_x_min: Optional[float] = None,
        gap_y_min: Optional[float] = None,
    ) -> Optional[KSSSession]:
        """Update session adjustable parameters."""
        session = self.get_session(session_id)
        if not session:
            return None
        
        if max_waves is not None:
            session.max_waves = max_waves
        if isolated_fund is not None:
            session.isolated_fund = isolated_fund
        if tp_pct is not None:
            session.tp_pct = tp_pct
        if distance_pct is not None:
            session.distance_pct = distance_pct
        if timeout_x_min is not None:
            session.timeout_x_min = timeout_x_min
        if gap_y_min is not None:
            session.gap_y_min = gap_y_min
        
        self.db.commit()
        self.db.refresh(session)
        
        logger.info(f"Updated KSS session {session_id} params")
        return session
    
    def delete_session(self, session_id: int) -> bool:
        """Delete session and its waves."""
        session = self.get_session(session_id)
        if not session:
            return False
        
        self.db.delete(session)
        self.db.commit()
        
        logger.info(f"Deleted KSS session {session_id}")
        return True
    
    # ============================================================
    # Wave CRUD
    # ============================================================
    
    def create_wave(
        self,
        session_id: int,
        wave_num: int,
        quantity: float,
        target_price: float,
        pending_order_id: Optional[int] = None,
    ) -> KSSWave:
        """Create a new wave for a session."""
        wave = KSSWave(
            session_id=session_id,
            wave_num=wave_num,
            quantity=quantity,
            target_price=target_price,
            status=KSSWaveStatus.PENDING,
            pending_order_id=pending_order_id,
        )
        
        self.db.add(wave)
        self.db.commit()
        self.db.refresh(wave)
        
        logger.info(f"Created wave {wave_num} for session {session_id}")
        return wave
    
    def get_wave(self, wave_id: int) -> Optional[KSSWave]:
        """Get wave by ID."""
        return self.db.query(KSSWave).filter(KSSWave.id == wave_id).first()
    
    def get_wave_by_order_id(self, pending_order_id: int) -> Optional[KSSWave]:
        """Get wave by pending order ID."""
        return self.db.query(KSSWave).filter(
            KSSWave.pending_order_id == pending_order_id
        ).first()
    
    def get_session_waves(self, session_id: int) -> List[KSSWave]:
        """Get all waves for a session."""
        return self.db.query(KSSWave).filter(
            KSSWave.session_id == session_id
        ).order_by(KSSWave.wave_num).all()
    
    def update_wave_sent(
        self,
        wave_id: int,
        pending_order_id: int,
    ) -> Optional[KSSWave]:
        """Mark wave as sent to pending queue."""
        wave = self.get_wave(wave_id)
        if not wave:
            return None
        
        wave.status = KSSWaveStatus.SENT
        wave.sent_at = datetime.utcnow()
        wave.pending_order_id = pending_order_id
        
        self.db.commit()
        self.db.refresh(wave)
        return wave
    
    def update_wave_filled(
        self,
        wave_id: int,
        filled_qty: float,
        filled_price: float,
    ) -> Optional[KSSWave]:
        """Mark wave as filled."""
        wave = self.get_wave(wave_id)
        if not wave:
            return None
        
        wave.status = KSSWaveStatus.FILLED
        wave.filled_qty = filled_qty
        wave.filled_price = filled_price
        wave.filled_at = datetime.utcnow()
        
        self.db.commit()
        self.db.refresh(wave)
        
        logger.info(f"Wave {wave.wave_num} filled: {filled_qty} @ {filled_price}")
        return wave
    
    def update_wave_cancelled(self, wave_id: int) -> Optional[KSSWave]:
        """Mark wave as cancelled."""
        wave = self.get_wave(wave_id)
        if not wave:
            return None
        
        wave.status = KSSWaveStatus.CANCELLED
        
        self.db.commit()
        self.db.refresh(wave)
        return wave
    
    # ============================================================
    # Conversion helpers
    # ============================================================
    
    def db_to_pyramid_session(self, db_session: KSSSession) -> PyramidSession:
        """Convert DB model to PyramidSession dataclass."""
        # Map status
        status_map = {
            KSSSessionStatus.PENDING: PyramidSessionStatus.PENDING,
            KSSSessionStatus.ACTIVE: PyramidSessionStatus.ACTIVE,
            KSSSessionStatus.STOPPED: PyramidSessionStatus.STOPPED,
            KSSSessionStatus.COMPLETED: PyramidSessionStatus.COMPLETED,
            KSSSessionStatus.TP_TRIGGERED: PyramidSessionStatus.TP_TRIGGERED,
        }
        
        pyramid = PyramidSession(
            symbol=db_session.symbol,
            entry_price=db_session.entry_price,
            distance_pct=db_session.distance_pct,
            max_waves=db_session.max_waves,
            isolated_fund=db_session.isolated_fund,
            tp_pct=db_session.tp_pct,
            timeout_x_min=db_session.timeout_x_min,
            gap_y_min=db_session.gap_y_min,
        )
        
        pyramid.id = db_session.id
        pyramid.status = status_map.get(db_session.status, PyramidSessionStatus.PENDING)
        pyramid.current_wave = db_session.current_wave
        pyramid.avg_price = db_session.avg_price
        pyramid.total_filled_qty = db_session.total_filled_qty
        pyramid.total_cost = db_session.total_cost
        pyramid.start_time = db_session.started_at
        pyramid.last_fill_time = db_session.last_fill_at
        pyramid.created_at = db_session.created_at
        
        # Load waves
        pyramid.waves = []
        for w in db_session.waves or []:
            wave_info = WaveInfo(
                wave_num=w.wave_num,
                quantity=w.quantity,
                target_price=w.target_price,
                status=w.status.value if hasattr(w.status, 'value') else w.status,
                filled_qty=w.filled_qty or 0.0,
                filled_price=w.filled_price or 0.0,
                filled_time=w.filled_at,
                pending_order_id=w.pending_order_id,
            )
            pyramid.waves.append(wave_info)
        
        return pyramid
    
    def pyramid_to_db_session(self, pyramid: PyramidSession) -> KSSSession:
        """Convert PyramidSession dataclass to DB model."""
        status_map = {
            PyramidSessionStatus.PENDING: KSSSessionStatus.PENDING,
            PyramidSessionStatus.ACTIVE: KSSSessionStatus.ACTIVE,
            PyramidSessionStatus.STOPPED: KSSSessionStatus.STOPPED,
            PyramidSessionStatus.COMPLETED: KSSSessionStatus.COMPLETED,
            PyramidSessionStatus.TP_TRIGGERED: KSSSessionStatus.TP_TRIGGERED,
        }
        
        return KSSSession(
            id=pyramid.id,
            strategy_type="pyramid",
            symbol=pyramid.symbol,
            entry_price=pyramid.entry_price,
            distance_pct=pyramid.distance_pct,
            max_waves=pyramid.max_waves,
            isolated_fund=pyramid.isolated_fund,
            tp_pct=pyramid.tp_pct,
            timeout_x_min=pyramid.timeout_x_min,
            gap_y_min=pyramid.gap_y_min,
            status=status_map.get(pyramid.status, KSSSessionStatus.PENDING),
            current_wave=pyramid.current_wave,
            avg_price=pyramid.avg_price,
            total_filled_qty=pyramid.total_filled_qty,
            total_cost=pyramid.total_cost,
            started_at=pyramid.start_time,
            last_fill_at=pyramid.last_fill_time,
            created_at=pyramid.created_at,
        )
