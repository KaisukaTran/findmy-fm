"""C1: sweep_deadlines closes at most `deadline_max_closes_per_day` sessions per rolling 24 h,
most-overdue first. The throttle only delays QUEUING a calendar close — it never touches a SELL
already in the queue (exits are never gated)."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from app import models, runtime
from app.clock import utcnow
from app.config import settings
from app.kss import service

_EX = {"minQty": 0.00001, "stepSize": 0.00001, "maxQty": 10000.0}


@pytest.fixture
def mock_market(monkeypatch):
    monkeypatch.setattr("app.kss.pyramid.get_exchange_info", lambda s: _EX)
    monkeypatch.setattr("app.kss.pyramid.get_current_prices", lambda syms: {"BTC": 49000.0})
    monkeypatch.setattr("app.orders.get_current_prices", lambda syms: {"BTC": 49000.0})


def _overdue(db, days_overdue: float, now=None) -> int:
    """An ACTIVE session holding inventory whose deadline passed `days_overdue` days before `now`."""
    row = service.create_session(
        db, symbol="BTC", entry_price=50000, distance_pct=2, max_waves=3,
        isolated_fund=100000, tp_pct=3, timeout_x_min=30, gap_y_min=5, deadline_days=30,
    )
    service.start_session(db, row.id)
    row = db.get(models.KssSession, row.id)
    row.total_filled_qty = 0.001
    row.deadline_at = (now or utcnow()) - timedelta(days=days_overdue)
    db.commit()
    return row.id


def _sells(db, session_id: int, kind: str) -> list[models.PendingOrder]:
    return (
        db.query(models.PendingOrder)
        .filter(models.PendingOrder.source_ref == f"pyramid:{session_id}:{kind}")
        .all()
    )


def _audit(db, action: str) -> list[models.AuditLog]:
    return db.query(models.AuditLog).filter(models.AuditLog.action == action).all()


def _queue_sell(db, session_id: int, kind: str) -> models.PendingOrder:
    """A SELL already waiting in the approval queue for this session (as check_stop/TP leave it)."""
    o = models.PendingOrder(
        symbol="BTC", side="SELL", order_type="MARKET", quantity=0.001, price=0.0,
        source="kss", source_ref=f"pyramid:{session_id}:{kind}", status=models.PENDING,
    )
    db.add(o)
    db.commit()
    return o


def test_limit_zero_keeps_old_behaviour(db, mock_market, monkeypatch):
    monkeypatch.setattr(settings, "deadline_max_closes_per_day", 0)
    ids = [_overdue(db, k) for k in range(1, 6)]

    closed = service.sweep_deadlines(db)

    assert sorted(closed) == sorted(ids)
    for sid in ids:
        sells = _sells(db, sid, "deadline")
        assert len(sells) == 1 and sells[0].side == "SELL" and sells[0].order_type == "MARKET"
    assert _audit(db, "deadline_deferred") == []


def test_limit_caps_closes_per_sweep(db, mock_market, monkeypatch):
    monkeypatch.setattr(settings, "deadline_max_closes_per_day", 2)
    ids = [_overdue(db, k) for k in range(1, 6)]

    closed = service.sweep_deadlines(db)

    assert len(closed) == 2
    assert len(_audit(db, "deadline_close")) == 2
    deferred = _audit(db, "deadline_deferred")
    assert len(deferred) == 1
    detail = json.loads(deferred[0].detail)
    assert detail["deferred"] == 3 and detail["done_24h"] == 0 and detail["limit"] == 2
    assert deferred[0].actor == "scheduler" and deferred[0].entity == "kss:deadline"
    for sid in ids:
        row = db.get(models.KssSession, sid)
        if sid in closed:
            assert row.status == models.SESSION_STOPPED
        else:
            assert row.status == models.SESSION_ACTIVE
            assert _sells(db, sid, "deadline") == []


def test_most_overdue_first(db, mock_market, monkeypatch):
    monkeypatch.setattr(settings, "deadline_max_closes_per_day", 2)
    oldest = _overdue(db, 5)
    newest = _overdue(db, 1)
    middle = _overdue(db, 3)

    closed = service.sweep_deadlines(db)

    assert closed == [oldest, middle]
    assert db.get(models.KssSession, newest).status == models.SESSION_ACTIVE


def test_budget_is_rolling_24h(db, mock_market, monkeypatch):
    monkeypatch.setattr(settings, "deadline_max_closes_per_day", 2)
    now = utcnow()
    [_overdue(db, k, now) for k in range(1, 5)]

    first = service.sweep_deadlines(db, now)
    assert len(first) == 2

    # Same instant: the 24 h budget is already spent, so nothing closes — and that is visible.
    second = service.sweep_deadlines(db, now)
    assert second == []
    assert len(_audit(db, "deadline_deferred")) == 2

    # Once the first two closes age out of the window, the remaining two get their turn.
    third = service.sweep_deadlines(db, now + timedelta(hours=24, minutes=1))
    assert len(third) == 2
    assert set(first).isdisjoint(third)


def test_exit_in_flight_does_not_consume_budget(db, mock_market, monkeypatch):
    monkeypatch.setattr(settings, "deadline_max_closes_per_day", 2)
    busy = _overdue(db, 5)  # most overdue → would be first in line
    a = _overdue(db, 3)
    b = _overdue(db, 2)
    stop = _queue_sell(db, busy, "sl")

    closed = service.sweep_deadlines(db)

    assert closed == [a, b]
    assert _sells(db, busy, "deadline") == []  # no duplicate exit queued behind the stop
    assert db.get(models.KssSession, busy).status == models.SESSION_ACTIVE
    assert _audit(db, "deadline_deferred") == []  # nothing was left over because of the budget
    db.refresh(stop)
    assert stop.status == models.PENDING


def test_throttle_never_touches_queued_sells(db, mock_market, monkeypatch):
    monkeypatch.setattr(settings, "deadline_max_closes_per_day", 1)
    other = _overdue(db, 1)
    for k in range(2, 5):
        _overdue(db, k)
    tp = _queue_sell(db, other, "tp")
    before = (tp.status, tp.quantity, tp.price, tp.order_type, tp.source_ref)

    service.sweep_deadlines(db)

    db.refresh(tp)
    assert (tp.status, tp.quantity, tp.price, tp.order_type, tp.source_ref) == before
    assert tp.status == models.PENDING


def test_knob_persists_via_api(db, monkeypatch):
    monkeypatch.setattr(settings, "deadline_max_closes_per_day", 5)  # restored on teardown
    runtime.set_kss_settings(db, {"deadline_max_closes_per_day": 3})
    assert settings.deadline_max_closes_per_day == 3
    assert runtime.kss_settings(db)["deadline_max_closes_per_day"] == 3
    # simulate restart: corrupt in-memory, then restore from runtime_config
    settings.deadline_max_closes_per_day = 9
    runtime.sync_from_db(db)
    assert settings.deadline_max_closes_per_day == 3


def test_knob_round_trips_through_endpoint(monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app

    monkeypatch.setattr(settings, "deadline_max_closes_per_day", 5)  # restored on teardown
    with TestClient(app) as c:
        r = c.post("/api/kss-settings", json={"deadline_max_closes_per_day": 3})
        assert r.status_code == 200 and r.json()["deadline_max_closes_per_day"] == 3
        assert c.get("/api/kss-settings").json()["deadline_max_closes_per_day"] == 3
        # 0 is valid (= no cap); negatives are not
        assert c.post("/api/kss-settings", json={"deadline_max_closes_per_day": 0}).status_code == 200
        assert c.post("/api/kss-settings", json={"deadline_max_closes_per_day": -1}).status_code == 422
