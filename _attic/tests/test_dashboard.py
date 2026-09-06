"""
Tests for dashboard endpoints and HTML rendering.
"""

import pytest
from fastapi.testclient import TestClient
from src.findmy.api.main import app

client = TestClient(app)


class TestDashboardRoute:
    """Tests for the main dashboard route."""

    def test_dashboard_returns_html(self):
        """Test that the dashboard returns HTML."""
        response = client.get("/")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        assert "FINDMY FM Dashboard" in response.text

    def test_dashboard_has_required_sections(self):
        """Test that dashboard HTML contains all required sections."""
        response = client.get("/")
        assert "System Status" in response.text
        assert "Current Positions" in response.text
        assert "Trade History" in response.text


class TestDataEndpoints:
    """Tests for the data API endpoints."""

    def test_get_positions_returns_json(self):
        """Test that positions endpoint returns valid JSON."""
        response = client.get("/api/positions")
        assert response.status_code == 200
        assert isinstance(response.json(), list)

    def test_get_trades_returns_json(self):
        """Test that trades endpoint returns valid JSON."""
        response = client.get("/api/trades")
        assert response.status_code == 200
        assert isinstance(response.json(), list)

    def test_get_summary_returns_json(self):
        """Test that summary endpoint returns valid JSON with expected fields."""
        response = client.get("/api/summary")
        assert response.status_code == 200
        data = response.json()
        assert "total_trades" in data
        assert "realized_pnl" in data
        assert "unrealized_pnl" in data
        assert "total_invested" in data
        assert "last_trade_time" in data
        assert "status" in data

    def test_summary_has_correct_types(self):
        """Test that summary endpoint returns correct data types."""
        response = client.get("/api/summary")
        data = response.json()
        assert isinstance(data["total_trades"], int)
        assert isinstance(data["realized_pnl"], float)
        assert isinstance(data["unrealized_pnl"], float)
        assert isinstance(data["total_invested"], float)


class TestStaticFiles:
    """Tests for static file serving."""

    def test_custom_css_loads(self):
        """Test that custom CSS file is accessible."""
        response = client.get("/static/css/style.css")
        assert response.status_code == 200
        assert "FINDMY FM Dashboard - Custom Styles" in response.text


class TestAPIDocumentation:
    """Tests for API documentation endpoints."""

    def test_swagger_ui_available(self):
        """Test that Swagger UI is still available."""
        response = client.get("/docs")
        assert response.status_code == 200
        assert "Swagger UI" in response.text

    def test_redoc_available(self):
        """Test that ReDoc is still available."""
        response = client.get("/redoc")
        assert response.status_code == 200
