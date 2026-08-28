"""Loss-guard fixes A + B (see memory loss-cases.md).

A — the dynamic trailing/TP exit must NOT sell below the TRUE blended Position cost basis
    (an orphan / cross-session lot can inflate the wallet avg above this session's trail floor —
    the BIO case). It DEFERS instead of locking a loss, but a genuinely deep loser is still cut
    by the hard-SL floor.
B — a symbol with a SELL order in flight must not be re-opened (the BICO re-entry race), and a
    dynamic trail_sl fill must set the re-entry cooldown (previously missing).

Prices/ATR monkeypatched; no network. Mirrors test_dynamic_exit_wiring.py.
"""

from __future__ import annotations

import pytest

from app import market, runtime, scanner
from app.config import settings
from app.kss import service
from app.models import (
    PENDING,
    SESSION_ACTIVE,
    SESSION_STOPPED,
    KssSession,
    PendingOrder,
    Position,
)

TP_TRIGGERED = "tp_triggered"


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
    monkeypatch.setattr(service, "_session_atr_pct", lambda sym: 6.0)


def _price(monkeypatch, px):
    monkeypatch.setattr(market, "get_current_prices", lambda syms, force=False: {"AAA": px})


def _session(db, **kw):
    d = {"symbol": "AAA", "entry_price": 100.0, "distance_pct": 1.5, "max_waves": 6,
         "isolated_fund": 1000.0, "tp_pct": 4.0, "timeout_x_min": 43200.0, "gap_y_min": 0.0,
         "status": SESSION_ACTIVE, "current_wave": 2, "avg_price": 100.0, "total_filled_qty": 10.0,
         "total_cost": 1000.0, "peak_price": 120.0, "sl_pct": 8.0, "trail_active": True,
         "trail_sl_price": 110.0}
    d.update(kw)
    s = KssSession(**d)
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


def _position(db, avg):
    db.add(Position(symbol="AAA", quantity=10.0, avg_entry_price=avg, total_cost=avg * 10.0))
    db.commit()


def _sells(db, sid, kind):
    return db.query(PendingOrder).filter(
        PendingOrder.source_ref == f"pyramid:{sid}:{kind}", PendingOrder.side == "SELL").count()


# ===== Fix A =====

def test_trail_defers_when_below_blended_position_cost(db, monkeypatch):
    """trail_sl hit, but the blended Position avg (115) is above the trail → defer, do NOT sell."""
    s = _session(db)
    _position(db, avg=115.0)            # orphan/cross-session lot inflated the wallet basis
    _price(monkeypatch, 109.0)          # ≤ trail_sl 110 but far below cost 115; above hard floor 92
    service.manage_open_sessions(db)
    db.refresh(s)
    assert _sells(db, s.id, "trail_sl") == 0 and _sells(db, s.id, "sl") == 0
    assert s.status == SESSION_ACTIVE   # deferred, holding


def test_deferred_deep_loser_is_cut_by_hard_sl(db, monkeypatch):
    """Below blended cost AND below the hard-SL floor (avg×(1-8%)=92) → hard SL still cuts it."""
    s = _session(db)
    _position(db, avg=115.0)
    _price(monkeypatch, 91.0)           # ≤ hard floor 92 → genuine deep loser
    triggered = service.manage_open_sessions(db)
    db.refresh(s)
    assert _sells(db, s.id, "sl") == 1 and _sells(db, s.id, "trail_sl") == 0
    assert s.status == SESSION_STOPPED and s.id in triggered


def test_trail_still_sells_when_it_clears_cost(db, monkeypatch):
    """Regression: with no blended excess (no Position basis), the trail exits normally."""
    s = _session(db)                    # no Position row → _tp_clears_cost True
    _price(monkeypatch, 109.0)          # ≤ trail_sl 110
    service.manage_open_sessions(db)
    db.refresh(s)
    assert _sells(db, s.id, "trail_sl") == 1 and s.status == SESSION_STOPPED


def test_tp_defers_when_below_blended_cost(db, monkeypatch):
    """The spike-grab TP also defers if it would realize below the blended cost basis."""
    s = _session(db, trail_sl_price=110.0)
    _position(db, avg=130.0)            # cost basis above the carried TP
    _price(monkeypatch, 116.0)          # ≥ carried TP (110×1.05=115.5) but < cost 130
    service.manage_open_sessions(db)
    db.refresh(s)
    assert _sells(db, s.id, "tp") == 0 and s.status == SESSION_ACTIVE


# ===== Fix B =====

def test_open_blocked_when_pending_sell_in_flight(db):
    s = _session(db)
    db.add(PendingOrder(symbol="AAA", side="SELL", quantity=10, price=0.0, order_type="MARKET",
                        status=PENDING, source="kss", source_ref=f"pyramid:{s.id}:trail_sl"))
    db.commit()
    assert scanner._trade_block_reason(db, "AAA") == "exit (SELL) in flight"


def test_dynamic_trail_sl_fill_sets_cooldown(db):
    """A dynamic trail_sl fill must STOP the session AND set the re-entry cooldown (was missing)."""
    s = _session(db)
    service.handle_fill_event(db, f"pyramid:{s.id}:trail_sl", 10.0, 109.0)
    db.refresh(s)
    assert s.status == SESSION_STOPPED
    assert runtime.get(db, "stop_cooldown:AAA") is not None
