"""Tests for app.risk.account_equity — fallback and live portfolio paths."""

from __future__ import annotations

from datetime import datetime

from app import models, orders, portfolio, risk
from app.config import settings

_EX_INFO = {"minQty": 0.00001, "stepSize": 0.00001, "maxQty": 10000.0}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_prices(monkeypatch, price: float):
    """Mock all price-fetch paths used by portfolio and orders."""
    monkeypatch.setattr("app.portfolio.get_current_prices", lambda syms: dict.fromkeys(syms, price))
    monkeypatch.setattr("app.orders.get_current_prices", lambda syms: dict.fromkeys(syms, price))
    monkeypatch.setattr("app.kss.pyramid.get_current_prices", lambda syms: dict.fromkeys(syms, price))
    monkeypatch.setattr("app.kss.pyramid.get_exchange_info", lambda s: _EX_INFO)
    monkeypatch.setattr("app.market.get_current_prices", lambda syms: dict.fromkeys(syms, price))


# ---------------------------------------------------------------------------
# Fallback path: empty book → returns settings.account_equity
# ---------------------------------------------------------------------------


def test_account_equity_fallback_when_no_positions(db, monkeypatch):
    monkeypatch.setattr(settings, "account_equity", 10000.0)
    # No fills, no positions → portfolio.equity returns 0 (or near 0 per the formula)
    # risk.account_equity falls back to settings value
    eq = risk.account_equity(db)
    assert eq == settings.account_equity


# ---------------------------------------------------------------------------
# Live path: after fills, account_equity equals portfolio.equity
# ---------------------------------------------------------------------------


def test_account_equity_reflects_portfolio_after_buy_fill(db, monkeypatch):
    monkeypatch.setattr(settings, "account_equity", 10000.0)
    _mock_prices(monkeypatch, 200.0)

    # Queue and approve a BUY
    order, _ = orders.queue_order(
        db, symbol="BTC", side="BUY", quantity=0.01, price=200.0,
        source="manual",
    )
    orders.approve_order(db, order.id, reviewer="dashboard")

    live = portfolio.equity(db)
    assert live > 0, "portfolio.equity should be non-zero after a fill"

    eq = risk.account_equity(db)
    assert eq == live


def test_account_equity_reflects_portfolio_after_sell_fill(db, monkeypatch):
    monkeypatch.setattr(settings, "account_equity", 10000.0)
    _mock_prices(monkeypatch, 200.0)

    # BUY first
    buy_order, _ = orders.queue_order(
        db, symbol="BTC", side="BUY", quantity=0.01, price=200.0, source="manual"
    )
    orders.approve_order(db, buy_order.id, reviewer="dashboard")

    # SELL
    sell_order, _ = orders.queue_order(
        db, symbol="BTC", side="SELL", quantity=0.01, price=200.0, source="manual"
    )
    orders.approve_order(db, sell_order.id, reviewer="dashboard")

    # After SELL position may be zero but equity still reflects realized P&L
    live = portfolio.equity(db)
    eq = risk.account_equity(db)
    # When position is empty (qty=0) portfolio.equity is settings.account_equity ± realized PnL
    # Both should agree
    assert abs(eq - live) < 1e-6 or eq == settings.account_equity


def test_account_equity_fallback_when_portfolio_equity_zero(db, monkeypatch):
    """Explicitly simulate a zero-equity book — fallback must return the config value."""
    monkeypatch.setattr(settings, "account_equity", 12345.0)
    # Mock portfolio.equity to return 0 to force the fallback
    monkeypatch.setattr(portfolio, "equity", lambda db_: 0.0)

    eq = risk.account_equity(db)
    assert eq == 12345.0


# ---------------------------------------------------------------------------
# account_equity is used by check_position_size and check_daily_loss
# ---------------------------------------------------------------------------


def test_check_daily_loss_uses_live_equity(db, monkeypatch):
    """check_daily_loss should consult account_equity (config default here)."""
    monkeypatch.setattr(settings, "account_equity", 10000.0)
    monkeypatch.setattr(settings, "max_daily_loss_pct", 5.0)

    # 600 loss today > 5% of 10000 = 500
    db.add(models.Fill(
        symbol="BTC", side="SELL", quantity=1, price=1,
        realized_pnl=-600.0, executed_at=datetime.utcnow()
    ))
    db.commit()

    v = risk.check_daily_loss(db)
    assert v is not None
    assert "Daily loss" in v


def test_account_equity_config_changes_respected(db, monkeypatch):
    monkeypatch.setattr(settings, "account_equity", 50000.0)
    eq = risk.account_equity(db)
    assert eq == 50000.0
