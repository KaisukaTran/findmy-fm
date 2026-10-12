"""Paper manual approval must respect marketability.

Bug (paper, 2026-09-21..10-12): the dashboard "Duyệt" button executed a resting take-profit
SELL whose limit sat ABOVE the market at ``max(limit, market)`` — a real venue leaves that
order resting. Three approvals (2Z, BANK, RVN) filled 14-33% over market and minted ~$275 of
phantom profit. The same hole existed for BUY rungs under the market and for "Duyệt tất cả".

A non-marketable LIMIT approved without a touch-model ``fill_price`` is now refused with
`orders.NotMarketable` (a ValueError, so approve_all skips it) and left exactly as it was.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import execution, models, orders
from app.main import app as fastapi_app
from app.models import EXECUTED, PENDING, AuditLog, Fill, PendingOrder, Position


@pytest.fixture(autouse=True)
def _paper(monkeypatch):
    monkeypatch.setattr(execution, "live_enabled", lambda: False)


def _px(monkeypatch, prices: dict[str, float]):
    monkeypatch.setattr(orders, "get_current_prices", lambda syms: {s: prices.get(s, 0.0) for s in syms})
    monkeypatch.setattr("app.portfolio.get_current_prices", lambda syms: {s: prices.get(s, 0.0) for s in syms})


def _held(db, symbol="SOL", qty=10.0, avg=10.0):
    db.add(Position(symbol=symbol, quantity=qty, avg_entry_price=avg, total_cost=qty * avg))
    db.commit()


def _order(db, side, price, symbol="SOL", qty=2.0, ref=None, source="manual"):
    o, _ = orders.queue_order(db, symbol=symbol, side=side, quantity=qty, price=price,
                              source=source, source_ref=ref)
    return o


def _audits(db, action):
    return db.query(AuditLog).filter(AuditLog.action == action).all()


# --- refused: not marketable -------------------------------------------------------------


def test_sell_limit_above_market_is_refused_and_untouched(db, monkeypatch):
    _px(monkeypatch, {"SOL": 10.0})
    _held(db)
    o = _order(db, "SELL", 12.0)

    with pytest.raises(orders.NotMarketable) as ei:
        orders.approve_order(db, o.id, reviewer="dashboard")

    assert "chưa chạm giá" in str(ei.value)
    db.refresh(o)
    assert o.status == PENDING
    assert o.reviewer is None and o.decided_at is None
    assert db.query(Fill).count() == 0
    pos = db.query(Position).filter_by(symbol="SOL").one()
    assert pos.quantity == 10.0 and pos.realized_pnl == 0.0
    (row,) = _audits(db, "approve_not_marketable")
    assert row.entity == f"order:{o.id}"
    assert '"market": 10.0' in row.detail and '"limit": 12.0' in row.detail


def test_buy_limit_below_market_is_refused_and_untouched(db, monkeypatch):
    _px(monkeypatch, {"SOL": 10.0})
    o = _order(db, "BUY", 9.0)

    with pytest.raises(orders.NotMarketable):
        orders.approve_order(db, o.id, reviewer="dashboard")

    db.refresh(o)
    assert o.status == PENDING and o.reviewer is None and o.decided_at is None
    assert db.query(Fill).count() == 0
    assert db.query(Position).count() == 0
    assert len(_audits(db, "approve_not_marketable")) == 1


def test_refusal_happens_before_the_cash_cap_mutates_the_order(db, monkeypatch):
    """_apply_cash_cap may shrink a BUY's quantity; a refused order must keep its original."""
    from app.config import settings

    monkeypatch.setattr(settings, "account_equity", 5.0)
    monkeypatch.setattr(settings, "cash_floor_usd", 0.0)
    monkeypatch.setattr(settings, "cash_floor_pct", 0.0)
    _px(monkeypatch, {"SOL": 10.0})
    o = _order(db, "BUY", 9.0, qty=2.0)

    with pytest.raises(orders.NotMarketable):
        orders.approve_order(db, o.id, reviewer="dashboard")

    db.refresh(o)
    assert o.quantity == 2.0 and o.status == PENDING


# --- still executed: marketable / touch / market -----------------------------------------


def test_marketable_sell_still_fills_at_market(db, monkeypatch):
    _px(monkeypatch, {"SOL": 12.5})
    _held(db)
    o = _order(db, "SELL", 12.0)

    fill = orders.approve_order(db, o.id, reviewer="dashboard")

    assert fill.price == pytest.approx(12.5 * (1 - orders.settings.slippage_pct / 100))
    db.refresh(o)
    assert o.status == EXECUTED


def test_marketable_buy_still_fills_at_market(db, monkeypatch):
    _px(monkeypatch, {"SOL": 8.0})
    o = _order(db, "BUY", 9.0)

    fill = orders.approve_order(db, o.id, reviewer="dashboard")

    assert fill.price == pytest.approx(8.0 * (1 + orders.settings.slippage_pct / 100))


def test_exactly_at_the_limit_is_marketable(db, monkeypatch):
    _px(monkeypatch, {"SOL": 12.0})
    _held(db)
    o = _order(db, "SELL", 12.0)
    assert orders.approve_order(db, o.id, reviewer="dashboard").id is not None


