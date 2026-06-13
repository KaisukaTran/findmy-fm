"""Take-profit fee floor (2x highest Binance fee) + post-stop re-entry cooldown."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app import costengine, runtime, scanner
from app.config import settings
from app.kss import service


# ---------------------------------------------------------------------------
# Profit floor = 2x binance_max_fee_pct
# ---------------------------------------------------------------------------


def test_min_profit_pct(monkeypatch):
    monkeypatch.setattr(settings, "binance_max_fee_pct", 0.1)
    assert costengine.min_profit_pct() == pytest.approx(0.2)


def _mk(db, **over):
    params = dict(symbol="BTC", entry_price=100.0, distance_pct=2.0, max_waves=5,
                  isolated_fund=1000.0, tp_pct=3.0, timeout_x_min=9999.0, gap_y_min=0.0)
    params.update(over)
    return service.create_session(db, **params)


def test_create_session_raises_tp_to_floor(db, monkeypatch):
    monkeypatch.setattr(settings, "binance_max_fee_pct", 0.1)  # floor = 0.2%
    row = _mk(db, tp_pct=0.05)  # below the floor
    assert row.tp_pct == pytest.approx(0.2)


def test_create_session_keeps_tp_above_floor(db, monkeypatch):
    monkeypatch.setattr(settings, "binance_max_fee_pct", 0.1)
    row = _mk(db, tp_pct=3.0)
    assert row.tp_pct == pytest.approx(3.0)


# ---------------------------------------------------------------------------
# Post-stop cooldown
# ---------------------------------------------------------------------------


def test_cooldown_blocks_then_expires(db, monkeypatch):
    monkeypatch.setattr(settings, "stop_cooldown_min", 240.0)
    assert scanner._in_stop_cooldown(db, "BTC") is False  # nothing recorded

    runtime.set(db, "stop_cooldown:BTC", datetime.utcnow().isoformat())
    assert scanner._in_stop_cooldown(db, "BTC") is True   # within window

    expired = (datetime.utcnow() - timedelta(minutes=300)).isoformat()
    runtime.set(db, "stop_cooldown:BTC", expired)
    assert scanner._in_stop_cooldown(db, "BTC") is False  # past window


def test_cooldown_disabled_when_zero(db, monkeypatch):
    monkeypatch.setattr(settings, "stop_cooldown_min", 0.0)
    runtime.set(db, "stop_cooldown:BTC", datetime.utcnow().isoformat())
    assert scanner._in_stop_cooldown(db, "BTC") is False


def test_stop_fill_records_cooldown(db, monkeypatch):
    """A stop-loss sell fill should stamp the symbol's cooldown key."""
    monkeypatch.setattr("app.kss.pyramid.get_exchange_info",
                        lambda s: {"minQty": 0.00001, "stepSize": 0.00001, "maxQty": 10000.0})
    row = _mk(db, symbol="ETH")
    row.status = "active"
    db.commit()

    # Simulate the queued stop-loss SELL filling: a :sl source_ref event.
    service.handle_fill_event(db, f"pyramid:{row.id}:sl", filled_qty=1.0, filled_price=90.0)

    assert runtime.get(db, "stop_cooldown:ETH") is not None
    db.refresh(row)
    assert row.status == "stopped"
