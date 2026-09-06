"""
Tests for Session State Recovery.

Verifies:
- Session state loads correctly after restart
- Wave history preserved
- Fill data preserved
- Status correctly restored
"""

import pytest
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

from src.findmy.kss.pyramid import PyramidSession, PyramidSessionStatus, WaveInfo
from src.findmy.kss.manager import KSSManager


class TestRestartRecovery:
    """Test session recovery after restart."""
    
    def test_session_to_dict_complete(self):
        """Test session can be fully serialized to dict."""
        session = PyramidSession(
            symbol="BTC",
            entry_price=50000.0,
            distance_pct=2.0,
            max_waves=10,
            isolated_fund=1000.0,
            tp_pct=3.0,
            timeout_x_min=30.0,
            gap_y_min=5.0,
        )
        
        session.id = 42
        session.start()
        
        # Simulate some fills
        wave = session.waves[0]
        with patch.object(session, '_check_timeout', return_value=False):
            with patch('src.findmy.kss.pyramid.get_current_prices', return_value={"BTC": 48000.0}):
                session.on_fill(0, wave.quantity, wave.target_price)
        
        status = session.get_status()
        
        # Should have all fields needed for recovery
        assert "id" in status
        assert "symbol" in status
        assert "status" in status
        assert "entry_price" in status
        assert "distance_pct" in status
        assert "max_waves" in status
        assert "isolated_fund" in status
        assert "tp_pct" in status
        assert "current_wave" in status
        assert "total_filled_qty" in status
        assert "avg_price" in status
        assert "total_cost" in status
        assert "waves" in status
    
    def test_wave_info_serialization(self):
        """Test WaveInfo can be serialized and recovered."""
        wave = WaveInfo(
            wave_num=5,
            quantity=0.001,
            target_price=48000.0,
            status="filled",
            filled_qty=0.001,
            filled_price=47950.0,
            filled_time=datetime(2026, 1, 12, 10, 30, 0),
            pending_order_id=123,
        )
        
        d = wave.to_dict()
        
        # All fields present
        assert d["wave_num"] == 5
        assert d["quantity"] == 0.001
        assert d["target_price"] == 48000.0
        assert d["status"] == "filled"
        assert d["filled_qty"] == 0.001
        assert d["filled_price"] == 47950.0
        assert d["pending_order_id"] == 123
        assert d["filled_time"] is not None
    
    def test_session_state_restoration(self):
        """Test session can be recreated from saved state."""
        # Create and run original session
        original = PyramidSession(
            symbol="BTC",
            entry_price=50000.0,
            distance_pct=2.0,
            max_waves=10,
            isolated_fund=1000.0,
            tp_pct=3.0,
            timeout_x_min=30.0,
            gap_y_min=5.0,
        )
        
        original.id = 1
        original.start()
        
        wave = original.waves[0]
        with patch.object(original, '_check_timeout', return_value=False):
            with patch('src.findmy.kss.pyramid.get_current_prices', return_value={"BTC": 48000.0}):
                original.on_fill(0, wave.quantity, wave.target_price)
        
        # Save state
        saved_state = original.get_status()
        
        # "Restart" - create new session with same params
        restored = PyramidSession(
            symbol=saved_state["symbol"],
            entry_price=saved_state["entry_price"],
            distance_pct=saved_state["distance_pct"],
            max_waves=saved_state["max_waves"],
            isolated_fund=saved_state["isolated_fund"],
            tp_pct=saved_state["tp_pct"],
            timeout_x_min=saved_state["timeout_x_min"],
            gap_y_min=saved_state["gap_y_min"],
        )
        
        # Restore state
        restored.id = saved_state["id"]
        restored.current_wave = saved_state["current_wave"]
        restored.total_filled_qty = saved_state["total_filled_qty"]
        restored.avg_price = saved_state["avg_price"]
        restored.total_cost = saved_state["total_cost"]
        restored.status = PyramidSessionStatus(saved_state["status"])
        
        # Restore waves
        for wave_data in saved_state["waves"]:
            wave = WaveInfo(
                wave_num=wave_data["wave_num"],
                quantity=wave_data["quantity"],
                target_price=wave_data["target_price"],
                status=wave_data["status"],
                filled_qty=wave_data.get("filled_qty", 0),
                filled_price=wave_data.get("filled_price", 0),
            )
            restored.waves.append(wave)
        
        # Verify restoration
        assert restored.id == original.id
        assert restored.symbol == original.symbol
        assert restored.status == original.status
        assert restored.current_wave == original.current_wave
        assert restored.total_filled_qty == original.total_filled_qty
        assert abs(restored.avg_price - original.avg_price) < 0.01
        assert len(restored.waves) == len(original.waves)
    
    def test_pending_session_recovery(self):
        """Test pending session can be recovered."""
        session = PyramidSession(
            symbol="ETH",
            entry_price=3000.0,
            distance_pct=3.0,
            max_waves=5,
            isolated_fund=500.0,
            tp_pct=5.0,
            timeout_x_min=20.0,
            gap_y_min=3.0,
        )
        
        session.id = 10
        # Don't start - stays pending
        
        status = session.get_status()
        
        assert status["status"] == "pending"
        assert status["current_wave"] == 0
        assert status["total_filled_qty"] == 0
        assert len(status["waves"]) == 0
    
    def test_active_session_recovery(self):
        """Test active session with partial fills can be recovered."""
        session = PyramidSession(
            symbol="BTC",
            entry_price=50000.0,
            distance_pct=2.0,
            max_waves=10,
            isolated_fund=1000.0,
            tp_pct=3.0,
            timeout_x_min=30.0,
            gap_y_min=5.0,
        )
        
        session.id = 1
        session.start()
        
        # Fill first 3 waves
        for i in range(3):
            if i < len(session.waves):
                wave = session.waves[i]
            else:
                wave = session.generate_wave(i)
                session.waves.append(wave)
            
            with patch.object(session, '_check_timeout', return_value=False):
                with patch('src.findmy.kss.pyramid.get_current_prices', return_value={"BTC": 48000.0}):
                    result = session.on_fill(i, wave.quantity, wave.target_price)
                    if result.get("action") == "next_wave":
                        next_wave = session.generate_wave(i + 1)
                        next_wave.status = "sent"
                        session.waves.append(next_wave)
        
        status = session.get_status()
        
        # Should show active with partial progress
        assert status["status"] == "active"
        assert status["filled_waves_count"] >= 3
        assert status["total_filled_qty"] > 0
        assert status["avg_price"] > 0
    
    def test_tp_triggered_session_recovery(self):
        """Test TP-triggered session state preserved."""
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
        session.status = PyramidSessionStatus.ACTIVE
        session.total_filled_qty = 0.01
        session.avg_price = 49000.0
        session.total_cost = 490.0
        
        # Trigger TP
        session.check_tp(current_market_price=55000.0)
        
        status = session.get_status()
        
        assert status["status"] == "tp_triggered"
        assert status["total_filled_qty"] == 0.01
        assert status["avg_price"] == 49000.0
    
    def test_stopped_session_recovery(self):
        """Test stopped session state preserved."""
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
                session.on_fill(0, wave.quantity, wave.target_price)
        
        session.stop("manual_stop")
        
        status = session.get_status()
        
        assert status["status"] == "stopped"
        # Filled data preserved
        assert status["total_filled_qty"] > 0


