"""P0.5 + P0.6 — the 90s position guard as a freeze-immune, fast hard-SL net.

P0.6 (C3): a NON-armed dca_down session's avg-anchored hard SL was only sampled by the 30-min
cycle, so a fast drop overshot the floor (NFP: -17.3% vs -15%). run_position_guard now checks it
every ~90s.

P0.5 (C2): the guard fills queued KSS exit SELLs via reviewer="guard" (a non-AUTO reviewer), so a
circuit-breaker freeze — which no-ops auto_fill_due_orders — can never trap a protective SL exit.
"""

from __future__ import annotations

import pytest

from app import market, orders
from app.config import settings
from app.kss import service
from app.models import (
    PENDING,
    SESSION_ACTIVE,
    SESSION_STOPPED,
    AuditLog,
    KssSession,
    PendingOrder,
)


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    monkeypatch.setattr(settings, "kss_dynamic_tp_enabled", True)
    monkeypatch.setattr(settings, "sl_pct", 15.0)
    monkeypatch.setattr(service, "_session_atr_pct", lambda sym: 6.0)


def _price(monkeypatch, px):
    monkeypatch.setattr(market, "get_current_prices", lambda syms, force=False: {"AAA": px})


def _session(db, **kw):
    d = {"symbol": "AAA", "entry_price": 100.0, "distance_pct": 1.5, "max_waves": 8,
         "isolated_fund": 1000.0, "tp_pct": 4.0, "timeout_x_min": 43200.0, "gap_y_min": 0.0,
         "status": SESSION_ACTIVE, "current_wave": 2, "avg_price": 100.0, "total_filled_qty": 10.0,
         "total_cost": 1000.0, "peak_price": 0.0, "sl_pct": 8.0, "trail_active": False,
         "strategy_mode": "dca_down"}
    d.update(kw)
    s = KssSession(**d)
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


def _stub_approve(monkeypatch):
    """Isolate from the fill machinery; record every (order_id, reviewer) the guard fills."""
    calls: list[tuple[int, str | None]] = []

    def _f(_db, oid, reviewer=None):
        po = _db.get(PendingOrder, oid)
        if po is not None:
            po.status = "FILLED"
        calls.append((oid, reviewer))
        return type("F", (), {"price": 90.0})()

    monkeypatch.setattr(orders, "approve_order", _f)
    return calls


# --- P0.6: non-armed hard-SL net -------------------------------------------

def test_guard_cuts_non_armed_session_below_floor(db, monkeypatch):
    """avg=100, sl=8 → floor 92. Price 91 (< floor) on a NON-armed session → guard queues+fills SL."""
    s = _session(db, sl_pct=8.0, trail_active=False)
    _price(monkeypatch, 91.0)
    calls = _stub_approve(monkeypatch)

    res = service.run_position_guard(db)
    db.refresh(s)

    assert s.status == SESSION_STOPPED
    assert s.id in res["exited"]
    assert db.query(PendingOrder).filter_by(source_ref=f"pyramid:{s.id}:sl", side="SELL").count() == 1
    assert len(calls) == 1 and calls[0][1] == "guard"  # filled by the guard (freeze-immune reviewer)
    assert db.query(AuditLog).filter_by(action="stop_queued").count() == 1


def test_guard_holds_non_armed_session_above_floor(db, monkeypatch):
    """Price above the floor → no exit (the guard must not cut a healthy session)."""
    s = _session(db, sl_pct=8.0, trail_active=False)
    _price(monkeypatch, 95.0)   # above floor 92
    _stub_approve(monkeypatch)

    res = service.run_position_guard(db)
    db.refresh(s)

    assert s.status == SESSION_ACTIVE
    assert res["exited"] == []
    assert db.query(PendingOrder).filter_by(source_ref=f"pyramid:{s.id}:sl").count() == 0


def test_guard_ignores_session_with_no_inventory(db, monkeypatch):
    s = _session(db, total_filled_qty=0.0, trail_active=False)
    _price(monkeypatch, 50.0)
    _stub_approve(monkeypatch)
    res = service.run_position_guard(db)
    db.refresh(s)
    assert s.status == SESSION_ACTIVE and res["checked"] == 0


# --- P0.5: freeze-immune fill of an already-queued SL SELL -------------------

def test_guard_fills_pending_sl_for_stopped_session_while_frozen(db, monkeypatch):
    """A hard-SL SELL queued by the 30-min cycle (session already STOPPED, no ACTIVE session left)
    is still filled by the guard while the breaker is frozen. auto_fill_due_orders no-ops when
    runtime.is_frozen, but the guard calls approve_order(reviewer='guard') directly — never gated —
    and the fill loop runs even when there are zero ACTIVE sessions."""
    from app import runtime
    monkeypatch.setattr(runtime, "is_frozen", lambda _db=None: True)

    s = _session(db, status=SESSION_STOPPED, trail_active=False)
    po = PendingOrder(symbol="AAA", side="SELL", quantity=10.0, price=0.0, order_type="MARKET",
                      status=PENDING, source_ref=f"pyramid:{s.id}:sl", strategy_name="Pyramid_AAA")
    db.add(po)
    db.commit()
    po_id = po.id
    calls = _stub_approve(monkeypatch)

    res = service.run_position_guard(db)

    assert res["checked"] == 0                       # no ACTIVE session — but the fill loop still ran
    assert calls == [(po_id, "guard")]               # the queued SL SELL was filled, freeze-immune
