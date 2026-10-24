"""K3: a session's stop-loss is frozen onto it when it opens - 0 means OFF, never "follow the
global". The four readers used to resolve `row.sl_pct if row.sl_pct > 0 else settings.sl_pct`
at read time, and every session stored 0 (the scanner and the API never passed one), so every
open session followed the GLOBAL retroactively: losing the one runtime_config row that holds the
operator's `sl_pct=0` would have let the 90s guard market-sell every open session at -8%.
The global now applies to NEW sessions only (snapshotted in `create_session`, like first_wave_usd).
"""

from __future__ import annotations

import pytest

from app import market, models, orders
from app.config import settings
from app.kss import service

_EX = {"minQty": 0.00001, "stepSize": 0.00001, "maxQty": 10000.0}
_PARAMS = {
    "symbol": "BTC", "entry_price": 50000, "distance_pct": 2, "max_waves": 3,
    "isolated_fund": 100000, "tp_pct": 3, "timeout_x_min": 30, "gap_y_min": 5,
    "deadline_days": 30,
}


@pytest.fixture
def px(monkeypatch) -> dict[str, float]:
    """A mutable BTC price behind every price entry point; frozen exits only (no dynamic
    channel / tp-then-trail / resting TP, whatever the trading machine's .env says)."""
    table = {"BTC": 50000.0}

    def prices(syms, force=False):
        return {s: table[s] for s in syms if s in table}

    monkeypatch.setattr("app.kss.pyramid.get_exchange_info", lambda s: _EX)
    monkeypatch.setattr("app.kss.pyramid.get_current_prices", prices)
    monkeypatch.setattr("app.orders.get_current_prices", prices)
    monkeypatch.setattr("app.market.get_current_prices", prices)
    monkeypatch.setattr(settings, "kss_dynamic_tp_enabled", False)
    monkeypatch.setattr(settings, "kss_trail_after_tp_pct", 0.0)
    monkeypatch.setattr(settings, "paper_fill_touch_1m", False)
    return table


def _held_session(db) -> models.KssSession:
    """An ACTIVE BTC session that really holds inventory (wave 0 filled via the real approve)."""
    row = service.create_session(db, **_PARAMS)
    res = service.start_session(db, row.id)
    orders.approve_order(db, res["pending_order_id"])
    db.refresh(row)
    assert row.status == models.SESSION_ACTIVE and row.total_filled_qty > 0
    return row


def _sl_sells(db, session_id: int) -> list[models.PendingOrder]:
    """Every stop-loss SELL queued for the session, in ANY status: the 90s guard force-fills what
    it queues within the same tick, so a PENDING-only filter would miss a stop that already ran."""
    return (
        db.query(models.PendingOrder)
        .filter(
            models.PendingOrder.source_ref == f"pyramid:{session_id}:sl",
            models.PendingOrder.side == "SELL",
        )
        .all()
    )


def _run_both_loops(db) -> None:
    service.run_position_guard(db)
    service.manage_open_sessions(db)


# --- create_session snapshots the global ----------------------------------------------------


def test_create_snapshots_global_sl(db, px, monkeypatch):
    monkeypatch.setattr(settings, "sl_pct", 8.0)
    assert service.create_session(db, **_PARAMS).sl_pct == 8.0
    monkeypatch.setattr(settings, "sl_pct", 0.0)
    assert service.create_session(db, **_PARAMS).sl_pct == 0.0


def test_explicit_sl_is_kept(db, px, monkeypatch):
    monkeypatch.setattr(settings, "sl_pct", 8.0)
    assert service.create_session(db, **_PARAMS, sl_pct=12.0).sl_pct == 12.0


# --- the readers no longer fall back to the global -------------------------------------------


def test_global_change_does_not_touch_open_session(db, px, monkeypatch):
    monkeypatch.setattr(settings, "sl_pct", 0.0)         # the operator's posture: stop-loss off
    row = _held_session(db)
    assert row.sl_pct == 0.0
    monkeypatch.setattr(settings, "sl_pct", 8.0)         # the global comes back (lost row / reset)
    price = row.avg_price * 0.85                         # well past an 8% floor
    px["BTC"] = price

    assert service._guard_hard_sl(db, row, price) is False
    assert service._to_pyramid(row).sl_pct == 0.0
    _run_both_loops(db)
    assert _sl_sells(db, row.id) == []
    db.refresh(row)
    assert row.status == models.SESSION_ACTIVE


