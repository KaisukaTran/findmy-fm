"""Orphan-position sweeper: held qty no session/OPUS covers still gets TP/SL-managed."""

from __future__ import annotations

from app import market, portfolio
from app.config import settings
from app.kss import service
from app.models import PENDING, SESSION_ACTIVE, KssSession, PendingOrder, Position


def _pos(db, sym, qty, avg):
    db.add(Position(symbol=sym, quantity=qty, avg_entry_price=avg, total_cost=qty * avg))
    db.commit()


def _sells(db, sym, suffix):
    return db.query(PendingOrder).filter(
        PendingOrder.symbol == sym, PendingOrder.status == PENDING,
        PendingOrder.source_ref == f"orphan:{suffix}").count()


def test_orphan_in_profit_takes_profit(db, monkeypatch):
    monkeypatch.setattr(settings, "scan_tp_pct", 3.0)
    monkeypatch.setattr(settings, "binance_max_fee_pct", 0.1)
    _pos(db, "VVV", 10.8, 14.67)              # no session/OPUS → orphan
    monkeypatch.setattr(market, "get_current_prices", lambda s: {"VVV": 18.36})  # +25%
    swept = service.manage_orphan_positions(db)
    assert "VVV" in swept and _sells(db, "VVV", "tp") == 1


def test_orphan_small_profit_waits(db, monkeypatch):
    monkeypatch.setattr(settings, "scan_tp_pct", 3.0)
    _pos(db, "TRX", 100, 0.31)
    monkeypatch.setattr(market, "get_current_prices", lambda s: {"TRX": 0.315})  # +1.6% < TP
    assert service.manage_orphan_positions(db) == []


def test_orphan_below_sl_is_cut(db, monkeypatch):
    monkeypatch.setattr(settings, "sl_pct", 8.0)
    _pos(db, "NIGHT", 1000, 0.04)
    monkeypatch.setattr(market, "get_current_prices", lambda s: {"NIGHT": 0.0356})  # −11%
    swept = service.manage_orphan_positions(db)
    assert "NIGHT" in swept and _sells(db, "NIGHT", "sl") == 1


def test_managed_symbol_not_swept(db, monkeypatch):
    monkeypatch.setattr(settings, "scan_tp_pct", 3.0)
    _pos(db, "BTC", 1.0, 100.0)
    db.add(KssSession(symbol="BTC", entry_price=100, distance_pct=2, max_waves=5,
                      isolated_fund=100, tp_pct=3, timeout_x_min=1, gap_y_min=0,
                      status=SESSION_ACTIVE))
    db.commit()
    monkeypatch.setattr(market, "get_current_prices", lambda s: {"BTC": 130.0})  # +30%
    assert service.manage_orphan_positions(db) == []  # active session owns it


def test_positions_view_value_pct(db, monkeypatch):
    monkeypatch.setattr(settings, "account_equity", 10000.0)
    monkeypatch.setattr(portfolio, "get_current_prices", lambda s: {"BTC": 100.0})
    _pos(db, "BTC", 5.0, 80.0)  # mv 500; equity = 10000 − 400 + 0 + 500 = 10100
    row = portfolio.positions_view(db)[0]
    assert abs(row["market_value"] - 500.0) < 1e-6
    assert abs(row["market_value_pct"] - 500.0 / 10100.0 * 100) < 1e-6
