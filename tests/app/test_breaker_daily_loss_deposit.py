"""A deposit landing mid-day must not dilute the breaker's daily-loss ratio.

Integration cross-check 2026-09-21: `circuit.metrics` divided today's realized loss by equity,
and equity now includes deposits — $360 lost on a $7,000 book is 5.1%, but a $1,000 deposit
recorded afterwards turned it into 4.5%, which could clear a blocking reason and rearm."""

from __future__ import annotations

from datetime import timedelta

import pytest

from app import circuit, portfolio, risk
from app.clock import utcnow
from app.models import Deposit


@pytest.fixture
def book(monkeypatch):
    monkeypatch.setattr(portfolio, "performance_view",
                        lambda db, *a, **k: {"current_drawdown_pct": 0.0, "max_drawdown_pct": 0.0})
    monkeypatch.setattr(risk, "daily_loss", lambda db: 360.0)
    monkeypatch.setattr(circuit, "_consecutive_losses", lambda db: 0)
    monkeypatch.setattr(circuit, "_consecutive_loss_events", lambda db: 0)


def test_todays_deposit_does_not_dilute_daily_loss(db, book, monkeypatch):
    monkeypatch.setattr(portfolio, "equity", lambda db: 8_000.0)  # 7,000 + today's 1,000
    db.add(Deposit(amount=1_000.0, note="monthly"))
    db.commit()
    assert circuit.metrics(db)["daily_loss_pct"] == pytest.approx(360 / 7_000 * 100)


def test_an_older_deposit_is_ordinary_capital(db, book, monkeypatch):
    monkeypatch.setattr(portfolio, "equity", lambda db: 8_000.0)
    db.add(Deposit(amount=1_000.0, note="last month", created_at=utcnow() - timedelta(days=2)))
    db.commit()
    assert circuit.metrics(db)["daily_loss_pct"] == pytest.approx(360 / 8_000 * 100)


def test_no_deposit_is_unchanged(db, book, monkeypatch):
    monkeypatch.setattr(portfolio, "equity", lambda db: 7_000.0)
    assert circuit.metrics(db)["daily_loss_pct"] == pytest.approx(360 / 7_000 * 100)
