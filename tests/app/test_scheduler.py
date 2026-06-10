"""Phase D: scheduler cycle, auto-fill of due KSS orders, TP management."""

import pytest

from app import models, orders, scanner, scheduler
from app.config import settings
from app.kss import service

_DAY = 86_400_000


def _uptrend(n=60, start=100.0, vol=1e6):
    out, price = [], start
    for d in range(n):
        out.append({"ts": d * _DAY, "open": price, "high": price,
                    "low": price * 0.999, "close": price, "volume": vol})
        price *= 1.01
    return out


class _FakeProvider:
    def get_ohlcv(self, symbol, timeframe="1d", limit=200):
        return _uptrend() if symbol == "BTC" else []

    def all_symbols(self, min_quote_volume=0.0):
        return ["BTC"]

    def top_symbols(self, n=10):
        return ["BTC"]

    def get_prices(self, symbols):
        return dict.fromkeys(symbols, 1.0)

    def get_exchange_info(self, symbol):
        return {"minQty": 0.00001, "stepSize": 0.00001, "maxQty": 10000.0}


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(scanner, "data_provider", lambda: _FakeProvider())
    monkeypatch.setattr("app.kss.pyramid.get_exchange_info",
                        lambda s: {"minQty": 0.00001, "stepSize": 0.00001, "maxQty": 10000.0})
    monkeypatch.setattr("app.kss.pyramid.get_current_prices", lambda syms: dict.fromkeys(syms, 1.0))
    monkeypatch.setattr("app.orders.get_current_prices", lambda syms: dict.fromkeys(syms, 1.0))
    monkeypatch.setattr("app.market.get_current_prices", lambda syms: dict.fromkeys(syms, 1.0))
    monkeypatch.setattr(settings, "watchlist", ["BTC"])
    monkeypatch.setattr(settings, "min_confidence", 0.0)
    monkeypatch.setattr(settings, "min_win_rate", 0.0)
    monkeypatch.setattr(settings, "max_loss_rate", 100.0)
    monkeypatch.setattr(settings, "auto_trade", True)


def _new_session(db):
    row = service.create_session(
        db, symbol="BTC", entry_price=100.0, distance_pct=2, max_waves=3,
        isolated_fund=100000, tp_pct=3, timeout_x_min=999999.0, gap_y_min=0.0,
    )
    service.start_session(db, row.id)
    return row


def test_auto_fill_due_orders(db, env):
    row = _new_session(db)
    approved = orders.auto_fill_due_orders(db)  # wave0 limit 100 ≥ price 1.0 → BUY due
    assert approved
    assert db.query(models.Fill).count() == 1
    db.refresh(row)
    assert row.total_filled_qty > 0


def test_manage_queues_tp(db, env, monkeypatch):
    row = _new_session(db)
    orders.auto_fill_due_orders(db)
    monkeypatch.setattr("app.market.get_current_prices", lambda syms: dict.fromkeys(syms, 1e9))
    triggered = service.manage_open_sessions(db)
    assert row.id in triggered
    assert db.query(models.PendingOrder).filter(
        models.PendingOrder.source_ref == f"pyramid:{row.id}:tp").count() >= 1


def test_run_cycle_full_auto(db, env):
    summary = scheduler.run_cycle(db)
    assert summary["scan_id"] is not None
    assert db.query(models.Fill).count() >= 1          # wave 0 auto-filled
    assert db.query(models.AuditLog).filter_by(action="cycle").count() == 1


def test_expired_veto_is_cleared_and_refilled(db, env, monkeypatch):
    """A stale Guardian veto must not deadlock a due KSS DCA wave: the TTL expires
    it, the cycle re-enables the order, and (price being due) it fills."""
    from datetime import datetime, timedelta

    monkeypatch.setattr("app.guardian.enabled", lambda: False)  # deterministic, no re-veto
    monkeypatch.setattr(settings, "guardian_veto_ttl_min", 30)
    row = _new_session(db)
    order = db.query(models.PendingOrder).filter_by(
        source_ref=f"pyramid:{row.id}:wave:0").one()
    order.auto_veto = True
    order.auto_veto_reason = "stale veto"
    order.auto_veto_at = datetime.utcnow() - timedelta(minutes=31)
    db.commit()

    scheduler.run_cycle(db)

    db.refresh(order)
    assert not order.auto_veto
    assert db.query(models.AuditLog).filter_by(action="veto_expired").count() == 1
    assert order.status == models.EXECUTED  # cleared veto → auto-filled (price due)


def test_fresh_veto_survives_within_ttl(db, env, monkeypatch):
    """A veto younger than the TTL is left in place — it still blocks auto-fill."""
    from datetime import datetime

    monkeypatch.setattr("app.guardian.enabled", lambda: False)
    monkeypatch.setattr(settings, "guardian_veto_ttl_min", 30)
    row = _new_session(db)
    order = db.query(models.PendingOrder).filter_by(
        source_ref=f"pyramid:{row.id}:wave:0").one()
    order.auto_veto = True
    order.auto_veto_reason = "fresh veto"
    order.auto_veto_at = datetime.utcnow()
    db.commit()

    scheduler.run_cycle(db)

    db.refresh(order)
    assert order.auto_veto
    assert order.status == models.PENDING  # still blocked, not filled
