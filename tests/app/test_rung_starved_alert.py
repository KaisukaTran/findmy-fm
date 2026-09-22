"""A rung BUY refused by the cash floor (`InsufficientCashError`) used to be total silence:
`auto_fill_due_orders` only logged `logger.warning` and retried every tick forever — the
session, the dashboard, and the operator never heard about it.

Now: the FIRST refusal of a given pending order writes an audit row (`rung_starved`, entity
`kss:{session_id}`) and fires one Telegram risk alert; a refusal that repeats within
`rung_starved_alert_min` minutes is silent (the order is still queued and still retried every
tick — only the ALERT is throttled); several rungs starved in the same pass are summarised into
ONE Telegram message, never one per rung. Wave 0 is excluded — its own cash refusal is already
audited by `scanner.open_underfunded` on the synchronous open path. A Pyramid-UP add is
excluded too (new risk, not this alert's ladder). A PARTIAL fill (`_apply_cash_cap` trims
instead of outright refusing) fires the same alert with reason="trimmed". The tracker itself is
pruned each pass so it never grows for an order that has since resolved one way or another.
"""

from __future__ import annotations

import json

import pytest

from app import execution, orders
from app import notify as notify_module
from app.clock import utcnow
from app.config import settings
from app.models import PENDING, REJECTED, AuditLog, KssSession, PendingOrder


@pytest.fixture(autouse=True)
def _clean():
    orders.reset_rung_starved_alert_state()
    yield
    orders.reset_rung_starved_alert_state()


def _rung(db, *, symbol="SOL", price=9.0, wave=1, session_id=1, quantity=2.0) -> PendingOrder:
    o = PendingOrder(
        symbol=symbol, side="BUY", order_type="LIMIT", quantity=quantity, price=price,
        source="kss", source_ref=f"pyramid:{session_id}:wave:{wave}", status=PENDING,
    )
    db.add(o)
    db.commit()
    db.refresh(o)
    return o


def _wire(monkeypatch, *, prices: dict, starve_ids: set[int] | None = None):
    """Paper, sampled-price model (touch model off): every symbol in *prices* is at/below its
    order's limit (due), and every order whose id is in *starve_ids* (default: all) is refused
    by the cash floor."""
    monkeypatch.setattr(execution, "live_enabled", lambda: False)
    monkeypatch.setattr(orders, "get_current_prices", lambda syms: {s: prices.get(s, 0.0) for s in syms})
    monkeypatch.setattr(settings, "paper_fill_touch_1m", False)
    # Risk alerts bypass the master mute but still gate on their own switch — this repo's own
    # .env ships it off (measured 2026-09-21 quiet run), so tests turn it on explicitly, same
    # as every other notify-risk test in this suite (see test_notify_routing.py).
    monkeypatch.setattr(settings, "telegram_notify_risk", True)

    def _cash_cap(db_, o):
        if starve_ids is None or o.id in starve_ids:
            raise orders.InsufficientCashError("no cash")

    monkeypatch.setattr(orders, "_apply_cash_cap", _cash_cap)


def _rows(db):
    return db.query(AuditLog).filter(AuditLog.action == "rung_starved").all()


def test_first_refusal_audits_and_notifies_once(db, monkeypatch):
    sent: list[str] = []
    monkeypatch.setattr(notify_module, "send", lambda text, **k: sent.append(text) or True)
    _rung(db, price=9.0)
    _wire(monkeypatch, prices={"SOL": 9.0})

    assert orders.auto_fill_due_orders(db) == []

    rows = _rows(db)
    assert len(rows) == 1
    assert rows[0].entity == "kss:1"
    assert len(sent) == 1
    assert "1 rung" in sent[0]


def test_repeated_refusal_within_the_window_is_silent(db, monkeypatch):
    sent: list[str] = []
    monkeypatch.setattr(notify_module, "send", lambda text, **k: sent.append(text) or True)
    _rung(db, price=9.0)
    _wire(monkeypatch, prices={"SOL": 9.0})

    orders.auto_fill_due_orders(db)
    assert len(_rows(db)) == 1
    assert len(sent) == 1

    # Same order, refused again immediately — well inside rung_starved_alert_min.
    orders.auto_fill_due_orders(db)
    orders.auto_fill_due_orders(db)

    assert len(_rows(db)) == 1, "no new audit row within the alert window"
    assert len(sent) == 1, "no new Telegram message within the alert window"


def test_alert_fires_again_after_the_window_elapses(db, monkeypatch):
    sent: list[str] = []
    monkeypatch.setattr(notify_module, "send", lambda text, **k: sent.append(text) or True)
    rung = _rung(db, price=9.0)
    _wire(monkeypatch, prices={"SOL": 9.0})

    orders.auto_fill_due_orders(db)
    assert len(_rows(db)) == 1
    assert len(sent) == 1

    # Fast-forward past the window by back-dating the tracked timestamp directly (avoids
    # depending on wall-clock sleeps).
    from datetime import timedelta

    orders._rung_starved_last_alert[rung.id] = utcnow() - timedelta(
        minutes=settings.rung_starved_alert_min + 1
    )

    orders.auto_fill_due_orders(db)

    assert len(_rows(db)) == 2, "a fresh refusal after the window must alert again"
    assert len(sent) == 2


def test_several_starved_rungs_in_one_pass_get_one_aggregated_message(db, monkeypatch):
    sent: list[str] = []
    monkeypatch.setattr(notify_module, "send", lambda text, **k: sent.append(text) or True)
    _rung(db, symbol="SOL", price=9.0, wave=1, session_id=1)
    _rung(db, symbol="ETH", price=1500.0, wave=2, session_id=2)
    _wire(monkeypatch, prices={"SOL": 9.0, "ETH": 1500.0})

    assert orders.auto_fill_due_orders(db) == []

    rows = _rows(db)
    assert len(rows) == 2, "each starved rung still gets its own audit row"
    assert len(sent) == 1, "but only ONE Telegram message summarises the whole pass"
    assert "2 rung" in sent[0]


