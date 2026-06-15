"""K-trail: a trailing stop only LOCKS PROFIT — never sells below cost+fee; hard SL still cuts."""

from __future__ import annotations

from app import market
from app.config import settings
from app.kss import service
from app.models import PENDING, PendingOrder, Position


def _active(db, *, avg, qty, sl=8.0, trail=3.0):
    row = service.create_session(db, symbol="BTC", entry_price=avg, distance_pct=2.0,
                                 max_waves=5, isolated_fund=1000.0, tp_pct=3.0,
                                 timeout_x_min=9999.0, gap_y_min=0.0,
                                 sl_pct=sl, trailing_pct=trail)
    row.status = "active"
    row.avg_price = avg
    row.total_filled_qty = qty
    row.total_cost = avg * qty
    row.peak_price = avg * 1.02   # peaked only +2% (not enough to lock after a 3% trail)
    db.commit()
    return row


def _pos(db, avg, qty):
    db.add(Position(symbol="BTC", quantity=qty, avg_entry_price=avg, total_cost=avg * qty))
    db.commit()


def _tp_sells(db):
    return db.query(PendingOrder).filter(
        PendingOrder.status == PENDING, PendingOrder.source_ref.like("%:trailing")).count()


def test_trailing_below_cost_is_deferred(db, monkeypatch):
    monkeypatch.setattr(settings, "binance_max_fee_pct", 0.1)
    row = _active(db, avg=100.0, qty=2.0)        # peak 102
    _pos(db, 100.0, 2.0)
    # price 98.9: ≤ peak×0.97 (98.94) → trailing fires, but below cost 100 → defer
    monkeypatch.setattr(market, "get_current_prices", lambda s: {"BTC": 98.9})
    service.manage_open_sessions(db)
    db.refresh(row)
    assert row.status == "active"                # not stopped
    assert _tp_sells(db) == 0                     # no trailing sell queued


def test_hard_sl_still_cuts_real_losers(db, monkeypatch):
    monkeypatch.setattr(settings, "binance_max_fee_pct", 0.1)
    _active(db, avg=100.0, qty=2.0, sl=8.0)
    _pos(db, 100.0, 2.0)
    # price 90 ≤ avg×0.92 → hard stop-loss fires (always, even below cost)
    monkeypatch.setattr(market, "get_current_prices", lambda s: {"BTC": 90.0})
    service.manage_open_sessions(db)
    n = db.query(PendingOrder).filter(PendingOrder.source_ref.like("%:sl")).count()
    assert n == 1                                 # hard SL cut the loser


def test_trailing_in_profit_still_sells(db, monkeypatch):
    monkeypatch.setattr(settings, "binance_max_fee_pct", 0.1)
    row = _active(db, avg=100.0, qty=2.0, trail=3.0)
    row.peak_price = 106.0                         # peaked +6% → real profit to lock
    db.commit()
    _pos(db, 100.0, 2.0)
    # price 102.8: ≤ peak×0.97 (102.82) → trailing fires; < TP (103) so not a TP; ≥ cost+fee
    #   → trailing locks profit and sells.
    monkeypatch.setattr(market, "get_current_prices", lambda s: {"BTC": 102.8})
    service.manage_open_sessions(db)
    assert _tp_sells(db) == 1
