"""
Unit tests for KSSManager class.

Tests manager lifecycle:
- Session creation
- Session retrieval
- Fill event routing
- Session listing/filtering
- Summary statistics
"""

import pytest
from unittest.mock import patch, MagicMock

from src.findmy.kss.manager import KSSManager
from src.findmy.kss.pyramid import PyramidSession, PyramidSessionStatus


class TestManagerSingleton:
    """Test KSSManager singleton pattern."""
    
    def test_singleton_returns_same_instance(self):
        """Test that multiple calls return same instance."""
        m1 = KSSManager()
        m2 = KSSManager()
        
        assert m1 is m2
    
    def test_singleton_state_shared(self):
        """Test that state is shared across instances."""
        m1 = KSSManager()
        m1.reset()
        
        session = m1.create_pyramid_session(
            symbol="BTC",
            entry_price=50000.0,
            distance_pct=2.0,
            max_waves=10,
            isolated_fund=1000.0,
            tp_pct=3.0,
            timeout_x_min=30.0,
            gap_y_min=5.0,
        )
        
        m2 = KSSManager()
        assert len(m2._sessions) == 1
        assert session.id in m2._sessions


class TestSessionCreation:
    """Test creating pyramid sessions through manager."""
    
    @pytest.fixture(autouse=True)
    def reset_manager(self):
        """Reset manager before each test."""
        manager = KSSManager()
        manager.reset()
        yield
    
    def test_create_basic_session(self):
        """Test creating a basic session."""
        manager = KSSManager()
        
        session = manager.create_pyramid_session(
            symbol="BTC",
            entry_price=50000.0,
            distance_pct=2.0,
            max_waves=10,
            isolated_fund=1000.0,
            tp_pct=3.0,
            timeout_x_min=30.0,
            gap_y_min=5.0,
        )
        
        assert session is not None
        assert session.id is not None
        assert session.symbol == "BTC"
        assert session in manager._sessions.values()
    
    def test_create_multiple_sessions(self):
        """Test creating multiple sessions."""
        manager = KSSManager()
        
        session1 = manager.create_pyramid_session(
            symbol="BTC",
            entry_price=50000.0,
            distance_pct=2.0,
            max_waves=10,
            isolated_fund=1000.0,
            tp_pct=3.0,
            timeout_x_min=30.0,
            gap_y_min=5.0,
        )
        
        session2 = manager.create_pyramid_session(
            symbol="ETH",
            entry_price=3000.0,
            distance_pct=1.5,
            max_waves=5,
            isolated_fund=500.0,
            tp_pct=2.5,
            timeout_x_min=20.0,
            gap_y_min=3.0,
        )
        
        assert len(manager._sessions) == 2
        assert session1.id != session2.id
    
    def test_create_session_auto_increments_id(self):
        """Test that session IDs auto-increment."""
        manager = KSSManager()
        
        s1 = manager.create_pyramid_session(
            symbol="BTC",
            entry_price=50000.0,
            distance_pct=2.0,
            max_waves=10,
            isolated_fund=1000.0,
            tp_pct=3.0,
            timeout_x_min=30.0,
            gap_y_min=5.0,
        )
        
        s2 = manager.create_pyramid_session(
            symbol="ETH",
            entry_price=3000.0,
            distance_pct=1.5,
            max_waves=5,
            isolated_fund=500.0,
            tp_pct=2.5,
            timeout_x_min=20.0,
            gap_y_min=3.0,
        )
        
        assert s2.id == s1.id + 1


