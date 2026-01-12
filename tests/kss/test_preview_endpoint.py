"""
Tests for Preview Endpoint API.

Tests the /api/kss/preview endpoint for:
- Correct projected waves
- Quantity scaling
- Price drop calculations
- Cumulative fund tracking
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.findmy.kss.routes import router


class TestPreviewEndpoint:
    """Test /api/kss/preview endpoint."""
    
    @pytest.fixture
    def client(self):
        """Create test client."""
        app = FastAPI()
        app.include_router(router)
        return TestClient(app)
    
    def test_preview_returns_waves(self, client):
        """Test preview returns correct number of waves."""
        data = {
            "symbol": "BTC",
            "entry_price": 50000.0,
            "distance_pct": 2.0,
            "max_waves": 10,
            "isolated_fund": 1000.0,
            "tp_pct": 3.0,
        }
        
        response = client.post("/api/kss/preview", json=data)
        
        assert response.status_code == 200
        result = response.json()
        assert len(result["waves"]) == 10
    
    def test_preview_wave_prices_decreasing(self, client):
        """Test wave prices decrease correctly."""
        data = {
            "symbol": "BTC",
            "entry_price": 100.0,
            "distance_pct": 10.0,  # 10% per wave
            "max_waves": 5,
            "isolated_fund": 500.0,
            "tp_pct": 5.0,
        }
        
        response = client.post("/api/kss/preview", json=data)
        result = response.json()
        
        prices = [w["target_price"] for w in result["waves"]]
        
        # Each price should be lower than previous
        for i in range(1, len(prices)):
            assert prices[i] < prices[i-1]
    
    def test_preview_qty_per_wave(self, client):
        """Test quantity per wave calculation."""
        data = {
            "symbol": "ETH",
            "entry_price": 1000.0,
            "distance_pct": 5.0,
            "max_waves": 4,
            "isolated_fund": 400.0,
            "tp_pct": 3.0,
        }
        
        response = client.post("/api/kss/preview", json=data)
        result = response.json()
        
        # qty_per_wave = isolated_fund / max_waves / entry_price
        # = 400 / 4 / 1000 = 0.1
        assert abs(result["qty_per_wave"] - 0.1) < 0.0001
    
    def test_preview_cumulative_qty(self, client):
        """Test cumulative quantity is correct."""
        data = {
            "symbol": "BTC",
            "entry_price": 50000.0,
            "distance_pct": 2.0,
            "max_waves": 5,
            "isolated_fund": 1000.0,
            "tp_pct": 3.0,
        }
        
        response = client.post("/api/kss/preview", json=data)
        result = response.json()
        
        # Cumulative qty should increase
        prev_cum = 0
        for wave in result["waves"]:
            assert wave["cumulative_qty"] > prev_cum
            prev_cum = wave["cumulative_qty"]
        
        # Final cumulative should equal total
        assert abs(result["waves"][-1]["cumulative_qty"] - result["total_qty"]) < 0.0001
    
    def test_preview_cumulative_cost(self, client):
        """Test cumulative cost tracking."""
        data = {
            "symbol": "BTC",
            "entry_price": 100.0,
            "distance_pct": 5.0,
            "max_waves": 3,
            "isolated_fund": 300.0,
            "tp_pct": 3.0,
        }
        
        response = client.post("/api/kss/preview", json=data)
        result = response.json()
        
        # Verify cumulative cost increases
        prev_cost = 0
        for wave in result["waves"]:
            assert wave["cumulative_cost"] > prev_cost
            prev_cost = wave["cumulative_cost"]
        
        # Final cumulative should equal total
        assert abs(result["waves"][-1]["cumulative_cost"] - result["total_cost"]) < 0.01
    
    def test_preview_avg_price_after(self, client):
        """Test average price calculation after each wave."""
        data = {
            "symbol": "BTC",
            "entry_price": 100.0,
            "distance_pct": 10.0,
            "max_waves": 3,
            "isolated_fund": 300.0,
            "tp_pct": 5.0,
        }
        
        response = client.post("/api/kss/preview", json=data)
        result = response.json()
        
        # Average price should decrease as we buy at lower prices
        for i in range(1, len(result["waves"])):
            assert result["waves"][i]["avg_price_after"] < result["waves"][i-1]["avg_price_after"]
    
    def test_preview_tp_price_after(self, client):
        """Test TP price calculation after each wave."""
        data = {
            "symbol": "BTC",
            "entry_price": 100.0,
            "distance_pct": 5.0,
            "max_waves": 5,
            "isolated_fund": 500.0,
            "tp_pct": 10.0,  # 10% above avg
        }
        
        response = client.post("/api/kss/preview", json=data)
        result = response.json()
        
        for wave in result["waves"]:
            expected_tp = wave["avg_price_after"] * 1.10
            assert abs(wave["tp_price_after"] - expected_tp) < 0.01
    
    def test_preview_price_range_pct(self, client):
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
        
        # Price range = (entry - last_wave) / entry * 100
        first_price = result["waves"][0]["target_price"]
        last_price = result["waves"][-1]["target_price"]
        expected_range = (first_price - last_price) / first_price * 100
        
        assert abs(result["price_range_pct"] - expected_range) < 0.1
    
    def test_preview_final_values(self, client):
        """Test final aggregate values."""
        data = {
            "symbol": "BTC",
            "entry_price": 50000.0,
            "distance_pct": 2.0,
            "max_waves": 10,
            "isolated_fund": 1000.0,
            "tp_pct": 3.0,
        }
        
        response = client.post("/api/kss/preview", json=data)
        result = response.json()
        
        # Final avg should equal last wave's avg
        assert abs(result["final_avg_price"] - result["waves"][-1]["avg_price_after"]) < 0.01
        
        # Final TP should equal last wave's TP
        assert abs(result["final_tp_price"] - result["waves"][-1]["tp_price_after"]) < 0.01
    
    def test_preview_invalid_entry_price(self, client):
        """Test preview with invalid entry price."""
        data = {
            "symbol": "BTC",
            "entry_price": 0,  # Invalid
            "distance_pct": 2.0,
            "max_waves": 5,
            "isolated_fund": 1000.0,
            "tp_pct": 3.0,
        }
        
        response = client.post("/api/kss/preview", json=data)
        assert response.status_code == 422
    
    def test_preview_invalid_max_waves(self, client):
        """Test preview with invalid max_waves."""
        data = {
            "symbol": "BTC",
            "entry_price": 50000.0,
            "distance_pct": 2.0,
            "max_waves": 0,  # Invalid
            "isolated_fund": 1000.0,
            "tp_pct": 3.0,
        }
        
        response = client.post("/api/kss/preview", json=data)
        assert response.status_code == 422
    
    def test_preview_single_wave(self, client):
        """Test preview with single wave."""
        data = {
            "symbol": "BTC",
            "entry_price": 50000.0,
            "distance_pct": 2.0,
            "max_waves": 1,
            "isolated_fund": 100.0,
            "tp_pct": 3.0,
        }
        
        response = client.post("/api/kss/preview", json=data)
        result = response.json()
        
        assert len(result["waves"]) == 1
        assert result["price_range_pct"] == 0.0
    
    def test_preview_high_distance_pct(self, client):
        """Test preview with high distance percentage."""
        data = {
            "symbol": "BTC",
            "entry_price": 100.0,
            "distance_pct": 15.0,  # 15% per wave - valid high value
            "max_waves": 3,
            "isolated_fund": 300.0,
            "tp_pct": 10.0,
        }
        
        response = client.post("/api/kss/preview", json=data)
        
        # May be rejected if distance_pct validation has limits
        if response.status_code == 200:
            result = response.json()
            # All target prices should be positive
            assert all(w["target_price"] > 0 for w in result["waves"])
        else:
            # If rejected, 422 is expected
            assert response.status_code == 422
    
    def test_preview_preserves_symbol(self, client):
        """Test symbol is preserved in response."""
        data = {
            "symbol": "DOGE",
            "entry_price": 0.1,
            "distance_pct": 5.0,
            "max_waves": 5,
            "isolated_fund": 50.0,
            "tp_pct": 10.0,
        }
        
        response = client.post("/api/kss/preview", json=data)
        result = response.json()
        
        assert result["symbol"] == "DOGE"
    
    def test_preview_returns_all_params(self, client):
        """Test all input params are echoed in response."""
        data = {
            "symbol": "ETH",
            "entry_price": 3000.0,
            "distance_pct": 3.0,
            "max_waves": 7,
            "isolated_fund": 700.0,
            "tp_pct": 5.0,
        }
        
        response = client.post("/api/kss/preview", json=data)
        result = response.json()
        
        assert result["symbol"] == data["symbol"]
        assert result["entry_price"] == data["entry_price"]
        assert result["distance_pct"] == data["distance_pct"]
        assert result["max_waves"] == data["max_waves"]
        assert result["isolated_fund"] == data["isolated_fund"]
        assert result["tp_pct"] == data["tp_pct"]
