"""Tests for PyramidSession.check_stop and manage_open_sessions stop integration."""

from __future__ import annotations

import pytest

from app import orders
from app.kss import service
from app.kss.pyramid import PyramidSession, PyramidSessionStatus

_EX_INFO = {"minQty": 0.00001, "stepSize": 0.00001, "maxQty": 10000.0}


# ---------------------------------------------------------------------------
# Fixture: a minimal ACTIVE PyramidSession with some filled qty
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_market(monkeypatch):
    monkeypatch.setattr("app.kss.pyramid.get_exchange_info", lambda s: _EX_INFO)
    monkeypatch.setattr("app.kss.pyramid.get_current_prices", lambda syms: dict.fromkeys(syms, 100.0))


@pytest.fixture
def active_session(mock_market):
    """An ACTIVE PyramidSession with wave 0 filled at 100.0."""
    py = PyramidSession(
        symbol="BTC",
        entry_price=100.0,
        distance_pct=2.0,
        max_waves=5,
        isolated_fund=10000.0,
        tp_pct=10.0,
        timeout_x_min=9999.0,
        gap_y_min=0.0,
    )
    py.id = 1
    py.status = PyramidSessionStatus.ACTIVE
    py.avg_price = 100.0
    py.total_filled_qty = 1.0
    py.total_cost = 100.0
    py.sl_pct = 5.0       # hard stop at avg * (1 - 0.05) = 95.0
    py.trailing_pct = 3.0  # trailing 3% below peak
    py.peak_price = 100.0
    return py


# ---------------------------------------------------------------------------
# check_stop — no filled qty
# ---------------------------------------------------------------------------


def test_check_stop_no_qty_returns_none(mock_market):
    py = PyramidSession(
        symbol="BTC", entry_price=100.0, distance_pct=2.0, max_waves=3,
        isolated_fund=500.0, tp_pct=5.0, timeout_x_min=99.0, gap_y_min=0.0,
    )
    py.sl_pct = 5.0
    py.trailing_pct = 3.0
    py.total_filled_qty = 0.0   # nothing filled
    assert py.check_stop(90.0) is None


# ---------------------------------------------------------------------------
# check_stop — both stops disabled
# ---------------------------------------------------------------------------


def test_check_stop_both_disabled_returns_none(active_session):
    active_session.sl_pct = 0.0
    active_session.trailing_pct = 0.0
    assert active_session.check_stop(50.0) is None  # price well below avg but disabled


# ---------------------------------------------------------------------------
# Hard stop-loss
# ---------------------------------------------------------------------------


def test_check_stop_hard_sl_triggers(active_session):
    # avg=100, sl=5% → threshold=95.0.  Price 94 should trigger.
    result = active_session.check_stop(94.0)
    assert result is not None
    assert result["action"] == "stop_loss"
    assert result["order"]["side"] == "SELL"
    assert ":sl" in result["order"]["source_ref"]


def test_check_stop_hard_sl_at_exact_threshold(active_session):
    # Price exactly at avg*(1-sl/100) = 95.0 → triggers (<=)
    result = active_session.check_stop(95.0)
    assert result is not None
    assert result["action"] == "stop_loss"


def test_check_stop_hard_sl_not_triggered_above_threshold(active_session):
    # Price 96 > 95 → no trigger
    assert active_session.check_stop(96.0) is None


# ---------------------------------------------------------------------------
# Trailing stop
# ---------------------------------------------------------------------------


def test_check_stop_trailing_updates_peak(active_session):
    active_session.peak_price = 100.0
    active_session.check_stop(110.0)  # drive peak up
    assert active_session.peak_price == 110.0


def test_check_stop_trailing_triggers_after_pullback(active_session):
    active_session.sl_pct = 0.0  # disable hard SL so trailing is the only guard
    # drive peak to 110 (in profit: peak > avg=100)
    active_session.check_stop(110.0)
    # pullback: 110 * (1 - 0.03) = 106.7.  Price 106 <= 106.7 → trailing
    result = active_session.check_stop(106.0)
    assert result is not None
    assert result["action"] == "trailing_stop"
    assert result["order"]["side"] == "SELL"
    assert ":trailing" in result["order"]["source_ref"]


def test_check_stop_trailing_no_trigger_when_not_in_profit(active_session):
    active_session.sl_pct = 0.0
    # peak stays at avg=100 (no new high) — trailing requires peak > avg
    result = active_session.check_stop(95.0)
    # Still below avg, trailing should NOT fire (no profit peak)
    assert result is None


def test_check_stop_sl_takes_precedence_over_trailing(active_session):
    """When both conditions are met simultaneously, stop_loss wins."""
    # drive peak well above avg so trailing could also fire
    active_session.check_stop(150.0)  # peak → 150, in profit
    # drop price far enough to satisfy BOTH hard SL and trailing:
    #   hard SL: price <= 100*(1-0.05) = 95
    #   trailing: price <= 150*(1-0.03) = 145.5
    # use 90 which satisfies both
    result = active_session.check_stop(90.0)
    assert result is not None
    assert result["action"] == "stop_loss"


# ---------------------------------------------------------------------------
# Integration: manage_open_sessions queues :sl source_ref
# ---------------------------------------------------------------------------


def test_manage_open_sessions_queues_sl_order(db, monkeypatch):
    monkeypatch.setattr("app.kss.pyramid.get_exchange_info", lambda s: _EX_INFO)
    monkeypatch.setattr("app.kss.pyramid.get_current_prices", lambda syms: dict.fromkeys(syms, 100.0))
    monkeypatch.setattr("app.orders.get_current_prices", lambda syms: dict.fromkeys(syms, 100.0))

    # Create and start a session with a meaningful entry price
    row = service.create_session(
        db, symbol="BTC", entry_price=100.0, distance_pct=2.0, max_waves=3,
        isolated_fund=50000.0, tp_pct=50.0, timeout_x_min=9999.0, gap_y_min=0.0,
        sl_pct=5.0, trailing_pct=0.0,
    )
    service.start_session(db, row.id)

    # Approve wave-0 fill at price 100 → fills the position
    monkeypatch.setattr("app.orders.get_current_prices", lambda syms: dict.fromkeys(syms, 100.0))
    pend = orders.list_pending(db)
    orders.approve_order(db, pend[0].id)

    db.refresh(row)
    assert row.total_filled_qty > 0

    # Now set price well below SL threshold (avg ≈ 100, sl=5% → 95; use 80)
    monkeypatch.setattr("app.market.get_current_prices", lambda syms: dict.fromkeys(syms, 80.0))

    service.manage_open_sessions(db)

    # A pending SELL order with source_ref ending in :sl should be queued
    pending_after = orders.list_pending(db)
    sl_orders = [p for p in pending_after if p.source_ref and p.source_ref.endswith(":sl")]
    assert len(sl_orders) >= 1
    assert sl_orders[0].side == "SELL"
