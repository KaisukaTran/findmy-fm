"""Tests for app.circuit — consecutive-loss trip, daily-loss trip, reset, auto-rearm."""

from __future__ import annotations

from datetime import datetime, timedelta

from app import circuit, models, risk, runtime
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


def _sell_fill_at(db, pnl: float, executed_at: datetime) -> models.Fill:
    f = models.Fill(
        symbol="BTC",
        side="SELL",
        quantity=1.0,
        price=100.0,
        realized_pnl=pnl,
        executed_at=executed_at,
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


# ---------------------------------------------------------------------------
# risk.weekly_loss() — rolling 7-day window
# ---------------------------------------------------------------------------


def test_weekly_loss_sums_only_last_7_days(db):
    now = datetime.utcnow()
    _sell_fill_at(db, -30.0, now)                        # today — included
    _sell_fill_at(db, -20.0, now - timedelta(days=3))     # 3 days ago — included
    _sell_fill_at(db, -999.0, now - timedelta(days=8))    # 8 days ago — excluded
    _sell_fill_at(db, +500.0, now)                        # win — not a loss, ignored

    assert risk.weekly_loss(db) == 50.0


# ---------------------------------------------------------------------------
# evaluate() — absolute-USD daily-loss trip
# ---------------------------------------------------------------------------


def test_evaluate_trips_on_daily_loss_hard_usd(db, monkeypatch):
    monkeypatch.setattr(settings, "daily_loss_hard_usd", 100.0)
    # Disable every other gate so only the new USD rule can trip.
    monkeypatch.setattr(settings, "daily_loss_hard_pct", 100.0)
    monkeypatch.setattr(settings, "max_drawdown_pct", 100.0)
    monkeypatch.setattr(settings, "max_consecutive_losses", 9999)

    _sell_fill(db, -150.0)  # today's realized loss ≥ $100 cap

    result = circuit.evaluate(db)

    assert result["frozen"] is True
    assert any("daily_loss" in r and "$" in r for r in result["reasons"])


def test_daily_loss_hard_usd_off_when_zero(db, monkeypatch):
    monkeypatch.setattr(settings, "daily_loss_hard_usd", 0.0)  # off
    monkeypatch.setattr(settings, "daily_loss_hard_pct", 100.0)
    monkeypatch.setattr(settings, "max_drawdown_pct", 100.0)
    monkeypatch.setattr(settings, "max_consecutive_losses", 9999)

    _sell_fill(db, -150.0)

    result = circuit.evaluate(db)

    assert result["frozen"] is False
    assert not any("$" in r for r in result["reasons"])


# ---------------------------------------------------------------------------
# evaluate() — rolling 7-day weekly-loss trip (multi-day bleed, no single-day breach)
# ---------------------------------------------------------------------------


def test_evaluate_trips_on_weekly_loss_hard_pct(db, monkeypatch):
    monkeypatch.setattr(settings, "account_equity", 10000.0)
    monkeypatch.setattr(settings, "weekly_loss_hard_pct", 5.0)  # 5% of equity = $500
    # Disable every other gate — the daily rule must NOT catch this (each day is small).
    monkeypatch.setattr(settings, "daily_loss_hard_pct", 100.0)
    monkeypatch.setattr(settings, "daily_loss_hard_usd", 0.0)
    monkeypatch.setattr(settings, "max_drawdown_pct", 100.0)
    monkeypatch.setattr(settings, "max_consecutive_losses", 9999)

    now = datetime.utcnow()
    # $150/day over 4 distinct days within the window = $600 > $500 weekly cap, but each
    # single day is well below any daily threshold.
    for days_ago in (0, 2, 4, 6):
        _sell_fill_at(db, -150.0, now - timedelta(days=days_ago))

    result = circuit.evaluate(db)

    assert result["frozen"] is True
    assert any("weekly_loss" in r and "%" in r for r in result["reasons"])


def test_evaluate_trips_on_weekly_loss_hard_usd(db, monkeypatch):
    monkeypatch.setattr(settings, "weekly_loss_hard_usd", 500.0)
    monkeypatch.setattr(settings, "daily_loss_hard_pct", 100.0)
    monkeypatch.setattr(settings, "daily_loss_hard_usd", 0.0)
    monkeypatch.setattr(settings, "max_drawdown_pct", 100.0)
    monkeypatch.setattr(settings, "max_consecutive_losses", 9999)

    now = datetime.utcnow()
    for days_ago in (0, 2, 4, 6):
        _sell_fill_at(db, -150.0, now - timedelta(days=days_ago))  # $600 total

    result = circuit.evaluate(db)

    assert result["frozen"] is True
    assert any("weekly_loss" in r and "$" in r for r in result["reasons"])


# ---------------------------------------------------------------------------
# A weekly-loss freeze must NOT block auto-rearm after cooldown (mirrors the
# existing consecutive-loss rearm behaviour) — a 7-day window can't clear
# within a short cooldown, so it must not deadlock the breaker frozen forever.
# ---------------------------------------------------------------------------


def test_weekly_loss_freeze_does_not_block_rearm_after_cooldown(db, monkeypatch):
    monkeypatch.setattr(settings, "weekly_loss_hard_usd", 100.0)
    monkeypatch.setattr(settings, "breaker_cooldown_min", 0)
    monkeypatch.setattr(settings, "daily_loss_hard_pct", 100.0)
    monkeypatch.setattr(settings, "daily_loss_hard_usd", 0.0)
    monkeypatch.setattr(settings, "max_drawdown_pct", 100.0)
    monkeypatch.setattr(settings, "max_consecutive_losses", 9999)

    now = datetime.utcnow()
    _sell_fill_at(db, -150.0, now - timedelta(days=3))  # trips weekly-loss

    circuit.evaluate(db)
    assert runtime.is_frozen(db) is True

    # The weekly loss is still breached (it's a 7-day rolling sum — a single old fill
    # doesn't clear), yet with cooldown=0 the breaker must still attempt + succeed rearm
    # because weekly_loss reasons are excluded from `blocking`.
    result = circuit.evaluate(db)
    assert result["frozen"] is False
    assert runtime.is_frozen(db) is False


# ---------------------------------------------------------------------------
# ALL-OFF (every new knob 0) — no-regression proof: metrics gains the new
# fields but evaluate() trips no NEW reasons vs. before this change.
# ---------------------------------------------------------------------------


def test_all_new_knobs_off_no_new_trips(db, monkeypatch):
    monkeypatch.setattr(settings, "daily_loss_hard_usd", 0.0)
    monkeypatch.setattr(settings, "weekly_loss_hard_pct", 0.0)
    monkeypatch.setattr(settings, "weekly_loss_hard_usd", 0.0)
    monkeypatch.setattr(settings, "max_drawdown_usd", 0.0)
    # Keep the legacy gates permissive so nothing else trips either.
    monkeypatch.setattr(settings, "daily_loss_hard_pct", 100.0)
    monkeypatch.setattr(settings, "max_drawdown_pct", 100.0)
    monkeypatch.setattr(settings, "max_consecutive_losses", 9999)

    now = datetime.utcnow()
    for days_ago in (0, 2, 4, 6):
        _sell_fill_at(db, -150.0, now - timedelta(days=days_ago))  # would trip weekly if on

    m = circuit.metrics(db)
    assert "daily_loss_usd" in m
    assert "weekly_loss_usd" in m
    assert "weekly_loss_pct" in m

    result = circuit.evaluate(db)
    assert result["frozen"] is False
    assert result["reasons"] == []
