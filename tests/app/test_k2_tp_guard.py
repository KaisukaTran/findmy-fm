"""K-2: never take profit below the true aggregate cost basis + 2x fee."""

from __future__ import annotations

from app import market
from app.config import settings
from app.kss import service
from app.models import PENDING, PendingOrder, Position


def test_tp_clears_cost_helper(db, monkeypatch):
    monkeypatch.setattr(settings, "binance_max_fee_pct", 0.1)  # floor = 0.2%
    db.add(Position(symbol="BTC", quantity=2.0, avg_entry_price=100.0, total_cost=200.0))
    db.commit()
    assert service._tp_clears_cost(db, "BTC", 100.0) is False        # below cost
    assert service._tp_clears_cost(db, "BTC", 100.19) is False       # below +0.2%
    assert service._tp_clears_cost(db, "BTC", 100.25) is True         # clears cost + fee
    assert service._tp_clears_cost(db, "ETH", 50.0) is True           # no position → allow


def _active_session(db, avg, qty, tp=3.0):
    row = service.create_session(db, symbol="BTC", entry_price=avg, distance_pct=2.0,
                                 max_waves=5, isolated_fund=1000.0, tp_pct=tp,
                                 timeout_x_min=9999.0, gap_y_min=0.0)
    row.status = "active"
    row.avg_price = avg
    row.total_filled_qty = qty
    row.total_cost = avg * qty
    db.commit()
    return row


def test_manage_defers_tp_below_true_cost(db, monkeypatch):
    monkeypatch.setattr(settings, "binance_max_fee_pct", 0.1)
    # session avg 90 (TP at 92.7) but the coin's TRUE Position avg is 100 (blended/legacy)
    row = _active_session(db, avg=90.0, qty=2.0)
    db.add(Position(symbol="BTC", quantity=2.0, avg_entry_price=100.0, total_cost=200.0))
    db.commit()
    monkeypatch.setattr(market, "get_current_prices", lambda s: {"BTC": 93.0})  # ≥ session TP
    service.manage_open_sessions(db)
    db.refresh(row)
    assert row.status == "active"  # deferred, stays open
    assert db.query(PendingOrder).filter(
        PendingOrder.status == PENDING, PendingOrder.source_ref.like("%:tp")
    ).count() == 0  # no TP sell queued


def test_manage_queues_tp_above_true_cost(db, monkeypatch):
    monkeypatch.setattr(settings, "binance_max_fee_pct", 0.1)
    _active_session(db, avg=90.0, qty=2.0)
    db.add(Position(symbol="BTC", quantity=2.0, avg_entry_price=100.0, total_cost=200.0))
    db.commit()
    monkeypatch.setattr(market, "get_current_prices", lambda s: {"BTC": 101.0})  # clears cost+fee
    service.manage_open_sessions(db)
    assert db.query(PendingOrder).filter(
        PendingOrder.status == PENDING, PendingOrder.source_ref.like("%:tp")
    ).count() == 1  # TP sell queued


def test_single_owner_tp_not_affected(db, monkeypatch):
    """K-1 normal case: session avg == Position avg → TP clears and queues as before."""
    monkeypatch.setattr(settings, "binance_max_fee_pct", 0.1)
    _active_session(db, avg=100.0, qty=2.0)
    db.add(Position(symbol="BTC", quantity=2.0, avg_entry_price=100.0, total_cost=200.0))
    db.commit()
    monkeypatch.setattr(market, "get_current_prices", lambda s: {"BTC": 103.5})  # avg×1.035
    service.manage_open_sessions(db)
    assert db.query(PendingOrder).filter(PendingOrder.source_ref.like("%:tp")).count() == 1
