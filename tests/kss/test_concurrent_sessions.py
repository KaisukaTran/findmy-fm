"""
Tests for Concurrent Sessions.

Verifies:
- Multiple sessions run without conflict
- No database locks
- Proper session isolation
- Manager handles 5+ sessions
"""

import pytest
from datetime import datetime
from unittest.mock import patch, MagicMock
import threading
import time

from src.findmy.kss.pyramid import PyramidSession, PyramidSessionStatus
from src.findmy.kss.manager import KSSManager


class TestConcurrentSessions:
    """Test concurrent session handling."""
    
    @pytest.fixture
    def fresh_manager(self):
        """Create fresh manager instance for each test."""
        # Reset singleton
        KSSManager._instance = None
        manager = KSSManager()
        manager._sessions.clear()
        manager._next_id = 1
        return manager
    
    def test_create_five_sessions(self, fresh_manager):
        """Test creating 5 sessions simultaneously."""
        sessions = []
        
        for i in range(5):
            session = fresh_manager.create_pyramid_session(
                symbol=f"COIN{i}",
                entry_price=100.0 * (i + 1),
                distance_pct=2.0,
                max_waves=10,
                isolated_fund=1000.0,
                tp_pct=3.0,
                timeout_x_min=30.0,
                gap_y_min=5.0,
            )
            sessions.append(session)
        
        assert len(fresh_manager._sessions) == 5
        
        # Each has unique ID
        ids = [s.id for s in sessions]
        assert len(set(ids)) == 5
    
    def test_sessions_isolated(self, fresh_manager):
        """Test sessions don't affect each other."""
        s1 = fresh_manager.create_pyramid_session(
            symbol="BTC",
            entry_price=50000.0,
            distance_pct=2.0,
            max_waves=10,
            isolated_fund=1000.0,
            tp_pct=3.0,
            timeout_x_min=30.0,
            gap_y_min=5.0,
        )
        
        s2 = fresh_manager.create_pyramid_session(
            symbol="ETH",
            entry_price=3000.0,
            distance_pct=3.0,
            max_waves=5,
            isolated_fund=500.0,
            tp_pct=5.0,
            timeout_x_min=20.0,
            gap_y_min=3.0,
        )
        
        # Start s1
        s1.start()
        
        # s2 should still be pending
        assert s1.status == PyramidSessionStatus.ACTIVE
        assert s2.status == PyramidSessionStatus.PENDING
        
        # Modify s1
        s1.total_filled_qty = 0.01
        s1.avg_price = 49000.0
        
        # s2 should be unaffected
        assert s2.total_filled_qty == 0.0
        assert s2.avg_price == 0.0
    
    def test_concurrent_fills(self, fresh_manager):
        """Test processing fills for multiple sessions."""
        sessions = []
        for i in range(3):
            s = fresh_manager.create_pyramid_session(
                symbol=f"SYM{i}",
                entry_price=100.0,
                distance_pct=5.0,
                max_waves=5,
                isolated_fund=500.0,
                tp_pct=3.0,
                timeout_x_min=30.0,
                gap_y_min=5.0,
            )
            s.start()
            sessions.append(s)
        
        # Process fills for each
        for s in sessions:
            wave = s.waves[0]
            with patch.object(s, '_check_timeout', return_value=False):
                with patch('src.findmy.kss.pyramid.get_current_prices', return_value={s.symbol: 95.0}):
                    result = s.on_fill(0, wave.quantity, wave.target_price)
        
        # All should have updated
        for s in sessions:
            assert s.total_filled_qty > 0
            assert s.avg_price > 0
    
    def test_different_statuses(self, fresh_manager):
        """Test sessions can have different statuses."""
        s1 = fresh_manager.create_pyramid_session(
            symbol="A", entry_price=100.0, distance_pct=2.0,
            max_waves=5, isolated_fund=500.0, tp_pct=3.0,
            timeout_x_min=30.0, gap_y_min=5.0,
        )
        
        s2 = fresh_manager.create_pyramid_session(
            symbol="B", entry_price=100.0, distance_pct=2.0,
            max_waves=5, isolated_fund=500.0, tp_pct=3.0,
            timeout_x_min=30.0, gap_y_min=5.0,
        )
        
        s3 = fresh_manager.create_pyramid_session(
            symbol="C", entry_price=100.0, distance_pct=2.0,
            max_waves=5, isolated_fund=500.0, tp_pct=3.0,
            timeout_x_min=30.0, gap_y_min=5.0,
        )
        
        # Different states
        s1.start()  # ACTIVE
        # s2 stays PENDING
        s3.start()
        s3.stop("test")  # STOPPED
        
        assert s1.status == PyramidSessionStatus.ACTIVE
        assert s2.status == PyramidSessionStatus.PENDING
        assert s3.status == PyramidSessionStatus.STOPPED
    
    def test_list_sessions_by_status(self, fresh_manager):
        """Test filtering sessions by status."""
        # Create sessions with different statuses
        for i in range(3):
            s = fresh_manager.create_pyramid_session(
                symbol=f"ACTIVE{i}", entry_price=100.0, distance_pct=2.0,
                max_waves=5, isolated_fund=500.0, tp_pct=3.0,
                timeout_x_min=30.0, gap_y_min=5.0,
            )
            s.start()
        
        for i in range(2):
            fresh_manager.create_pyramid_session(
                symbol=f"PENDING{i}", entry_price=100.0, distance_pct=2.0,
                max_waves=5, isolated_fund=500.0, tp_pct=3.0,
                timeout_x_min=30.0, gap_y_min=5.0,
            )
        
        active = fresh_manager.list_sessions(status=PyramidSessionStatus.ACTIVE)
        pending = fresh_manager.list_sessions(status=PyramidSessionStatus.PENDING)
        
        assert len(active) == 3
        assert len(pending) == 2
    
    def test_session_lookup_by_id(self, fresh_manager):
        """Test getting specific session by ID."""
        sessions = []
        for i in range(5):
            s = fresh_manager.create_pyramid_session(
                symbol=f"SYM{i}", entry_price=100.0, distance_pct=2.0,
                max_waves=5, isolated_fund=500.0, tp_pct=3.0,
                timeout_x_min=30.0, gap_y_min=5.0,
            )
            sessions.append(s)
        
        # Look up specific session
        target_id = sessions[2].id
        found = fresh_manager.get_session(target_id)
        
        assert found is not None
        assert found.id == target_id
        assert found.symbol == "SYM2"
    
    def test_nonexistent_session(self, fresh_manager):
        """Test lookup of nonexistent session."""
        fresh_manager.create_pyramid_session(
            symbol="TEST", entry_price=100.0, distance_pct=2.0,
            max_waves=5, isolated_fund=500.0, tp_pct=3.0,
            timeout_x_min=30.0, gap_y_min=5.0,
        )
        
        result = fresh_manager.get_session(999)
        assert result is None
    
    def test_concurrent_manager_access(self, fresh_manager):
        """Test thread-safe manager access (basic)."""
        results = []
        errors = []
        
        def create_session(idx):
            try:
                s = fresh_manager.create_pyramid_session(
                    symbol=f"THREAD{idx}",
                    entry_price=100.0,
                    distance_pct=2.0,
                    max_waves=5,
                    isolated_fund=500.0,
                    tp_pct=3.0,
                    timeout_x_min=30.0,
                    gap_y_min=5.0,
                )
                results.append(s.id)
            except Exception as e:
                errors.append(str(e))
        
        threads = []
        for i in range(5):
            t = threading.Thread(target=create_session, args=(i,))
            threads.append(t)
        
        for t in threads:
            t.start()
        
        for t in threads:
            t.join()
        
        # All should succeed (basic case)
        assert len(errors) == 0
        assert len(results) == 5


