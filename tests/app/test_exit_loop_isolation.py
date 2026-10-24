"""O4: one session that raises must not stall the exit loops for every session after it.

`manage_open_sessions` (15-min cycle) and `run_position_guard` (90s guard) used to run their
whole `for row in active:` body unprotected: a row whose evaluation raised (a validation error in
`_to_pyramid`, an OperationalError on queue/commit, a bad price shape) aborted the loop, so every
later session went unevaluated that tick - and in the guard `_force_fill_queued_exits` never ran,
so exit SELLs that were already queued did not fill either. Exits are the one thing that must
never be slowed, so each session is now an isolated unit (mirrors `auto_fill_due_orders`).
"""

from __future__ import annotations

import pytest

from app import models, orders
from app.config import settings
from app.kss import service

_EX = {"minQty": 0.00001, "stepSize": 0.00001, "maxQty": 10000.0}
_ENTRY = {"BTC": 50000.0, "ETH": 3000.0, "SOL": 150.0}


@pytest.fixture
def px(monkeypatch) -> dict[str, float]:
    """One mutable price table behind every price entry point the exit paths touch."""
    table = dict(_ENTRY)

    def prices(syms, force=False):
        return {s: table[s] for s in syms if s in table}

    monkeypatch.setattr("app.kss.pyramid.get_exchange_info", lambda s: _EX)
    monkeypatch.setattr("app.kss.pyramid.get_current_prices", prices)
    monkeypatch.setattr("app.orders.get_current_prices", prices)
    monkeypatch.setattr("app.market.get_current_prices", prices)
    # The frozen TP / hard-SL paths are under test: no dynamic channel, no tp-then-trail, and no
    # resting-TP model (a .env on the trading machine may switch any of these on).
    monkeypatch.setattr(settings, "kss_dynamic_tp_enabled", False)
    monkeypatch.setattr(settings, "kss_trail_after_tp_pct", 0.0)
    monkeypatch.setattr(settings, "paper_fill_touch_1m", False)
    return table


def _held(db, symbol: str) -> models.KssSession:
    """An ACTIVE session that really holds inventory - wave 0 filled through the real approve
    path, so the Position, the Fill and the KSS fill hook are all genuine."""
    row = service.create_session(
        db, symbol=symbol, entry_price=_ENTRY[symbol], distance_pct=2, max_waves=3,
        isolated_fund=100000, tp_pct=3, timeout_x_min=30, gap_y_min=5, deadline_days=30,
    )
    res = service.start_session(db, row.id)
    orders.approve_order(db, res["pending_order_id"])
    db.refresh(row)
    assert row.status == models.SESSION_ACTIVE and row.total_filled_qty > 0
    return row


def _three(db) -> dict[str, models.KssSession]:
    return {s: _held(db, s) for s in ("BTC", "ETH", "SOL")}


def _pending_sells(db, session_id: int, kind: str) -> list[models.PendingOrder]:
    return (
        db.query(models.PendingOrder)
        .filter(
            models.PendingOrder.source_ref == f"pyramid:{session_id}:{kind}",
            models.PendingOrder.side == "SELL",
            models.PendingOrder.status == models.PENDING,
        )
        .all()
    )


def _failures(db) -> list[models.AuditLog]:
    return db.query(models.AuditLog).filter(models.AuditLog.action == "session_eval_failed").all()


def _fail_for(monkeypatch, name: str, *symbols: str) -> None:
    """Make service.<name> raise for the given symbols and behave normally for the rest."""
    original = getattr(service, name)

    def flaky(db, row, *args, **kwargs):
        if row.symbol in symbols:
            raise RuntimeError("boom")
        return original(db, row, *args, **kwargs)

    monkeypatch.setattr(service, name, flaky)


def _spy(monkeypatch, name: str, *, call_through: bool = True) -> list[int]:
    """Record calls to service.<name>; optionally still run the real thing."""
    original = getattr(service, name)
    calls: list[int] = []

    def wrapper(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs) if call_through else None

    monkeypatch.setattr(service, name, wrapper)
    return calls


# --- manage_open_sessions ----------------------------------------------------------------------


def test_manage_isolates_a_failing_session(db, px, monkeypatch):
    rows = _three(db)
    for sym, row in rows.items():
        px[sym] = row.avg_price * 1.2          # well past every session's take-profit
    _fail_for(monkeypatch, "_manage_one_session", "ETH")

    triggered = service.manage_open_sessions(db)  # no exception escapes

    assert triggered == [rows["BTC"].id, rows["SOL"].id]
    assert len(_pending_sells(db, rows["BTC"].id, "tp")) == 1
    assert len(_pending_sells(db, rows["SOL"].id, "tp")) == 1
    assert _pending_sells(db, rows["ETH"].id, "tp") == []
    failed = _failures(db)
    assert len(failed) == 1
    assert failed[0].entity == f"kss:{rows['ETH'].id}" and failed[0].actor == "kss"
    assert '"where": "manage"' in failed[0].detail and "RuntimeError" in failed[0].detail


