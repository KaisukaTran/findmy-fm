"""Tests for app.circuit — consecutive-loss trip, daily-loss trip, reset, auto-rearm."""

from __future__ import annotations

from datetime import datetime

from app import circuit, models, runtime
from app.config import settings

# ---------------------------------------------------------------------------
# Helpers — inject Fill rows directly (mirrors test_market_risk.py style)
# ---------------------------------------------------------------------------


def _sell_fill(db, pnl: float) -> models.Fill:
    f = models.Fill(
        symbol="BTC",
        side="SELL",
        quantity=1.0,
        price=100.0,
        realized_pnl=pnl,
        executed_at=datetime.utcnow(),
    )
    db.add(f)
    db.commit()
    return f


def _buy_fill(db) -> models.Fill:
    f = models.Fill(
        symbol="BTC",
        side="BUY",
        quantity=1.0,
        price=100.0,
        realized_pnl=0.0,
        executed_at=datetime.utcnow(),
    )
    db.add(f)
    db.commit()
    return f


# ---------------------------------------------------------------------------
# metrics()
# ---------------------------------------------------------------------------


def test_metrics_empty_db(db):
    m = circuit.metrics(db)
    assert m["consecutive_losses"] == 0
    assert m["daily_loss_pct"] == 0.0
    assert "drawdown_pct" in m


# ---------------------------------------------------------------------------
# evaluate() — consecutive losses trip
# ---------------------------------------------------------------------------


def test_evaluate_trips_on_consecutive_losses(db, monkeypatch):
    monkeypatch.setattr(settings, "max_consecutive_losses", 3)

    # Insert 3 losing SELL fills → breaker should trip
    for _ in range(3):
        _sell_fill(db, -50.0)

    result = circuit.evaluate(db)

    assert result["frozen"] is True
    assert runtime.is_frozen(db) is True
    assert any("consecutive_losses" in r for r in result["reasons"])


def test_evaluate_no_trip_below_consecutive_threshold(db, monkeypatch):
    monkeypatch.setattr(settings, "max_consecutive_losses", 4)

    # Only 2 losing SELLs — threshold is 4
    for _ in range(2):
        _sell_fill(db, -50.0)

    result = circuit.evaluate(db)

    assert result["frozen"] is False
    assert runtime.is_frozen(db) is False


def test_evaluate_win_resets_consecutive_count(db, monkeypatch):
    monkeypatch.setattr(settings, "max_consecutive_losses", 3)
    monkeypatch.setattr(settings, "max_drawdown_pct", 100.0)
    monkeypatch.setattr(settings, "daily_loss_hard_pct", 100.0)

    # 2 losses, 1 win, 1 loss — streak broken, should NOT trip
    _sell_fill(db, -50.0)
    _sell_fill(db, -50.0)
    _sell_fill(db, +200.0)   # win resets streak
    _sell_fill(db, -50.0)

    result = circuit.evaluate(db)

    assert result["frozen"] is False


# ---------------------------------------------------------------------------
# evaluate() — daily loss trip
# ---------------------------------------------------------------------------


def test_evaluate_trips_on_daily_loss(db, monkeypatch):
    monkeypatch.setattr(settings, "daily_loss_hard_pct", 1.0)  # 1% of equity
    monkeypatch.setattr(settings, "account_equity", 10000.0)
    monkeypatch.setattr(settings, "max_consecutive_losses", 9999)  # disable other gate

    # equity ≈ 10000, 1% limit → 100 USD.  Insert a 200 USD loss today.
    _sell_fill(db, -200.0)

    result = circuit.evaluate(db)

    assert result["frozen"] is True
    assert runtime.is_frozen(db) is True
    assert any("daily_loss" in r for r in result["reasons"])


# ---------------------------------------------------------------------------
# reset() — manual unfreeze bypasses cooldown
# ---------------------------------------------------------------------------


def test_reset_unfreezes(db, monkeypatch):
    monkeypatch.setattr(settings, "max_consecutive_losses", 3)

    for _ in range(3):
        _sell_fill(db, -50.0)
    circuit.evaluate(db)
    assert runtime.is_frozen(db) is True

    state = circuit.reset(db)
    assert state["frozen"] is False
    assert runtime.is_frozen(db) is False


def test_reset_on_unfrozen_is_safe(db):
    """reset() on a non-frozen system is a no-op (no error raised)."""
    state = circuit.reset(db)
    assert state["frozen"] is False


# ---------------------------------------------------------------------------
# AUTO_REVIEWERS constant
# ---------------------------------------------------------------------------


def test_auto_reviewers_contains_expected(db):
    assert "auto-trader" in circuit.AUTO_REVIEWERS
    assert "auto-approver" in circuit.AUTO_REVIEWERS
    assert "scheduler" in circuit.AUTO_REVIEWERS


# ---------------------------------------------------------------------------
# Auto-rearm when cooldown is zero and no breaching condition
# ---------------------------------------------------------------------------


def test_auto_rearm_after_zero_cooldown(db, monkeypatch):
    monkeypatch.setattr(settings, "max_consecutive_losses", 3)
    monkeypatch.setattr(settings, "breaker_cooldown_min", 0)
    monkeypatch.setattr(settings, "max_drawdown_pct", 100.0)
    monkeypatch.setattr(settings, "daily_loss_hard_pct", 100.0)

    # Trip the breaker
    for _ in range(3):
        _sell_fill(db, -50.0)
    circuit.evaluate(db)
    assert runtime.is_frozen(db) is True

    # Clear the breaching condition: add wins so streak is broken
    for _ in range(5):
        _sell_fill(db, +200.0)

    # With cooldown=0 and no violation → should auto-rearm
    result = circuit.evaluate(db)
    assert result["frozen"] is False
    assert runtime.is_frozen(db) is False


# ---------------------------------------------------------------------------
# evaluate() is idempotent when already frozen
# ---------------------------------------------------------------------------


def test_evaluate_idempotent_when_already_frozen(db, monkeypatch):
    monkeypatch.setattr(settings, "max_consecutive_losses", 3)

    for _ in range(3):
        _sell_fill(db, -50.0)
    circuit.evaluate(db)
    first_frozen_at = runtime.get(db, runtime.KEY_FROZEN_AT)

    # Second call should not change the frozen_at timestamp
    circuit.evaluate(db)
    second_frozen_at = runtime.get(db, runtime.KEY_FROZEN_AT)

    assert first_frozen_at == second_frozen_at
