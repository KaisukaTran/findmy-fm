"""
Positions table click-to-sort: server-side ordering by a whitelisted column so it
survives the 15s poll + pagination. 1st click = ascending, click again = descending.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.portfolio as portfolio
from app import models
from app.main import app as fastapi_app


def _seed(db, rows):
    """rows = list of (symbol, quantity). avg/cost fixed; price mocked by caller."""
    for sym, qty in rows:
        db.add(models.Position(symbol=sym, quantity=qty, avg_entry_price=10.0,
                               total_cost=qty * 10.0))
    db.commit()


# --- positions_view unit sort ------------------------------------------------

def test_sort_by_quantity_asc_then_desc(db, monkeypatch):
    monkeypatch.setattr(portfolio, "get_current_prices",
                        lambda syms: dict.fromkeys(syms, 100.0))
    _seed(db, [("AAA", 3.0), ("BBB", 1.0), ("CCC", 2.0)])
    asc = [r["symbol"] for r in portfolio.positions_view(db, sort="quantity", direction="asc")]
    desc = [r["symbol"] for r in portfolio.positions_view(db, sort="quantity", direction="desc")]
    assert asc == ["BBB", "CCC", "AAA"]   # qty 1 → 2 → 3
    assert desc == ["AAA", "CCC", "BBB"]  # qty 3 → 2 → 1


def test_sort_by_symbol_is_case_insensitive(db, monkeypatch):
    monkeypatch.setattr(portfolio, "get_current_prices",
                        lambda syms: dict.fromkeys(syms, 100.0))
    _seed(db, [("ETH", 1.0), ("BTC", 1.0), ("sol", 1.0)])
    asc = [r["symbol"] for r in portfolio.positions_view(db, sort="symbol", direction="asc")]
    assert asc == ["BTC", "ETH", "sol"]   # lower-cased compare, not ASCII (sol not before BTC)


def test_sort_by_market_value_uses_live_price(db, monkeypatch):
    # Same qty, different price → market_value order is driven by price.
    monkeypatch.setattr(portfolio, "get_current_prices",
                        lambda syms: {"AAA": 5.0, "BBB": 50.0, "CCC": 20.0})
    _seed(db, [("AAA", 1.0), ("BBB", 1.0), ("CCC", 1.0)])
    desc = [r["symbol"] for r in portfolio.positions_view(db, sort="market_value", direction="desc")]
    assert desc == ["BBB", "CCC", "AAA"]  # mv 50 → 20 → 5


def test_bogus_sort_key_keeps_default_order(db, monkeypatch):
    monkeypatch.setattr(portfolio, "get_current_prices",
                        lambda syms: dict.fromkeys(syms, 100.0))
    _seed(db, [("ZZZ", 1.0), ("AAA", 1.0)])
    # An unknown key is ignored → natural DB order (insertion order here).
    out = [r["symbol"] for r in portfolio.positions_view(db, sort="drop table", direction="asc")]
    assert out == ["ZZZ", "AAA"]


# --- route: sort/dir round-trip + toggle -------------------------------------

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(portfolio, "get_current_prices",
                        lambda syms: dict.fromkeys(syms, 100.0))
    with TestClient(fastapi_app) as c:
        yield c


def test_partial_positions_orders_rows_and_marks_active_column(db, client):
    _seed(db, [("AAA", 3.0), ("BBB", 1.0), ("CCC", 2.0)])
    r = client.get("/partials/positions?sort=quantity&dir=asc")
    assert r.status_code == 200
    body = r.text
    # BBB(1) before CCC(2) before AAA(3)
    assert body.index("BBB") < body.index("CCC") < body.index("AAA")
    # active column shows the ▲ (ascending) marker and the pane carries the sort on poll.
    assert "▲" in body
    assert "sort=quantity&dir=asc" in body
    # The clicked column's own header link now offers to flip to desc.
    assert "sort=quantity&dir=desc" in body


def test_partial_positions_default_has_no_sort_param(db, client):
    _seed(db, [("AAA", 1.0)])
    r = client.get("/partials/positions")
    assert r.status_code == 200
    # Pane poll URL stays clean when no column is chosen.
    assert 'hx-get="/partials/positions?page=1"' in r.text
