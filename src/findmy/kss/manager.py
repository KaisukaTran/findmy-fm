"""
KSS Manager - Manages multiple concurrent strategy sessions.

Provides:
- Session lifecycle management (create, start, stop, list)
- Fill event routing to correct session
- Persistence coordination with DB layer
"""

from typing import Dict, List, Optional, Any
from datetime import datetime
import logging

from src.findmy.kss.pyramid import PyramidSession, PyramidSessionStatus

logger = logging.getLogger(__name__)


class KSSManager:
    """
    Singleton manager for KSS (Kai Strategy Service) sessions.
    
    Manages:
    - In-memory session registry
    - Session creation and lifecycle
    - Fill event routing
    - Integration with pending orders service
    """
    
    _instance: Optional["KSSManager"] = None
    
    def __new__(cls) -> "KSSManager":
        """Singleton pattern."""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        """Initialize manager (only once due to singleton)."""
        if self._initialized:
            return
        
        self._sessions: Dict[int, PyramidSession] = {}
        self._next_id: int = 1
        self._initialized = True
        logger.info("KSSManager initialized")
    
    def create_pyramid_session(
        self,
        symbol: str,
        entry_price: float,
        distance_pct: float,
        max_waves: int,
        isolated_fund: float,
        tp_pct: float,
        timeout_x_min: float,
        gap_y_min: float,
    ) -> PyramidSession:
        """
        Create a new pyramid DCA session.
        
        Args:
            symbol: Trading pair symbol (e.g., "BTC")
            entry_price: Starting price for wave 0
            distance_pct: Price decrease % per wave
            max_waves: Maximum number of waves
            isolated_fund: Fund allocated for this session
            tp_pct: Take profit % above avg price
            timeout_x_min: Stop if no fill for X minutes
            gap_y_min: Minimum time between fills before timeout applies
        
        Returns:
            Created PyramidSession (status=PENDING)
        """
        session = PyramidSession(
            symbol=symbol,
            entry_price=entry_price,
            distance_pct=distance_pct,
            max_waves=max_waves,
            isolated_fund=isolated_fund,
            tp_pct=tp_pct,
            timeout_x_min=timeout_x_min,
            gap_y_min=gap_y_min,
        )
        
        # Assign ID and register
        session.id = self._next_id
        self._sessions[session.id] = session
        self._next_id += 1
        
        logger.info(
            f"Created pyramid session {session.id}: {symbol} @ {entry_price}, "
            f"max_waves={max_waves}, fund={isolated_fund}"
        )
        
        return session
    
    def start_session(self, session_id: int) -> Optional[Dict[str, Any]]:
        """
        Start a pyramid session by ID.
        
        Args:
            session_id: ID of session to start
        
        Returns:
            Order dict for wave 0, or None if failed
        """
        session = self._sessions.get(session_id)
        if not session:
            logger.error(f"Session {session_id} not found")
            return None
        
        return session.start()
    
    def stop_session(self, session_id: int, reason: str = "manual") -> bool:
        """
        Stop a pyramid session by ID.
        
        Args:
            session_id: ID of session to stop
            reason: Reason for stopping
        
        Returns:
            True if stopped, False if not found
        """
        session = self._sessions.get(session_id)
        if not session:
            logger.error(f"Session {session_id} not found")
            return False
        
        session.stop(reason)
        return True
    
    def get_session(self, session_id: int) -> Optional[PyramidSession]:
        """Get session by ID."""
        return self._sessions.get(session_id)
    
    def get_session_status(self, session_id: int) -> Optional[Dict[str, Any]]:
        """Get session status dict by ID."""
        session = self._sessions.get(session_id)
        if not session:
            return None
        return session.get_status()
    
    def list_sessions(
        self,
        status: Optional[PyramidSessionStatus] = None,
        symbol: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        List sessions with optional filters.
        
        Args:
            status: Filter by session status
            symbol: Filter by symbol
        
        Returns:
            List of session status dicts
        """
        results = []
        for session in self._sessions.values():
            # Apply filters
            if status and session.status != status:
                continue
            if symbol and session.symbol != symbol:
                continue
            
            results.append(session.get_status())
        
        # Sort by created_at desc
        results.sort(key=lambda x: x["created_at"], reverse=True)
        return results
    
    def adjust_session(
        self,
        session_id: int,
        **kwargs,
    ) -> Optional[Dict[str, Any]]:
        """
        Adjust session parameters.
        
        Args:
            session_id: ID of session to adjust
            **kwargs: Parameters to adjust (max_waves, isolated_fund, etc.)
        
        Returns:
            Dict of changes made, or None if session not found
        """
        session = self._sessions.get(session_id)
        if not session:
            logger.error(f"Session {session_id} not found")
            return None
        
        return session.adjust_params(**kwargs)
    
    def on_fill(
        self,
        source_ref: str,
        filled_qty: float,
        filled_price: float,
        current_market_price: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Process a fill event from execution engine.
        
        Routes the fill to the correct session based on source_ref.
        
        Args:
            source_ref: Source reference from order (e.g., "pyramid:1:wave:0")
            filled_qty: Actual filled quantity
            filled_price: Actual fill price
            current_market_price: Current market price for TP check
        
        Returns:
            Action result from session, or None if not a KSS order
        """
        # Parse source_ref: "pyramid:{session_id}:wave:{wave_num}"
        if not source_ref or not source_ref.startswith("pyramid:"):
            return None
        
        try:
            parts = source_ref.split(":")
            if len(parts) < 4:
                return None
            
            session_id = int(parts[1])
            wave_type = parts[2]  # "wave" or "tp"
            
            session = self._sessions.get(session_id)
            if not session:
                logger.warning(f"Session {session_id} not found for fill")
                return None
            
            if wave_type == "wave":
                wave_num = int(parts[3])
                return session.on_fill(
                    wave_num=wave_num,
                    filled_qty=filled_qty,
                    filled_price=filled_price,
                    current_market_price=current_market_price,
                )
            elif wave_type == "tp":
                # TP order filled - session complete
                session.status = PyramidSessionStatus.COMPLETED
                logger.info(f"Pyramid {session_id} TP order filled, session complete")
                return {
                    "action": "completed",
                    "message": f"TP order filled, session {session_id} complete",
                }
            
        except (ValueError, IndexError) as e:
            logger.error(f"Failed to parse source_ref {source_ref}: {e}")
        
        return None
    
    def get_active_sessions_count(self) -> int:
        """Get count of active sessions."""
        return sum(
            1 for s in self._sessions.values() 
            if s.status == PyramidSessionStatus.ACTIVE
        )
    
    def get_total_isolated_fund(self) -> float:
        """Get total isolated fund across active sessions."""
        return sum(
            s.isolated_fund for s in self._sessions.values()
            if s.status == PyramidSessionStatus.ACTIVE
        )
    
    def get_summary(self) -> Dict[str, Any]:
        """Get manager summary for dashboard."""
        active_sessions = [
            s for s in self._sessions.values()
            if s.status == PyramidSessionStatus.ACTIVE
        ]
        
        return {
            "total_sessions": len(self._sessions),
            "active_sessions": len(active_sessions),
            "total_isolated_fund": sum(s.isolated_fund for s in active_sessions),
            "total_used_fund": sum(s.used_fund for s in active_sessions),
            "total_unrealized_pnl": sum(
                s.get_status().get("unrealized_pnl", 0) for s in active_sessions
            ),
        }
    
    def clear_completed(self) -> int:
        """
        Remove completed/stopped sessions from memory.
        
        Returns:
            Number of sessions cleared
        """
        to_remove = [
            sid for sid, s in self._sessions.items()
            if s.status in (
                PyramidSessionStatus.COMPLETED,
                PyramidSessionStatus.STOPPED,
                PyramidSessionStatus.TP_TRIGGERED,
            )
        ]
        
        for sid in to_remove:
            del self._sessions[sid]
        
        if to_remove:
            logger.info(f"Cleared {len(to_remove)} completed sessions")
        
        return len(to_remove)
    
    def reset(self) -> None:
        """Reset manager state (for testing)."""
        self._sessions.clear()
        self._next_id = 1
        logger.info("KSSManager reset")


# Global manager instance
kss_manager = KSSManager()
