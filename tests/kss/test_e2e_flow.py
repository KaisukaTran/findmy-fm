"""
End-to-end tests for complete KSS Pyramid DCA session flow.

Tests full scenarios:
- Create → Start → Fill → Next Wave → TP
- Create → Start → Fill → Fill → Timeout
- Create → Start → Adjust → Continue
- Multiple concurrent sessions
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from unittest.mock import patch, MagicMock
from datetime import datetime, timedelta

from src.findmy.kss.routes import router
from src.findmy.kss.manager import KSSManager
from src.findmy.kss.pyramid import PyramidSessionStatus

# Create minimal test app
test_app = FastAPI()
test_app.include_router(router)


@pytest.fixture(scope="function", autouse=True)
def reset_manager():
    """Reset manager before each test."""
    manager = KSSManager()
    manager.reset()
    yield


@pytest.fixture
def client():
    """Create test client."""
    return TestClient(test_app)


class TestCompleteSessionLifecycle:
    """Test complete session from creation to completion."""
    
    @patch('src.findmy.kss.pyramid.get_exchange_info')
    @patch('src.findmy.kss.pyramid.get_current_prices')
    def test_full_lifecycle_with_tp(self, mock_prices, mock_exchange, client):
        """Test: Create → Start → Fill → TP."""
        # Setup mocks
        mock_exchange.return_value = {
            "minQty": 0.00001,
            "stepSize": 0.00001,
            "maxQty": 10000.0,
        }
        mock_prices.return_value = {"BTC": 50000.0}
        
        # Step 1: Create session
        create_response = client.post("/kss/sessions", json={
            "symbol": "BTC",
            "entry_price": 50000.0,
            "distance_pct": 2.0,
            "max_waves": 10,
            "isolated_fund": 1000.0,
            "tp_pct": 3.0,
            "timeout_x_min": 30.0,
            "gap_y_min": 5.0,
        })
        assert create_response.status_code == 200
        session_id = create_response.json()["id"]
        
        # Step 2: Start session
        start_response = client.post(f"/kss/sessions/{session_id}/start")
        assert start_response.status_code == 200
        assert "order" in start_response.json()
        
        # Step 3: Simulate fill
        manager = KSSManager()
        session = manager.get_session(session_id)
        session.on_fill(
            wave_num=0,
            filled_qty=0.00002,
            filled_price=50000.0,
            current_market_price=50000.0,
        )
        
        # Verify session state after fill
        assert session.total_filled_qty == 0.00002
        assert session.avg_price > 0
        
        # Step 4: Check TP (price rises above threshold)
        mock_prices.return_value = {"BTC": 52000.0}
        tp_response = client.post(
            f"/kss/sessions/{session_id}/check-tp",
            json={"current_price": 52000.0}
        )
        assert tp_response.status_code == 200
        assert tp_response.json()["tp_triggered"] is True
        
        # Verify final status
        assert session.status == PyramidSessionStatus.TP_TRIGGERED
    
    @patch('src.findmy.kss.pyramid.get_exchange_info')
    @patch('src.findmy.kss.pyramid.get_current_prices')
    def test_full_lifecycle_with_timeout(self, mock_prices, mock_exchange, client):
        """Test: Create → Start → Fill → Timeout."""
        mock_exchange.return_value = {
            "minQty": 0.00001,
            "stepSize": 0.00001,
            "maxQty": 10000.0,
        }
        mock_prices.return_value = {"BTC": 50000.0}
        
        # Create and start
        create_response = client.post("/kss/sessions", json={
            "symbol": "BTC",
            "entry_price": 50000.0,
            "distance_pct": 2.0,
            "max_waves": 10,
            "isolated_fund": 1000.0,
            "tp_pct": 3.0,
            "timeout_x_min": 30.0,
            "gap_y_min": 5.0,
        })
        session_id = create_response.json()["id"]
        
        client.post(f"/kss/sessions/{session_id}/start")
        
        # Simulate fill
        manager = KSSManager()
        session = manager.get_session(session_id)
        session.on_fill(
            wave_num=0,
            filled_qty=0.00002,
            filled_price=50000.0,
            current_market_price=50000.0,
        )
        
        # Simulate timeout by setting old last_fill_time
        session.last_fill_time = datetime.utcnow() - timedelta(minutes=35)
        
        # Check timeout
        result = session.check_timeout()
        assert result is True
        assert session.status == PyramidSessionStatus.TIMEOUT


class TestMultiWaveProgression:
    """Test progression through multiple waves."""
    
    @patch('src.findmy.kss.pyramid.get_exchange_info')
    @patch('src.findmy.kss.pyramid.get_current_prices')
    def test_three_wave_progression(self, mock_prices, mock_exchange, client):
        """Test filling 3 consecutive waves."""
        mock_exchange.return_value = {
            "minQty": 0.00001,
            "stepSize": 0.00001,
            "maxQty": 10000.0,
        }
        
        # Create and start
        create_response = client.post("/kss/sessions", json={
            "symbol": "BTC",
            "entry_price": 50000.0,
            "distance_pct": 2.0,
            "max_waves": 10,
            "isolated_fund": 1000.0,
            "tp_pct": 3.0,
            "timeout_x_min": 30.0,
            "gap_y_min": 0.1,  # Short gap for testing
        })
        session_id = create_response.json()["id"]
        client.post(f"/kss/sessions/{session_id}/start")
        
        manager = KSSManager()
        session = manager.get_session(session_id)
        
        # Wave 0 fill
        mock_prices.return_value = {"BTC": 49000.0}
        session.on_fill(
            wave_num=0,
            filled_qty=0.00002,
            filled_price=50000.0,
            current_market_price=49000.0,
        )
        assert len(session.waves) >= 2  # Wave 1 should be generated
        
        # Wave 1 fill
        mock_prices.return_value = {"BTC": 48000.0}
        session.on_fill(
            wave_num=1,
            filled_qty=0.00004,
            filled_price=49000.0,
            current_market_price=48000.0,
        )
        assert len(session.waves) >= 3  # Wave 2 should be generated
        
        # Wave 2 fill
        mock_prices.return_value = {"BTC": 47000.0}
        session.on_fill(
            wave_num=2,
            filled_qty=0.00006,
            filled_price=48000.0,
            current_market_price=47000.0,
        )
        
        # Verify cumulative quantities
        expected_qty = 0.00002 + 0.00004 + 0.00006
        assert session.total_filled_qty == pytest.approx(expected_qty, rel=1e-6)
        
        # Verify average price calculation
        assert session.avg_price < 50000.0  # Should be lower than entry
        assert session.avg_price > 47000.0  # But not as low as last fill


class TestParameterAdjustmentFlow:
    """Test adjusting parameters during session."""
    
    @patch('src.findmy.kss.pyramid.get_exchange_info')
    def test_adjust_then_continue(self, mock_exchange, client):
        """Test: Create → Start → Adjust → Continue."""
        mock_exchange.return_value = {
            "minQty": 0.00001,
            "stepSize": 0.00001,
            "maxQty": 10000.0,
        }
        
        # Create and start
        create_response = client.post("/kss/sessions", json={
            "symbol": "BTC",
            "entry_price": 50000.0,
            "distance_pct": 2.0,
            "max_waves": 10,
            "isolated_fund": 1000.0,
            "tp_pct": 3.0,
            "timeout_x_min": 30.0,
            "gap_y_min": 5.0,
        })
        session_id = create_response.json()["id"]
        client.post(f"/kss/sessions/{session_id}/start")
        
        # Adjust parameters
        adjust_response = client.patch(
            f"/kss/sessions/{session_id}",
            json={
                "max_waves": 15,
                "tp_pct": 5.0,
            }
        )
        assert adjust_response.status_code == 200
        
        # Verify adjustments
        manager = KSSManager()
        session = manager.get_session(session_id)
        assert session.max_waves == 15
        assert session.tp_pct == 5.0
        
        # Continue with fills (should use new parameters)
        session.on_fill(
            wave_num=0,
            filled_qty=0.00002,
            filled_price=50000.0,
            current_market_price=50000.0,
        )
        
        # New TP threshold should be based on 5% not 3%
        expected_tp = session.avg_price * 1.05
        assert session.estimated_tp_price == pytest.approx(expected_tp)


class TestConcurrentSessions:
    """Test running multiple sessions concurrently."""
    
    @patch('src.findmy.kss.pyramid.get_exchange_info')
    @patch('src.findmy.kss.pyramid.get_current_prices')
    def test_two_sessions_independent(self, mock_prices, mock_exchange, client):
        """Test two independent sessions running concurrently."""
        mock_exchange.return_value = {
            "minQty": 0.00001,
            "stepSize": 0.00001,
            "maxQty": 10000.0,
        }
        
        # Create BTC session
        btc_response = client.post("/kss/sessions", json={
            "symbol": "BTC",
            "entry_price": 50000.0,
            "distance_pct": 2.0,
            "max_waves": 10,
            "isolated_fund": 1000.0,
            "tp_pct": 3.0,
            "timeout_x_min": 30.0,
            "gap_y_min": 5.0,
        })
        btc_id = btc_response.json()["id"]
        
        # Create ETH session
        eth_response = client.post("/kss/sessions", json={
            "symbol": "ETH",
            "entry_price": 3000.0,
            "distance_pct": 1.5,
            "max_waves": 5,
            "isolated_fund": 500.0,
            "tp_pct": 2.5,
            "timeout_x_min": 20.0,
            "gap_y_min": 3.0,
        })
        eth_id = eth_response.json()["id"]
        
        # Start both
        client.post(f"/kss/sessions/{btc_id}/start")
        client.post(f"/kss/sessions/{eth_id}/start")
        
        # Fill BTC
        manager = KSSManager()
        btc_session = manager.get_session(btc_id)
        mock_prices.return_value = {"BTC": 50000.0}
        btc_session.on_fill(
            wave_num=0,
            filled_qty=0.00002,
            filled_price=50000.0,
            current_market_price=50000.0,
        )
        
        # Fill ETH
        eth_session = manager.get_session(eth_id)
        mock_prices.return_value = {"ETH": 3000.0}
        eth_session.on_fill(
            wave_num=0,
            filled_qty=0.001,
            filled_price=3000.0,
            current_market_price=3000.0,
        )
        
        # Verify both sessions are independent
        assert btc_session.total_filled_qty == 0.00002
        assert eth_session.total_filled_qty == 0.001
        assert btc_session.symbol == "BTC"
        assert eth_session.symbol == "ETH"
        
        # Check summary
        summary_response = client.get("/kss/summary")
        summary = summary_response.json()
        assert summary["total_sessions"] == 2
        assert summary["active_sessions"] == 2
        assert summary["total_isolated_fund"] == 1500.0


class TestErrorRecovery:
    """Test error handling and recovery scenarios."""
    
    @patch('src.findmy.kss.pyramid.get_exchange_info')
    def test_start_fail_rollback(self, mock_exchange, client):
        """Test that failed start doesn't corrupt state."""
        # Simulate exchange info failure
        mock_exchange.side_effect = Exception("Exchange API error")
        
        create_response = client.post("/kss/sessions", json={
            "symbol": "BTC",
            "entry_price": 50000.0,
            "distance_pct": 2.0,
            "max_waves": 10,
            "isolated_fund": 1000.0,
            "tp_pct": 3.0,
            "timeout_x_min": 30.0,
            "gap_y_min": 5.0,
        })
        session_id = create_response.json()["id"]
        
        # Try to start (should fail)
        start_response = client.post(f"/kss/sessions/{session_id}/start")
        assert start_response.status_code in [400, 500]
        
        # Session should still be in pending state
        manager = KSSManager()
        session = manager.get_session(session_id)
        assert session.status == PyramidSessionStatus.PENDING
    
    def test_invalid_fill_ignored(self, client):
        """Test that invalid fills don't crash the system."""
        manager = KSSManager()
        
        # Try to route fill to nonexistent session
        result = manager.on_fill(
            source_ref="pyramid:99999:wave:0",
            filled_qty=0.00002,
            filled_price=50000.0,
        )
        
        # Should return None, not crash
        assert result is None


