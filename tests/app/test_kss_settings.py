"""Master KSS settings: persist + restore + endpoint + ladder-depth coherence."""

from __future__ import annotations

from app import runtime
from app.config import settings


def test_kss_settings_body_accepts_new_knobs():
    """Regression: the API/form body must include the dynamic-TP + entry-eval knobs, else
    model_dump(exclude_none=True) silently drops them and the Strategy form can't save them."""
    from app.routes import KssSettingsBody

    dumped = KssSettingsBody(
        rel_strength_enabled=True, regime_ramp_enabled=True, mae_quartile_gate_enabled=True,
        kss_dynamic_tp_enabled=True, kss_tp_gap_pct=6.0, entry_momentum_gate=False,
    ).model_dump(exclude_none=True)
    for k in ("rel_strength_enabled", "regime_ramp_enabled", "mae_quartile_gate_enabled",
              "kss_dynamic_tp_enabled", "kss_tp_gap_pct", "entry_momentum_gate"):
        assert k in dumped, f"{k} dropped by KssSettingsBody"
    assert dumped["kss_tp_gap_pct"] == 6.0


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


def test_min_net_edge_persists_and_restores(db):
    """B9: min_net_edge must be runtime-editable + survive a restart, like every other gate."""
    runtime.set_kss_settings(db, {"min_net_edge": 0.8})
    assert settings.min_net_edge == 0.8
    settings.min_net_edge = 0.5  # corrupt in-memory, then restore from runtime_config
    runtime.sync_from_db(db)
    assert settings.min_net_edge == 0.8


def test_min_net_edge_endpoint_roundtrip():
    from fastapi.testclient import TestClient

    from app.main import app
    with TestClient(app) as c:
        r = c.post("/api/kss-settings", json={"min_net_edge": 0.75})
        assert r.status_code == 200
        assert r.json()["min_net_edge"] == 0.75
        assert c.get("/api/kss-settings").json()["min_net_edge"] == 0.75


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


def test_exchange_timeout_sec_persists_and_restores(db):
    """2026-09-03 hang hardening: the ccxt socket timeout must be a real runtime knob, not a
    literal — else fixing the hang requires an edit + restart instead of a Strategy-tab save."""
    runtime.set_kss_settings(db, {"exchange_timeout_sec": 12.5})
    assert settings.exchange_timeout_sec == 12.5
    settings.exchange_timeout_sec = 20.0  # corrupt in-memory, then restore from DB
    runtime.sync_from_db(db)
    assert settings.exchange_timeout_sec == 12.5


def test_exchange_timeout_sec_endpoint_and_partial_render():
    from fastapi.testclient import TestClient

    from app.main import app
    with TestClient(app) as c:
        r = c.post("/api/kss-settings", json={"exchange_timeout_sec": 15.0})
        assert r.status_code == 200
        assert r.json()["exchange_timeout_sec"] == 15.0
        assert c.get("/api/kss-settings").json()["exchange_timeout_sec"] == 15.0
        html = c.get("/partials/kss-settings").text
        assert 'name="exchange_timeout_sec"' in html


def test_exchange_timeout_sec_endpoint_validates_bounds():
    from fastapi.testclient import TestClient

    from app.main import app
    with TestClient(app) as c:
        # 0.5 violates ge=1.0 -> 422, settings untouched
        assert c.post("/api/kss-settings", json={"exchange_timeout_sec": 0.5}).status_code == 422


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


# --- P4 Fix 4: the three new/orphan knobs must be VISIBLE runtime knobs, not buried in
# .env — kss_ladder_reserve_slack_pct (added in P1, never wired into the form/app.js payload
# until now), min_net_edge and heartbeat_url (both new in this phase). ---------------------


def test_kss_ladder_reserve_slack_pct_persists_and_restores(db):
    runtime.set_kss_settings(db, {"kss_ladder_reserve_slack_pct": 3.0})
    assert settings.kss_ladder_reserve_slack_pct == 3.0
    settings.kss_ladder_reserve_slack_pct = 1.0  # corrupt in-memory, then restore from DB
    runtime.sync_from_db(db)
    assert settings.kss_ladder_reserve_slack_pct == 3.0


def test_heartbeat_url_persists_and_restores(db):
    runtime.set_kss_settings(db, {"heartbeat_url": "https://hc-ping.com/xyz"})
    assert settings.heartbeat_url == "https://hc-ping.com/xyz"
    settings.heartbeat_url = ""  # corrupt in-memory, then restore from DB
    runtime.sync_from_db(db)
    assert settings.heartbeat_url == "https://hc-ping.com/xyz"


def test_the_three_new_knobs_persist_through_the_endpoint_and_render_in_the_partial():
    """The submit handler in app.js builds its POST payload from an EXPLICIT field list — a
    field missing there is silently dropped on save (bit this project before). This exercises
    the same round trip the browser does: POST /api/kss-settings, then re-render the partial
    and check both the field AND its current value are present."""
    from fastapi.testclient import TestClient

    from app.main import app
    with TestClient(app) as c:
        r = c.post("/api/kss-settings", json={
            "kss_ladder_reserve_slack_pct": 2.5,
            "min_net_edge": 0.6,
            "heartbeat_url": "https://hc-ping.com/abc123",
            "placement_alert_after": 5,
        })
        assert r.status_code == 200
        body = r.json()
        assert body["kss_ladder_reserve_slack_pct"] == 2.5
        assert body["min_net_edge"] == 0.6
        assert body["heartbeat_url"] == "https://hc-ping.com/abc123"
        assert body["placement_alert_after"] == 5

        html = c.get("/partials/kss-settings").text
        assert 'name="kss_ladder_reserve_slack_pct"' in html
        assert 'name="min_net_edge"' in html
        assert 'name="heartbeat_url"' in html
        assert "https://hc-ping.com/abc123" in html  # current value rendered back
        assert 'name="placement_alert_after" type="number" step="1" min="1" value="5"' in html
