"""Phase 2: dynamic trailing channel wired into service.manage_open_sessions.

Covers activation (cancel ladder, set SL, no premature sell), the no-activate hysteresis, the
channel exits (sell at SL / at TP), the ratchet, precedence over the frozen fixed-TP, and the
disabled no-op. Prices + ATR are monkeypatched (no network).
"""

from __future__ import annotations

import pytest

from app import market, orders
from app.config import settings
from app.kss import service
from app.models import (
    PENDING,
    REJECTED,
    SESSION_ACTIVE,
    SESSION_STOPPED,
    WAVE_CANCELLED,
    WAVE_SENT,
    KssSession,
    KssWave,
    PendingOrder,
)

TP_TRIGGERED = "tp_triggered"


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    monkeypatch.setattr(settings, "taker_fee_pct", 0.1)
    monkeypatch.setattr(settings, "slippage_pct", 0.05)          # round-trip cost 0.3% → floor +0.9%
    monkeypatch.setattr(settings, "kss_exit_fee_mult", 3.0)
    monkeypatch.setattr(settings, "kss_tp_gap_pct", 5.0)
    monkeypatch.setattr(settings, "kss_trail_atr_mult", 1.0)
    monkeypatch.setattr(settings, "kss_trail_min_pct", 3.0)
    monkeypatch.setattr(settings, "kss_dynamic_tp_enabled", True)
    monkeypatch.setattr(service, "_session_atr_pct", lambda sym: 6.0)  # no candle fetch


def _price(monkeypatch, px):
    # accept force= so it also stands in for the guard's get_current_prices(..., force=True)
    monkeypatch.setattr(market, "get_current_prices", lambda syms, force=False: {"AAA": px})


def _session(db, **kw):
    d = dict(symbol="AAA", entry_price=100.0, distance_pct=1.5, max_waves=6, isolated_fund=1000.0,
             tp_pct=4.0, timeout_x_min=43200.0, gap_y_min=0.0, status=SESSION_ACTIVE, current_wave=2,
             avg_price=100.0, total_filled_qty=10.0, total_cost=1000.0, peak_price=0.0, sl_pct=8.0)
    d.update(kw)
    s = KssSession(**d)
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


def _sells(db, sid, kind):
    return db.query(PendingOrder).filter(
        PendingOrder.source_ref == f"pyramid:{sid}:{kind}", PendingOrder.side == "SELL").count()


# ----- disabled = no-op -----

def test_disabled_does_not_activate(db, monkeypatch):
    monkeypatch.setattr(settings, "kss_dynamic_tp_enabled", False)
    s = _session(db)
    _price(monkeypatch, 110.0)             # well above avg×(1+d)
    service.manage_open_sessions(db)
    db.refresh(s)
    assert s.trail_active is False         # the frozen path handled it, dynamic stayed off


# ----- activation (cancel ladder, set SL, no sell) -----

def test_activation_cancels_ladder_and_arms_sl(db, monkeypatch):
    s = _session(db, peak_price=0.0)
    # a pending DCA wave that must be cancelled on activation
    db.add(PendingOrder(symbol="AAA", side="BUY", quantity=5, price=98.0, order_type="LIMIT",
                        status=PENDING, source="kss", source_ref=f"pyramid:{s.id}:wave:3"))
    db.add(KssWave(session_id=s.id, wave_num=3, quantity=5, target_price=98.0, status=WAVE_SENT))
    db.commit()
    _price(monkeypatch, 102.0)             # ≥ 101.5 activation threshold
    service.manage_open_sessions(db)
    db.refresh(s)
    assert s.trail_active is True
    assert s.trail_sl_price >= service.dynamic_exit.fee_floor_price(100.0) - 1e-6   # ≥ +0.9%
    assert s.status == SESSION_ACTIVE      # activation does NOT sell on the same tick
    assert _sells(db, s.id, "tp") == 0 and _sells(db, s.id, "trail_sl") == 0
    # ladder cancelled
    wave_po = db.query(PendingOrder).filter(
        PendingOrder.source_ref == f"pyramid:{s.id}:wave:3").one()
    assert wave_po.status == REJECTED
    assert db.query(KssWave).filter(KssWave.session_id == s.id).one().status == WAVE_CANCELLED


