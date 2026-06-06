"""Circuit-breaker: a consecutive-loss freeze must auto-rearm after cooldown (no deadlock)."""

from __future__ import annotations

from datetime import datetime, timedelta

from app import circuit, runtime
from app.config import settings


def _freeze_ago(db, minutes):
    runtime.freeze(db, "test")
    runtime.set(db, runtime.KEY_FROZEN_AT, (datetime.utcnow() - timedelta(minutes=minutes)).isoformat())


def test_streak_freeze_autorearms_after_cooldown(db, monkeypatch):
    monkeypatch.setattr(settings, "max_consecutive_losses", 4)
    monkeypatch.setattr(settings, "breaker_cooldown_min", 60)
    # streak still 4 (can't clear while frozen) but no current drawdown/daily-loss
    monkeypatch.setattr(circuit, "metrics",
                        lambda db: {"drawdown_pct": 0.2, "daily_loss_pct": 0.0, "consecutive_losses": 4})
    _freeze_ago(db, 90)
    assert circuit.evaluate(db)["frozen"] is False  # cooldown released the stale streak


def test_streak_freeze_holds_before_cooldown(db, monkeypatch):
    monkeypatch.setattr(settings, "max_consecutive_losses", 4)
    monkeypatch.setattr(settings, "breaker_cooldown_min", 60)
    monkeypatch.setattr(circuit, "metrics",
                        lambda db: {"drawdown_pct": 0.2, "daily_loss_pct": 0.0, "consecutive_losses": 4})
    _freeze_ago(db, 10)  # cooldown not elapsed
    assert circuit.evaluate(db)["frozen"] is True


def test_drawdown_freeze_stays_after_cooldown(db, monkeypatch):
    monkeypatch.setattr(settings, "max_drawdown_pct", 15.0)
    monkeypatch.setattr(settings, "breaker_cooldown_min", 60)
    # current-state drawdown still breaching → must NOT auto-rearm
    monkeypatch.setattr(circuit, "metrics",
                        lambda db: {"drawdown_pct": 20.0, "daily_loss_pct": 0.0, "consecutive_losses": 0})
    _freeze_ago(db, 90)
    assert circuit.evaluate(db)["frozen"] is True
