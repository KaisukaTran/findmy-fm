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


def test_max_sessions_per_symbol_persists_and_restores(db):
    # K-1 cap is now runtime-editable (root fix for duplicate sessions per coin).
    runtime.set_kss_settings(db, {"max_sessions_per_symbol": 1})
    assert settings.max_sessions_per_symbol == 1
    settings.max_sessions_per_symbol = 9  # corrupt in-memory, then restore from runtime_config
    runtime.sync_from_db(db)
    assert settings.max_sessions_per_symbol == 1


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


def test_live_exec_knobs_persist_and_restore(db):
    runtime.set_kss_settings(db, {
        "maker_orders": True, "order_fill_timeout_sec": 30, "live_use_testnet": True,
    })
    assert settings.maker_orders
    assert settings.order_fill_timeout_sec == 30
    assert settings.live_use_testnet
    # simulate restart: reset then sync from runtime_config
    settings.maker_orders = False
    settings.order_fill_timeout_sec = 0
    settings.live_use_testnet = False
    runtime.sync_from_db(db)
    assert settings.maker_orders
    assert settings.order_fill_timeout_sec == 30
    assert settings.live_use_testnet


def test_live_bool_false_restores_as_false(db):
    # The naive `bool("0")` cast is truthy → a disabled flag would wrongly restore True.
    # _to_bool must round-trip a stored False back to False.
    runtime.set_kss_settings(db, {"maker_orders": True})
    runtime.set_kss_settings(db, {"maker_orders": False})
    assert not settings.maker_orders
    settings.maker_orders = True  # corrupt in-memory, then restore from DB
    runtime.sync_from_db(db)
    assert not settings.maker_orders


def test_live_exec_endpoint_and_partial_render():
    from fastapi.testclient import TestClient

    from app.main import app
    with TestClient(app) as c:
        r = c.post("/api/kss-settings", json={
            "maker_orders": True, "live_use_testnet": True, "order_fill_timeout_sec": 45,
        })
        assert r.status_code == 200
        body = r.json()
        assert body["maker_orders"] and body["live_use_testnet"]
        assert body["order_fill_timeout_sec"] == 45
        html = c.get("/partials/kss-settings").text
        assert 'name="maker_orders"' in html
        assert 'name="live_use_testnet"' in html
        assert 'name="order_fill_timeout_sec"' in html
        assert "VIP0" in html  # BNB / fee-reality note is present


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
