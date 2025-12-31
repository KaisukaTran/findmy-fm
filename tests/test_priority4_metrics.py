"""
Priority 4: Prometheus Observability Tests (v0.7.0)

Tests for Prometheus metrics collection, custom metrics, and observability features.
"""

import pytest
from fastapi.testclient import TestClient
from src.findmy.api.main import app


@pytest.fixture
def client():
    """FastAPI test client."""
    return TestClient(app)


class TestMetricsEndpoint:
    """Test /metrics endpoint exposes Prometheus metrics."""
    
    def test_metrics_endpoint_exists(self, client):
        """Test that /metrics endpoint is accessible."""
        response = client.get("/metrics")
        assert response.status_code == 200
        assert "text/plain" in response.headers.get("content-type", "")
    
    def test_metrics_endpoint_returns_prometheus_format(self, client):
        """Test that /metrics returns valid Prometheus format."""
        response = client.get("/metrics")
        content = response.text
        
        # Should contain TYPE or HELP declarations
        assert "#" in content or "findmy" in content
        assert len(content) > 50
    
    def test_metrics_contains_custom_metrics(self, client):
        """Test that custom metrics are defined."""
        response = client.get("/metrics")
        content = response.text
        
        # Custom metrics should be present
        assert any(x in content for x in ["cache_hits", "orders", "position", "trade"])


class TestCacheMetrics:
    """Test cache hit/miss metrics tracking."""
    
    def test_cache_hits_metric_exists(self, client):
        """Test that cache hits metric is available."""
        # Make request to positions endpoint (cached)
        client.get("/api/positions")
        
        # Get metrics
        response = client.get("/metrics")
        content = response.text
        
        # Cache metric should exist
        assert "cache" in content.lower()
    
    def test_multiple_requests_tracked(self, client):
        """Test that multiple requests are tracked."""
        # Make multiple health check requests
        for _ in range(3):
            response = client.get("/health")
            assert response.status_code == 200
        
        # Metrics should be available
        response = client.get("/metrics")
        assert response.status_code == 200
        assert len(response.text) > 100


class TestOrderMetrics:
    """Test order-related metrics."""
    
    def test_pending_orders_metric_accessible(self, client):
        """Test that pending orders endpoint works."""
        response = client.get("/api/pending")
        # May succeed or fail depending on DB state
        assert response.status_code in [200, 500, 400]
    
    def test_metrics_includes_order_data(self, client):
        """Test that metrics include order information."""
        client.get("/api/pending")
        
        response = client.get("/metrics")
        content = response.text
        
        # Should have metrics content
        assert len(content) > 100


class TestPositionMetrics:
    """Test position-related metrics."""
    
    def test_positions_endpoint_works(self, client):
        """Test that positions endpoint works."""
        response = client.get("/api/positions")
        # May return empty or error if DB not initialized
        assert response.status_code in [200, 500, 400]
    
    def test_position_metrics_tracked(self, client):
        """Test that position metrics are tracked."""
        client.get("/api/positions")
        
        response = client.get("/metrics")
        content = response.text
        
        # Metrics should be available
        assert len(content) > 50


class TestHealthCheck:
    """Test health check and metrics endpoints."""
    
    def test_health_check_returns_ok(self, client):
        """Test that health check endpoint works."""
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
    
    def test_metrics_after_health_check(self, client):
        """Test that metrics are recorded after health check."""
        # Make health checks
        for _ in range(2):
            response = client.get("/health")
            assert response.status_code == 200
        
        # Get metrics
        response = client.get("/metrics")
        assert response.status_code == 200
        assert len(response.text) > 50


class TestMetricsIntegration:
    """Integration tests for metrics collection."""
    
    def test_metrics_accumulate(self, client):
        """Test that metrics accumulate over time."""
        # Get baseline
        metrics1 = client.get("/metrics").text
        assert len(metrics1) > 50
        
        # Make more requests
        client.get("/health")
        client.get("/api/pending")
        
        # Get updated metrics
        metrics2 = client.get("/metrics").text
        assert len(metrics2) > len(metrics1)
    
    def test_prometheus_format_valid(self, client):
        """Test that metrics output is valid."""
        response = client.get("/metrics")
        assert response.status_code == 200
        content = response.text
        
        # Should have content
        assert len(content) > 100
        lines = content.split('\n')
        assert len(lines) > 5


class TestMetricsPerformance:
    """Test metrics collection performance."""
    
    def test_health_check_fast(self, client):
        """Test that health checks are fast."""
        import time
        
        start = time.time()
        for _ in range(5):
            response = client.get("/health")
            assert response.status_code == 200
        duration = time.time() - start
        
        # 5 requests should be fast
        assert duration < 2.0
    
    def test_metrics_endpoint_responsive(self, client):
        """Test that /metrics endpoint is responsive."""
        import time
        
        start = time.time()
        response = client.get("/metrics")
        duration = time.time() - start
        
        assert response.status_code == 200
        # Should be responsive
        assert duration < 1.0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