def test_sub_d_poke_does_not_activate(db, monkeypatch):
    s = _session(db)
    _price(monkeypatch, 101.0)             # > avg but < avg×(1+1.5%)=101.5
    service.manage_open_sessions(db)
    db.refresh(s)
    assert s.trail_active is False and s.status == SESSION_ACTIVE


# ----- channel exits -----

def test_trailing_sells_at_sl(db, monkeypatch):
    s = _session(db, trail_active=True, trail_sl_price=110.0, peak_price=120.0)
    _price(monkeypatch, 109.0)             # ≤ SL
    triggered = service.manage_open_sessions(db)
    db.refresh(s)
    assert _sells(db, s.id, "trail_sl") == 1
    assert s.status == SESSION_STOPPED and s.id in triggered


def test_trailing_sells_at_tp(db, monkeypatch):
    s = _session(db, trail_active=True, trail_sl_price=110.0, peak_price=120.0)
    _price(monkeypatch, 116.0)             # ≥ carried TP (110×1.05 = 115.5)
    triggered = service.manage_open_sessions(db)
    db.refresh(s)
    assert _sells(db, s.id, "tp") == 1
    assert s.status == TP_TRIGGERED and s.id in triggered


def test_trailing_ratchets_without_selling(db, monkeypatch):
    s = _session(db, trail_active=True, trail_sl_price=110.0, peak_price=120.0)
    _price(monkeypatch, 114.0)             # between SL 110 and TP 115.5 → hold + ratchet
    service.manage_open_sessions(db)
    db.refresh(s)
    assert s.trail_sl_price > 110.0        # SL stepped up toward peak
    assert s.status == SESSION_ACTIVE
    assert _sells(db, s.id, "tp") == 0 and _sells(db, s.id, "trail_sl") == 0


def test_trailing_supersedes_frozen_fixed_tp(db, monkeypatch):
    # Price sits exactly at the frozen TP (avg×(1+tp%)=104) but below the carried channel TP.
    s = _session(db, trail_active=True, trail_sl_price=101.0, peak_price=104.0)
    _price(monkeypatch, 104.0)             # carried TP = 101×1.05 = 106.05 → no channel exit yet
    service.manage_open_sessions(db)
    db.refresh(s)
    assert _sells(db, s.id, "tp") == 0     # frozen fixed-TP did NOT fire (channel governs)
    assert s.status == SESSION_ACTIVE


# ----- manual take-profit (Phase 3) -----

def test_manual_tp_requires_trailing(db):
    s = _session(db, trail_active=False)
    with pytest.raises(ValueError):
        service.manual_take_profit(db, s.id)


def test_manual_tp_requires_filled_qty(db):
    s = _session(db, trail_active=True, total_filled_qty=0.0)
    with pytest.raises(ValueError):
        service.manual_take_profit(db, s.id)


def test_manual_tp_queues_full_sell_and_marks_done(db, monkeypatch):
    s = _session(db, trail_active=True, total_filled_qty=10.0)
    captured = {}

    def _stub_approve(_db, oid, reviewer=None):
        captured["reviewer"] = reviewer
        return type("F", (), {"price": 123.0})()

    monkeypatch.setattr(orders, "approve_order", _stub_approve)   # isolate from the fill machinery
    r = service.manual_take_profit(db, s.id)
    db.refresh(s)
    assert s.status == TP_TRIGGERED
    assert _sells(db, s.id, "manual_tp") == 1                     # full-qty SELL queued
    assert captured["reviewer"] == "manual"                      # filled immediately, human reviewer
    assert r["quantity"] == 10.0 and r["price"] == 123.0