class TestSessionRetrieval:
    """Test retrieving sessions from manager."""
    
    @pytest.fixture(autouse=True)
    def reset_manager(self):
        """Reset manager before each test."""
        manager = KSSManager()
        manager.reset()
        yield
    
    @pytest.fixture
    def sample_session(self):
        """Create a sample session."""
        manager = KSSManager()
        return manager.create_pyramid_session(
            symbol="BTC",
            entry_price=50000.0,
            distance_pct=2.0,
            max_waves=10,
            isolated_fund=1000.0,
            tp_pct=3.0,
            timeout_x_min=30.0,
            gap_y_min=5.0,
        )
    
    def test_get_session_by_id(self, sample_session):
        """Test getting session by ID."""
        manager = KSSManager()
        
        retrieved = manager.get_session(sample_session.id)
        
        assert retrieved is sample_session
    
    def test_get_nonexistent_session_returns_none(self):
        """Test getting nonexistent session returns None."""
        manager = KSSManager()
        
        result = manager.get_session(99999)
        
        assert result is None
    
    def test_delete_session(self, sample_session):
        """Test deleting a session."""
        manager = KSSManager()
        
        result = manager.delete_session(sample_session.id)
        
        assert result is True
        assert sample_session.id not in manager._sessions
    
    def test_delete_nonexistent_session(self):
        """Test deleting nonexistent session returns False."""
        manager = KSSManager()
        
        result = manager.delete_session(99999)
        
        assert result is False


class TestSessionListing:
    """Test listing and filtering sessions."""
    
    @pytest.fixture(autouse=True)
    def reset_manager(self):
        """Reset manager before each test."""
        manager = KSSManager()
        manager.reset()
        yield
    
    @pytest.fixture
    def multiple_sessions(self):
        """Create multiple sessions with different symbols and states."""
        manager = KSSManager()
        
        btc1 = manager.create_pyramid_session(
            symbol="BTC",
            entry_price=50000.0,
            distance_pct=2.0,
            max_waves=10,
            isolated_fund=1000.0,
            tp_pct=3.0,
            timeout_x_min=30.0,
            gap_y_min=5.0,
        )
        btc1.status = PyramidSessionStatus.ACTIVE
        
        btc2 = manager.create_pyramid_session(
            symbol="BTC",
            entry_price=51000.0,
            distance_pct=2.0,
            max_waves=10,
            isolated_fund=1000.0,
            tp_pct=3.0,
            timeout_x_min=30.0,
            gap_y_min=5.0,
        )
        btc2.status = PyramidSessionStatus.PENDING
        
        eth = manager.create_pyramid_session(
            symbol="ETH",
            entry_price=3000.0,
            distance_pct=1.5,
            max_waves=5,
            isolated_fund=500.0,
            tp_pct=2.5,
            timeout_x_min=20.0,
            gap_y_min=3.0,
        )
        eth.status = PyramidSessionStatus.ACTIVE
        
        return {"btc1": btc1, "btc2": btc2, "eth": eth}
    
    def test_list_all_sessions(self, multiple_sessions):
        """Test listing all sessions."""
        manager = KSSManager()
        
        sessions = manager.list_sessions()
        
        assert len(sessions) == 3
    
    def test_filter_by_symbol(self, multiple_sessions):
        """Test filtering by symbol."""
        manager = KSSManager()
        
        btc_sessions = manager.list_sessions(symbol="BTC")
        
        assert len(btc_sessions) == 2
        assert all(s["symbol"] == "BTC" for s in btc_sessions)
    
    def test_filter_by_status(self, multiple_sessions):
        """Test filtering by status."""
        manager = KSSManager()
        
        active = manager.list_sessions(status="active")
        
        assert len(active) == 2
        assert all(s["status"] == "active" for s in active)
    
    def test_filter_by_symbol_and_status(self, multiple_sessions):
        """Test filtering by both symbol and status."""
        manager = KSSManager()
        
        btc_active = manager.list_sessions(symbol="BTC", status="active")
        
        assert len(btc_active) == 1
        assert btc_active[0]["symbol"] == "BTC"
        assert btc_active[0]["status"] == "active"


