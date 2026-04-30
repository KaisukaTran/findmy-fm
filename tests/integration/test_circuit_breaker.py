"""Integration tests: circuit breaker rules."""

import pytest
from services.trading.circuit_breaker import check, CircuitBreakerResult, MAX_ORDERS_PER_MINUTE


class TestCircuitBreakerRules:
    def test_small_order_passes(self):
        """Order within position size limit is allowed."""
        # 0.001 BTC @ 50k = $50, well under 10% of $10k fund
        result = check("BTC", 0.001, 50000.0)
        assert result.allowed is True
        assert result.violations == []

    def test_oversized_order_blocked(self):
        """Order exceeding max_position_size_pct is blocked."""
        # 1 BTC @ 50k = $50,000, over $10k fund entirely
        result = check("BTC", 1.0, 50000.0)
        assert result.allowed is False
        assert any("position size" in v.lower() for v in result.violations)

    def test_result_has_violation_message(self):
        """Violations list is populated with human-readable messages."""
        result = check("BTC", 100.0, 50000.0)
        assert len(result.violations) > 0
        for v in result.violations:
            assert isinstance(v, str)
            assert len(v) > 0

    def test_zero_price_handled(self):
        """Zero-price order (market order preview) does not crash."""
        result = check("BTC", 0.001, 0.0)
        # $0 order value — should pass position-size check
        assert result.allowed is True


class TestCircuitBreakerAPI:
    def test_circuit_status_endpoint(self, client):
        resp = client.get("/api/system/circuit-status")
        assert resp.status_code == 200
        data = resp.json()
        assert "max_position_size_pct" in data
        assert "max_orders_per_minute" in data
        assert "orders_last_minute" in data
        assert data["max_orders_per_minute"] == MAX_ORDERS_PER_MINUTE

    def test_approve_oversized_order_blocked(self, client, admin_headers):
        """Approving an oversized order returns 400 from circuit breaker."""
        from services.sot.pending_orders_service import queue_order
        # Queue a massive order: 999 BTC @ $50k = $49.95M
        order, _ = queue_order("BTC", "BUY", 999.0, 50000.0, source="test")

        resp = client.post(
            f"/api/pending/approve/{order.id}",
            headers=admin_headers,
        )
        assert resp.status_code == 400
        assert "circuit breaker" in resp.json()["detail"].lower()
