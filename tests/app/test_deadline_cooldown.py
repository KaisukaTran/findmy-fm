"""C2: a deadline close leaves its own re-entry cooldown (`deadline_cooldown:{symbol}`, duration
`deadline_cooldown_min`) so the scanner does not re-open the coin it just sold. The risk exits
(sl / trailing / trail_sl) keep `stop_cooldown:{symbol}` / `stop_cooldown_min`; tp and stop set none."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app import models, orders, runtime, scanner
from app.clock import utcnow
from app.config import settings
from app.kss import service

_EX = {"minQty": 0.00001, "stepSize": 0.00001, "maxQty": 10000.0}


@pytest.fixture
def mock_market(monkeypatch):
    monkeypatch.setattr("app.kss.pyramid.get_exchange_info", lambda s: _EX)
    monkeypatch.setattr("app.kss.pyramid.get_current_prices", lambda syms: {"BTC": 49000.0})
    monkeypatch.setattr("app.orders.get_current_prices", lambda syms: {"BTC": 49000.0})


def _held_session(db) -> models.KssSession:
    """An ACTIVE BTC session that really holds inventory — wave 0 filled through the real
    approve path, so the Position, the Fill and the KSS fill hook are all genuine."""
    row = service.create_session(
        db, symbol="BTC", entry_price=50000, distance_pct=2, max_waves=3,
        isolated_fund=100000, tp_pct=3, timeout_x_min=30, gap_y_min=5, deadline_days=30,
    )
    res = service.start_session(db, row.id)
    orders.approve_order(db, res["pending_order_id"])
    db.refresh(row)
    assert row.status == models.SESSION_ACTIVE and row.total_filled_qty > 0
    return row


def _ago(minutes: float) -> str:
    return (utcnow() - timedelta(minutes=minutes)).isoformat()


def test_deadline_fill_sets_deadline_cooldown(db, mock_market):
    row = _held_session(db)
    row.deadline_at = utcnow() - timedelta(days=1)
    db.commit()

    assert service.sweep_deadlines(db) == [row.id]
    sell = (
        db.query(models.PendingOrder)
        .filter(models.PendingOrder.source_ref == f"pyramid:{row.id}:deadline")
        .one()
    )
    orders.approve_order(db, sell.id)  # REAL paper fill → real KSS fill hook

    stamp = runtime.get(db, "deadline_cooldown:BTC")
    assert stamp is not None
    assert abs(utcnow() - datetime.fromisoformat(stamp)) < timedelta(minutes=1)
    assert runtime.get(db, "stop_cooldown:BTC") is None  # a calendar exit is not a risk exit
    db.refresh(row)
    assert row.status == models.SESSION_STOPPED


def test_scanner_blocks_symbol_in_deadline_cooldown(db, monkeypatch):
    monkeypatch.setattr(settings, "deadline_cooldown_min", 1440)
    runtime.set(db, "deadline_cooldown:BTC", _ago(10))
    assert scanner._in_stop_cooldown(db, "BTC") is True

    runtime.set(db, "deadline_cooldown:BTC", _ago(1441))
    assert scanner._in_stop_cooldown(db, "BTC") is False


def test_trade_block_reason_reports_the_cooldown(db, monkeypatch):
    monkeypatch.setattr(settings, "deadline_cooldown_min", 1440)
    runtime.set(db, "deadline_cooldown:BTC", _ago(10))

    reason = scanner._trade_block_reason(db, "BTC")

    assert reason is not None and "cooldown" in reason
    assert db.query(models.AuditLog).filter_by(action="skipped_cooldown", entity="BTC").count() == 1


def test_deadline_cooldown_zero_disables(db, monkeypatch):
    monkeypatch.setattr(settings, "deadline_cooldown_min", 0.0)
    runtime.set(db, "deadline_cooldown:BTC", utcnow().isoformat())
    assert scanner._in_stop_cooldown(db, "BTC") is False


def test_each_key_uses_its_own_duration(db, monkeypatch):
    """A 5 h-old deadline stamp is long past the 4 h stop window but well inside the 24 h one."""
    monkeypatch.setattr(settings, "stop_cooldown_min", 240.0)
    monkeypatch.setattr(settings, "deadline_cooldown_min", 1440.0)
    runtime.set(db, "deadline_cooldown:BTC", _ago(300))
    assert scanner._in_stop_cooldown(db, "BTC") is True

    runtime.set(db, "deadline_cooldown:BTC", _ago(1441))  # past its own window
    runtime.set(db, "stop_cooldown:BTC", _ago(300))  # same age as before, but the stop key → expired
    assert scanner._in_stop_cooldown(db, "BTC") is False


def test_sl_cooldown_unchanged(db, mock_market, monkeypatch):
    monkeypatch.setattr(settings, "stop_cooldown_min", 240.0)
    monkeypatch.setattr(settings, "deadline_cooldown_min", 1440.0)
    row = _held_session(db)

    service.handle_fill_event(db, f"pyramid:{row.id}:sl", row.total_filled_qty, 45000.0)

    db.refresh(row)
    assert row.status == models.SESSION_STOPPED
    assert runtime.get(db, "stop_cooldown:BTC") is not None
    assert runtime.get(db, "deadline_cooldown:BTC") is None
    # the SL key still obeys stop_cooldown_min, not the deadline duration
    runtime.set(db, "stop_cooldown:BTC", _ago(10))
    assert scanner._in_stop_cooldown(db, "BTC") is True
    runtime.set(db, "stop_cooldown:BTC", _ago(241))
    assert scanner._in_stop_cooldown(db, "BTC") is False


def test_tp_fill_sets_no_cooldown(db, mock_market):
    row = _held_session(db)

    service.handle_fill_event(db, f"pyramid:{row.id}:tp", row.total_filled_qty, 52000.0)

    db.refresh(row)
    assert row.status == models.SESSION_COMPLETED
    assert runtime.get(db, "stop_cooldown:BTC") is None
    assert runtime.get(db, "deadline_cooldown:BTC") is None


def test_cooldown_knobs_persist_via_api(db, monkeypatch):
    monkeypatch.setattr(settings, "stop_cooldown_min", 240.0)  # restored on teardown
    monkeypatch.setattr(settings, "deadline_cooldown_min", 1440.0)

    runtime.set_kss_settings(db, {"stop_cooldown_min": 90.0, "deadline_cooldown_min": 2880.0})
    assert settings.stop_cooldown_min == 90.0 and settings.deadline_cooldown_min == 2880.0
    got = runtime.kss_settings(db)
    assert got["stop_cooldown_min"] == 90.0 and got["deadline_cooldown_min"] == 2880.0
    # simulate restart: corrupt in-memory, then restore from runtime_config
    settings.stop_cooldown_min = 1.0
    settings.deadline_cooldown_min = 2.0
    runtime.sync_from_db(db)
    assert settings.stop_cooldown_min == 90.0
    assert settings.deadline_cooldown_min == 2880.0


@pytest.mark.parametrize("knob", ["stop_cooldown_min", "deadline_cooldown_min"])
def test_cooldown_knob_round_trips_through_endpoint(knob, monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app

    monkeypatch.setattr(settings, knob, 240.0)  # restored on teardown
    with TestClient(app) as c:
        r = c.post("/api/kss-settings", json={knob: 77})
        assert r.status_code == 200 and r.json()[knob] == 77
        assert c.get("/api/kss-settings").json()[knob] == 77
        # 0 = disabled is valid; negative and absurd values are not
        assert c.post("/api/kss-settings", json={knob: 0}).status_code == 200
        assert c.post("/api/kss-settings", json={knob: -1}).status_code == 422
        assert c.post("/api/kss-settings", json={knob: 43201}).status_code == 422
