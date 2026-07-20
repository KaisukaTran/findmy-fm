"""Per-session deploy cap `max_session_deploy_usd` — the capital-preservation wall for going live
(Phase 0). A session's total DEPLOYED cost (filled + still-pending waves) may never exceed the cap;
any wave — auto-chain, manual DCA+, or the Telegram '➕' button — that would breach it is refused.
This is the missing 'top fix' from loss-cases.md (C deployed $162k unbounded; the ➕ button could
queue a ~$10k rung). 0 = off (paper wide-test unchanged).

Market is stubbed; no network.
"""

from __future__ import annotations

import pytest

from app import market
from app.config import settings
from app.kss import service
from app.models import PENDING, SESSION_ACTIVE, KssSession, PendingOrder


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    monkeypatch.setattr(settings, "max_session_deploy_usd", 0.0)     # default off
    monkeypatch.setattr(market, "get_current_prices", lambda syms, force=False: {"AAA": 95.0})


def _session(db, **kw):
    d = {"symbol": "AAA", "entry_price": 100.0, "distance_pct": 1.5, "max_waves": 6,
         "isolated_fund": 100000.0, "tp_pct": 4.0, "timeout_x_min": 43200.0, "gap_y_min": 0.0,
         "status": SESSION_ACTIVE, "current_wave": 0, "avg_price": 100.0,
         "total_filled_qty": 1.0, "total_cost": 90.0, "sl_pct": 90.0}   # sl 90% → floor 10, rungs clear
    d.update(kw)
    s = KssSession(**d)
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


# ---- headroom helper ----

def test_headroom_off_is_infinite(db):
    s = _session(db)
    assert service._session_deploy_headroom(db, s.id, s.total_cost) == float("inf")


def test_headroom_counts_deployed_and_pending(db, monkeypatch):
    monkeypatch.setattr(settings, "max_session_deploy_usd", 200.0)
    s = _session(db, total_cost=90.0)
    db.add(PendingOrder(symbol="AAA", side="BUY", quantity=10, price=3.0, order_type="LIMIT",
                        status=PENDING, source="kss", source_ref=f"pyramid:{s.id}:1"))  # $30 pending
    db.commit()
    # cap 200 − deployed 90 − pending 30 = 80
    assert service._session_deploy_headroom(db, s.id, s.total_cost) == pytest.approx(80.0)


def test_pending_notional_ignores_other_sessions_and_sells(db):
    s = _session(db)
    db.add(PendingOrder(symbol="AAA", side="BUY", quantity=10, price=2.0, order_type="LIMIT",
                        status=PENDING, source="kss", source_ref=f"pyramid:{s.id}:1"))     # counts $20
    db.add(PendingOrder(symbol="AAA", side="SELL", quantity=10, price=2.0, order_type="LIMIT",
                        status=PENDING, source="kss", source_ref=f"pyramid:{s.id}:sl"))     # SELL — ignored
    db.add(PendingOrder(symbol="AAA", side="BUY", quantity=10, price=2.0, order_type="LIMIT",
                        status=PENDING, source="kss", source_ref="pyramid:9999:1"))         # other session
    db.commit()
    assert service._pending_wave_notional(db, s.id) == pytest.approx(20.0)


# ---- enforcement: manual DCA+ (queue_next_wave) ----

def test_manual_wave_rejected_over_cap(db, monkeypatch):
    monkeypatch.setattr(settings, "max_session_deploy_usd", 100.0)
    s = _session(db, total_cost=90.0)                       # headroom 10
    with pytest.raises(ValueError, match="trần triển khai"):
        service.queue_next_wave(db, s.id, amount_usd=50.0)  # $50 > $10 headroom


def test_manual_wave_ok_within_cap(db, monkeypatch):
    monkeypatch.setattr(settings, "max_session_deploy_usd", 1000.0)
    s = _session(db, total_cost=90.0)                       # headroom 910
    res = service.queue_next_wave(db, s.id, amount_usd=50.0)
    assert res["wave_num"] == 1 and res["cost"] > 0


def test_cap_off_allows_large_wave(db):
    s = _session(db, total_cost=90.0)                       # cap 0 = off
    res = service.queue_next_wave(db, s.id, amount_usd=5000.0)
    assert res["wave_num"] == 1


# ---- enforcement: auto-chain (_queue_wave_if_above_sl) ----

def test_auto_chain_skipped_over_cap(db, monkeypatch):
    monkeypatch.setattr(settings, "max_session_deploy_usd", 100.0)
    s = _session(db, total_cost=90.0)
    py = service._to_pyramid(s)
    order = {"symbol": "AAA", "side": "BUY", "quantity": 10.0, "price": 9.0,
             "order_type": "LIMIT", "source": "kss", "source_ref": f"pyramid:{s.id}:1"}
    ok = service._queue_wave_if_above_sl(db, py, s.id, "AAA", order)   # ~$90+ wave > $10 headroom
    assert ok is False
    from app.models import AuditLog
    assert db.query(AuditLog).filter(AuditLog.action == "deploy_cap_hit").count() == 1


# ---- enforcement: pyramid_up base ladder (create_pyramid_up_session) ----
# The base wave is a MARKET buy that never passes _session_deploy_headroom (unlike every later
# rung), so the cap must bound the ladder at its source. GIGGLE #11: scan_fund=1000 → a $486
# base on a $1k book, blowing past max_session_deploy_usd=150 in one shot.

def _stub_exchange_info(monkeypatch):
    monkeypatch.setattr(market, "get_exchange_info",
                        lambda sym: {"stepSize": 0.00001, "minQty": 0.00001})


def test_pyramid_up_ladder_capped_by_deploy_cap(db, monkeypatch):
    monkeypatch.setattr(settings, "max_session_deploy_usd", 150.0)
    monkeypatch.setattr(settings, "scan_fund", 1000.0)
    _stub_exchange_info(monkeypatch)
    row = service.create_pyramid_up_session(
        db, symbol="AAA", entry_price=27.0, tp_pct=4.0, deadline_days=30)
    assert row.isolated_fund <= 150.0


def test_pyramid_up_ladder_uses_scan_fund_when_cap_off(db, monkeypatch):
    monkeypatch.setattr(settings, "max_session_deploy_usd", 0.0)
    monkeypatch.setattr(settings, "scan_fund", 1000.0)
    _stub_exchange_info(monkeypatch)
    row = service.create_pyramid_up_session(
        db, symbol="AAA", entry_price=27.0, tp_pct=4.0, deadline_days=30)
    assert row.isolated_fund > 150.0     # unclamped — sized off scan_fund as before


# ---- enforcement: the Telegram ➕ button (queue_manual_extra_wave) ----

def test_button_rejected_and_rolls_back_over_cap(db, monkeypatch):
    monkeypatch.setattr(settings, "max_session_deploy_usd", 100.0)
    s = _session(db, current_wave=5, max_waves=6, total_cost=95.0)  # full ladder, headroom 5
    # the standard next rung (kss_first_wave_usd sizing) will far exceed $5 → reject + rollback the bump
    monkeypatch.setattr(settings, "kss_first_wave_usd", 1500.0)
    with pytest.raises(ValueError):
        service.queue_manual_extra_wave(db, s.id)
    db.expire_all()
    assert db.get(KssSession, s.id).max_waves == 6                  # bump rolled back