def test_manage_watchdogs_still_run_after_failure(db, px, monkeypatch):
    rows = _three(db)
    for sym, row in rows.items():
        px[sym] = row.avg_price * 1.2
    _fail_for(monkeypatch, "_manage_one_session", "ETH")
    rearm = _spy(monkeypatch, "_rearm_dead_ladders")
    reconcile = _spy(monkeypatch, "_reconcile_tp_triggered")

    service.manage_open_sessions(db)

    assert len(rearm) == 1 and len(reconcile) == 1


@pytest.mark.parametrize("broken, other", [
    ("_rearm_dead_ladders", "_reconcile_tp_triggered"),
    ("_reconcile_tp_triggered", "_rearm_dead_ladders"),
])
def test_manage_one_watchdog_failing_does_not_skip_the_other(db, px, monkeypatch, broken, other):
    rows = _three(db)
    for sym, row in rows.items():
        px[sym] = row.avg_price * 1.2

    def boom(db):
        raise RuntimeError("watchdog down")

    monkeypatch.setattr(service, broken, boom)
    survivor = _spy(monkeypatch, other)

    triggered = service.manage_open_sessions(db)  # returns normally

    assert len(survivor) == 1
    assert sorted(triggered) == sorted(r.id for r in rows.values())  # the loop's work is kept


def test_manage_without_failures_is_unchanged(db, px):
    rows = _three(db)
    for sym, row in rows.items():
        px[sym] = row.avg_price * 1.2

    triggered = service.manage_open_sessions(db)

    assert sorted(triggered) == sorted(r.id for r in rows.values())
    assert _failures(db) == []


# --- run_position_guard ------------------------------------------------------------------------


def _below_sl(db, px, rows) -> None:
    """Every session 15% under its average with a 10% hard SL - _guard_hard_sl cuts it."""
    for sym, row in rows.items():
        row.sl_pct = 10.0
        px[sym] = row.avg_price * 0.85
    db.commit()


def test_guard_isolates_and_still_force_fills(db, px, monkeypatch):
    rows = {s: _held(db, s) for s in ("BTC", "ETH")}
    _below_sl(db, px, rows)
    _fail_for(monkeypatch, "_guard_one_session", "BTC")  # the FIRST session raises
    forced = _spy(monkeypatch, "_force_fill_queued_exits", call_through=False)

    out = service.run_position_guard(db)  # no exception escapes

    assert out == {"checked": 2, "exited": [rows["ETH"].id]}
    assert len(_pending_sells(db, rows["ETH"].id, "sl")) == 1   # the second session still got cut
    assert _pending_sells(db, rows["BTC"].id, "sl") == []
    assert len(forced) == 1
    failed = _failures(db)
    assert len(failed) == 1 and failed[0].entity == f"kss:{rows['BTC'].id}"
    assert '"where": "guard"' in failed[0].detail


def test_guard_force_fill_runs_when_last_session_fails(db, px, monkeypatch):
    rows = {s: _held(db, s) for s in ("BTC", "ETH")}
    _below_sl(db, px, rows)
    _fail_for(monkeypatch, "_guard_one_session", "ETH")  # the LAST session raises
    forced = _spy(monkeypatch, "_force_fill_queued_exits", call_through=False)

    out = service.run_position_guard(db)

    assert out == {"checked": 2, "exited": [rows["BTC"].id]}
    assert len(forced) == 1
    assert len(_failures(db)) == 1


def test_guard_force_fills_an_exit_that_was_queued_before_the_failure(db, px, monkeypatch):
    """The whole point: an exit SELL already in the queue must still fill this tick."""
    rows = {s: _held(db, s) for s in ("BTC", "ETH")}
    sell, _ = orders.queue_order(
        db, symbol="BTC", side="SELL", quantity=rows["BTC"].total_filled_qty, price=0.0,
        order_type="MARKET", source="kss", source_ref=f"pyramid:{rows['BTC'].id}:sl",
    )
    _fail_for(monkeypatch, "_guard_one_session", "BTC", "ETH")  # every session raises

    service.run_position_guard(db)

    db.refresh(sell)
    assert sell.status == models.EXECUTED


def test_guard_without_failures_is_unchanged(db, px):
    rows = {s: _held(db, s) for s in ("BTC", "ETH")}
    _below_sl(db, px, rows)

    out = service.run_position_guard(db)

    assert out["checked"] == 2 and sorted(out["exited"]) == sorted(r.id for r in rows.values())
    assert _failures(db) == []
