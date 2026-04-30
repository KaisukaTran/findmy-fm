"""Integration tests: emergency stop / resume across workers (DB-backed)."""

import pytest
from services.sot.system_state import is_halted, set_halt


class TestEmergencyStop:
    def test_halt_state_persists_across_reads(self):
        """DB-backed halt is visible from any call (worker-safe)."""
        set_halt(True)
        assert is_halted() is True
        set_halt(False)
        assert is_halted() is False

    def test_emergency_stop_endpoint_requires_admin(self, client, trader_headers):
        """Trader cannot activate emergency stop."""
        resp = client.post("/api/emergency-stop", headers=trader_headers)
        assert resp.status_code == 403
        assert is_halted() is False

    def test_emergency_stop_endpoint_ok_for_admin(self, client, admin_headers):
        resp = client.post("/api/emergency-stop", headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["status"] == "halted"
        assert is_halted() is True

    def test_emergency_resume_clears_halt(self, client, admin_headers):
        set_halt(True)
        resp = client.post("/api/emergency-resume", headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["status"] == "active"
        assert is_halted() is False

    def test_approve_blocked_when_halted(self, client, admin_headers):
        """approve endpoint returns 503 when halt is active."""
        from services.sot.pending_orders_service import queue_order
        order, _ = queue_order("BTC", "BUY", 0.001, 50000.0, source="test")

        set_halt(True)
        resp = client.post(
            f"/api/pending/approve/{order.id}",
            headers=admin_headers,
        )
        assert resp.status_code == 503
        assert "halt" in resp.json()["detail"].lower()

    def test_approve_allowed_after_resume(self, client, admin_headers):
        """Orders can be approved after halt is cleared."""
        from services.sot.pending_orders_service import queue_order
        order, _ = queue_order("ETH", "BUY", 0.01, 3000.0, source="test")

        set_halt(False)
        resp = client.post(
            f"/api/pending/approve/{order.id}",
            headers=admin_headers,
        )
        # 200 = approved; circuit breaker may reject if over limit → 400 is also OK
        assert resp.status_code in (200, 400)

    def test_system_status_endpoint(self, client):
        set_halt(False)
        resp = client.get("/api/system/status")
        assert resp.status_code == 200
        assert resp.json()["emergency_halt"] is False
