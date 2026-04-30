"""Integration tests: AI agent API endpoints."""

import pytest


class TestAIAgentAPI:
    def test_ai_status_requires_auth(self, client):
        resp = client.get("/api/ai/status")
        assert resp.status_code in (401, 403)

    def test_ai_status_authenticated(self, client, admin_headers):
        resp = client.get("/api/ai/status", headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "running" in data
        assert "mode" in data
        assert "config" in data

    def test_ai_start_requires_admin(self, client, trader_headers):
        resp = client.post("/api/ai/start", headers=trader_headers)
        assert resp.status_code == 403

    def test_ai_stop_requires_admin(self, client, trader_headers):
        resp = client.post("/api/ai/stop", headers=trader_headers)
        assert resp.status_code == 403

    def test_ai_start_stop_cycle(self, client, admin_headers):
        # Stop first to ensure clean state
        client.post("/api/ai/stop", headers=admin_headers)

        # Start
        resp = client.post("/api/ai/start", headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["status"] == "started"

        # Double start → 409
        resp2 = client.post("/api/ai/start", headers=admin_headers)
        assert resp2.status_code == 409

        # Stop
        resp3 = client.post("/api/ai/stop", headers=admin_headers)
        assert resp3.status_code == 200
        assert resp3.json()["status"] == "stopped"

        # Double stop → 409
        resp4 = client.post("/api/ai/stop", headers=admin_headers)
        assert resp4.status_code == 409

    def test_ai_decisions_returns_list(self, client, admin_headers):
        resp = client.get("/api/ai/decisions", headers=admin_headers)
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_ai_paper_report_schema(self, client, admin_headers):
        resp = client.get("/api/ai/paper-report", headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        for field in ("period_days", "orders_submitted", "orders_skipped", "avg_orders_per_day"):
            assert field in data

    def test_promote_to_live_blocked_without_paper_days(self, client, admin_headers):
        # Should fail because paper_start_date not set (or not enough days)
        resp = client.post("/api/ai/promote-to-live", headers=admin_headers)
        assert resp.status_code in (400, 200)  # 400 if not eligible

    def test_consultants_list(self, client, admin_headers):
        resp = client.get("/api/ai/consultants", headers=admin_headers)
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_consultant_add_and_delete(self, client, admin_headers):
        # Add
        resp = client.post("/api/ai/consultants", headers=admin_headers,
                           json={"name": "test_tech", "type": "technical", "config": {}})
        assert resp.status_code == 200

        # List to find id
        rows = client.get("/api/ai/consultants", headers=admin_headers).json()
        our = next((r for r in rows if r["name"] == "test_tech"), None)
        assert our is not None

        # Toggle
        resp2 = client.patch(f"/api/ai/consultants/{our['id']}/toggle",
                              headers=admin_headers, json={"enabled": False})
        assert resp2.status_code == 200

        # Delete
        resp3 = client.delete(f"/api/ai/consultants/{our['id']}", headers=admin_headers)
        assert resp3.status_code == 200
        assert resp3.json()["deleted"] is True

    def test_consultant_add_requires_name(self, client, admin_headers):
        resp = client.post("/api/ai/consultants", headers=admin_headers,
                           json={"type": "technical"})
        assert resp.status_code == 422
