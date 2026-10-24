"""S1: real money is never armed while auth is off, and a persisted live flag that no longer
fits the auth posture is forced OFF at boot (fail closed, never raise).

Paper ships with REQUIRE_AUTH=false, which makes `require_api_key` a no-op — so before this, one
unauthenticated POST to /api/live-trading could arm the master switch, and the persisted flag was
restored by `runtime.sync_from_db` AFTER the import-time `validate_auth_config` boot guard ran.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy.orm import Session

from app import execution, runtime, security
from app.config import settings
from app.main import app
from app.models import AuditLog

_STRONG_KEY = "a-strong-test-key-0123456789"


@pytest.fixture(autouse=True)
def _keys_present(monkeypatch):
    """No real exchange key is needed — only the auth gate is under test."""
    monkeypatch.setattr(execution, "live_key_present", lambda: True)


def _forced_off_rows(db) -> list[AuditLog]:
    return db.query(AuditLog).filter(AuditLog.action == "live_forced_off").all()


# --- the route ---------------------------------------------------------------


def test_enable_refused_without_auth(db, monkeypatch):
    monkeypatch.setattr(settings, "require_auth", False)
    with TestClient(app) as c:
        r = c.post("/api/live-trading", json={"enabled": True, "confirm": "LIVE-TRADING"})
    assert r.status_code == 409
    assert "REQUIRE_AUTH" in r.json()["detail"]
    assert runtime.get(db, runtime.KEY_LIVE_TRADING) != "1"
    assert settings.live_trading is False


def test_enable_allowed_with_auth(monkeypatch):
    monkeypatch.setattr(settings, "require_auth", True)
    monkeypatch.setattr(settings, "api_key", SecretStr(_STRONG_KEY))
    headers = {"X-API-Key": _STRONG_KEY}
    with TestClient(app) as c:
        r = c.post("/api/live-trading", headers=headers,
                   json={"enabled": True, "confirm": "LIVE-TRADING"})
        assert r.status_code == 200 and r.json()["live_trading"] is True
        assert settings.live_trading is True
        # leave the switch off (the conftest restores `settings`, not the DB row)
        r = c.post("/api/live-trading", headers=headers, json={"enabled": False})
        assert r.status_code == 200 and r.json()["live_trading"] is False


def test_disable_always_allowed(monkeypatch):
    monkeypatch.setattr(settings, "require_auth", False)
    with TestClient(app) as c:
        # set AFTER the lifespan: boot would (rightly) force this combination off
        monkeypatch.setattr(settings, "live_trading", True)
        r = c.post("/api/live-trading", json={"enabled": False})
    assert r.status_code == 200 and r.json()["live_trading"] is False
    assert settings.live_trading is False


# --- the boot-time posture check ---------------------------------------------


def test_posture_forces_live_off(db, monkeypatch):
    monkeypatch.setattr(settings, "require_auth", False)
    monkeypatch.setattr(settings, "api_key", SecretStr(_STRONG_KEY))
    runtime.set(db, runtime.KEY_LIVE_TRADING, "1")
    runtime.sync_from_db(db)
    assert settings.live_trading is True        # the restore that the import-time guard never saw

    msg = security.enforce_auth_posture(db)

    assert isinstance(msg, str) and msg
    assert settings.live_trading is False
    assert runtime.get_bool(db, runtime.KEY_LIVE_TRADING) is False
    rows = _forced_off_rows(db)
    assert len(rows) == 1 and rows[0].actor == "security"
    assert "require_auth" in rows[0].detail


def test_posture_forces_off_on_default_key(db, monkeypatch):
    monkeypatch.setattr(settings, "require_auth", True)
    monkeypatch.setattr(settings, "api_key", SecretStr("dev-key"))
    monkeypatch.setattr(settings, "live_trading", True)

    msg = security.enforce_auth_posture(db)

    assert msg
    assert "dev-key" not in msg                 # never echo the key
    assert settings.live_trading is False
    assert runtime.get_bool(db, runtime.KEY_LIVE_TRADING) is False
    assert len(_forced_off_rows(db)) == 1


def test_posture_noop_when_safe(db, monkeypatch):
    monkeypatch.setattr(settings, "require_auth", True)
    monkeypatch.setattr(settings, "api_key", SecretStr(_STRONG_KEY))
    monkeypatch.setattr(settings, "live_trading", True)

    assert security.enforce_auth_posture(db) is None
    assert settings.live_trading is True
    assert _forced_off_rows(db) == []


def test_posture_noop_when_live_is_off(db, monkeypatch):
    """Paper (live off) with auth off is the normal posture — nothing to enforce or audit."""
    monkeypatch.setattr(settings, "require_auth", False)
    monkeypatch.setattr(settings, "live_trading", False)

    assert security.enforce_auth_posture(db) is None
    assert _forced_off_rows(db) == []


def test_posture_never_raises_when_db_write_fails(db, monkeypatch):
    """A raise inside lifespan makes uvicorn exit and the watchdog restart-loop; the flag must
    still end up off in memory and the reason still come back."""
    monkeypatch.setattr(settings, "require_auth", False)
    monkeypatch.setattr(settings, "live_trading", True)

    def boom(*a, **kw):
        raise RuntimeError("db is down")

    monkeypatch.setattr(runtime, "set_live_trading", boom)
    msg = security.enforce_auth_posture(db)

    assert msg
    assert settings.live_trading is False


def test_lifespan_enforces_posture(monkeypatch):
    calls: list[object] = []

    def spy(db):
        calls.append(db)
        return None

    monkeypatch.setattr("app.security.enforce_auth_posture", spy)
    with TestClient(app):
        pass
    assert len(calls) == 1 and isinstance(calls[0], Session)
