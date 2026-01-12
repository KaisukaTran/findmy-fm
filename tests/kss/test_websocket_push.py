"""
Tests for KSS WebSocket Push Notifications.

Verifies:
- Dashboard updates on fill events
- Dashboard updates on TP trigger
- Dashboard updates on timeout
- Mock WebSocket client behavior
"""

import pytest
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock, AsyncMock
import asyncio

from src.findmy.kss.pyramid import PyramidSession, PyramidSessionStatus
from src.findmy.kss.manager import KSSManager


class TestWebSocketPush:
    """Test WebSocket push notification scenarios."""
    
    @pytest.fixture
    def session(self):
        """Create a test session."""
        return PyramidSession(
            symbol="BTC",
            entry_price=50000.0,
            distance_pct=2.0,
            max_waves=10,
            isolated_fund=1000.0,
            tp_pct=3.0,
            timeout_x_min=30.0,
            gap_y_min=5.0,
        )
    
    @pytest.fixture
    def mock_ws_manager(self):
        """Create mock WebSocket connection manager."""
        manager = MagicMock()
        manager.broadcast = AsyncMock()
        manager.send_to_session = AsyncMock()
        return manager
    
    def test_fill_event_generates_update(self, session):
        """Test fill event generates dashboard update data."""
        session.id = 1
        session.start()
        wave = session.waves[0]
        
        with patch.object(session, '_check_timeout', return_value=False):
            with patch('src.findmy.kss.pyramid.get_current_prices', return_value={"BTC": 48000.0}):
                result = session.on_fill(0, wave.quantity, wave.target_price)
        
        # Result contains data for dashboard update
        assert result is not None
        assert "action" in result
        assert "message" in result
        
        # Session state can be serialized for WS
        status = session.get_status()
        assert "id" in status
        assert "symbol" in status
        assert "status" in status
        assert "avg_price" in status
        assert "total_filled_qty" in status
    
    def test_tp_trigger_generates_update(self, session):
        """Test TP trigger generates dashboard update."""
        session.id = 1
        session.start()
        
        # Simulate having filled position
        session.total_filled_qty = 0.001
        session.avg_price = 50000.0
        session.total_cost = 50.0
        session.status = PyramidSessionStatus.ACTIVE
        
        # Trigger TP with high price
        result = session.check_tp(current_market_price=55000.0)
        
        assert result is not None
        assert result["action"] == "tp_triggered"
        
        # Session status updated for dashboard
        assert session.status == PyramidSessionStatus.TP_TRIGGERED
        status = session.get_status()
        assert status["status"] == "tp_triggered"
    
    def test_timeout_status_for_dashboard(self, session):
        """Test timeout generates dashboard-ready status."""
        session.id = 1
        session.start()
        
        # Simulate old fill time (beyond timeout)
        session.last_fill_time = datetime.utcnow() - timedelta(minutes=60)
        session.start_time = datetime.utcnow() - timedelta(minutes=90)
        
        # Fill should trigger timeout check
        wave = session.waves[0]
        with patch('src.findmy.kss.pyramid.get_current_prices', return_value={"BTC": 48000.0}):
            result = session.on_fill(0, wave.quantity, wave.target_price)
        
        # Result indicates status change
        status = session.get_status()
        assert "status" in status
    
    def test_status_contains_ws_data(self, session):
        """Test get_status returns all data needed for WebSocket push."""
        session.id = 1
        session.start()
        
        status = session.get_status()
        
        # Required fields for dashboard WebSocket update
        required_fields = [
            "id", "symbol", "status", "entry_price",
            "distance_pct", "max_waves", "isolated_fund",
            "tp_pct", "current_wave", "filled_waves_count",
            "pending_waves_count", "total_filled_qty",
            "avg_price", "total_cost", "used_fund",
            "remaining_fund", "estimated_tp_price",
            "unrealized_pnl", "waves"
        ]
        
        for field in required_fields:
            assert field in status, f"Missing field: {field}"
    
    def test_wave_status_for_color_coding(self, session):
        """Test wave status enables color coding in dashboard."""
        session.id = 1
        session.start()
        
        # Initial wave is sent/pending
        status = session.get_status()
        assert len(status["waves"]) == 1
        assert status["waves"][0]["status"] == "sent"
        
        # After fill, status changes
        wave = session.waves[0]
        with patch.object(session, '_check_timeout', return_value=False):
            with patch('src.findmy.kss.pyramid.get_current_prices', return_value={"BTC": 48000.0}):
                session.on_fill(0, wave.quantity, wave.target_price)
        
        status = session.get_status()
        assert status["waves"][0]["status"] == "filled"
    
    def test_pnl_calculation_for_dashboard(self, session):
        """Test unrealized PnL calculation for dashboard display."""
        session.id = 1
        session.start()
        wave = session.waves[0]
        
        with patch.object(session, '_check_timeout', return_value=False):
            with patch('src.findmy.kss.pyramid.get_current_prices', return_value={"BTC": 52000.0}):
                session.on_fill(0, wave.quantity, wave.target_price)
        
        status = session.get_status()
        
        # PnL fields for dashboard
        assert "unrealized_pnl" in status
        assert "unrealized_pnl_pct" in status
        assert "current_price" in status
    
    def test_multiple_sessions_update(self):
        """Test multiple sessions can each generate updates."""
        manager = KSSManager()
        manager._sessions.clear()
        manager._next_id = 1
        
        # Create multiple sessions
        sessions = []
        for i in range(3):
            s = manager.create_pyramid_session(
                symbol=f"COIN{i}",
                entry_price=100.0 * (i + 1),
                distance_pct=2.0,
                max_waves=5,
                isolated_fund=500.0,
                tp_pct=3.0,
                timeout_x_min=30.0,
                gap_y_min=5.0,
            )
            sessions.append(s)
        
        # Each session can generate status for WS
        for s in sessions:
            s.start()
            status = s.get_status()
            assert status["id"] is not None
            assert status["symbol"].startswith("COIN")
    
    def test_status_json_serializable(self, session):
        """Test status dict is JSON serializable for WebSocket."""
        import json
        
        session.id = 1
        session.start()
        
        status = session.get_status()
        
        # Should not raise
        json_str = json.dumps(status)
        assert json_str is not None
        
        # Round-trip should preserve data
        decoded = json.loads(json_str)
        assert decoded["id"] == status["id"]
        assert decoded["symbol"] == status["symbol"]


