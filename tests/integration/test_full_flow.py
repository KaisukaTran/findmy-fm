"""Integration tests: paper trading full flow.

queue_order → approve → position created (paper execution side-effects
are not fully exercised here because they depend on Trade Service DB
schema, which may be seeded separately). Focus is on the API contract.
"""

import pytest


class TestPaperTradingFlow:
    def test_queue_and_list_pending(self, client):
        """Queue an order, verify it appears in /api/pending."""
        from services.sot.pending_orders_service import queue_order
        order, _ = queue_order("BTC", "BUY", 0.001, 50000.0, source="test")

        resp = client.get("/api/pending?status=pending")
        assert resp.status_code == 200
        ids = [o["id"] for o in resp.json()]
        assert order.id in ids

    def test_approve_order_changes_status(self, client, admin_headers):
        """Approved order transitions from pending → approved."""
        from services.sot.pending_orders_service import queue_order, get_pending_orders
        order, _ = queue_order("ETH", "BUY", 0.01, 3000.0, source="test")

        resp = client.post(f"/api/pending/approve/{order.id}", headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["status"] == "approved"

    def test_reject_order_changes_status(self, client, admin_headers):
        from services.sot.pending_orders_service import queue_order
        order, _ = queue_order("ETH", "SELL", 0.01, 3000.0, source="test")

        resp = client.post(
            f"/api/pending/reject/{order.id}?note=test+rejection",
            headers=admin_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "rejected"

    def test_approve_already_approved_returns_400(self, client, admin_headers):
        """Double-approving same order is rejected."""
        from services.sot.pending_orders_service import queue_order
        order, _ = queue_order("BTC", "BUY", 0.001, 50000.0, source="test")

        client.post(f"/api/pending/approve/{order.id}", headers=admin_headers)
        resp = client.post(f"/api/pending/approve/{order.id}", headers=admin_headers)
        assert resp.status_code == 400

    def test_approve_nonexistent_order_400(self, client, admin_headers):
        resp = client.post("/api/pending/approve/999999", headers=admin_headers)
        assert resp.status_code == 400

    def test_summary_endpoint_returns_schema(self, client):
        resp = client.get("/api/summary")
        assert resp.status_code == 200
        data = resp.json()
        for field in ("total_trades", "realized_pnl", "unrealized_pnl",
                      "initial_fund", "available_fund", "fund_utilization_pct"):
            assert field in data, f"Missing field: {field}"

    def test_health_endpoint(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] in ("ok", "degraded", "unhealthy")
        assert "components" in data
