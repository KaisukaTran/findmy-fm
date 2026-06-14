"""
Tests for app.savings — the KAI savings/holdings ledger and its isolation from auto-sell.
"""

from __future__ import annotations

import pytest

from app import savings
from app.models import Position, SavingsHolding


# --- write surface ------------------------------------------------------------


def test_add_holding_creates(db):
    h = savings.add_holding(db, "sol", 10, 150.0, note="cold storage")
    assert h.symbol == "SOL"  # normalised
    assert h.quantity == 10
    assert h.avg_cost == 150.0
    assert h.src == "KAI"


def test_add_holding_accumulates_weighted_avg(db):
    savings.add_holding(db, "SOL", 10, 100.0)
    h = savings.add_holding(db, "SOL", 10, 200.0)
    assert h.quantity == 20
    assert h.avg_cost == pytest.approx(150.0)  # (10*100 + 10*200)/20


def test_add_holding_rejects_bad_input(db):
    with pytest.raises(ValueError):
        savings.add_holding(db, "", 1, 1)
    with pytest.raises(ValueError):
        savings.add_holding(db, "SOL", 0, 100)
    with pytest.raises(ValueError):
        savings.add_holding(db, "SOL", 5, -1)


def test_set_holding_overwrites(db):
    savings.add_holding(db, "SOL", 10, 100.0)
    h = savings.set_holding(db, "SOL", 3, 250.0, note="corrected")
    assert h.quantity == 3
    assert h.avg_cost == 250.0
    assert h.note == "corrected"


def test_remove_holding(db):
    savings.add_holding(db, "SOL", 10, 100.0)
    assert savings.remove_holding(db, "sol") is True
    assert savings.remove_holding(db, "SOL") is False  # already gone
    assert savings._get(db, "SOL") is None


# --- read surface (priced live, mocked) ---------------------------------------


def test_list_holdings_prices_and_pnl(db, monkeypatch):
    savings.add_holding(db, "SOL", 10, 100.0)
    monkeypatch.setattr("app.market.get_current_prices", lambda syms: {"SOL": 150.0})
    rows = savings.list_holdings(db)
    assert len(rows) == 1
    r = rows[0]
    assert r["value"] == pytest.approx(1500.0)
    assert r["cost_basis"] == pytest.approx(1000.0)
    assert r["unrealized_pnl"] == pytest.approx(500.0)
    assert r["unrealized_pnl_pct"] == pytest.approx(50.0)


def test_summary_totals(db, monkeypatch):
    savings.add_holding(db, "SOL", 10, 100.0)
    savings.add_holding(db, "BTC", 1, 50000.0)
    monkeypatch.setattr("app.market.get_current_prices",
                        lambda syms: {"SOL": 120.0, "BTC": 60000.0})
    s = savings.summary(db)
    assert s["count"] == 2
    assert s["cost_basis"] == pytest.approx(1000.0 + 50000.0)
    assert s["value"] == pytest.approx(1200.0 + 60000.0)
    assert s["unrealized_pnl"] == pytest.approx(200.0 + 10000.0)


# --- the protection guarantee -------------------------------------------------


def test_savings_does_not_create_a_position(db):
    """A savings holding must NOT appear in the trading Position table — that is what keeps it
    invisible to the orphan manager / scanner / OPUS (all of which read Position) so it can
    never be auto-sold."""
    savings.add_holding(db, "SOL", 10, 100.0)
    assert db.query(Position).filter(Position.symbol == "SOL").one_or_none() is None
    assert db.query(SavingsHolding).filter(SavingsHolding.symbol == "SOL").count() == 1


def test_orphan_manager_ignores_savings(db, monkeypatch):
    """End-to-end: a coin held ONLY as savings is never swept by manage_orphan_positions."""
    from app.kss import service as kss
    savings.add_holding(db, "SOL", 10, 100.0)
    # Even if SOL has mooned, the orphan manager has no SOL Position to act on.
    monkeypatch.setattr("app.market.get_current_prices", lambda syms: {"SOL": 999.0})
    swept = kss.manage_orphan_positions(db)
    assert "SOL" not in swept