class TestFillEventRouting:
    """Test routing fill events to correct session."""
    
    @pytest.fixture(autouse=True)
    def reset_manager(self):
        """Reset manager before each test."""
        manager = KSSManager()
        manager.reset()
        yield
    
    @pytest.fixture
    def active_session(self):
        """Create an active session."""
        manager = KSSManager()
        session = manager.create_pyramid_session(
            symbol="BTC",
            entry_price=50000.0,
            distance_pct=2.0,
            max_waves=10,
            isolated_fund=1000.0,
            tp_pct=3.0,
            timeout_x_min=30.0,
            gap_y_min=5.0,
        )
        session.start()
        return session
    
    @patch('src.findmy.kss.pyramid.get_current_prices')
    def test_route_fill_to_correct_session(self, mock_prices, active_session):
        """Test fill event routes to correct session."""
        mock_prices.return_value = {"BTC": 50000.0}
        manager = KSSManager()
        
        source_ref = f"pyramid:{active_session.id}:wave:0"
        result = manager.on_fill(
            source_ref=source_ref,
            filled_qty=0.00002,
            filled_price=50000.0,
            current_market_price=49000.0,
        )
        
        assert result is not None
        assert active_session.total_filled_qty > 0
    
    def test_invalid_source_ref_returns_none(self):
        """Test invalid source_ref returns None."""
        manager = KSSManager()
        
        result = manager.on_fill(
            source_ref="invalid:ref:format",
            filled_qty=0.00002,
            filled_price=50000.0,
        )
        
        assert result is None
    
    def test_nonexistent_session_returns_none(self):
        """Test routing to nonexistent session returns None."""
        manager = KSSManager()
        
        result = manager.on_fill(
            source_ref="pyramid:99999:wave:0",
            filled_qty=0.00002,
            filled_price=50000.0,
        )
        
        assert result is None
    
    @patch('src.findmy.kss.pyramid.get_current_prices')
    def test_route_multiple_fills(self, mock_prices, active_session):
        """Test routing multiple fills to same session."""
        mock_prices.return_value = {"BTC": 49000.0}
        manager = KSSManager()
        
        # First fill
        source_ref = f"pyramid:{active_session.id}:wave:0"
        result1 = manager.on_fill(
            source_ref=source_ref,
            filled_qty=0.00002,
            filled_price=50000.0,
            current_market_price=49000.0,
        )
        
        # Second fill (wave 1)
        if len(active_session.waves) > 1:
            source_ref = f"pyramid:{active_session.id}:wave:1"
            result2 = manager.on_fill(
                source_ref=source_ref,
                filled_qty=0.00004,
                filled_price=49000.0,
                current_market_price=48000.0,
            )
            
            assert active_session.total_filled_qty >= 0.00006


class TestSessionControl:
    """Test starting and stopping sessions through manager."""
    
    @pytest.fixture(autouse=True)
    def reset_manager(self):
        """Reset manager before each test."""
        manager = KSSManager()
        manager.reset()
        yield
    
    @pytest.fixture
    def pending_session(self):
        """Create a pending session."""
        manager = KSSManager()
        return manager.create_pyramid_session(
            symbol="BTC",
            entry_price=50000.0,
            distance_pct=2.0,
            max_waves=10,
            isolated_fund=1000.0,
            tp_pct=3.0,
            timeout_x_min=30.0,
            gap_y_min=5.0,
        )
    
    @patch('src.findmy.kss.pyramid.get_exchange_info')
    def test_start_session(self, mock_exchange, pending_session):
        """Test starting a session through manager."""
        mock_exchange.return_value = {
            "minQty": 0.00001,
            "stepSize": 0.00001,
            "maxQty": 10000.0,
        }
        manager = KSSManager()
        
        result = manager.start_session(pending_session.id)
        
        assert result is not None
        assert pending_session.status == PyramidSessionStatus.ACTIVE
    
    def test_start_nonexistent_session(self):
        """Test starting nonexistent session returns None."""
        manager = KSSManager()
        
        result = manager.start_session(99999)
        
        assert result is None
    
    def test_stop_session(self, pending_session):
        """Test stopping a session through manager."""
        manager = KSSManager()
        pending_session.status = PyramidSessionStatus.ACTIVE
        
        result = manager.stop_session(pending_session.id)
        
        assert result is True
        assert pending_session.status == PyramidSessionStatus.STOPPED
    
    def test_stop_nonexistent_session(self):
        """Test stopping nonexistent session returns False."""
        manager = KSSManager()
        
        result = manager.stop_session(99999)
        
        assert result is False


