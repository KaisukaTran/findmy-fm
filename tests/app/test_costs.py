"""
Tests for app.costs — withdrawal recording + cost_summary aggregation.

No network. Rates + TZ are pinned via monkeypatch so the math/bucketing is deterministic.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from app import costs
from app.config import settings
from app.models import Fill
from app.orchestrator.models import OpusCostLedger


@pytest.fixture(autouse=True)
def _cost_knobs(monkeypatch):
    monkeypatch.setattr(settings, "withdrawal_fee_pct", 0.1)
    monkeypatch.setattr(settings, "withdrawal_fee_tolerance_pct", 0.05)
    monkeypatch.setattr(settings, "vat_pct", 10.0)
    monkeypatch.setattr(settings, "ai_monthly_claude_usd", 25.0)
    monkeypatch.setattr(settings, "ai_monthly_grok_usd", 20.0)
    monkeypatch.setattr(settings, "tz_offset_hours", 0)  # local == UTC for deterministic buckets


# --- record_withdrawal --------------------------------------------------------


def test_record_withdrawal_computes_fee_and_vat(db):
    w = costs.record_withdrawal(db, 1000.0, note="test")
    assert w.fee == pytest.approx(1.5)  # 1000 × (0.1 + 0.05)/100
    assert w.vat == pytest.approx(100.0)  # 1000 × 10/100
    assert w.amount == 1000.0
    assert w.to_dict()["total_cost"] == pytest.approx(101.5)


def test_record_withdrawal_rejects_nonpositive(db):
    with pytest.raises(ValueError):
        costs.record_withdrawal(db, 0)
    with pytest.raises(ValueError):
        costs.record_withdrawal(db, -5)


def test_withdrawal_fee_frozen_after_rate_change(db, monkeypatch):
    w = costs.record_withdrawal(db, 1000.0)
    monkeypatch.setattr(settings, "vat_pct", 99.0)  # change the rate AFTER booking
    db.refresh(w)
    assert w.vat == pytest.approx(100.0)  # snapshot, not recomputed


# --- cost_summary -------------------------------------------------------------


def test_trade_fees_and_withdrawal_in_current_bucket(db):
    db.add(Fill(symbol="BTC", side="SELL", quantity=1, price=100, fee=2.5,
                executed_at=datetime.utcnow()))
    db.commit()
    costs.record_withdrawal(db, 1000.0)
    cur = costs.cost_summary(db, period="month", buckets=1)["current"]
    assert cur["trade_fees"] == pytest.approx(2.5)
    assert cur["withdrawal_fee"] == pytest.approx(1.5)
    assert cur["vat"] == pytest.approx(100.0)
    # total = trade + fee + vat + ai_estimate
    assert cur["total"] == pytest.approx(2.5 + 1.5 + 100.0 + cur["ai_total"])


def test_ai_estimate_fallback_when_no_metered(db):
    cur = costs.cost_summary(db, period="month", buckets=1)["current"]
    assert cur["ai_estimated"] is True
    assert cur["ai_claude"] > 0 and cur["ai_grok"] > 0
    # ratio is rate-pinned (tiny slack: each leg is rounded to 4 dp before the division)
    assert cur["ai_claude"] / cur["ai_grok"] == pytest.approx(25.0 / 20.0, rel=1e-3)


def test_ai_metered_split_claude_vs_grok(db):
    now = datetime.utcnow()
    db.add(OpusCostLedger(billed_cost=3.0, purpose="decision", ts=now))       # Claude/Opus
    db.add(OpusCostLedger(billed_cost=1.0, purpose="grok_decision", ts=now))  # Grok
    db.add(OpusCostLedger(billed_cost=0.5, purpose="grok_scanner", ts=now))   # Grok
    db.commit()
    cur = costs.cost_summary(db, period="month", buckets=1)["current"]
    assert cur["ai_estimated"] is False  # metered, no fallback
    assert cur["ai_claude"] == pytest.approx(3.0)
    assert cur["ai_grok"] == pytest.approx(1.5)
    assert cur["ai_total"] == pytest.approx(4.5)


def test_period_validation_and_series_shape(db):
    for p in ("week", "month", "year"):
        s = costs.cost_summary(db, period=p, buckets=3)
        assert s["period"] == p
        assert len(s["series"]) == 3
        assert s["series"][-1]["current"] is True
        assert s["series"][0]["current"] is False
    assert costs.cost_summary(db, period="bogus")["period"] == "month"  # bad → month


def test_recent_fill_excluded_from_older_bucket(db):
    db.add(Fill(symbol="BTC", side="SELL", quantity=1, price=100, fee=9.9,
                executed_at=datetime.utcnow()))
    db.commit()
    s = costs.cost_summary(db, period="year", buckets=2)
    assert s["series"][0]["trade_fees"] == 0.0  # last year: no fills
    assert s["series"][-1]["trade_fees"] == pytest.approx(9.9)  # this year
    # grand totals sum the series
    assert s["totals"]["trade_fees"] == pytest.approx(9.9)
