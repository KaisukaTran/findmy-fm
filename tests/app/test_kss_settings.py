"""Master KSS settings: persist + restore + endpoint + ladder-depth coherence."""

from __future__ import annotations

from app import runtime
from app.config import settings


def test_set_kss_settings_persists_and_restores(db):
    runtime.set_kss_settings(db, {"scan_max_waves": 6, "sl_pct": 12.0, "scan_distance_pct": 2.0})
    assert settings.scan_max_waves == 6
    assert settings.sl_pct == 12.0
    # simulate restart: reset then sync from runtime_config
    settings.scan_max_waves = 10
    settings.sl_pct = 8.0
    runtime.sync_from_db(db)
    assert settings.scan_max_waves == 6
    assert settings.sl_pct == 12.0


def test_set_kss_settings_ignores_missing(db):
    before = settings.scan_tp_pct
    runtime.set_kss_settings(db, {"sl_pct": 9.0})  # tp not provided → unchanged
    assert settings.scan_tp_pct == before
    assert settings.sl_pct == 9.0


def test_kss_settings_endpoint_roundtrip(monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app
    with TestClient(app) as c:
        r = c.post("/api/kss-settings", json={"scan_max_waves": 5, "sl_pct": 6.0})
        assert r.status_code == 200
        assert r.json()["scan_max_waves"] == 5 and r.json()["sl_pct"] == 6.0
        assert c.get("/api/kss-settings").json()["scan_max_waves"] == 5


def test_endpoint_validates_bounds(monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app
    with TestClient(app) as c:
        # max_waves 0 violates ge=1 → 422, settings untouched
        assert c.post("/api/kss-settings", json={"scan_max_waves": 0}).status_code == 422


def test_partial_shows_depth_warning(monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app
    monkeypatch.setattr(settings, "scan_distance_pct", 2.0)
    monkeypatch.setattr(settings, "scan_max_waves", 10)
    monkeypatch.setattr(settings, "sl_pct", 8.0)
    with TestClient(app) as c:
        html = c.get("/partials/kss-settings").text
        assert "−18" in html or "18.3" in html  # ladder depth ≈ 18.3%
        assert "⚠" in html  # SL 8% < 0.6×18.3% → warning