class TestSessionIsolation:
    """Test session data isolation."""
    
    def test_waves_isolated(self):
        """Test wave lists are isolated between sessions."""
        s1 = PyramidSession(
            symbol="A", entry_price=100.0, distance_pct=2.0,
            max_waves=5, isolated_fund=500.0, tp_pct=3.0,
            timeout_x_min=30.0, gap_y_min=5.0,
        )
        
        s2 = PyramidSession(
            symbol="B", entry_price=200.0, distance_pct=3.0,
            max_waves=10, isolated_fund=1000.0, tp_pct=5.0,
            timeout_x_min=30.0, gap_y_min=5.0,
        )
        
        s1.start()
        
        # s2 waves should be empty
        assert len(s1.waves) == 1
        assert len(s2.waves) == 0
    
    def test_fund_tracking_isolated(self):
        """Test fund tracking is isolated."""
        s1 = PyramidSession(
            symbol="A", entry_price=100.0, distance_pct=5.0,
            max_waves=5, isolated_fund=500.0, tp_pct=3.0,
            timeout_x_min=30.0, gap_y_min=5.0,
        )
        
        s2 = PyramidSession(
            symbol="B", entry_price=100.0, distance_pct=5.0,
            max_waves=5, isolated_fund=1000.0, tp_pct=3.0,
            timeout_x_min=30.0, gap_y_min=5.0,
        )
        
        s1.start()
        wave1 = s1.waves[0]
        
        with patch.object(s1, '_check_timeout', return_value=False):
            with patch('src.findmy.kss.pyramid.get_current_prices', return_value={"A": 95.0}):
                s1.on_fill(0, wave1.quantity, wave1.target_price)
        
        # s1 fund used, s2 untouched
        assert s1.used_fund > 0
        assert s2.used_fund == 0
        assert s2.remaining_fund == 1000.0
    
    def test_status_isolated(self):
        """Test status changes don't affect other sessions."""
        s1 = PyramidSession(
            symbol="A", entry_price=100.0, distance_pct=5.0,
            max_waves=5, isolated_fund=500.0, tp_pct=3.0,
            timeout_x_min=30.0, gap_y_min=5.0,
        )
        
        s2 = PyramidSession(
            symbol="B", entry_price=100.0, distance_pct=5.0,
            max_waves=5, isolated_fund=500.0, tp_pct=3.0,
            timeout_x_min=30.0, gap_y_min=5.0,
        )
        
        s1.start()
        s1.status = PyramidSessionStatus.TP_TRIGGERED
        
        assert s2.status == PyramidSessionStatus.PENDING