class TestEdgeCases:
    """Test edge cases and boundary conditions."""
    
    @patch('src.findmy.kss.pyramid.get_exchange_info')
    @patch('src.findmy.kss.pyramid.get_current_prices')
    def test_max_waves_reached(self, mock_prices, mock_exchange, client):
        """Test behavior when max_waves is reached."""
        mock_exchange.return_value = {
            "minQty": 0.00001,
            "stepSize": 0.00001,
            "maxQty": 10000.0,
        }
        mock_prices.return_value = {"BTC": 40000.0}  # Always below entry
        
        # Create session with only 3 max waves
        create_response = client.post("/kss/sessions", json={
            "symbol": "BTC",
            "entry_price": 50000.0,
            "distance_pct": 2.0,
            "max_waves": 3,
            "isolated_fund": 1000.0,
            "tp_pct": 3.0,
            "timeout_x_min": 30.0,
            "gap_y_min": 0.1,
        })
        session_id = create_response.json()["id"]
        client.post(f"/kss/sessions/{session_id}/start")
        
        manager = KSSManager()
        session = manager.get_session(session_id)
        
        # Fill all 3 waves
        for i in range(3):
            if i < len(session.waves):
                session.on_fill(
                    wave_num=i,
                    filled_qty=0.00002 * (i + 1),
                    filled_price=50000.0 * (1 - 0.02 * i),
                    current_market_price=40000.0,
                )
        
        # Should have exactly 3 waves, no more generated
        assert session.current_wave >= 2
        assert len(session.waves) <= 3
    
    @patch('src.findmy.kss.pyramid.get_exchange_info')
    def test_very_small_quantities(self, mock_exchange, client):
        """Test with very small quantity values."""
        mock_exchange.return_value = {
            "minQty": 0.000001,
            "stepSize": 0.000001,
            "maxQty": 10000.0,
        }
        
        create_response = client.post("/kss/sessions", json={
            "symbol": "BTC",
            "entry_price": 50000.0,
            "distance_pct": 0.5,  # Very small distance
            "max_waves": 10,
            "isolated_fund": 10.0,  # Small fund
            "tp_pct": 0.5,  # Small TP
            "timeout_x_min": 30.0,
            "gap_y_min": 5.0,
        })
        assert create_response.status_code == 200
        
        session_id = create_response.json()["id"]
        start_response = client.post(f"/kss/sessions/{session_id}/start")
        assert start_response.status_code == 200


