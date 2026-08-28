"""Auth boot-guard + CSRF Origin-check (P0 security hardening)."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app import security
from app.config import settings

# --- validate_auth_config: fail closed on an insecure posture ----------------

def test_boot_ok_with_auth_and_strong_key(monkeypatch):
    monkeypatch.setattr(settings, "require_auth", True)
    monkeypatch.setattr(settings, "live_trading", False)
    monkeypatch.setattr(settings, "api_key", SecretStr("a-strong-unique-key"))
    security.validate_auth_config()  # must not raise


def test_boot_refuses_auth_on_with_default_key(monkeypatch):
    monkeypatch.setattr(settings, "require_auth", True)
    monkeypatch.setattr(settings, "live_trading", False)
    monkeypatch.setattr(settings, "api_key", SecretStr("dev-key"))
    with pytest.raises(RuntimeError, match="default"):
        security.validate_auth_config()


def test_boot_refuses_live_without_auth(monkeypatch):
    monkeypatch.setattr(settings, "require_auth", False)
    monkeypatch.setattr(settings, "live_trading", True)
    monkeypatch.setattr(settings, "api_key", SecretStr("a-strong-unique-key"))
    with pytest.raises(RuntimeError, match="require_auth is OFF"):
        security.validate_auth_config()


def test_boot_refuses_live_with_default_key(monkeypatch):
    monkeypatch.setattr(settings, "require_auth", True)
    monkeypatch.setattr(settings, "live_trading", True)
    monkeypatch.setattr(settings, "api_key", SecretStr("change_this_api_key"))
    with pytest.raises(RuntimeError):
        security.validate_auth_config()


def test_boot_ok_paper_auth_off(monkeypatch):
    """Auth off + paper is a permitted (local demo) posture — no raise."""
    monkeypatch.setattr(settings, "require_auth", False)
    monkeypatch.setattr(settings, "live_trading", False)
    monkeypatch.setattr(settings, "api_key", SecretStr("dev-key"))
    security.validate_auth_config()  # must not raise


# --- CSRF Origin middleware ---------------------------------------------------

@pytest.fixture()
def csrf_app(monkeypatch):
    monkeypatch.setattr(settings, "cors_origins",
                        ["http://localhost:8000", "http://127.0.0.1:8000"])
    app = FastAPI()
    app.add_middleware(security.CSRFOriginMiddleware)

    @app.post("/mutate")
    def _mutate():
        return {"ok": True}

    @app.get("/read")
    def _read():
        return {"ok": True}

    return app


def test_csrf_blocks_foreign_origin(csrf_app):
    c = TestClient(csrf_app)
    r = c.post("/mutate", headers={"Origin": "http://evil.example"})
    assert r.status_code == 403


def test_csrf_allows_same_origin(csrf_app):
    c = TestClient(csrf_app)
    r = c.post("/mutate", headers={"Origin": "http://localhost:8000"})
    assert r.status_code == 200


def test_csrf_allows_request_host_origin(csrf_app):
    c = TestClient(csrf_app)
    # Origin equals the request Host (typical same-origin browser fetch)
    r = c.post("/mutate", headers={"Origin": "http://testserver", "Host": "testserver"})
    assert r.status_code == 200


def test_csrf_allows_no_origin_client(csrf_app):
    """curl / server-to-server (no Origin header) is not a CSRF vector — passes."""
    c = TestClient(csrf_app)
    assert c.post("/mutate").status_code == 200


def test_csrf_ignores_get(csrf_app):
    c = TestClient(csrf_app)
    r = c.get("/read", headers={"Origin": "http://evil.example"})
    assert r.status_code == 200
