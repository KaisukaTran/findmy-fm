"""
Tests for KSS (Kai Strategy Service) - Pyramid DCA Strategy.

Tests cover:
- PyramidSession class functionality
- Wave generation and calculations
- Fill event handling
- Take profit logic
- Timeout logic
- Parameter adjustment
- API endpoints
"""

import pytest
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

from src.findmy.kss.pyramid import (
    PyramidSession,
    PyramidSessionStatus,
    WaveInfo,
)
from src.findmy.kss.manager import KSSManager


class TestPyramidSession:
    """Tests for PyramidSession class."""
    
    @pytest.fixture
    def session(self):
        """Create a basic test session."""
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
    
    def test_init_valid_params(self, session):
        """Test session initialization with valid parameters."""
        assert session.symbol == "BTC"
        assert session.entry_price == 50000.0
        assert session.distance_pct == 2.0
        assert session.max_waves == 10
        assert session.isolated_fund == 1000.0
        assert session.tp_pct == 3.0
        assert session.status == PyramidSessionStatus.PENDING
        assert session.current_wave == 0
        assert session.avg_price == 0.0
        assert session.total_filled_qty == 0.0
    
    def test_init_invalid_entry_price(self):
        """Test that negative entry price raises error."""
        with pytest.raises(ValueError, match="Entry price must be positive"):
            PyramidSession(
                symbol="BTC",
                entry_price=-100.0,
                distance_pct=2.0,
                max_waves=10,
                isolated_fund=1000.0,
                tp_pct=3.0,
                timeout_x_min=30.0,
                gap_y_min=5.0,
            )
    
    def test_init_invalid_distance_pct(self):
        """Test that invalid distance_pct raises error."""
        with pytest.raises(ValueError, match="Distance must be"):
            PyramidSession(
                symbol="BTC",
                entry_price=50000.0,
                distance_pct=150.0,  # Invalid: > 100%
                max_waves=10,
                isolated_fund=1000.0,
                tp_pct=3.0,
                timeout_x_min=30.0,
                gap_y_min=5.0,
            )
    
    def test_init_invalid_max_waves(self):
        """Test that zero max_waves raises error."""
        with pytest.raises(ValueError, match="Max waves must be"):
            PyramidSession(
                symbol="BTC",
                entry_price=50000.0,
                distance_pct=2.0,
                max_waves=0,
                isolated_fund=1000.0,
                tp_pct=3.0,
                timeout_x_min=30.0,
                gap_y_min=5.0,
            )
    
    def test_pip_size(self, session):
        """Test pip size calculation."""
        # pip_size = pip_multiplier × minQty
        # Default: 2.0 × 0.00001 = 0.00002
        assert session.pip_size > 0
    
    def test_generate_wave_0(self, session):
        """Test wave 0 generation."""
        wave = session.generate_wave(0)
        
        assert wave.wave_num == 0
        assert wave.quantity > 0  # 1 pip
        assert wave.target_price == session.entry_price
        assert wave.status == "pending"
    
    def test_generate_wave_1(self, session):
        """Test wave 1 generation with price decrease."""
        wave = session.generate_wave(1)
        
        assert wave.wave_num == 1
        # Wave 1 qty should be 2 pips (wave_num + 1)
        assert wave.quantity > session.generate_wave(0).quantity
        # Price should decrease by distance_pct
        expected_price = session.entry_price * (1 - session.distance_pct / 100)
        assert abs(wave.target_price - expected_price) < 0.01
    
    def test_generate_wave_5(self, session):
        """Test wave 5 generation."""
        wave = session.generate_wave(5)
        
        assert wave.wave_num == 5
        # Wave 5 qty = 6 pips
        assert wave.quantity > session.generate_wave(0).quantity * 5
        # Price = entry × (1 - 2%)^5
        factor = (1 - session.distance_pct / 100) ** 5
        expected_price = session.entry_price * factor
        assert abs(wave.target_price - expected_price) < 1.0
    
    def test_estimate_total_cost(self, session):
        """Test total cost estimation."""
        cost = session.estimate_total_cost(5)
        assert cost > 0
        
        # Cost for all waves
        full_cost = session.estimate_total_cost()
        assert full_cost >= cost
    
    @patch('src.findmy.kss.pyramid.get_exchange_info')
    def test_start_session(self, mock_exchange_info, session):
        """Test starting a session."""
        mock_exchange_info.return_value = {
            "minQty": 0.00001,
            "stepSize": 0.00001,
            "maxQty": 10000.0,
        }
        
        order = session.start()
        
        assert session.status == PyramidSessionStatus.ACTIVE
        assert session.start_time is not None
        assert session.current_wave == 0
        assert len(session.waves) == 1
        assert order is not None
        assert order["symbol"] == "BTC"
        assert order["side"] == "BUY"
    
    def test_start_already_started(self, session):
        """Test that starting an already started session returns None."""
        session.status = PyramidSessionStatus.ACTIVE
        order = session.start()
        assert order is None
    
    @patch('src.findmy.kss.pyramid.get_current_prices')
    def test_on_fill_updates_state(self, mock_prices, session):
        """Test that on_fill updates session state correctly."""
        mock_prices.return_value = {"BTC": 50000.0}
        
        # Start session first
        session.start()
        
        # Simulate fill
        result = session.on_fill(
            wave_num=0,
            filled_qty=0.00002,
            filled_price=50000.0,
            current_market_price=50000.0,
        )
        
        assert session.total_filled_qty == 0.00002
        assert abs(session.avg_price - 50000.0) < 0.01  # Float tolerance
        assert session.total_cost == 0.00002 * 50000.0
        assert session.last_fill_time is not None
    
    @patch('src.findmy.kss.pyramid.get_current_prices')
    def test_on_fill_generates_next_wave(self, mock_prices, session):
        """Test that on_fill generates next wave when conditions are met."""
        mock_prices.return_value = {"BTC": 49000.0}  # Below TP
        
        session.start()
        
        result = session.on_fill(
            wave_num=0,
            filled_qty=0.00002,
            filled_price=50000.0,
            current_market_price=49000.0,
        )
        
        assert result["action"] == "next_wave"
        assert result["order"] is not None
        assert len(session.waves) == 2  # Wave 0 + Wave 1
    
    @patch('src.findmy.kss.pyramid.get_current_prices')
    def test_check_tp_triggers(self, mock_prices, session):
        """Test TP triggers when price exceeds threshold."""
        mock_prices.return_value = {"BTC": 52000.0}
        
        session.start()
        
        # Simulate a fill to have position
        session.on_fill(
            wave_num=0,
            filled_qty=0.00002,
            filled_price=50000.0,
            current_market_price=50000.0,
        )
        
        # TP threshold = 50000 × 1.03 = 51500
        # Price 52000 > 51500, should trigger
        result = session.check_tp(52000.0)
        
        assert result is not None
        assert result["action"] == "tp_triggered"
        assert result["order"]["side"] == "SELL"
        assert session.status == PyramidSessionStatus.TP_TRIGGERED
    
    @patch('src.findmy.kss.pyramid.get_current_prices')
    def test_check_tp_not_triggered(self, mock_prices, session):
        """Test TP does not trigger when price is below threshold."""
        mock_prices.return_value = {"BTC": 50500.0}
        
        session.start()
        session.on_fill(
            wave_num=0,
            filled_qty=0.00002,
            filled_price=50000.0,
            current_market_price=50000.0,
        )
        
        # TP threshold = 50000 × 1.03 = 51500
        # Price 50500 < 51500, should not trigger
        result = session.check_tp(50500.0)
        
        assert result is None
        assert session.status == PyramidSessionStatus.ACTIVE
    
    def test_adjust_params_max_waves(self, session):
        """Test adjusting max_waves parameter."""
        changes = session.adjust_params(max_waves=15)
        
        assert "max_waves" in changes
        assert session.max_waves == 15
    
    def test_adjust_params_tp_pct(self, session):
        """Test adjusting tp_pct parameter."""
        changes = session.adjust_params(tp_pct=5.0)
        
        assert "tp_pct" in changes
        assert session.tp_pct == 5.0
    
    def test_adjust_params_invalid_max_waves(self, session):
        """Test that adjusting max_waves below current wave is rejected."""
        session.current_wave = 5
        changes = session.adjust_params(max_waves=3)  # Below current wave
        
        assert "max_waves" not in changes
        assert session.max_waves == 10  # Unchanged
    
    def test_stop_session(self, session):
        """Test stopping a session."""
        session.status = PyramidSessionStatus.ACTIVE
        session.stop("manual")
        
        assert session.status == PyramidSessionStatus.STOPPED
    
    def test_get_status(self, session):
        """Test get_status returns complete dict."""
        status = session.get_status()
        
        assert "id" in status
        assert "symbol" in status
        assert "status" in status
        assert "entry_price" in status
        assert "avg_price" in status
        assert "total_filled_qty" in status
        assert "estimated_tp_price" in status
        assert "waves" in status
    
    def test_estimated_tp_price(self, session):
        """Test estimated TP price calculation."""
        # Before fills, based on entry price
        expected = session.entry_price * (1 + session.tp_pct / 100)
        assert abs(session.estimated_tp_price - expected) < 0.01
        
        # After fills, based on avg price
        session.avg_price = 48000.0
        expected = 48000.0 * (1 + session.tp_pct / 100)
        assert abs(session.estimated_tp_price - expected) < 0.01