class TestWebSocketEventTypes:
    """Test different event types for WebSocket."""
    
    def test_fill_event_type(self):
        """Test fill event can be typed for WS handler."""
        session = PyramidSession(
            symbol="BTC",
            entry_price=50000.0,
            distance_pct=2.0,
            max_waves=5,
            isolated_fund=1000.0,
            tp_pct=3.0,
            timeout_x_min=30.0,
            gap_y_min=5.0,
        )
        session.id = 1
        session.start()
        
        wave = session.waves[0]
        with patch.object(session, '_check_timeout', return_value=False):
            with patch('src.findmy.kss.pyramid.get_current_prices', return_value={"BTC": 48000.0}):
                result = session.on_fill(0, wave.quantity, wave.target_price)
        
        # Construct WS message
        ws_message = {
            "type": "kss_update",
            "action": result.get("action"),
            "session_id": session.id,
            "data": session.get_status(),
        }
        
        assert ws_message["type"] == "kss_update"
        assert ws_message["action"] in ["next_wave", "none", "stopped", "completed", "tp_triggered"]
    
    def test_tp_event_type(self):
        """Test TP event can be typed for WS handler."""
        session = PyramidSession(
            symbol="BTC",
            entry_price=50000.0,
            distance_pct=2.0,
            max_waves=5,
            isolated_fund=1000.0,
            tp_pct=3.0,
            timeout_x_min=30.0,
            gap_y_min=5.0,
        )
        session.id = 1
        session.total_filled_qty = 0.001
        session.avg_price = 50000.0
        session.status = PyramidSessionStatus.ACTIVE
        
        result = session.check_tp(55000.0)
        
        ws_message = {
            "type": "kss_tp_triggered",
            "session_id": session.id,
            "tp_order": result.get("order") if result else None,
            "data": session.get_status(),
        }
        
        assert ws_message["type"] == "kss_tp_triggered"
        assert ws_message["tp_order"] is not None
    
    def test_stop_event_type(self):
        """Test stop event can be typed for WS handler."""
        session = PyramidSession(
            symbol="BTC",
            entry_price=50000.0,
            distance_pct=2.0,
            max_waves=5,
            isolated_fund=1000.0,
            tp_pct=3.0,
            timeout_x_min=30.0,
            gap_y_min=5.0,
        )
        session.id = 1
        session.start()
        session.stop("manual")
        
        ws_message = {
            "type": "kss_stopped",
            "session_id": session.id,
            "reason": "manual",
            "data": session.get_status(),
        }
        
        assert ws_message["type"] == "kss_stopped"
        assert ws_message["data"]["status"] == "stopped"
