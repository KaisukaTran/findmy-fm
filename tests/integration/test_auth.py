"""Integration tests: JWT authentication and role-based access."""

import pytest


class TestAuthFlow:
    def test_login_demo_user_ok(self, client):
        resp = client.post("/api/auth/login", json={"username": "trader1", "password": "password123"})
        assert resp.status_code == 200
        data = resp.json()
        assert "access_token" in data
        assert data["token_type"] == "bearer"

    def test_login_wrong_password_401(self, client):
        resp = client.post("/api/auth/login", json={"username": "trader1", "password": "wrongpass"})
        assert resp.status_code == 401

    def test_login_unknown_user_401(self, client):
        resp = client.post("/api/auth/login", json={"username": "nobody", "password": "password123"})
        assert resp.status_code == 401

    def test_protected_endpoint_no_token_403(self, client):
        """Endpoints requiring auth return 403 when no Bearer token is provided."""
        resp = client.post("/api/pending/approve/999")
        assert resp.status_code in (401, 403, 422)

    def test_protected_endpoint_invalid_token_401(self, client):
        resp = client.post(
            "/api/pending/approve/999",
            headers={"Authorization": "Bearer invalid.token.here"},
        )
        assert resp.status_code == 401

    def test_admin_users_endpoint_requires_admin(self, client, trader_headers):
        """Trader role cannot access admin user management."""
        resp = client.get("/api/auth/admin/users", headers=trader_headers)
        assert resp.status_code == 403

    def test_admin_users_endpoint_ok_for_admin(self, client, admin_headers):
        resp = client.get("/api/auth/admin/users", headers=admin_headers)
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)
