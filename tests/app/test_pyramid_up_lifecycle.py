"""Pyramid-UP lifecycle integration (fill → manage cycle): capital-safety invariants.

Verifies the service-layer wiring around the pure ``pyramid_up`` math:
  - the base (n=0) market fill must NOT instant-stop (a BE+ stop armed at/above the
    just-filled price would sell at break-even on the very next tick);
  - before the trail arms, a pyramid_up position that DROPS must be cut by a hard SL
    (there is no DCA-down ladder to average it back — momentum cut, not accumulate);
  - an add fill in genuine profit arms the BE+ trailing channel (free-roll), and a
    later retrace locks ≥ the lock floor (never sells below cost);
  - the add-trigger loop queues a marketable BUY once price clears the armed trigger,
    re-asserting the capital gates.

Prices + ATR monkeypatched (no network). Mirrors test_dynamic_exit_wiring.py.
"""

from __future__ import annotations

import pytest

from app import market, scanner
from app.config import settings
from app.kss import service
from app.models import (
    SESSION_ACTIVE,
    SESSION_STOPPED,
    WAVE_ARMED,
    WAVE_FILLED,
    WAVE_SENT,
    KssSession,
    KssWave,
    PendingOrder,
)


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    monkeypatch.setattr(settings, "taker_fee_pct", 0.1)
    monkeypatch.setattr(settings, "slippage_pct", 0.05)
    monkeypatch.setattr(settings, "kss_exit_fee_mult", 3.0)
    monkeypatch.setattr(settings, "kss_tp_gap_pct", 5.0)
    monkeypatch.setattr(settings, "kss_trail_atr_mult", 1.0)
    monkeypatch.setattr(settings, "kss_trail_min_pct", 3.0)
    monkeypatch.setattr(settings, "kss_trail_arm_pct", 5.0)
    monkeypatch.setattr(settings, "kss_trail_lock_pct", 2.0)
    monkeypatch.setattr(settings, "kss_dynamic_tp_enabled", True)
    monkeypatch.setattr(settings, "pyramid_up_lock_pct", 1.0)
    monkeypatch.setattr(settings, "pyramid_up_max_adds", 2)
    monkeypatch.setattr(settings, "sl_pct", 15.0)
    monkeypatch.setattr(service, "_session_atr_pct", lambda sym: 6.0)
    # capital gates: never block in these unit tests unless a test overrides them
    monkeypatch.setattr(scanner, "_symbol_at_cap", lambda db, sym: False)
    monkeypatch.setattr(scanner, "_can_open", lambda db, need: (True, ""))


def _price(monkeypatch, px):
    monkeypatch.setattr(market, "get_current_prices", lambda syms, force=False: {"AAA": px})


def _pyr_session(db, **kw):
    """A pyramid_up session with the base wave already SENT (wave 0) and one armed add (wave 1)."""
    d = {
        "symbol": "AAA", "entry_price": 100.0, "distance_pct": 2.0, "max_waves": 3,
        "isolated_fund": 1000.0, "tp_pct": 4.0, "timeout_x_min": 43200.0, "gap_y_min": 0.0,
        "status": SESSION_ACTIVE, "current_wave": 0, "avg_price": 0.0, "total_filled_qty": 0.0,
        "total_cost": 0.0, "peak_price": 0.0, "sl_pct": 15.0, "strategy_mode": "pyramid_up",
    }
    d.update(kw)
    s = KssSession(**d)
    db.add(s)
    db.commit()
    db.refresh(s)
    db.add(KssWave(session_id=s.id, wave_num=0, quantity=8.0, target_price=100.0, status=WAVE_SENT))
    db.add(KssWave(session_id=s.id, wave_num=1, quantity=5.0, target_price=102.0, status=WAVE_ARMED))
    db.commit()
    return s


def _sells(db, sid):
    return db.query(PendingOrder).filter(
        PendingOrder.source_ref.like(f"pyramid:{sid}:%"), PendingOrder.side == "SELL").count()


def _fill_base(db, s, price=100.0, qty=8.0):
    service.handle_fill_event(db, f"pyramid:{s.id}:wave:0", qty, price)


# ----- (1) the base market fill must not instant-stop -----

def test_base_fill_does_not_instant_stop(db, monkeypatch):
    s = _pyr_session(db)
    _fill_base(db, s, price=100.0)
    db.refresh(s)
    # the fill MUST be reflected in the session (autoflush=False: the handler has to
    # flush/account the just-filled wave, else avg stays 0 and the position is invisible).
    assert s.total_filled_qty == 8.0 and abs(s.avg_price - 100.0) < 1e-9
    _price(monkeypatch, 100.0)          # price unchanged from the fill → must NOT sell at breakeven
    service.manage_open_sessions(db)
    db.refresh(s)
    assert _sells(db, s.id) == 0, "base fill armed a BE+ stop above price → instant stop-out"
    assert s.status == SESSION_ACTIVE


# ----- (2) hard SL cuts a loser before the trail arms -----

def test_hard_sl_cuts_loser_before_arm(db, monkeypatch):
    s = _pyr_session(db)
    _fill_base(db, s, price=100.0)
    _price(monkeypatch, 80.0)           # −20% (> sl_pct 15%) and no add → hard SL must cut it
    triggered = service.manage_open_sessions(db)
    db.refresh(s)
    assert _sells(db, s.id) == 1, "pyramid_up has no hard SL before arming → unbounded downside"
    assert s.status == SESSION_STOPPED and s.id in triggered


# ----- (3) an add in profit arms a BE+ trail; retrace locks >= floor -----

def test_add_in_profit_arms_then_locks(db, monkeypatch):
    s = _pyr_session(db)
    _fill_base(db, s, price=100.0)
    # add wave 1 fills at 102 (price moved up into strength)
    db.query(KssWave).filter(KssWave.session_id == s.id, KssWave.wave_num == 1).update(
        {"status": WAVE_FILLED, "filled_qty": 5.0, "filled_price": 102.0})
    db.commit()
    service.handle_fill_event(db, f"pyramid:{s.id}:wave:1", 5.0, 102.0)
    db.refresh(s)
    assert s.trail_active is True       # in profit beyond BE+ → armed
    assert s.trail_sl_price >= s.avg_price  # locks at/above avg (free-roll)
    # a retrace down to the locked SL exits, and never below cost
    _price(monkeypatch, s.trail_sl_price - 1e-6)
    service.manage_open_sessions(db)
    db.refresh(s)
    assert _sells(db, s.id) == 1 and s.status == SESSION_STOPPED


# ----- (4) the add-trigger loop queues a marketable BUY once price clears the trigger -----

def test_add_trigger_queues_market_buy(db, monkeypatch):
    s = _pyr_session(db)
    _fill_base(db, s, price=100.0)
    # Regression: the up-add must fire even when the symbol is "at cap" — the session occupies its
    # own per-symbol slot; scaling it is not a new open. The old code re-asserted the cap and
    # blocked every up-add (pyramid_add_queued was always 0).
    monkeypatch.setattr(scanner, "_symbol_at_cap", lambda db, sym: True)
    _price(monkeypatch, 102.0)          # clears the wave-1 trigger (102)
    service.manage_open_sessions(db)
    buys = db.query(PendingOrder).filter(
        PendingOrder.source_ref == f"pyramid:{s.id}:wave:1", PendingOrder.side == "BUY").count()
    assert buys == 1
