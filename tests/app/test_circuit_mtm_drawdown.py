"""The breaker's drawdown is measured on MARK-TO-MARKET equity (hourly snapshots), not on the
realized-P&L curve.

Measured bug (paper, 2026-10-15): with 0 losing closes the realized curve only rises, so its
"peak" was start capital + all realized profit (~$7,577) - a level true equity never reached
(real peak $7,327). The breaker froze at "DD 16.1%" while the true mark-to-market drawdown at the
trough was ~12.1%. Only HOW drawdown is measured changed; thresholds, freeze scope, rearm and
streak rules are untouched (see test_exit_never_blocked.py for "exits are never gated").
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app import circuit, equity_history, orders, runtime
from app.config import settings
from app.models import PENDING, Deposit, EquitySnapshot, Fill, PendingOrder

T0 = datetime(2026, 10, 5, 12)


def _snap(db, ts, equity):
    db.add(EquitySnapshot(ts=ts, equity=equity, cash=0.0, market_value=equity, cost_basis=equity,
                          unrealized=0.0, realized_cum=0.0, active_sessions=0, btc_price=None,
                          source="live"))
    db.commit()


def _world(monkeypatch, *, equity, realized_dd=12.27):
    """Pin the three inputs metrics() reads besides the snapshots."""
    monkeypatch.setattr(circuit.portfolio, "performance_view", lambda db: {
        "max_drawdown_pct": realized_dd, "current_drawdown_pct": realized_dd})
    monkeypatch.setattr(circuit.portfolio, "equity", lambda db: equity)
    monkeypatch.setattr(circuit.risk, "daily_loss", lambda db: 0.0)
    monkeypatch.setattr(circuit.risk, "deposited_today", lambda db: 0.0)
    monkeypatch.setattr(settings, "max_drawdown_pct", 15.0)
    monkeypatch.setattr(settings, "account_equity", 7000.0)


def test_mtm_drawdown_replaces_the_realized_curve(db, monkeypatch):
    _world(monkeypatch, equity=6647.0)           # realized curve alone would say 12.27%
    _snap(db, T0, 6900.0)
    _snap(db, T0 + timedelta(hours=1), 7327.0)   # true peak
    _snap(db, T0 + timedelta(hours=2), 6439.0)   # trough

    m = circuit.metrics(db)

    assert m["drawdown_source"] == "mtm_snapshots"
    assert m["drawdown_pct"] == pytest.approx((7327 - 6647) / 7327 * 100)   # ~9.28
    assert m["drawdown_peak_equity"] == pytest.approx(7327.0)
    assert circuit.evaluate(db)["frozen"] is False


def test_mtm_drawdown_over_the_limit_freezes_with_the_usual_reason(db, monkeypatch):
    _world(monkeypatch, equity=6200.0, realized_dd=1.0)
    _snap(db, T0, 7327.0)

    result = circuit.evaluate(db)

    assert result["frozen"] is True
    assert any("drawdown" in r and "15" in r for r in result["reasons"])
    assert runtime.is_frozen(db) is True


def test_a_frozen_breaker_still_lets_an_exit_through_on_mtm_drawdown(db, monkeypatch):
    """Freeze path is the same function; a SELL is never gated by it."""
    _world(monkeypatch, equity=6200.0, realized_dd=1.0)
    _snap(db, T0, 7327.0)
    assert circuit.evaluate(db)["frozen"] is True
    monkeypatch.setattr("app.market.get_current_prices", lambda syms: {"SOL": 100.0})
    sell = PendingOrder(symbol="SOL", side="SELL", order_type="MARKET", quantity=1.0, price=0.0,
                        source="kss", source_ref="pyramid:1:sl", status=PENDING)
    db.add(sell)
    db.commit()

    fill = orders.approve_order(db, sell.id, reviewer="auto-trader")

    assert fill.side == "SELL"


def test_no_snapshots_keeps_the_realized_curve_number(db, monkeypatch):
    _world(monkeypatch, equity=6647.0, realized_dd=12.27)

    m = circuit.metrics(db)

    assert m["drawdown_source"] == "realized_curve"
    assert m["drawdown_pct"] == 12.27
    assert m["drawdown_peak_equity"] is None


def test_a_deposit_after_the_peak_is_not_recovery(db, monkeypatch):
    # 7327 peak; a $1,000 deposit lands; the book then sits at 6647 of its own money.
    _world(monkeypatch, equity=7647.0)
    _snap(db, T0, 7327.0)
    db.add(Deposit(amount=1000.0, created_at=T0 + timedelta(hours=1, minutes=30)))
    db.commit()
    _snap(db, T0 + timedelta(hours=3), 7500.0)   # 6500 net of the deposit

    m = circuit.metrics(db)

    assert m["drawdown_pct"] == pytest.approx((7327 - 6647) / 7327 * 100)
    assert m["drawdown_peak_equity"] == pytest.approx(7327.0)


def test_a_deposit_before_the_peak_is_netted_out_of_the_peak_too(db, monkeypatch):
    # Deposit first, then the high of 8327 (= 7327 of own money + 1000 deposited).
    db.add(Deposit(amount=1000.0, created_at=T0 - timedelta(hours=2)))
    db.commit()
    _world(monkeypatch, equity=7647.0)
    _snap(db, T0, 8327.0)

    m = circuit.metrics(db)

    assert m["drawdown_peak_equity"] == pytest.approx(7327.0)
    assert m["drawdown_pct"] == pytest.approx((7327 - 6647) / 7327 * 100)


def test_current_above_every_snapshot_is_zero_drawdown_and_the_new_peak(db, monkeypatch):
    _world(monkeypatch, equity=7500.0)
    _snap(db, T0, 7327.0)

    m = circuit.metrics(db)

    assert m["drawdown_pct"] == 0.0
    assert m["drawdown_peak_equity"] == pytest.approx(7500.0)
    assert m["drawdown_source"] == "mtm_snapshots"


def test_helper_returns_none_without_snapshots_and_peak_ts_otherwise(db):
    assert equity_history.mtm_drawdown(db, 6000.0) is None

    _snap(db, T0, 7327.0)
    _snap(db, T0 + timedelta(hours=1), 7000.0)
    out = equity_history.mtm_drawdown(db, 6000.0, now=T0 + timedelta(hours=2, minutes=5))

    assert out["peak_equity"] == pytest.approx(7327.0)
    assert out["peak_ts"] == T0
    assert out["current"] == pytest.approx(6000.0)
    assert out["drawdown_pct"] == pytest.approx((7327 - 6000) / 7327 * 100)


def test_snapshots_from_before_the_current_books_first_fill_are_ignored(db, monkeypatch):
    """A reset deletes fills but not equity_snapshots: an old book's peak must not count."""
    _world(monkeypatch, equity=6647.0)
    _snap(db, T0 - timedelta(days=30), 50_000.0)  # old book, pre-reset
    _snap(db, T0 + timedelta(hours=1), 7327.0)
    db.add(Fill(symbol="AAA", side="BUY", quantity=1.0, price=1.0, fee=0.0,
                executed_at=T0 + timedelta(minutes=5)))
    db.commit()

    m = circuit.metrics(db)

    assert m["drawdown_peak_equity"] == pytest.approx(7327.0)
    assert m["drawdown_pct"] == pytest.approx((7327 - 6647) / 7327 * 100)
