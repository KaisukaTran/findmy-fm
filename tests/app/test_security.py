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


# --- require_api_key: brute-force lockout ------------------------------------

from app.main import app as fastapi_app  # noqa: E402


@pytest.fixture
def auth_client(monkeypatch):
    """A real client against the full app, with auth ON and a strong key, and
    the brute-force window/threshold shrunk so tests stay fast."""
    monkeypatch.setattr(settings, "require_auth", True)
    monkeypatch.setattr(settings, "api_key", SecretStr("a-strong-unique-key"))
    monkeypatch.setattr(settings, "auth_max_failures", 3)
    monkeypatch.setattr(settings, "auth_lockout_window_sec", 60)
    monkeypatch.setattr(settings, "auth_lockout_sec", 30)
    security._reset_auth_throttle()
    with TestClient(fastapi_app) as c:
        yield c
    security._reset_auth_throttle()


def test_lockout_trips_after_max_failures(auth_client):
    for _ in range(settings.auth_max_failures):
        r = auth_client.post("/api/guardian", json={"enabled": True},
                              headers={"X-API-Key": "wrong-key"})
        assert r.status_code == 401

    r = auth_client.post("/api/guardian", json={"enabled": True},
                          headers={"X-API-Key": "wrong-key"})
    assert r.status_code == 429
    assert "Retry-After" in r.headers


def test_correct_key_resets_failure_counter(auth_client):
    for _ in range(settings.auth_max_failures - 1):
        r = auth_client.post("/api/guardian", json={"enabled": True},
                              headers={"X-API-Key": "wrong-key"})
        assert r.status_code == 401

    ok = auth_client.post("/api/guardian", json={"enabled": True},
                           headers={"X-API-Key": "a-strong-unique-key"})
    assert ok.status_code == 200

    # Counter reset — another round of wrong keys must NOT immediately 429.
    for _ in range(settings.auth_max_failures):
        r = auth_client.post("/api/guardian", json={"enabled": True},
                              headers={"X-API-Key": "wrong-key"})
        assert r.status_code == 401


def test_no_lockout_when_auth_disabled(monkeypatch):
    monkeypatch.setattr(settings, "require_auth", False)
    monkeypatch.setattr(settings, "auth_max_failures", 3)
    security._reset_auth_throttle()
    with TestClient(fastapi_app) as c:
        for _ in range(10):
            r = c.post("/api/guardian", json={"enabled": True},
                        headers={"X-API-Key": "wrong-key"})
            assert r.status_code == 200
    security._reset_auth_throttle()
