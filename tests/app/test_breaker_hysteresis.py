"""C3: the breaker re-arms only once drawdown (and daily loss) is a hysteresis margin BELOW its
freeze limit, so a book hovering at the limit stops oscillating freeze -> rearm -> freeze.
The freeze thresholds themselves never move, and a stale loss streak still never blocks rearm."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app import circuit, models, runtime
from app.config import settings


def _freeze_ago(db, minutes):
    runtime.freeze(db, "test")
    runtime.set(db, runtime.KEY_FROZEN_AT, (datetime.utcnow() - timedelta(minutes=minutes)).isoformat())


def _metrics(monkeypatch, dd=0.0, daily=0.0, streak=0):
    monkeypatch.setattr(circuit, "metrics",
                        lambda db: {"drawdown_pct": dd, "daily_loss_pct": daily,
                                    "consecutive_losses": streak})


@pytest.fixture(autouse=True)
def _limits(monkeypatch):
    monkeypatch.setattr(settings, "max_drawdown_pct", 15.0)
    monkeypatch.setattr(settings, "daily_loss_hard_pct", 5.0)
    monkeypatch.setattr(settings, "max_consecutive_losses", 4)
    monkeypatch.setattr(settings, "breaker_cooldown_min", 60)
    monkeypatch.setattr(settings, "breaker_rearm_hysteresis_pct", 2.0)


def _rearms(db) -> int:
    return db.query(models.AuditLog).filter_by(action="rearm").count()


def test_rearm_waits_for_hysteresis(db, monkeypatch):
    _metrics(monkeypatch, dd=14.8)  # under the 15 freeze limit, but not under 15 - 2
    _freeze_ago(db, 90)

    out = circuit.evaluate(db)

    assert out["frozen"] is True
    assert out["rearm_floor_pct"] == 13.0
    assert out["rearm_blocked_by"] == "hysteresis"
    assert _rearms(db) == 0  # a blocked rearm runs every cycle and must not write audit rows


def test_rearm_when_below_floor(db, monkeypatch):
    _metrics(monkeypatch, dd=12.9)
    _freeze_ago(db, 90)

    out = circuit.evaluate(db)

    assert out["frozen"] is False
    assert out["rearm_blocked_by"] is None
    assert _rearms(db) == 1


def test_hysteresis_zero_is_old_behaviour(db, monkeypatch):
    monkeypatch.setattr(settings, "breaker_rearm_hysteresis_pct", 0.0)
    _metrics(monkeypatch, dd=14.8)
    _freeze_ago(db, 90)

    out = circuit.evaluate(db)

    assert out["frozen"] is False
    assert out["rearm_floor_pct"] == 15.0


def test_daily_loss_hysteresis(db, monkeypatch):
    _metrics(monkeypatch, dd=0.0, daily=3.5)  # inside the 5 limit, outside 5 - 2
    _freeze_ago(db, 90)
    out = circuit.evaluate(db)
    assert out["frozen"] is True and out["rearm_blocked_by"] == "hysteresis"

    _metrics(monkeypatch, dd=0.0, daily=2.9)
    out = circuit.evaluate(db)
    assert out["frozen"] is False and _rearms(db) == 1


def test_cooldown_still_required(db, monkeypatch):
    _metrics(monkeypatch, dd=10.0)  # comfortably clear...
    _freeze_ago(db, 10)  # ...but the 60 min cooldown has not elapsed

    out = circuit.evaluate(db)

    assert out["frozen"] is True
    assert out["rearm_blocked_by"] == "cooldown"


def test_freeze_threshold_unchanged(db, monkeypatch):
    """The margin is on the REARM side only: it neither lowers nor raises where the breaker trips."""
    _metrics(monkeypatch, dd=14.9)
    out = circuit.evaluate(db)
    assert out["frozen"] is False and out["rearm_blocked_by"] is None  # 14.9 < 15 → no freeze

    _metrics(monkeypatch, dd=15.1)
    out = circuit.evaluate(db)
    assert out["frozen"] is True
    assert out["reason_codes"] == [circuit.CODE_DRAWDOWN]


def test_streak_freeze_still_rearms_after_cooldown(db, monkeypatch):
    """Regression of the deadlock fix: a stale streak never blocks rearm; only current state does."""
    _metrics(monkeypatch, dd=0.2, daily=0.0, streak=4)
    _freeze_ago(db, 90)

    assert circuit.evaluate(db)["frozen"] is False


def test_hysteresis_knob_persists_via_api(db, monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app

    monkeypatch.setattr(settings, "breaker_rearm_hysteresis_pct", 2.0)  # restored on teardown
    runtime.set_kss_settings(db, {"breaker_rearm_hysteresis_pct": 3.5})
    assert settings.breaker_rearm_hysteresis_pct == 3.5
    assert runtime.kss_settings(db)["breaker_rearm_hysteresis_pct"] == 3.5
    settings.breaker_rearm_hysteresis_pct = 0.0  # simulate restart: corrupt, then restore
    runtime.sync_from_db(db)
    assert settings.breaker_rearm_hysteresis_pct == 3.5

    with TestClient(app) as c:
        r = c.post("/api/kss-settings", json={"breaker_rearm_hysteresis_pct": 2.5})
        assert r.status_code == 200 and r.json()["breaker_rearm_hysteresis_pct"] == 2.5
        assert c.get("/api/kss-settings").json()["breaker_rearm_hysteresis_pct"] == 2.5
        assert c.post("/api/kss-settings", json={"breaker_rearm_hysteresis_pct": 0}).status_code == 200
        assert c.post("/api/kss-settings", json={"breaker_rearm_hysteresis_pct": -1}).status_code == 422
        assert c.post("/api/kss-settings", json={"breaker_rearm_hysteresis_pct": 51}).status_code == 422
        assert c.post("/api/kss-settings", json={"breaker_rearm_hysteresis_pct": 2.5}).status_code == 200
        body = c.get("/api/breaker").json()
        assert body["breaker_rearm_hysteresis_pct"] == 2.5
        assert body["rearm_floor_pct"] == settings.max_drawdown_pct - 2.5
