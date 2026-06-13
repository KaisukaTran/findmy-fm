"""Tests for audit logging, the deadline field, and sweep_deadlines."""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import inspect

from app import audit, models
from app.db import engine
from app.kss import service

_EX = {"minQty": 0.00001, "stepSize": 0.00001, "maxQty": 10000.0}


@pytest.fixture
def mock_market(monkeypatch):
    monkeypatch.setattr("app.kss.pyramid.get_exchange_info", lambda s: _EX)
    monkeypatch.setattr("app.kss.pyramid.get_current_prices", lambda syms: {"BTC": 49000.0})
    monkeypatch.setattr("app.orders.get_current_prices", lambda syms: {"BTC": 49000.0})


def test_new_tables_created():
    tables = set(inspect(engine).get_table_names())
    assert {"scan_runs", "candidates", "agent_votes", "audit_log"} <= tables


def test_audit_log_append(db):
    audit.log(db, "scanner", "scan_start", entity="run:1", universe=5)
    db.commit()
    rows = db.query(models.AuditLog).all()
    assert len(rows) == 1
    assert rows[0].actor == "scanner" and rows[0].action == "scan_start"
    assert "universe" in rows[0].detail


def test_create_sets_deadline_days(db, mock_market):
    row = service.create_session(
        db, symbol="BTC", entry_price=50000, distance_pct=2, max_waves=3,
        isolated_fund=100000, tp_pct=3, timeout_x_min=30, gap_y_min=5, deadline_days=14,
    )
    assert row.deadline_days == 14
    assert row.deadline_at is None  # only set on start


def test_start_sets_deadline_at(db, mock_market):
    row = service.create_session(
        db, symbol="BTC", entry_price=50000, distance_pct=2, max_waves=3,
        isolated_fund=100000, tp_pct=3, timeout_x_min=30, gap_y_min=5, deadline_days=30,
    )
    service.start_session(db, row.id)
    db.refresh(row)
    assert row.deadline_at is not None
    assert row.deadline_at > datetime.utcnow() + timedelta(days=29)


def test_sweep_closes_overdue_and_queues_sell(db, mock_market):
    row = service.create_session(
        db, symbol="BTC", entry_price=50000, distance_pct=2, max_waves=3,
        isolated_fund=100000, tp_pct=3, timeout_x_min=30, gap_y_min=5, deadline_days=30,
    )
    service.start_session(db, row.id)

    # Pretend it filled some inventory and the deadline has passed.
    row = db.get(models.KssSession, row.id)
    row.total_filled_qty = 0.001
    row.deadline_at = datetime.utcnow() - timedelta(days=1)
    db.commit()

    closed = service.sweep_deadlines(db)
    assert row.id in closed

    db.refresh(row)
    assert row.status == models.SESSION_STOPPED

    sells = (
        db.query(models.PendingOrder)
        .filter(models.PendingOrder.source_ref == f"pyramid:{row.id}:deadline")
        .all()
    )
    assert sells and sells[0].side == "SELL" and sells[0].order_type == "MARKET"
    assert db.query(models.AuditLog).filter(models.AuditLog.action == "deadline_close").count() == 1