class TestSessionPersistence:
    """Test that session state persists correctly."""
    
    @patch('src.findmy.kss.pyramid.get_exchange_info')
    @patch('src.findmy.kss.pyramid.get_current_prices')
    def test_state_persists_across_operations(self, mock_prices, mock_exchange, client):
        """Test that session state remains consistent."""
        mock_exchange.return_value = {
            "minQty": 0.00001,
            "stepSize": 0.00001,
            "maxQty": 10000.0,
        }
        mock_prices.return_value = {"BTC": 50000.0}
        
        # Create session
        create_response = client.post("/kss/sessions", json={
            "symbol": "BTC",
            "entry_price": 50000.0,
            "distance_pct": 2.0,
            "max_waves": 10,
            "isolated_fund": 1000.0,
            "tp_pct": 3.0,
            "timeout_x_min": 30.0,
            "gap_y_min": 5.0,
        })
        session_id = create_response.json()["id"]
        
        # Start
        client.post(f"/kss/sessions/{session_id}/start")
        
        # Fill
        manager = KSSManager()
        session = manager.get_session(session_id)
        session.on_fill(
            wave_num=0,
            filled_qty=0.00002,
            filled_price=50000.0,
            current_market_price=50000.0,
        )
        
        # Get session via API
        list_response = client.get(f"/kss/sessions?symbol=BTC")
        sessions = list_response.json()
        
        # Verify state matches
        api_session = sessions[0]
        assert api_session["id"] == session_id
        assert api_session["status"] == "active"
        assert api_session["total_filled_qty"] == 0.00002
