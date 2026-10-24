"""K7: a DCA rung that cash can fund only in a thin slice is REFUSED (it waits in the queue for
enough cash) instead of filled at 27% - and only a rung filled above the threshold counts as a
rung for the take-profit step.

Before this, `_apply_cash_cap` trimmed any rung to whatever was affordable and the frozen `on_fill`
marked it FILLED, so `_to_pyramid` counted it as a full rung: +kss_tp_step_per_rung_pct on the TP
for a slice that barely moved the average. One knob, `kss_partial_rung_min_pct` (0 = old rule).
"""

from __future__ import annotations

import json

import pytest

from app import execution, models, orders, runtime
from app.config import settings
from app.kss import service

_Q = 100.0
_P = 10.0


@pytest.fixture(autouse=True)
def _cash_model(monkeypatch):
    """Paper, sampled-price model, no floor: free cash is exactly `account_equity`."""
    orders.reset_rung_starved_alert_state()
    monkeypatch.setattr(execution, "live_enabled", lambda: False)
    monkeypatch.setattr(orders, "get_current_prices", lambda syms: dict.fromkeys(syms, _P))
    monkeypatch.setattr(settings, "paper_fill_touch_1m", False)
    monkeypatch.setattr(settings, "telegram_notify_risk", False)
    monkeypatch.setattr(settings, "cash_floor_usd", 0.0)
    monkeypatch.setattr(settings, "slippage_pct", 0.05)
    monkeypatch.setattr(settings, "taker_fee_pct", 0.1)
    monkeypatch.setattr(settings, "scan_min_notional", 1.0)
    monkeypatch.setattr(settings, "kss_partial_rung_min_pct", 50.0)
    # PyramidSession() looks the symbol's filters up; never let that reach the exchange.
    monkeypatch.setattr("app.kss.pyramid.get_exchange_info",
                        lambda s: {"minQty": 0.00001, "stepSize": 0.00001, "maxQty": 10000.0})
    yield
    orders.reset_rung_starved_alert_state()


def _fund(monkeypatch, fraction: float) -> None:
    """Set cash so exactly `fraction` of the rung's quantity is affordable."""
    unit_cost = _P * (1 + settings.slippage_pct / 100.0) * (1 + settings.taker_fee_pct / 100.0)
    monkeypatch.setattr(settings, "account_equity", fraction * _Q * unit_cost)


def _rung(db, *, side="BUY", ref="pyramid:1:wave:2", qty=_Q) -> models.PendingOrder:
    order, _ = orders.queue_order(
        db, symbol="SOL", side=side, quantity=qty, price=_P, order_type="LIMIT",
        source="kss", source_ref=ref,
    )
    return order


def _audits(db, action: str) -> list[models.AuditLog]:
    return db.query(models.AuditLog).filter(models.AuditLog.action == action).all()


# --- the cash cap ------------------------------------------------------------------------------


def test_trim_below_threshold_is_refused(db, monkeypatch):
    order = _rung(db)
    _fund(monkeypatch, 0.30)

    with pytest.raises(orders.InsufficientCashError, match="50"):
        orders._apply_cash_cap(db, order)

    assert order.quantity == _Q                       # never mutated: the rung keeps its plan
    assert _audits(db, "partial_fill_cash") == []


def test_refused_rung_stays_pending_and_is_reported(db, monkeypatch):
    """Through the real fill loop: no Fill, still PENDING, and the existing starved-rung alert
    fires with reason "refused"."""
    order = _rung(db)
    _fund(monkeypatch, 0.30)

    assert orders.auto_fill_due_orders(db) == []

    db.refresh(order)
    assert order.status == models.PENDING and order.quantity == _Q
    assert db.query(models.Fill).count() == 0
    starved = _audits(db, "rung_starved")
    assert len(starved) == 1 and json.loads(starved[0].detail)["reason"] == "refused"
    assert _audits(db, "partial_fill_cash") == []


def test_trim_above_threshold_still_partial(db, monkeypatch):
    order = _rung(db)
    _fund(monkeypatch, 0.70)

    orders._apply_cash_cap(db, order)

    assert order.quantity == pytest.approx(0.70 * _Q, rel=1e-6)
    assert len(_audits(db, "partial_fill_cash")) == 1


def test_threshold_zero_is_old_behaviour(db, monkeypatch):
    monkeypatch.setattr(settings, "kss_partial_rung_min_pct", 0.0)
    order = _rung(db)
    _fund(monkeypatch, 0.30)

    orders._apply_cash_cap(db, order)                 # trimmed, not refused

    assert order.quantity == pytest.approx(0.30 * _Q, rel=1e-6)
    assert len(_audits(db, "partial_fill_cash")) == 1


