"""A frozen breaker must never trap a position.

Two faults that compose into the worst failure this system can have:

* the breaker's drawdown was the ALL-TIME maximum of the equity curve, a number that can only
  ever grow — so one 15% dip freezes it forever, and even a manual reset re-freezes on the next
  cycle because the reason never clears;
* `approve_order` refused every auto-reviewer order while frozen, without looking at the side.

Together: one bad day freezes the breaker permanently, and the freeze blocks every automated
SELL — take-profits, stop-losses, trailing exits — so the position cannot be closed by the app
at all. The breaker exists to stop NEW risk; turning it into something that holds a losing
position open is the opposite of its job.
"""

from __future__ import annotations

import pytest

from app import circuit, orders, runtime
from app.config import settings
from app.models import PENDING, PendingOrder


def _order(db, side: str, **kw) -> PendingOrder:
    defaults = {
        "symbol": "SOL", "side": side, "order_type": "MARKET", "quantity": 1.0,
        "price": 0.0, "source": "kss", "source_ref": "pyramid:1:sl", "status": PENDING,
    }
    defaults.update(kw)
    o = PendingOrder(**defaults)
    db.add(o)
    db.commit()
    db.refresh(o)
    return o


def test_a_frozen_breaker_does_not_block_an_exit(db, monkeypatch):
    monkeypatch.setattr("app.market.get_current_prices", lambda syms: {"SOL": 100.0})
    runtime.freeze(db, "test freeze")
    sell = _order(db, "SELL")

    fill = orders.approve_order(db, sell.id, reviewer="auto-trader")

    assert fill.side == "SELL", "an exit must go through while frozen"


def test_a_frozen_breaker_still_blocks_new_exposure(db, monkeypatch):
    monkeypatch.setattr("app.market.get_current_prices", lambda syms: {"SOL": 100.0})
    runtime.freeze(db, "test freeze")
    buy = _order(db, "BUY", source_ref="pyramid:1:wave:0")

    with pytest.raises(ValueError, match="frozen"):
        orders.approve_order(db, buy.id, reviewer="auto-trader")


def test_a_human_is_never_blocked(db, monkeypatch):
    monkeypatch.setattr("app.market.get_current_prices", lambda syms: {"SOL": 100.0})
    runtime.freeze(db, "test freeze")
    buy = _order(db, "BUY", source_ref="pyramid:1:wave:0")

    fill = orders.approve_order(db, buy.id, reviewer="dashboard")

    assert fill.side == "BUY"


# --- the breaker must measure CURRENT drawdown, not the all-time worst ------


def _metrics(monkeypatch, *, current_dd, all_time_dd):
    monkeypatch.setattr(circuit.portfolio, "performance_view", lambda db: {
        "max_drawdown_pct": all_time_dd, "current_drawdown_pct": current_dd,
    })
    monkeypatch.setattr(circuit.portfolio, "equity", lambda db: 2000.0)
    monkeypatch.setattr(circuit.risk, "daily_loss", lambda db: 0.0)


def test_the_breaker_reads_the_drawdown_it_is_in_now(db, monkeypatch):
    """Recovered from a past dip: the historical worst is 40%, but we are back at the peak."""
    _metrics(monkeypatch, current_dd=0.0, all_time_dd=40.0)
    monkeypatch.setattr(settings, "max_drawdown_pct", 15.0)

    assert circuit.metrics(db)["drawdown_pct"] == 0.0
    assert circuit.evaluate(db)["frozen"] is False


def test_a_real_current_drawdown_still_trips_it(db, monkeypatch):
    _metrics(monkeypatch, current_dd=20.0, all_time_dd=40.0)
    monkeypatch.setattr(settings, "max_drawdown_pct", 15.0)

    result = circuit.evaluate(db)

    assert result["frozen"] is True
    assert any("drawdown" in r for r in result["reasons"])


def test_recovering_lets_a_frozen_breaker_rearm(db, monkeypatch):
    """The freeze must be escapable: once the equity recovers, the reason is gone."""
    monkeypatch.setattr(settings, "max_drawdown_pct", 15.0)
    monkeypatch.setattr(settings, "breaker_cooldown_min", 0)
    _metrics(monkeypatch, current_dd=20.0, all_time_dd=20.0)
    circuit.evaluate(db)
    assert runtime.is_frozen(db) is True

    _metrics(monkeypatch, current_dd=2.0, all_time_dd=20.0)  # price came back
    circuit.evaluate(db)

    assert runtime.is_frozen(db) is False, "a recovered account must be able to trade again"


def test_performance_view_reports_both_drawdowns(db):
    from app import portfolio

    view = portfolio.performance_view(db)

    assert "max_drawdown_pct" in view, "the historical worst is still useful for reporting"
    assert "current_drawdown_pct" in view
    assert view["current_drawdown_pct"] <= max(view["max_drawdown_pct"], 0.0) + 1e-9
