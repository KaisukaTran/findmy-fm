"""
API tests for KSS routes.

Tests all REST endpoints:
- POST /kss/sessions
- POST /kss/sessions/{id}/start
- POST /kss/sessions/{id}/stop
- PATCH /kss/sessions/{id}
- GET /kss/sessions
- DELETE /kss/sessions/{id}
- POST /kss/sessions/{id}/check-tp
- GET /kss/summary
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from unittest.mock import patch, MagicMock

from src.findmy.kss.routes import router
from src.findmy.kss.manager import KSSManager

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


class TestCreateSession:
    """Test POST /kss/sessions endpoint."""
    
    def test_create_valid_session(self, client):
        """Test creating a session with valid data."""
        response = client.post("/kss/sessions", json={
            "symbol": "BTC",
            "entry_price": 50000.0,
            "distance_pct": 2.0,
            "max_waves": 10,
            "isolated_fund": 1000.0,
            "tp_pct": 3.0,
            "timeout_x_min": 30.0,
            "gap_y_min": 5.0,
        })
        
        assert response.status_code == 200
        data = response.json()
        assert data["symbol"] == "BTC"
        assert data["status"] == "pending"
        assert "id" in data
        assert "estimated_cost" in data
    
    def test_create_session_missing_fields(self, client):
        """Test creating session with missing required fields."""
        response = client.post("/kss/sessions", json={
            "symbol": "BTC",
            "entry_price": 50000.0,
            # Missing other required fields
        })
        
        assert response.status_code == 422  # Validation error
    
    def test_create_session_invalid_entry_price(self, client):
        """Test creating session with invalid entry_price."""
        response = client.post("/kss/sessions", json={
            "symbol": "BTC",
            "entry_price": -100.0,
            "distance_pct": 2.0,
            "max_waves": 10,
            "isolated_fund": 1000.0,
            "tp_pct": 3.0,
            "timeout_x_min": 30.0,
            "gap_y_min": 5.0,
        })
        
        assert response.status_code == 400
        assert "positive" in response.json()["detail"].lower()
    
    def test_create_session_invalid_distance_pct(self, client):
        """Test creating session with invalid distance_pct."""
        response = client.post("/kss/sessions", json={
            "symbol": "BTC",
            "entry_price": 50000.0,
            "distance_pct": 60.0,  # Too large
            "max_waves": 10,
            "isolated_fund": 1000.0,
            "tp_pct": 3.0,
            "timeout_x_min": 30.0,
            "gap_y_min": 5.0,
        })
        
        assert response.status_code == 400


class TestStartSession:
    """Test POST /kss/sessions/{id}/start endpoint."""
    
    @pytest.fixture
    def session_id(self, client):
        """Create a session and return its ID."""
        response = client.post("/kss/sessions", json={
            "symbol": "BTC",
            "entry_price": 50000.0,
            "distance_pct": 2.0,
            "max_waves": 10,
            "isolated_fund": 1000.0,
            "tp_pct": 3.0,
            "timeout_x_min": 30.0,
            "gap_y_min": 5.0,
        })
        return response.json()["id"]
    
    @patch('src.findmy.kss.pyramid.get_exchange_info')
    def test_start_session_success(self, mock_exchange, client, session_id):
        """Test starting a session."""
        mock_exchange.return_value = {
            "minQty": 0.00001,
            "stepSize": 0.00001,
            "maxQty": 10000.0,
        }
        
        response = client.post(f"/kss/sessions/{session_id}/start")
        
        assert response.status_code == 200
        data = response.json()
        assert "message" in data
        assert "order" in data
        assert data["order"]["symbol"] == "BTC"
    
    def test_start_nonexistent_session(self, client):
        """Test starting nonexistent session."""
        response = client.post("/kss/sessions/99999/start")
        
        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()
    
    @patch('src.findmy.kss.pyramid.get_exchange_info')
    def test_start_already_started_session(self, mock_exchange, client, session_id):
        """Test starting already active session."""
        mock_exchange.return_value = {
            "minQty": 0.00001,
            "stepSize": 0.00001,
            "maxQty": 10000.0,
        }
        
        # Start once
        client.post(f"/kss/sessions/{session_id}/start")
        
        # Try to start again
        response = client.post(f"/kss/sessions/{session_id}/start")
        
        assert response.status_code == 400
        assert "already" in response.json()["detail"].lower()


class TestStopSession:
    """Test POST /kss/sessions/{id}/stop endpoint."""
    
    @pytest.fixture
    def active_session_id(self, client):
        """Create and start a session."""
        response = client.post("/kss/sessions", json={
            "symbol": "BTC",
            "entry_price": 50000.0,
            "distance_pct": 2.0,
            "max_waves": 10,
            "isolated_fund": 1000.0,
            "tp_pct": 3.0,
            "timeout_x_min": 30.0,
            "gap_y_min": 5.0,
        })
        session_id = response.json()["id"]
        
        # Manually set to active
        manager = KSSManager()
        session = manager.get_session(session_id)
        session.status = "active"
        
        return session_id
    
    def test_stop_session_success(self, client, active_session_id):
        """Test stopping an active session."""
        response = client.post(
            f"/kss/sessions/{active_session_id}/stop",
            json={"reason": "manual"}
        )
        
        assert response.status_code == 200
        assert "stopped" in response.json()["message"].lower()
    
    def test_stop_nonexistent_session(self, client):
        """Test stopping nonexistent session."""
        response = client.post("/kss/sessions/99999/stop")
        
        assert response.status_code == 404


class TestAdjustParameters:
    """Test PATCH /kss/sessions/{id} endpoint."""
    
    @pytest.fixture
    def session_id(self, client):
        """Create a session."""
        response = client.post("/kss/sessions", json={
            "symbol": "BTC",
            "entry_price": 50000.0,
            "distance_pct": 2.0,
            "max_waves": 10,
            "isolated_fund": 1000.0,
            "tp_pct": 3.0,
            "timeout_x_min": 30.0,
            "gap_y_min": 5.0,
        })
        return response.json()["id"]
    
    def test_adjust_max_waves(self, client, session_id):
        """Test adjusting max_waves."""
        response = client.patch(
            f"/kss/sessions/{session_id}",
            json={"max_waves": 15}
        )
        
        assert response.status_code == 200
        data = response.json()
        assert "max_waves" in data["changes"]
        assert data["session"]["max_waves"] == 15
    
    def test_adjust_tp_pct(self, client, session_id):
        """Test adjusting tp_pct."""
        response = client.patch(
            f"/kss/sessions/{session_id}",
            json={"tp_pct": 5.0}
        )
        
        assert response.status_code == 200
        data = response.json()
        assert "tp_pct" in data["changes"]
    
    def test_adjust_multiple_params(self, client, session_id):
        """Test adjusting multiple parameters."""
        response = client.patch(
            f"/kss/sessions/{session_id}",
            json={
                "max_waves": 20,
                "tp_pct": 4.0,
                "timeout_x_min": 60.0,
            }
        )
        
        assert response.status_code == 200
        data = response.json()
        assert len(data["changes"]) == 3
    
    def test_adjust_nonexistent_session(self, client):
        """Test adjusting nonexistent session."""
        response = client.patch(
            "/kss/sessions/99999",
            json={"max_waves": 15}
        )
        
        assert response.status_code == 404


class TestListSessions:
    """Test GET /kss/sessions endpoint."""
    
    @pytest.fixture
    def multiple_sessions(self, client):
        """Create multiple sessions."""
        # BTC session
        client.post("/kss/sessions", json={
            "symbol": "BTC",
            "entry_price": 50000.0,
            "distance_pct": 2.0,
            "max_waves": 10,
            "isolated_fund": 1000.0,
            "tp_pct": 3.0,
            "timeout_x_min": 30.0,
            "gap_y_min": 5.0,
        })
        
        # ETH session
        client.post("/kss/sessions", json={
            "symbol": "ETH",
            "entry_price": 3000.0,
            "distance_pct": 1.5,
            "max_waves": 5,
            "isolated_fund": 500.0,
            "tp_pct": 2.5,
            "timeout_x_min": 20.0,
            "gap_y_min": 3.0,
        })
    
    def test_list_all_sessions(self, client, multiple_sessions):
        """Test listing all sessions."""
        response = client.get("/kss/sessions")
        
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 2
    
    def test_filter_by_symbol(self, client, multiple_sessions):
        """Test filtering by symbol."""
        response = client.get("/kss/sessions?symbol=BTC")
        
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["symbol"] == "BTC"
    
    def test_filter_by_status(self, client, multiple_sessions):
        """Test filtering by status."""
        response = client.get("/kss/sessions?status=pending")
        
        assert response.status_code == 200
        data = response.json()
        assert all(s["status"] == "pending" for s in data)
    
    def test_list_empty(self, client):
        """Test listing when no sessions exist."""
        response = client.get("/kss/sessions")
        
        assert response.status_code == 200
        assert response.json() == []


class TestDeleteSession:
    """Test DELETE /kss/sessions/{id} endpoint."""
    
    @pytest.fixture
    def session_id(self, client):
        """Create a session."""
        response = client.post("/kss/sessions", json={
            "symbol": "BTC",
            "entry_price": 50000.0,
            "distance_pct": 2.0,
            "max_waves": 10,
            "isolated_fund": 1000.0,
            "tp_pct": 3.0,
            "timeout_x_min": 30.0,
            "gap_y_min": 5.0,
        })
        return response.json()["id"]
    
    def test_delete_session_success(self, client, session_id):
        """Test deleting a session."""
        response = client.delete(f"/kss/sessions/{session_id}")
        
        assert response.status_code == 200
        assert "deleted" in response.json()["message"].lower()
        
        # Verify session is gone
        list_response = client.get("/kss/sessions")
        assert len(list_response.json()) == 0
    
    def test_delete_nonexistent_session(self, client):
        """Test deleting nonexistent session."""
        response = client.delete("/kss/sessions/99999")
        
        assert response.status_code == 404


class TestCheckTakeProfit:
    """Test POST /kss/sessions/{id}/check-tp endpoint."""
    
    @pytest.fixture
    def session_with_position(self, client):
        """Create session with simulated position."""
        response = client.post("/kss/sessions", json={
            "symbol": "BTC",
            "entry_price": 50000.0,
            "distance_pct": 2.0,
            "max_waves": 10,
            "isolated_fund": 1000.0,
            "tp_pct": 3.0,
            "timeout_x_min": 30.0,
            "gap_y_min": 5.0,
        })
        session_id = response.json()["id"]
        
        # Manually set position data
        manager = KSSManager()
        session = manager.get_session(session_id)
        session.status = "active"
        session.avg_price = 49000.0
        session.total_filled_qty = 0.001
        
        return session_id
    
    @patch('src.findmy.kss.pyramid.get_current_prices')
    def test_check_tp_triggers(self, mock_prices, client, session_with_position):
        """Test TP triggers with price above threshold."""
        mock_prices.return_value = {"BTC": 51000.0}
        
        response = client.post(
            f"/kss/sessions/{session_with_position}/check-tp",
            json={"current_price": 51000.0}
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["tp_triggered"] is True
        assert "order" in data
    
    @patch('src.findmy.kss.pyramid.get_current_prices')
    def test_check_tp_not_triggered(self, mock_prices, client, session_with_position):
        """Test TP doesn't trigger below threshold."""
        mock_prices.return_value = {"BTC": 50000.0}
        
        response = client.post(
            f"/kss/sessions/{session_with_position}/check-tp",
            json={"current_price": 50000.0}
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["tp_triggered"] is False
    
    def test_check_tp_nonexistent_session(self, client):
        """Test checking TP for nonexistent session."""
        response = client.post(
            "/kss/sessions/99999/check-tp",
            json={"current_price": 50000.0}
        )
        
        assert response.status_code == 404


class TestGetSummary:
    """Test GET /kss/summary endpoint."""
    
    def test_summary_no_sessions(self, client):
        """Test summary with no sessions."""
        response = client.get("/kss/summary")
        
        assert response.status_code == 200
        data = response.json()
        assert data["total_sessions"] == 0
        assert data["active_sessions"] == 0
        assert data["total_isolated_fund"] == 0.0
    
    def test_summary_with_sessions(self, client):
        """Test summary with multiple sessions."""
        # Create sessions
        client.post("/kss/sessions", json={
            "symbol": "BTC",
            "entry_price": 50000.0,
            "distance_pct": 2.0,
            "max_waves": 10,
            "isolated_fund": 1000.0,
            "tp_pct": 3.0,
            "timeout_x_min": 30.0,
            "gap_y_min": 5.0,
        })
        
        client.post("/kss/sessions", json={
            "symbol": "ETH",
            "entry_price": 3000.0,
            "distance_pct": 1.5,
            "max_waves": 5,
            "isolated_fund": 500.0,
            "tp_pct": 2.5,
            "timeout_x_min": 20.0,
            "gap_y_min": 3.0,
        })
        
        response = client.get("/kss/summary")
        
        assert response.status_code == 200
        data = response.json()
        assert data["total_sessions"] == 2
        assert data["total_isolated_fund"] == 1500.0


class TestAPIErrorHandling:
    """Test API error handling and validation."""
    
    def test_invalid_json_body(self, client):
        """Test handling of invalid JSON."""
        response = client.post(
            "/kss/sessions",
            data="invalid json",
            headers={"Content-Type": "application/json"}
        )
        
        assert response.status_code in [400, 422]
    
    def test_missing_content_type(self, client):
        """Test handling of missing content-type."""
        response = client.post(
            "/kss/sessions",
            data='{"symbol": "BTC"}',
        )
        
        # Should still work or return appropriate error
        assert response.status_code in [200, 400, 422]
    
    def test_invalid_session_id_type(self, client):
        """Test handling of non-numeric session ID."""
        response = client.post("/kss/sessions/abc/start")
        
        assert response.status_code == 422  # Validation error