def test_touch_fill_price_wins_over_the_ticker(db, monkeypatch):
    _px(monkeypatch, {"SOL": 10.0})
    _held(db)
    o = _order(db, "SELL", 12.0)

    fill = orders.approve_order(db, o.id, reviewer="auto-trader", fill_price=12.0)

    assert fill.price == 12.0 and fill.slippage == 0.0


def test_touch_fill_price_not_gated_even_for_a_dashboard_reviewer(db, monkeypatch):
    _px(monkeypatch, {"SOL": 10.0})
    _held(db)
    o = _order(db, "SELL", 12.0)
    assert orders.approve_order(db, o.id, reviewer="dashboard", fill_price=12.0).price == 12.0


def test_market_order_is_unchanged(db, monkeypatch):
    _px(monkeypatch, {"SOL": 10.0})
    _held(db)
    o, _ = orders.queue_order(db, symbol="SOL", side="SELL", quantity=2.0, price=0.0,
                              order_type="MARKET", source="manual")
    fill = orders.approve_order(db, o.id, reviewer="dashboard")
    assert fill.price < 10.0 + 1e-9


def test_no_market_price_keeps_the_offline_fallback(db, monkeypatch):
    """No ticker at all (offline tests, outage): documented legacy fallback = fill at the limit."""
    _px(monkeypatch, {})
    _held(db)
    o = _order(db, "SELL", 12.0)
    fill = orders.approve_order(db, o.id, reviewer="dashboard")
    assert fill.price == pytest.approx(12.0 * (1 - orders.settings.slippage_pct / 100))


def test_automated_guard_reviewer_is_not_gated(db, monkeypatch):
    """Exits are never slowed: the 90s guard's force-fill keeps its immediate-fill behaviour."""
    _px(monkeypatch, {"SOL": 10.0})
    _held(db)
    o = _order(db, "SELL", 12.0)
    assert orders.approve_order(db, o.id, reviewer="guard").id is not None


# --- approve_all ---------------------------------------------------------------------------


def test_approve_all_skips_non_marketable_and_executes_marketable(db, monkeypatch):
    _px(monkeypatch, {"SOL": 10.0, "ETH": 100.0})
    _held(db)
    resting_tp = _order(db, "SELL", 12.0)
    resting_rung = _order(db, "BUY", 9.0)
    ok_buy = _order(db, "BUY", 101.0, symbol="ETH", qty=0.1)

    done = orders.approve_all(db)

    assert done == [ok_buy.id]
    db.refresh(resting_tp)
    db.refresh(resting_rung)
    assert resting_tp.status == PENDING and resting_rung.status == PENDING
    assert db.query(Fill).count() == 1


# --- audit of manual approvals -------------------------------------------------------------


def test_successful_dashboard_approval_is_audited(db, monkeypatch):
    _px(monkeypatch, {"SOL": 8.0})
    o = _order(db, "BUY", 9.0)

    fill = orders.approve_order(db, o.id, reviewer="dashboard")

    (row,) = _audits(db, "manual_approve")
    assert row.entity == f"order:{o.id}"
    assert f'"fill_price": {fill.price}' in row.detail
    assert '"market": 8.0' in row.detail


def test_automated_approval_is_not_logged_as_manual(db, monkeypatch):
    _px(monkeypatch, {"SOL": 8.0})
    o = _order(db, "BUY", 9.0)
    orders.approve_order(db, o.id, reviewer="auto-trader")
    assert _audits(db, "manual_approve") == []


# --- route ---------------------------------------------------------------------------------


@pytest.fixture
def client():
    with TestClient(fastapi_app) as c:
        yield c


def test_route_returns_409_with_the_message(client, db, monkeypatch):
    _px(monkeypatch, {"SOL": 10.0})
    _held(db)
    o = _order(db, "SELL", 12.0)

    r = client.post(f"/api/pending/approve/{o.id}")

    assert r.status_code == 409
    assert "chưa chạm giá" in r.json()["detail"]
    db.expire_all()
    assert db.get(PendingOrder, o.id).status == PENDING


def test_route_still_200_for_a_marketable_order(client, db, monkeypatch):
    _px(monkeypatch, {"SOL": 8.0})
    o = _order(db, "BUY", 9.0)
    r = client.post(f"/api/pending/approve/{o.id}")
    assert r.status_code == 200 and r.json()["message"] == "approved"


# --- end to end: a KSS resting take-profit -------------------------------------------------


def test_kss_resting_tp_above_market_stays_resting(db, monkeypatch):
    _px(monkeypatch, {"SOL": 10.0})
    row = models.KssSession(
        symbol="SOL", entry_price=10.0, distance_pct=2.0, max_waves=5, isolated_fund=1000.0,
        tp_pct=3.0, timeout_x_min=60, gap_y_min=5, status=models.SESSION_ACTIVE,
        current_wave=1, avg_price=10.0, total_filled_qty=3.0, total_cost=30.0,
    )
    db.add(row)
    _held(db, qty=3.0)
    db.refresh(row)
    tp = _order(db, "SELL", 10.5, qty=3.0, ref=f"pyramid:{row.id}:tp", source="kss")

    with pytest.raises(orders.NotMarketable):
        orders.approve_order(db, tp.id, reviewer="dashboard")

    db.expire_all()
    assert db.get(models.KssSession, row.id).status == models.SESSION_ACTIVE
    assert db.get(PendingOrder, tp.id).status == PENDING
    assert db.query(Fill).count() == 0
    assert db.query(Position).filter_by(symbol="SOL").one().quantity == 3.0
