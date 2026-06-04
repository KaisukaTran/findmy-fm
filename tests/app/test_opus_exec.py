"""O-3 sandbox (policy) + O-4 watch state machine. No network: prices/candidates mocked."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app import market, orders, runtime
from app.config import settings
from app.orchestrator import brain, policy, service, watch
from app.orchestrator import models as om


@pytest.fixture
def opus_market(monkeypatch):
    """Patch every price/lot-size/candidate seam so the cage runs fully offline."""
    price = {"BTC": 100.0}

    def prices(syms):
        return {s: price[s] for s in syms if s in price}

    monkeypatch.setattr(market, "get_current_prices", prices)
    monkeypatch.setattr(orders, "get_current_prices", prices)
    monkeypatch.setattr("app.kss.pyramid.get_current_prices", prices)
    monkeypatch.setattr("app.kss.pyramid.get_exchange_info",
                        lambda s: {"minQty": 0.00001, "stepSize": 0.00001, "maxQty": 1e6})
    monkeypatch.setattr(brain, "_candidates",
                        lambda db, k=25: [{"symbol": "BTC", "decision": "trade",
                                           "consensus": 80, "win_rate": 90, "est_days_to_tp": 3}])
    monkeypatch.setattr(settings, "opus_shadow", False)
    monkeypatch.setattr(settings, "opus_allocation_usd", 2000.0)
    monkeypatch.setattr(settings, "opus_max_trade_notional", 200.0)
    return price


# --- O-3 policy sandbox -------------------------------------------------


def test_shadow_mode_does_not_execute(db, opus_market, monkeypatch):
    monkeypatch.setattr(settings, "opus_shadow", True)
    out = policy.apply_intents(db, [{"action": "open", "symbol": "BTC", "notional": 100}])
    assert out["shadow"] is True
    assert db.query(om.OpusPosition).count() == 0


def test_open_clamps_to_cap_and_creates_position(db, opus_market):
    out = policy.apply_intents(db, [{"action": "open", "symbol": "BTC", "notional": 500, "reason": "x"}])
    assert len(out["executed"]) == 1
    pos = db.query(om.OpusPosition).one()
    assert pos.symbol == "BTC" and pos.state == om.OPUS_WATCH
    # 500 clamped to the 200 cap → qty 2 @ ~100 (fill carries small slippage)
    assert abs(pos.qty - 2.0) < 1e-9
    assert abs(service.deployed(db) - 200.0) < 1.0  # slippage ≈ 0.05%


def test_open_rejects_unknown_symbol(db, opus_market):
    out = policy.apply_intents(db, [{"action": "open", "symbol": "ETH", "notional": 100}])
    assert out["executed"] == [] and out["rejected"][0]["reason"].startswith("symbol")


def test_open_rejects_dust(db, opus_market, monkeypatch):
    monkeypatch.setattr(settings, "scan_min_notional", 10.0)
    out = policy.apply_intents(db, [{"action": "open", "symbol": "BTC", "notional": 3}])
    assert out["executed"] == [] and db.query(om.OpusPosition).count() == 0


def test_frozen_blocks_all(db, opus_market):
    runtime.freeze(db, "test")
    out = policy.apply_intents(db, [{"action": "open", "symbol": "BTC", "notional": 100}])
    assert out["executed"] == [] and out["rejected"][0]["reason"] == "frozen"


def test_close_realizes_and_marks_closed(db, opus_market):
    policy.apply_intents(db, [{"action": "open", "symbol": "BTC", "notional": 100}])
    pos = db.query(om.OpusPosition).one()
    out = policy.apply_intents(db, [{"action": "close", "position_id": pos.id}])
    assert len(out["executed"]) == 1
    db.refresh(pos)
    assert pos.state == om.OPUS_CLOSED and pos.closed_at is not None


# --- O-4 watch state machine -------------------------------------------


def _watch_pos(db, *, qty, avg, hours_ago):
    pos = om.OpusPosition(symbol="BTC", state=om.OPUS_WATCH, qty=qty, avg_price=avg,
                          entry_price=avg, opened_at=datetime.utcnow() - timedelta(hours=hours_ago),
                          watch_started_at=datetime.utcnow() - timedelta(hours=hours_ago))
    db.add(pos)
    db.commit()
    return pos


def test_watch_winner_becomes_ride(db, opus_market):
    pos = _watch_pos(db, qty=1.0, avg=90.0, hours_ago=4)  # price 100 > avg 90 → winner
    watch.run(db)
    db.refresh(pos)
    assert pos.state == om.OPUS_RIDE


def test_watch_loser_rescued_into_kss(db, opus_market):
    pos = _watch_pos(db, qty=1.0, avg=120.0, hours_ago=4)  # price 100 < avg 120 → loser
    watch.run(db)
    db.refresh(pos)
    assert pos.state == om.OPUS_RESCUE
    assert pos.kss_session_id is not None
    from app.models import KssSession
    sess = db.get(KssSession, pos.kss_session_id)
    assert sess is not None and sess.status == "active"


def test_watch_young_position_untouched(db, opus_market):
    pos = _watch_pos(db, qty=1.0, avg=90.0, hours_ago=1)  # < 3h
    watch.run(db)
    db.refresh(pos)
    assert pos.state == om.OPUS_WATCH


def test_ride_hard_stop_closes(db, opus_market, monkeypatch):
    monkeypatch.setattr(settings, "opus_ride_hard_sl_pct", 10.0)  # stop at avg*0.9
    pos = om.OpusPosition(symbol="BTC", state=om.OPUS_RIDE, qty=1.0, avg_price=120.0,
                          entry_price=120.0, opened_at=datetime.utcnow() - timedelta(hours=5))
    db.add(pos)
    db.commit()
    # price 100 ≤ 120*0.9=108 → hard stop fires
    watch.run(db)
    db.refresh(pos)
    assert pos.state == om.OPUS_CLOSED