def test_session_sl_applies_when_set(db, px, monkeypatch):
    monkeypatch.setattr(settings, "sl_pct", 0.0)
    row = _held_session(db)
    row.sl_pct = 8.0
    db.commit()

    assert service._guard_hard_sl(db, row, row.avg_price * 0.90) is True
    assert len(_sl_sells(db, row.id)) == 1
    assert service._to_pyramid(row).sl_pct == 8.0


def test_zero_means_off_even_if_global_on(db, px, monkeypatch):
    monkeypatch.setattr(settings, "sl_pct", 0.0)
    row = _held_session(db)
    monkeypatch.setattr(settings, "sl_pct", 8.0)
    px["BTC"] = row.avg_price * 0.5                      # a 50% crash

    _run_both_loops(db)

    assert _sl_sells(db, row.id) == []


# --- the dynamic channel's hard-SL fallback (`_evaluate_dynamic_exit`) ------------------------


def _armed_loser(db, sl_pct: float) -> models.KssSession:
    """An ARMED session (carried trail stop 110) deep under its average. The caller adds a
    blended Position cost above the trail, so the trail exit DEFERS and only the hard-SL
    fallback can cut it (the shape of test_kss_loss_guards' deferred-deep-loser test)."""
    row = models.KssSession(
        symbol="AAA", entry_price=100.0, distance_pct=1.5, max_waves=6, isolated_fund=1000.0,
        tp_pct=4.0, timeout_x_min=43200.0, gap_y_min=0.0, status=models.SESSION_ACTIVE,
        current_wave=2, avg_price=100.0, total_filled_qty=10.0, total_cost=1000.0,
        peak_price=120.0, sl_pct=sl_pct, trail_active=True, trail_sl_price=110.0,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@pytest.fixture
def dynamic(monkeypatch):
    for name, value in (("taker_fee_pct", 0.1), ("slippage_pct", 0.05), ("kss_exit_fee_mult", 3.0),
                        ("kss_tp_gap_pct", 5.0), ("kss_trail_atr_mult", 1.0),
                        ("kss_trail_min_pct", 3.0), ("kss_trail_arm_pct", 5.0),
                        ("kss_trail_lock_pct", 2.0), ("kss_dynamic_tp_enabled", True),
                        ("kss_trail_after_tp_pct", 0.0), ("paper_fill_touch_1m", False)):
        monkeypatch.setattr(settings, name, value)
    monkeypatch.setattr(service, "_session_atr_pct", lambda sym: 6.0)
    monkeypatch.setattr(market, "get_current_prices", lambda syms, force=False: {"AAA": 91.0})


def test_dynamic_exit_fallback_uses_session_sl(db, dynamic, monkeypatch):
    monkeypatch.setattr(settings, "sl_pct", 8.0)         # global ON: must not leak into a 0 session
    db.add(models.Position(symbol="AAA", quantity=10.0, avg_entry_price=115.0, total_cost=1150.0))
    off = _armed_loser(db, sl_pct=0.0)
    service.manage_open_sessions(db)                     # price 91 <= 100 * (1 - 8%) = 92
    assert _sl_sells(db, off.id) == []

    on = _armed_loser(db, sl_pct=8.0)
    service.manage_open_sessions(db)
    assert len(_sl_sells(db, on.id)) == 1


def test_pyramid_up_hard_sl_uses_session_sl(db, monkeypatch):
    """The momentum-mode floor reads the same frozen value: 0 = off, whatever the global says."""
    monkeypatch.setattr(settings, "sl_pct", 8.0)
    row = models.KssSession(
        symbol="AAA", entry_price=100.0, distance_pct=1.5, max_waves=3, isolated_fund=1000.0,
        tp_pct=4.0, timeout_x_min=43200.0, gap_y_min=0.0, status=models.SESSION_ACTIVE,
        strategy_mode="pyramid_up", avg_price=100.0, total_filled_qty=10.0, total_cost=1000.0,
        sl_pct=0.0,
    )
    db.add(row)
    db.commit()
    assert service._pyramid_up_hard_sl(db, row, 80.0) is False
    assert _sl_sells(db, row.id) == []

    row.sl_pct = 8.0
    db.commit()
    assert service._pyramid_up_hard_sl(db, row, 80.0) is True
    assert len(_sl_sells(db, row.id)) == 1