def test_wave_zero_refusal_is_not_double_audited_here(db, monkeypatch):
    """Wave 0's own cash refusal is already audited by scanner.open_underfunded on the
    synchronous open path — auto_fill_due_orders must not also write a rung_starved row for it."""
    sent: list[str] = []
    monkeypatch.setattr(notify_module, "send", lambda text, **k: sent.append(text) or True)
    _rung(db, price=9.0, wave=0, session_id=1)
    _wire(monkeypatch, prices={"SOL": 9.0})

    assert orders.auto_fill_due_orders(db) == []

    assert _rows(db) == []
    assert sent == []


def test_a_non_cash_failure_never_starves_the_alert(db, monkeypatch):
    """A venue rejection / no-price failure is a different failure mode entirely — must not be
    misreported as a cash-starved rung."""
    sent: list[str] = []
    monkeypatch.setattr(notify_module, "send", lambda text, **k: sent.append(text) or True)
    _rung(db, price=9.0)
    monkeypatch.setattr(execution, "live_enabled", lambda: False)
    monkeypatch.setattr(orders, "get_current_prices", lambda syms: {"SOL": 9.0})
    monkeypatch.setattr(settings, "paper_fill_touch_1m", False)
    monkeypatch.setattr(settings, "telegram_notify_risk", True)

    def _boom(db_, oid, reviewer=None, fill_price=None):
        raise ValueError("simulated venue reject")

    monkeypatch.setattr(orders, "approve_order", _boom)

    assert orders.auto_fill_due_orders(db) == []

    assert _rows(db) == []
    assert sent == []


def test_pyramid_up_add_refusal_is_not_audited_here(db, monkeypatch):
    """A Pyramid-UP add reuses the exact same pyramid:{id}:wave:{k} shape as a dca_down rung —
    only the session's own strategy_mode tells them apart, and it is new risk, not the ladder
    this alert exists to surface."""
    sent: list[str] = []
    monkeypatch.setattr(notify_module, "send", lambda text, **k: sent.append(text) or True)
    row = KssSession(
        symbol="SOL", entry_price=9.0, distance_pct=4.0, max_waves=3, isolated_fund=100.0,
        tp_pct=5.0, timeout_x_min=60, gap_y_min=5, strategy_mode="pyramid_up",
        current_wave=1, avg_price=9.0, total_filled_qty=1.0, total_cost=9.0,
    )
    db.add(row)
    db.commit()
    _rung(db, price=9.0, wave=1, session_id=row.id)
    _wire(monkeypatch, prices={"SOL": 9.0})

    assert orders.auto_fill_due_orders(db) == []

    assert _rows(db) == []
    assert sent == []


def test_market_order_needed_usd_uses_the_sampled_market_price(db, monkeypatch):
    """A MARKET rung's own `price` is 0 — the $ needed must come from the sampled market price
    passed in by the caller, not silently read as $0."""
    o = PendingOrder(
        symbol="SOL", side="BUY", order_type="MARKET", quantity=2.0, price=0.0,
        source="kss", source_ref="pyramid:1:wave:1", status=PENDING,
    )
    db.add(o)
    db.commit()
    db.refresh(o)
    _wire(monkeypatch, prices={"SOL": 9.0})

    assert orders.auto_fill_due_orders(db) == []

    rows = _rows(db)
    assert len(rows) == 1
    detail = json.loads(rows[0].detail)
    assert detail["needed_usd"] == pytest.approx(2.0 * 9.0)


def test_stale_tracked_order_is_pruned_once_no_longer_pending(db, monkeypatch):
    rung = _rung(db, price=9.0)
    _wire(monkeypatch, prices={"SOL": 9.0})

    orders.auto_fill_due_orders(db)
    assert rung.id in orders._rung_starved_last_alert

    # The order resolves some other way (rejected, filled elsewhere, ...) — no longer PENDING.
    rung.status = REJECTED
    db.commit()

    orders.auto_fill_due_orders(db)  # nothing left to process, but pruning still runs

    assert rung.id not in orders._rung_starved_last_alert


def test_a_trimmed_rung_alerts_with_reason_trimmed(db, monkeypatch):
    """`_apply_cash_cap` trimming a k>=1 rung (partial fill, remainder dropped) is the same
    cash-starvation signal as an outright refusal — same alert, same throttle, reason
    "trimmed"."""
    sent: list[str] = []
    monkeypatch.setattr(notify_module, "send", lambda text, **k: sent.append(text) or True)
    monkeypatch.setattr(settings, "telegram_notify_risk", True)
    monkeypatch.setattr(execution, "live_enabled", lambda: False)
    monkeypatch.setattr(settings, "account_equity", 100.0)
    monkeypatch.setattr(settings, "cash_floor_usd", 0.0)
    monkeypatch.setattr(settings, "scan_min_notional", 1.0)

    o, _ = orders.queue_order(
        db, symbol="SOL", side="BUY", quantity=100.0, price=9.0,
        source="kss", source_ref="pyramid:1:wave:2",
    )

    fill = orders.approve_order(db, o.id, reviewer="auto-trader")

    assert fill.quantity < 100.0, "the order must have been trimmed, not fully funded"
    rows = _rows(db)
    assert len(rows) == 1
    detail = json.loads(rows[0].detail)
    assert detail["reason"] == "trimmed"
    assert len(sent) == 1