class TestManagerRecovery:
    """Test manager-level recovery."""
    
    @pytest.fixture
    def fresh_manager(self):
        """Create fresh manager."""
        KSSManager._instance = None
        manager = KSSManager()
        manager._sessions.clear()
        manager._next_id = 1
        return manager
    
    def test_manager_can_reload_sessions(self, fresh_manager):
        """Test manager can reload sessions from saved states."""
        # Create and save some sessions
        saved_states = []
        
        for i in range(3):
            s = fresh_manager.create_pyramid_session(
                symbol=f"SYM{i}",
                entry_price=100.0 * (i + 1),
                distance_pct=2.0,
                max_waves=5,
                isolated_fund=500.0,
                tp_pct=3.0,
                timeout_x_min=30.0,
                gap_y_min=5.0,
            )
            s.start()
            saved_states.append(s.get_status())
        
        # "Restart" - clear manager
        fresh_manager._sessions.clear()
        
        assert len(fresh_manager._sessions) == 0
        
        # Reload from saved states
        for state in saved_states:
            session = PyramidSession(
                symbol=state["symbol"],
                entry_price=state["entry_price"],
                distance_pct=state["distance_pct"],
                max_waves=state["max_waves"],
                isolated_fund=state["isolated_fund"],
                tp_pct=state["tp_pct"],
                timeout_x_min=state["timeout_x_min"],
                gap_y_min=state["gap_y_min"],
            )
            session.id = state["id"]
            session.status = PyramidSessionStatus(state["status"])
            fresh_manager._sessions[session.id] = session
        
        # Verify reloaded
        assert len(fresh_manager._sessions) == 3
        for state in saved_states:
            found = fresh_manager.get_session(state["id"])
            assert found is not None
            assert found.symbol == state["symbol"]
    
    def test_next_id_recovery(self, fresh_manager):
        """Test next_id properly set after recovery."""
        # Create sessions
        for i in range(5):
            fresh_manager.create_pyramid_session(
                symbol=f"SYM{i}",
                entry_price=100.0,
                distance_pct=2.0,
                max_waves=5,
                isolated_fund=500.0,
                tp_pct=3.0,
                timeout_x_min=30.0,
                gap_y_min=5.0,
            )
        
        # After 5 sessions, next_id should be 6
        assert fresh_manager._next_id == 6
        
        # Create another
        new_session = fresh_manager.create_pyramid_session(
            symbol="NEW",
            entry_price=100.0,
            distance_pct=2.0,
            max_waves=5,
            isolated_fund=500.0,
            tp_pct=3.0,
            timeout_x_min=30.0,
            gap_y_min=5.0,
        )
        
        assert new_session.id == 6