class TestKSSManager:
    """Tests for KSSManager class."""
    
    @pytest.fixture
    def manager(self):
        """Create a fresh manager for testing."""
        m = KSSManager()
        m.reset()
        return m
    
    def test_singleton_pattern(self):
        """Test that KSSManager is a singleton."""
        m1 = KSSManager()
        m2 = KSSManager()
        assert m1 is m2
    
    def test_create_pyramid_session(self, manager):
        """Test creating a pyramid session through manager."""
        session = manager.create_pyramid_session(
            symbol="ETH",
            entry_price=3000.0,
            distance_pct=1.5,
            max_waves=5,
            isolated_fund=500.0,
            tp_pct=2.5,
            timeout_x_min=20.0,
            gap_y_min=3.0,
        )
        
        assert session.id is not None
        assert session.symbol == "ETH"
        assert session in manager._sessions.values()
    
    def test_get_session(self, manager):
        """Test getting session by ID."""
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
        
        retrieved = manager.get_session(session.id)
        assert retrieved is session
    
    def test_get_session_not_found(self, manager):
        """Test getting non-existent session."""
        result = manager.get_session(99999)
        assert result is None
    
    def test_list_sessions(self, manager):
        """Test listing sessions."""
        manager.create_pyramid_session(
            symbol="BTC",
            entry_price=50000.0,
            distance_pct=2.0,
            max_waves=10,
            isolated_fund=1000.0,
            tp_pct=3.0,
            timeout_x_min=30.0,
            gap_y_min=5.0,
        )
        manager.create_pyramid_session(
            symbol="ETH",
            entry_price=3000.0,
            distance_pct=1.5,
            max_waves=5,
            isolated_fund=500.0,
            tp_pct=2.5,
            timeout_x_min=20.0,
            gap_y_min=3.0,
        )
        
        sessions = manager.list_sessions()
        assert len(sessions) == 2
    
    def test_list_sessions_filter_by_symbol(self, manager):
        """Test filtering sessions by symbol."""
        manager.create_pyramid_session(
            symbol="BTC",
            entry_price=50000.0,
            distance_pct=2.0,
            max_waves=10,
            isolated_fund=1000.0,
            tp_pct=3.0,
            timeout_x_min=30.0,
            gap_y_min=5.0,
        )
        manager.create_pyramid_session(
            symbol="ETH",
            entry_price=3000.0,
            distance_pct=1.5,
            max_waves=5,
            isolated_fund=500.0,
            tp_pct=2.5,
            timeout_x_min=20.0,
            gap_y_min=3.0,
        )
        
        sessions = manager.list_sessions(symbol="BTC")
        assert len(sessions) == 1
        assert sessions[0]["symbol"] == "BTC"
    
    def test_stop_session(self, manager):
        """Test stopping a session through manager."""
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
        session.status = PyramidSessionStatus.ACTIVE
        
        result = manager.stop_session(session.id)
        
        assert result is True
        assert session.status == PyramidSessionStatus.STOPPED
    
    def test_on_fill_routing(self, manager):
        """Test that on_fill routes to correct session."""
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
        
        source_ref = f"pyramid:{session.id}:wave:0"
        result = manager.on_fill(
            source_ref=source_ref,
            filled_qty=0.00002,
            filled_price=50000.0,
            current_market_price=49000.0,
        )
        
        assert result is not None
        assert session.total_filled_qty > 0
    
    def test_on_fill_invalid_source_ref(self, manager):
        """Test on_fill with invalid source_ref returns None."""
        result = manager.on_fill(
            source_ref="invalid:ref",
            filled_qty=0.00002,
            filled_price=50000.0,
        )
        assert result is None
    
    def test_get_summary(self, manager):
        """Test get_summary returns correct stats."""
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
        session.status = PyramidSessionStatus.ACTIVE
        
        summary = manager.get_summary()
        
        assert summary["total_sessions"] == 1
        assert summary["active_sessions"] == 1
        assert summary["total_isolated_fund"] == 1000.0
    
    def test_clear_completed(self, manager):
        """Test clearing completed sessions."""
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
        session1.status = PyramidSessionStatus.COMPLETED
        
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
        session2.status = PyramidSessionStatus.ACTIVE
        
        cleared = manager.clear_completed()
        
        assert cleared == 1
        assert len(manager._sessions) == 1
        assert session2.id in manager._sessions