def test_sell_never_trimmed_or_refused(db, monkeypatch):
    order = _rung(db, side="SELL", ref="pyramid:1:tp", qty=5.0)
    monkeypatch.setattr(settings, "account_equity", 0.0)

    orders._apply_cash_cap(db, order)                 # no-op: exits are never gated

    assert order.quantity == 5.0
    assert _audits(db, "partial_fill_cash") == []


@pytest.mark.parametrize("ref", ["pyramid:1:wave:0", "manual:buy", ""])
def test_only_ladder_rungs_are_subject_to_the_threshold(db, monkeypatch, ref):
    """The opening wave and any non-rung BUY keep the old trim - the knob is about DCA rungs."""
    order = _rung(db, ref=ref)
    _fund(monkeypatch, 0.30)

    orders._apply_cash_cap(db, order)

    assert order.quantity == pytest.approx(0.30 * _Q, rel=1e-6)


# --- the take-profit step ------------------------------------------------------------------------


def _session_with_rungs(db, fills: dict[int, float]) -> models.KssSession:
    """An ACTIVE session whose waves are all FILLED; `fills` = {wave_num: filled_qty}, every
    wave planned at quantity 10."""
    row = models.KssSession(
        symbol="AAA", entry_price=100.0, distance_pct=1.5, max_waves=6, isolated_fund=1000.0,
        tp_pct=5.0, timeout_x_min=43200.0, gap_y_min=0.0, status=models.SESSION_ACTIVE,
        current_wave=max(fills), avg_price=100.0, total_filled_qty=10.0, total_cost=1000.0,
    )
    db.add(row)
    db.commit()
    for num, filled in fills.items():
        db.add(models.KssWave(
            session_id=row.id, wave_num=num, quantity=10.0, target_price=100.0,
            status=models.WAVE_FILLED, filled_qty=filled, filled_price=100.0,
        ))
    db.commit()
    db.refresh(row)
    return row


def test_tp_step_ignores_sub_threshold_rungs(db, monkeypatch):
    monkeypatch.setattr(settings, "kss_tp_step_per_rung_pct", 0.5)
    row = _session_with_rungs(db, {1: 10.0, 2: 2.7})   # wave 2 filled at 27% of its plan

    monkeypatch.setattr(settings, "kss_partial_rung_min_pct", 50.0)
    assert service._to_pyramid(row).tp_pct == pytest.approx(5.5)    # only wave 1 counts

    monkeypatch.setattr(settings, "kss_partial_rung_min_pct", 0.0)
    assert service._to_pyramid(row).tp_pct == pytest.approx(6.0)    # old rule: both count


def test_rung_exactly_at_threshold_counts(db, monkeypatch):
    monkeypatch.setattr(settings, "kss_tp_step_per_rung_pct", 0.5)
    monkeypatch.setattr(settings, "kss_partial_rung_min_pct", 50.0)
    row = _session_with_rungs(db, {1: 5.0})

    assert service._to_pyramid(row).tp_pct == pytest.approx(5.5)


# --- the knob --------------------------------------------------------------------------------------


def test_knob_persists_via_api(db, monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app

    monkeypatch.setattr(settings, "kss_partial_rung_min_pct", 50.0)
    runtime.set_kss_settings(db, {"kss_partial_rung_min_pct": 25.0})
    assert settings.kss_partial_rung_min_pct == 25.0
    assert runtime.kss_settings(db)["kss_partial_rung_min_pct"] == 25.0
    settings.kss_partial_rung_min_pct = 90.0           # simulate a restart: memory forgets
    runtime.sync_from_db(db)
    assert settings.kss_partial_rung_min_pct == 25.0

    with TestClient(app) as c:
        r = c.post("/api/kss-settings", json={"kss_partial_rung_min_pct": 35})
        assert r.status_code == 200 and r.json()["kss_partial_rung_min_pct"] == 35
        assert c.get("/api/kss-settings").json()["kss_partial_rung_min_pct"] == 35
        assert c.post("/api/kss-settings", json={"kss_partial_rung_min_pct": 0}).status_code == 200
        assert c.post("/api/kss-settings", json={"kss_partial_rung_min_pct": 101}).status_code == 422
        assert c.post("/api/kss-settings", json={"kss_partial_rung_min_pct": -1}).status_code == 422
        # the Strategy tab renders the row with the stored value (the form posts it back)
        c.post("/api/kss-settings", json={"kss_partial_rung_min_pct": 35})
        html = c.get("/partials/kss-settings").text
        assert 'name="kss_partial_rung_min_pct"' in html and 'value="35"' in html
