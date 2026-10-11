"""Entry-timing gate (_falling_knife_veto): don't open a DCA ladder into a coin whose
short-term momentum is falling (Supertrend down AND MACD histogram negative).

Tighter than _downtrend_veto (which needs HTF+ST+ADX all confirming) — it catches the mild
short-term drops that left freshly-opened sessions sitting red. Toggle: entry_momentum_gate."""

from __future__ import annotations

from app import scanner
from app.config import settings


def _ta(st, macd_h, htf="up", adx=10.0):
    return {"st": st, "macd_h": macd_h, "htf": htf, "adx": adx}


def test_falling_knife_vetoes_st_down_macd_negative(monkeypatch):
    monkeypatch.setattr(settings, "entry_momentum_gate", True)
    assert scanner._falling_knife_veto(_ta("down", -0.5)) is not None


def test_falling_knife_allows_when_momentum_not_falling(monkeypatch):
    monkeypatch.setattr(settings, "entry_momentum_gate", True)
    assert scanner._falling_knife_veto(_ta("up", 0.5)) is None     # clear uptrend
    assert scanner._falling_knife_veto(_ta("up", -0.5)) is None    # ST up → not a knife
    assert scanner._falling_knife_veto(_ta("down", 0.3)) is None   # MACDh positive → turning up


def test_falling_knife_toggle_off(monkeypatch):
    monkeypatch.setattr(settings, "entry_momentum_gate", False)
    assert scanner._falling_knife_veto(_ta("down", -0.5)) is None


def test_entry_momentum_gate_is_runtime_tunable():
    from app.runtime import KSS_SETTING_FIELDS
    assert "entry_momentum_gate" in KSS_SETTING_FIELDS


# --- entry_rsi_max: skip a 'trade' candidate whose daily RSI(14) is hot (>= limit) ----------


def test_rsi_veto_fires_at_or_above_limit(monkeypatch):
    monkeypatch.setattr(settings, "entry_rsi_max", 60.0)
    assert scanner._entry_rsi_veto({"rsi": 75.0}) is not None
    assert scanner._entry_rsi_veto({"rsi": 60.0}) is not None   # equal counts as a veto
    assert "60" in scanner._entry_rsi_veto({"rsi": 61.0})


def test_rsi_veto_passes_below_limit(monkeypatch):
    monkeypatch.setattr(settings, "entry_rsi_max", 60.0)
    assert scanner._entry_rsi_veto({"rsi": 59.9}) is None
    assert scanner._entry_rsi_veto({"rsi": 30.0}) is None


def test_rsi_veto_off_when_knob_zero(monkeypatch):
    monkeypatch.setattr(settings, "entry_rsi_max", 0.0)
    assert scanner._entry_rsi_veto({"rsi": 99.0}) is None


def test_rsi_veto_none_when_rsi_missing(monkeypatch):
    monkeypatch.setattr(settings, "entry_rsi_max", 60.0)
    assert scanner._entry_rsi_veto({}) is None
    assert scanner._entry_rsi_veto({"rsi": None}) is None


def test_entry_rsi_max_default_is_off():
    from app.config import Settings
    assert Settings.model_fields["entry_rsi_max"].default == 0.0


def test_entry_rsi_max_is_runtime_tunable_and_snapshotted():
    from app.runtime import KSS_SETTING_FIELDS
    assert "entry_rsi_max" in KSS_SETTING_FIELDS
    assert "entry_rsi_max" in scanner._SNAPSHOT_KNOBS


def test_entry_rsi_max_endpoint_roundtrip_and_bounds():
    from fastapi.testclient import TestClient

    from app.main import app
    with TestClient(app) as c:
        r = c.post("/api/kss-settings", json={"entry_rsi_max": 60})
        assert r.status_code == 200
        assert r.json()["entry_rsi_max"] == 60
        assert c.get("/api/kss-settings").json()["entry_rsi_max"] == 60
        assert 'name="entry_rsi_max"' in c.get("/partials/kss-settings").text
        assert c.post("/api/kss-settings", json={"entry_rsi_max": 101}).status_code == 422
        assert c.post("/api/kss-settings", json={"entry_rsi_max": -1}).status_code == 422
        c.post("/api/kss-settings", json={"entry_rsi_max": 0})


def test_entry_rsi_max_persists_and_restores(db):
    from app import runtime
    runtime.set_kss_settings(db, {"entry_rsi_max": 65.0})
    assert settings.entry_rsi_max == 65.0
    settings.entry_rsi_max = 0.0  # corrupt in-memory, then restore from DB
    runtime.sync_from_db(db)
    assert settings.entry_rsi_max == 65.0
    runtime.set_kss_settings(db, {"entry_rsi_max": 0.0})