class TestSummaryStatistics:
    """Test summary statistics generation."""
    
    @pytest.fixture(autouse=True)
    def reset_manager(self):
        """Reset manager before each test."""
        manager = KSSManager()
        manager.reset()
        yield
    
    def test_empty_summary(self):
        """Test summary with no sessions."""
        manager = KSSManager()
        
        summary = manager.get_summary()
        
        assert summary["total_sessions"] == 0
        assert summary["active_sessions"] == 0
        assert summary["pending_sessions"] == 0
        assert summary["completed_sessions"] == 0
        assert summary["total_isolated_fund"] == 0.0
    
    def test_summary_with_sessions(self):
        """Test summary with multiple sessions."""
        manager = KSSManager()
        
        s1 = manager.create_pyramid_session(
            symbol="BTC",
            entry_price=50000.0,
            distance_pct=2.0,
            max_waves=10,
            isolated_fund=1000.0,
            tp_pct=3.0,
            timeout_x_min=30.0,
            gap_y_min=5.0,
        )
        s1.status = PyramidSessionStatus.ACTIVE
        
        s2 = manager.create_pyramid_session(
            symbol="ETH",
            entry_price=3000.0,
            distance_pct=1.5,
            max_waves=5,
            isolated_fund=500.0,
            tp_pct=2.5,
            timeout_x_min=20.0,
            gap_y_min=3.0,
        )
        s2.status = PyramidSessionStatus.PENDING
        
        s3 = manager.create_pyramid_session(
            symbol="SOL",
            entry_price=100.0,
            distance_pct=1.0,
            max_waves=8,
            isolated_fund=200.0,
            tp_pct=2.0,
            timeout_x_min=15.0,
            gap_y_min=2.0,
        )
        s3.status = PyramidSessionStatus.COMPLETED
        
        summary = manager.get_summary()
        
        assert summary["total_sessions"] == 3
        assert summary["active_sessions"] == 1
        assert summary["pending_sessions"] == 1
        assert summary["completed_sessions"] == 1
        assert summary["total_isolated_fund"] == 1700.0
        assert summary["active_isolated_fund"] == 1000.0


class TestClearCompleted:
    """Test clearing completed sessions."""
    
    @pytest.fixture(autouse=True)
    def reset_manager(self):
        """Reset manager before each test."""
        manager = KSSManager()
        manager.reset()
        yield
    
    def test_clear_completed_sessions(self):
        """Test clearing only completed sessions."""
        manager = KSSManager()
        
        # Create 3 sessions with different states
        s1 = manager.create_pyramid_session(
            symbol="BTC",
            entry_price=50000.0,
            distance_pct=2.0,
            max_waves=10,
            isolated_fund=1000.0,
            tp_pct=3.0,
            timeout_x_min=30.0,
            gap_y_min=5.0,
        )
        s1.status = PyramidSessionStatus.COMPLETED
        
        s2 = manager.create_pyramid_session(
            symbol="ETH",
            entry_price=3000.0,
            distance_pct=1.5,
            max_waves=5,
            isolated_fund=500.0,
            tp_pct=2.5,
            timeout_x_min=20.0,
            gap_y_min=3.0,
        )
        s2.status = PyramidSessionStatus.ACTIVE
        
        s3 = manager.create_pyramid_session(
            symbol="SOL",
            entry_price=100.0,
            distance_pct=1.0,
            max_waves=8,
            isolated_fund=200.0,
            tp_pct=2.0,
            timeout_x_min=15.0,
            gap_y_min=2.0,
        )
        s3.status = PyramidSessionStatus.TP_TRIGGERED
        
        cleared = manager.clear_completed()
        
        assert cleared == 2  # COMPLETED + TP_TRIGGERED
        assert len(manager._sessions) == 1
        assert s2.id in manager._sessions
    
    def test_clear_no_completed_sessions(self):
        """Test clearing when no completed sessions."""
        manager = KSSManager()
        
        s1 = manager.create_pyramid_session(
            symbol="BTC",
            entry_price=50000.0,
            distance_pct=2.0,
            max_waves=10,
            isolated_fund=1000.0,
            tp_pct=3.0,
            timeout_x_min=30.0,
            gap_y_min=5.0,
        )
        s1.status = PyramidSessionStatus.ACTIVE
        
        cleared = manager.clear_completed()
        
        assert cleared == 0
        assert len(manager._sessions) == 1