class TestWaveInfo:
    """Tests for WaveInfo dataclass."""
    
    def test_to_dict(self):
        """Test WaveInfo.to_dict() serialization."""
        wave = WaveInfo(
            wave_num=3,
            quantity=0.00008,
            target_price=48000.0,
            status="filled",
            filled_qty=0.00008,
            filled_price=47950.0,
            filled_time=datetime(2026, 1, 12, 10, 30, 0),
            pending_order_id=42,
        )
        
        d = wave.to_dict()
        
        assert d["wave_num"] == 3
        assert d["quantity"] == 0.00008
        assert d["target_price"] == 48000.0
        assert d["status"] == "filled"
        assert d["filled_qty"] == 0.00008
        assert d["filled_price"] == 47950.0
        assert d["pending_order_id"] == 42
        assert "2026-01-12" in d["filled_time"]


class TestKSSPreviewAPI:
    """Tests for KSS Preview API endpoint (Phase 7)."""
    
    @pytest.fixture
    def client(self):
        """Create test client with KSS routes."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from src.findmy.kss.routes import router
        
        app = FastAPI()
        app.include_router(router)
        return TestClient(app)
    
    def test_preview_basic(self, client):
        """Test basic preview endpoint returns correct wave structure."""
        data = {
            "symbol": "BTC",
            "entry_price": 50000.0,
            "distance_pct": 2.0,
            "max_waves": 5,
            "isolated_fund": 1000.0,
            "tp_pct": 3.0,
        }
        
        response = client.post("/api/kss/preview", json=data)
        
        assert response.status_code == 200
        result = response.json()
        
        assert result["symbol"] == "BTC"
        assert result["entry_price"] == 50000.0
        assert result["max_waves"] == 5
        assert len(result["waves"]) == 5
        
        # Check first wave
        assert result["waves"][0]["wave_num"] == 0
        assert result["waves"][0]["target_price"] == 50000.0
        
        # Check last wave price (entry * (1 - distance% * (waves-1)))
        last_wave_expected = 50000.0 * (1 - 0.02 * 4)  # 46000
        assert abs(result["waves"][4]["target_price"] - last_wave_expected) < 0.01
    
    def test_preview_qty_calculation(self, client):
        """Test quantity per wave calculation."""
        data = {
            "symbol": "ETH",
            "entry_price": 2000.0,
            "distance_pct": 5.0,
            "max_waves": 4,
            "isolated_fund": 800.0,
            "tp_pct": 2.0,
        }
        
        response = client.post("/api/kss/preview", json=data)
        result = response.json()
        
        # qty_per_wave = isolated_fund / max_waves / entry_price = 800/4/2000 = 0.1
        assert abs(result["qty_per_wave"] - 0.1) < 0.0001
        assert abs(result["total_qty"] - 0.4) < 0.0001
    
    def test_preview_running_averages(self, client):
        """Test cumulative averages are calculated correctly."""
        data = {
            "symbol": "BTC",
            "entry_price": 100.0,
            "distance_pct": 10.0,  # Each wave drops 10%
            "max_waves": 3,
            "isolated_fund": 300.0,
            "tp_pct": 5.0,
        }
        
        response = client.post("/api/kss/preview", json=data)
        result = response.json()
        
        # Wave 0: price=100, qty=1, cost=100, avg=100
        # Wave 1: price=90, qty=1, cost=90, total_cost=190, total_qty=2, avg=95
        # Wave 2: price=80, qty=1, cost=80, total_cost=270, total_qty=3, avg=90
        
        assert result["waves"][0]["avg_price_after"] == 100.0
        assert abs(result["waves"][1]["avg_price_after"] - 95.0) < 0.01
        assert abs(result["waves"][2]["avg_price_after"] - 90.0) < 0.01
    
    def test_preview_tp_prices(self, client):
        """Test TP prices are calculated correctly."""
        data = {
            "symbol": "BTC",
            "entry_price": 100.0,
            "distance_pct": 10.0,
            "max_waves": 2,
            "isolated_fund": 200.0,
            "tp_pct": 10.0,  # 10% profit target
        }
        
        response = client.post("/api/kss/preview", json=data)
        result = response.json()
        
        # After wave 0: avg=100, tp = 100 * 1.10 = 110
        assert abs(result["waves"][0]["tp_price_after"] - 110.0) < 0.01
        
        # After wave 1: avg=95, tp = 95 * 1.10 = 104.5
        assert abs(result["waves"][1]["tp_price_after"] - 104.5) < 0.01
    
    def test_preview_price_range(self, client):
        """Test price range percentage calculation."""
        data = {
            "symbol": "BTC",
            "entry_price": 100.0,
            "distance_pct": 5.0,
            "max_waves": 5,
            "isolated_fund": 500.0,
            "tp_pct": 3.0,
        }
        
        response = client.post("/api/kss/preview", json=data)
        result = response.json()
        
        # Price range: entry=100, last wave = 100 * (1 - 0.05*4) = 80
        # Range = (100 - 80) / 100 * 100 = 20%
        assert abs(result["price_range_pct"] - 20.0) < 0.01
    
    def test_preview_invalid_entry_price(self, client):
        """Test preview with invalid entry price returns 422."""
        data = {
            "symbol": "BTC",
            "entry_price": -100.0,  # Invalid
            "distance_pct": 2.0,
            "max_waves": 5,
            "isolated_fund": 1000.0,
            "tp_pct": 3.0,
        }
        
        response = client.post("/api/kss/preview", json=data)
        assert response.status_code == 422
    
    def test_preview_invalid_max_waves(self, client):
        """Test preview with invalid max_waves returns 422."""
        data = {
            "symbol": "BTC",
            "entry_price": 50000.0,
            "distance_pct": 2.0,
            "max_waves": 0,  # Invalid - must be >= 1
            "isolated_fund": 1000.0,
            "tp_pct": 3.0,
        }
        
        response = client.post("/api/kss/preview", json=data)
        assert response.status_code == 422
    
    def test_preview_single_wave(self, client):
        """Test preview with single wave."""
        data = {
            "symbol": "ETH",
            "entry_price": 3000.0,
            "distance_pct": 1.0,
            "max_waves": 1,
            "isolated_fund": 100.0,
            "tp_pct": 5.0,
        }
        
        response = client.post("/api/kss/preview", json=data)
        result = response.json()
        
        assert len(result["waves"]) == 1
        assert result["waves"][0]["wave_num"] == 0
        assert result["waves"][0]["target_price"] == 3000.0
        assert result["price_range_pct"] == 0.0

