"""``POST /api/autotrade`` wrote ``settings.auto_trade`` in-process only.

Live has ``full_auto=1`` persisted, and ``runtime.sync_from_db`` force-sets
``settings.auto_trade = True`` on every boot when full_auto is on. So clicking "Tắt auto-trade"
on the dashboard was undone by the next restart, with no message — this happened twice on live
2026-09-06.

Same family as the other three fixes this session: a control that reports (and here, ACTS on)
its own setting instead of surviving as its own effect across a restart.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import runtime
from app.config import settings
from app.main import app as fastapi_app


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr("app.portfolio.get_current_prices", lambda syms: dict.fromkeys(syms, 100.0))
    with TestClient(fastapi_app) as c:
        yield c


def test_autotrade_off_persists_and_survives_restart_with_full_auto_on(client, db, monkeypatch):
    # Operator explicitly turns auto-trade off via the dashboard.
    r = client.post("/api/autotrade", json={"enabled": False})
    assert r.status_code == 200
    assert r.json()["auto_trade"] is False

    # Simulate a restart while full_auto is persisted ON: reset in-memory settings first,
    # then persist full_auto (as the dashboard / .env would), then sync.
    monkeypatch.setattr(settings, "full_auto", False)
    monkeypatch.setattr(settings, "auto_trade", True)  # pretend the process booted with a stale True
    runtime.set_bool(db, runtime.KEY_FULL_AUTO, True)

    runtime.sync_from_db(db)

    assert settings.auto_trade is False, "explicit off must survive a restart even under full_auto"


def test_no_explicit_row_full_auto_still_cascades_auto_trade_on(db, monkeypatch):
    """Unchanged existing behaviour: with nothing explicitly persisted, full_auto still turns
    auto_trade on (mirrors test_runtime.test_sync_from_db_restores_full_auto)."""
    monkeypatch.setattr(settings, "full_auto", False)
    monkeypatch.setattr(settings, "auto_trade", False)
    runtime.set_bool(db, runtime.KEY_FULL_AUTO, True)

    runtime.sync_from_db(db)

    assert settings.auto_trade is True


def test_set_autotrade_persists_the_key(db, monkeypatch):
    monkeypatch.setattr(settings, "auto_trade", True)
    runtime.set_autotrade(db, False)
    assert runtime.get_bool(db, runtime.KEY_AUTO_TRADE) is False
    assert settings.auto_trade is False


def test_turning_full_auto_back_on_survives_a_restart_too(db, monkeypatch):
    """The mirror of the bug above, which the first fix for it introduced.

    ``full_auto_on`` set ``settings.auto_trade = True`` in memory and persisted only
    KEY_FULL_AUTO. With an explicit ``auto_trade=False`` already in the store — the very row the
    fix above adds — the next boot's override read it back and silently disarmed auto-trade
    inside a full-auto that the operator had just switched on. Persisting the operator's LAST
    action, in both directions, is what makes the switch honest.
    """
    runtime.set_autotrade(db, False)          # operator turns it off...
    runtime.full_auto_on(db)                  # ...then turns full-auto on
    assert settings.auto_trade is True

    monkeypatch.setattr(settings, "auto_trade", False)   # a fresh process, before sync
    runtime.sync_from_db(db)
    assert settings.auto_trade is True, "full-auto came back with auto-trade silently off"


def test_full_auto_off_persists_the_disarm(db, monkeypatch):
    runtime.full_auto_on(db)
    runtime.full_auto_off(db)
    assert settings.auto_trade is False

    monkeypatch.setattr(settings, "auto_trade", True)
    runtime.sync_from_db(db)
    assert settings.auto_trade is False, "full-auto off did not survive the restart"
