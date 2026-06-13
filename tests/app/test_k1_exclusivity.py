"""K-1: one owner per coin — 1 KSS session/symbol + KSS≠OPUS (no blended cost basis)."""

from __future__ import annotations

import pytest

from app import market, orders, scanner
from app.config import settings
from app.models import SESSION_ACTIVE, KssSession
from app.orchestrator import brain, policy
from app.orchestrator import models as om


def _active_kss(db, symbol="BTC"):
    db.add(KssSession(symbol=symbol, entry_price=100, distance_pct=2, max_waves=5,
                      isolated_fund=100, tp_pct=3, timeout_x_min=1, gap_y_min=0,
                      status=SESSION_ACTIVE))
    db.commit()


def test_symbol_cap_default_one(db, monkeypatch):
    monkeypatch.setattr(settings, "max_sessions_per_symbol", 1)
    assert scanner._symbol_at_cap(db, "BTC") is False
    _active_kss(db, "BTC")
    assert scanner._symbol_at_cap(db, "BTC") is True  # one already → at cap


def test_owned_by_opus_only_watch_ride(db):
    assert scanner._owned_by_opus(db, "BTC") is False
    db.add(om.OpusPosition(symbol="BTC", state=om.OPUS_RIDE, qty=1, avg_price=10))
    db.commit()
    assert scanner._owned_by_opus(db, "BTC") is True
    # rescue / closed are NOT ownership (handoff to KSS)
    db.query(om.OpusPosition).filter_by(symbol="BTC").update({"state": om.OPUS_RESCUE})
    db.commit()
    assert scanner._owned_by_opus(db, "BTC") is False


@pytest.fixture
def _opus_env(monkeypatch):
    monkeypatch.setattr(market, "get_current_prices", lambda s: {"BTC": 100.0})
    monkeypatch.setattr(orders, "get_current_prices", lambda s: {"BTC": 100.0})
    monkeypatch.setattr(brain, "_candidates", lambda db, k=25: [{"symbol": "BTC"}])
    monkeypatch.setattr(settings, "opus_shadow", False)
    monkeypatch.setattr(settings, "opus_allocation_usd", 2000.0)
    monkeypatch.setattr(settings, "opus_max_trade_notional", 200.0)


def test_opus_open_rejected_when_kss_active(db, _opus_env):
    _active_kss(db, "BTC")
    out = policy.apply_intents(db, [{"action": "open", "symbol": "BTC", "notional": 100}])
    assert out["executed"] == []
    assert "active KSS session" in out["rejected"][0]["reason"]
    assert db.query(om.OpusPosition).count() == 0


def test_opus_open_allowed_when_no_kss(db, _opus_env, monkeypatch):
    monkeypatch.setattr("app.kss.pyramid.get_exchange_info",
                        lambda s: {"minQty": 0.00001, "stepSize": 0.00001, "maxQty": 1e6})
    out = policy.apply_intents(db, [{"action": "open", "symbol": "BTC", "notional": 100}])
    assert len(out["executed"]) == 1