# ----- Phase 4: knobs, /summary tag, edge-triggered alert -----

def test_dynamic_knobs_round_trip(db, monkeypatch):
    from app import runtime
    monkeypatch.setattr(settings, "kss_exit_check_sec", 90)       # ensure restored after the test
    monkeypatch.setattr(settings, "kss_live_stop_orders", False)
    runtime.set_kss_settings(db, {"kss_dynamic_tp_enabled": "1", "kss_tp_gap_pct": "7.5",
                                  "kss_exit_check_sec": "120", "kss_live_stop_orders": "1"})
    k = runtime.kss_settings(db)
    assert k["kss_dynamic_tp_enabled"] is True
    assert k["kss_tp_gap_pct"] == 7.5
    assert k["kss_exit_check_sec"] == 120
    assert k["kss_live_stop_orders"] is True
    assert runtime.get(db, "kss:kss_tp_gap_pct") == "7.5"         # persisted


def test_kss_command_shows_trailing_mode(db, monkeypatch):
    from app import notify
    _session(db, trail_active=True, trail_sl_price=112.6)
    monkeypatch.setattr(market, "get_current_prices", lambda syms: {"AAA": 115.0})
    assert "trailing-TP" in notify.handle_command("/kss")


def test_activation_fires_one_telegram_alert(db, monkeypatch):
    from app import notify
    s = _session(db, peak_price=0.0)
    calls = []
    monkeypatch.setattr(notify, "event", lambda *a, **k: calls.append(a))
    _price(monkeypatch, 102.0)
    service.manage_open_sessions(db)                              # activates → 1 alert
    db.refresh(s)
    service.manage_open_sessions(db)                              # already trailing → no 2nd alert
    assert len(calls) == 1


# ----- Phase 5: position guard + crash-detect -----

def test_guard_noop_when_disabled(db, monkeypatch):
    monkeypatch.setattr(settings, "kss_dynamic_tp_enabled", False)
    _session(db, trail_active=True, trail_sl_price=110.0)
    assert service.run_position_guard(db)["checked"] == 0


def test_guard_executes_channel_exit_immediately(db, monkeypatch):
    s = _session(db, trail_active=True, trail_sl_price=110.0, peak_price=120.0)
    _price(monkeypatch, 109.0)                                    # ≤ SL → exit
    seen = []
    monkeypatch.setattr(orders, "approve_order",
                        lambda _db, oid, reviewer=None: seen.append(reviewer)
                        or type("F", (), {"price": 109.0})())
    service._guard_last_price.clear()
    out = service.run_position_guard(db)
    db.refresh(s)
    assert s.id in out["exited"] and s.status == SESSION_STOPPED
    assert _sells(db, s.id, "trail_sl") == 1
    assert seen == ["guard"]                                      # filled immediately by the guard


def test_guard_crash_detect_exits(db, monkeypatch):
    s = _session(db, trail_active=True, trail_sl_price=110.0, peak_price=130.0)
    monkeypatch.setattr(settings, "kss_crash_drop_pct", 10.0)
    monkeypatch.setattr(orders, "approve_order",
                        lambda _db, oid, reviewer=None: type("F", (), {"price": 108.0})())
    service._guard_last_price.clear()
    service._guard_last_price[s.id] = 125.0                       # last observation (high)
    _price(monkeypatch, 108.0)                                   # −13.6% AND ≤ SL → crash exit
    out = service.run_position_guard(db)
    db.refresh(s)
    assert s.id in out["exited"] and s.status == SESSION_STOPPED


def test_guard_ratchets_without_exit(db, monkeypatch):
    s = _session(db, trail_active=True, trail_sl_price=110.0, peak_price=120.0)
    _price(monkeypatch, 114.0)                                    # between SL and TP → hold + ratchet
    service._guard_last_price.clear()
    out = service.run_position_guard(db)
    db.refresh(s)
    assert out["exited"] == [] and s.status == SESSION_ACTIVE
    assert s.trail_sl_price > 110.0
