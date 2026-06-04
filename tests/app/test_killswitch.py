"""Tests for the kill-switch guard in app.orders when circuit-breaker is frozen."""

from __future__ import annotations

import pytest

from app import orders, runtime
from app.circuit import AUTO_REVIEWERS
from app.config import settings

_EX_INFO = {"minQty": 0.00001, "stepSize": 0.00001, "maxQty": 10000.0}


# ---------------------------------------------------------------------------
# Helper: queue a KSS order ready for approval
# ---------------------------------------------------------------------------


def _queue_kss_order(db):
    order, _ = orders.queue_order(
        db, symbol="BTC", side="BUY", quantity=0.001, price=100.0,
        source="kss", source_ref="pyramid:1:wave:0",
    )
    return order


def _mock_price(monkeypatch, price: float = 100.0):
    monkeypatch.setattr("app.orders.get_current_prices", lambda syms: dict.fromkeys(syms, price))
    monkeypatch.setattr("app.kss.pyramid.get_current_prices", lambda syms: dict.fromkeys(syms, price))
    monkeypatch.setattr("app.kss.pyramid.get_exchange_info", lambda s: _EX_INFO)
    monkeypatch.setattr("app.market.get_current_prices", lambda syms: dict.fromkeys(syms, price))


# ---------------------------------------------------------------------------
# approve_order raises ValueError when frozen + auto reviewer
# ---------------------------------------------------------------------------


def test_approve_order_blocked_when_frozen_auto_reviewer(db, monkeypatch):
    _mock_price(monkeypatch)
    runtime.freeze(db, "test freeze")
    order = _queue_kss_order(db)

    for reviewer in AUTO_REVIEWERS:
        with pytest.raises(ValueError, match="frozen"):
            orders.approve_order(db, order.id, reviewer=reviewer)


def test_approve_order_auto_trader_blocked_when_frozen(db, monkeypatch):
    _mock_price(monkeypatch)
    runtime.freeze(db, "breaker fired")
    order = _queue_kss_order(db)

    with pytest.raises(ValueError, match="frozen"):
        orders.approve_order(db, order.id, reviewer="auto-trader")


def test_approve_order_auto_approver_blocked_when_frozen(db, monkeypatch):
    _mock_price(monkeypatch)
    runtime.freeze(db, "breaker fired")
    order = _queue_kss_order(db)

    with pytest.raises(ValueError, match="frozen"):
        orders.approve_order(db, order.id, reviewer="auto-approver")


# ---------------------------------------------------------------------------
# 'dashboard' reviewer is NOT blocked by the kill-switch
# ---------------------------------------------------------------------------


def test_approve_order_dashboard_not_blocked_when_frozen(db, monkeypatch):
    _mock_price(monkeypatch)
    runtime.freeze(db, "breaker fired")
    order = _queue_kss_order(db)

    # Should succeed — dashboard is a human reviewer
    fill = orders.approve_order(db, order.id, reviewer="dashboard")
    assert fill is not None
    assert fill.symbol == "BTC"


def test_approve_order_none_reviewer_not_blocked_when_frozen(db, monkeypatch):
    """reviewer=None (manual) is not in AUTO_REVIEWERS — should go through."""
    _mock_price(monkeypatch)
    runtime.freeze(db, "breaker fired")
    order = _queue_kss_order(db)

    fill = orders.approve_order(db, order.id, reviewer=None)
    assert fill is not None


# ---------------------------------------------------------------------------
# auto_fill_due_orders returns [] when frozen
# ---------------------------------------------------------------------------


def test_auto_fill_due_orders_returns_empty_when_frozen(db, monkeypatch):
    _mock_price(monkeypatch)
    _queue_kss_order(db)
    runtime.freeze(db, "breaker fired")

    result = orders.auto_fill_due_orders(db)
    assert result == []


def test_auto_fill_due_orders_works_when_not_frozen(db, monkeypatch):
    # price=100 <= order price=100 → BUY is due
    _mock_price(monkeypatch, price=100.0)
    _queue_kss_order(db)

    result = orders.auto_fill_due_orders(db)
    assert len(result) >= 1


# ---------------------------------------------------------------------------
# auto_approve_by_policy returns [] when frozen
# ---------------------------------------------------------------------------


def test_auto_approve_by_policy_returns_empty_when_frozen(db, monkeypatch):
    monkeypatch.setattr(settings, "autoapprove_enabled", True)
    monkeypatch.setattr(settings, "autoapprove_sources", ["kss"])
    monkeypatch.setattr(settings, "autoapprove_max_notional", 1_000_000.0)
    _mock_price(monkeypatch)
    _queue_kss_order(db)
    runtime.freeze(db, "breaker fired")

    result = orders.auto_approve_by_policy(db)
    assert result == []


def test_auto_approve_by_policy_works_when_not_frozen(db, monkeypatch):
    monkeypatch.setattr(settings, "autoapprove_enabled", True)
    monkeypatch.setattr(settings, "autoapprove_sources", ["kss"])
    monkeypatch.setattr(settings, "autoapprove_max_notional", 1_000_000.0)
    monkeypatch.setattr(settings, "autoapprove_require_no_risk", False)
    _mock_price(monkeypatch, price=100.0)
    _queue_kss_order(db)

    result = orders.auto_approve_by_policy(db)
    assert len(result) >= 1


# ---------------------------------------------------------------------------
# Unfreeze allows auto-fills to resume
# ---------------------------------------------------------------------------


def test_auto_fill_resumes_after_unfreeze(db, monkeypatch):
    _mock_price(monkeypatch, price=100.0)
    _queue_kss_order(db)
    runtime.freeze(db, "test")

    blocked = orders.auto_fill_due_orders(db)
    assert blocked == []

    runtime.unfreeze(db)
    resumed = orders.auto_fill_due_orders(db)
    assert len(resumed) >= 1
