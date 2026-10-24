"""R6: the circuit-breaker thresholds are runtime knobs (persisted, on the Strategy tab), and a
persisted full_auto OFF beats `.env FULL_AUTO=true`.

Before: `max_drawdown_pct` / `daily_loss_hard_pct` / `max_consecutive_losses` / `breaker_cooldown_min`
were `.env`-only (safety knobs the operator could not see or change at runtime), and
`sync_from_db` used `settings.full_auto or persisted` - so a dashboard OFF was silently undone by
the next restart whenever `.env` said true.
"""

from __future__ import annotations

import pytest

from app import circuit, runtime
from app.config import settings

_KNOBS = {
    "max_drawdown_pct": 10.0,
    "daily_loss_hard_pct": 3.0,
    "max_consecutive_losses": 6,
    "breaker_cooldown_min": 30,
}


@pytest.fixture(autouse=True)
def _restore(monkeypatch):
    """`set_kss_settings` writes `settings` directly; have monkeypatch put the originals back."""
    for name in _KNOBS:
        monkeypatch.setattr(settings, name, getattr(settings, name))


# --- the four thresholds ---------------------------------------------------------------------------


def test_thresholds_persist_via_api(db):
    from fastapi.testclient import TestClient

    from app.main import app

    runtime.set_kss_settings(db, _KNOBS)
    for name, value in _KNOBS.items():
        assert getattr(settings, name) == value
        assert runtime.kss_settings(db)[name] == value
    for name in _KNOBS:                                  # simulate a restart: memory forgets
        setattr(settings, name, 1)
    runtime.sync_from_db(db)
    for name, value in _KNOBS.items():
        assert getattr(settings, name) == value
        assert type(getattr(settings, name)) is type(value)  # ints stay ints

    body = {"max_drawdown_pct": 12.5, "daily_loss_hard_pct": 4.5,
            "max_consecutive_losses": 5, "breaker_cooldown_min": 45}
    with TestClient(app) as c:
        r = c.post("/api/kss-settings", json=body)
        assert r.status_code == 200
        got = c.get("/api/kss-settings").json()
        assert {k: got[k] for k in body} == body
        for bad in ({"max_drawdown_pct": 0}, {"max_drawdown_pct": 101},
                    {"daily_loss_hard_pct": 0}, {"max_consecutive_losses": 0},
                    {"max_consecutive_losses": 51}, {"breaker_cooldown_min": -1},
                    {"breaker_cooldown_min": 1441}):
            assert c.post("/api/kss-settings", json=bad).status_code == 422, bad
        html = c.get("/partials/kss-settings").text      # the Strategy tab renders the rows
        for name in body:
            assert f'name="{name}"' in html


def test_evaluate_uses_persisted_threshold(db, monkeypatch):
    monkeypatch.setattr(circuit, "metrics", lambda db: {
        "drawdown_pct": 12.0, "daily_loss_pct": 0.0, "consecutive_losses": 0})
    monkeypatch.setattr(settings, "max_drawdown_pct", 15.0)
    assert circuit.evaluate(db)["frozen"] is False       # 12 is under the .env/default 15

    runtime.set_kss_settings(db, {"max_drawdown_pct": 10})
    out = circuit.evaluate(db)

    assert out["frozen"] is True
    assert any("drawdown" in r.lower() for r in out["reasons"])


def test_breaker_endpoint_reports_thresholds(db):
    from fastapi.testclient import TestClient

    from app.main import app

    runtime.set_kss_settings(db, {"max_drawdown_pct": 11, "max_consecutive_losses": 7})
    with TestClient(app) as c:
        th = c.get("/api/breaker").json()["thresholds"]
    assert th["max_drawdown_pct"] == 11 and th["max_consecutive_losses"] == 7


# --- a persisted full_auto beats .env ---------------------------------------------------------------


@pytest.fixture
def env_state(monkeypatch):
    """Start every full_auto test from a known in-memory state; return a setter for `.env`."""
    monkeypatch.setattr(settings, "auto_trade", False)
    monkeypatch.setattr(settings, "autoapprove_enabled", False)

    def set_env(full_auto: bool) -> None:
        monkeypatch.setattr(settings, "full_auto", full_auto)

    return set_env


def test_persisted_full_auto_off_beats_env(db, env_state):
    env_state(True)                                       # .env FULL_AUTO=true
    runtime.set(db, runtime.KEY_FULL_AUTO, "0")           # the dashboard turned it OFF

    runtime.sync_from_db(db)

    assert settings.full_auto is False
    assert settings.auto_trade is False and settings.autoapprove_enabled is False  # no cascade


def test_env_full_auto_applies_without_db_key(db, env_state):
    env_state(True)
    assert runtime.get(db, runtime.KEY_FULL_AUTO) is None

    runtime.sync_from_db(db)

    assert settings.full_auto is True
    assert settings.auto_trade is True and settings.autoapprove_enabled is True


def test_persisted_on_beats_env_off(db, env_state):
    env_state(False)
    runtime.set(db, runtime.KEY_FULL_AUTO, "1")

    runtime.sync_from_db(db)

    assert settings.full_auto is True
    assert settings.auto_trade is True and settings.autoapprove_enabled is True


def test_auto_trade_override_still_wins(db, env_state):
    env_state(False)
    runtime.set(db, runtime.KEY_FULL_AUTO, "1")
    runtime.set(db, runtime.KEY_AUTO_TRADE, "0")          # explicit OFF outranks the cascade

    runtime.sync_from_db(db)

    assert settings.full_auto is True
    assert settings.auto_trade is False
