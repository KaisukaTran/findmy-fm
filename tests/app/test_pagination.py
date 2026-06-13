"""
Pagination tests: trades_view offset, list_pending offset, partial routes page param,
and page>10 clamp.  No network — prices are monkeypatched throughout.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.portfolio as portfolio
from app import orders
from app.main import app as fastapi_app

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seed_fills(db, count: int, monkeypatch) -> None:
    """Queue + approve `count` BUY fills for BTC (price-mocked)."""
    monkeypatch.setattr(portfolio, "get_current_prices", lambda syms: dict.fromkeys(syms, 1000.0))
    for _ in range(count):
        o, _ = orders.queue_order(db, symbol="BTC", side="BUY", quantity=0.01, price=1000.0)
        orders.approve_order(db, o.id, reviewer="test")


def _seed_pending(db, count: int, monkeypatch) -> None:
    """Queue `count` pending BUY orders for ETH (not approved — they stay pending)."""
    monkeypatch.setattr(portfolio, "get_current_prices", lambda syms: dict.fromkeys(syms, 500.0))
    for _ in range(count):
        orders.queue_order(db, symbol="ETH", side="BUY", quantity=0.1, price=500.0)


# ---------------------------------------------------------------------------
# A) portfolio.trades_view offset
# ---------------------------------------------------------------------------

def test_trades_view_offset_returns_next_slice(db, monkeypatch):
    """Seeding 25 fills: offset=0 returns the 20 newest, offset=20 returns the next 5."""
    _seed_fills(db, 25, monkeypatch)
    page1 = portfolio.trades_view(db, limit=20, offset=0)
    page2 = portfolio.trades_view(db, limit=20, offset=20)
    assert len(page1) == 20
    assert len(page2) == 5
    # Page 1 IDs must all be newer (higher id) than page 2 IDs.
    min_p1 = min(r["id"] for r in page1)
    max_p2 = max(r["id"] for r in page2)
    assert min_p1 > max_p2


def test_trades_view_offset_zero_no_overlap(db, monkeypatch):
    """Offset=0 and offset=20 produce disjoint sets."""
    _seed_fills(db, 22, monkeypatch)
    ids1 = {r["id"] for r in portfolio.trades_view(db, limit=20, offset=0)}
    ids2 = {r["id"] for r in portfolio.trades_view(db, limit=20, offset=20)}
    assert not ids1 & ids2


# ---------------------------------------------------------------------------
# B) orders.list_pending offset
# ---------------------------------------------------------------------------

def test_list_pending_offset(db, monkeypatch):
    """Seeding 25 pending orders: offset=0 gives 20, offset=20 gives 5."""
    _seed_pending(db, 25, monkeypatch)
    page1 = orders.list_pending(db, limit=20, offset=0)
    page2 = orders.list_pending(db, limit=20, offset=20)
    assert len(page1) == 20
    assert len(page2) == 5


def test_list_pending_offset_no_overlap(db, monkeypatch):
    """offset=0 and offset=20 return disjoint ids."""
    _seed_pending(db, 22, monkeypatch)
    ids1 = {o.id for o in orders.list_pending(db, limit=20, offset=0)}
    ids2 = {o.id for o in orders.list_pending(db, limit=20, offset=20)}
    assert not ids1 & ids2


# ---------------------------------------------------------------------------
# Shared TestClient fixture (monkeypatches prices in both portfolio and orders)
# ---------------------------------------------------------------------------

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(portfolio, "get_current_prices", lambda syms: dict.fromkeys(syms, 1000.0))
    # also patch inside the orders module (used by auto_approve_by_policy)
    import app.orders as _orders_mod
    monkeypatch.setattr(_orders_mod, "get_current_prices", lambda syms: dict.fromkeys(syms, 1000.0))
    with TestClient(fastapi_app) as c:
        yield c


# ---------------------------------------------------------------------------
# C) /partials/trades — page param
# ---------------------------------------------------------------------------

def test_partial_trades_page1_vs_page2(db, client, monkeypatch):
    """After seeding 25 fills, page=2 shows older data than page=1."""
    _seed_fills(db, 25, monkeypatch)
    r1 = client.get("/partials/trades?page=1")
    r2 = client.get("/partials/trades?page=2")
    assert r1.status_code == 200
    assert r2.status_code == 200
    # The two pages must differ — page 1 has 20 newest rows, page 2 has 5 older.
    assert r1.text != r2.text


def test_partial_trades_page_gt10_clamps(db, client, monkeypatch):
    """page > 10 must return the same content as page=10 (clamped)."""
    _seed_fills(db, 5, monkeypatch)
    r10 = client.get("/partials/trades?page=10")
    r99 = client.get("/partials/trades?page=99")
    assert r10.status_code == 200
    assert r99.status_code == 200
    # Both should be page 10 (empty or same slice — text should match)
    assert r10.text == r99.text


# ---------------------------------------------------------------------------
# D) /partials/pending — page param
# ---------------------------------------------------------------------------

def test_partial_pending_page1_vs_page2(db, client, monkeypatch):
    """After seeding 25 pending orders, page=2 returns different content."""
    _seed_pending(db, 25, monkeypatch)
    r1 = client.get("/partials/pending?page=1")
    r2 = client.get("/partials/pending?page=2")
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.text != r2.text


def test_partial_pending_page_gt10_clamps(client):
    r10 = client.get("/partials/pending?page=10")
    r50 = client.get("/partials/pending?page=50")
    assert r10.status_code == 200
    assert r50.status_code == 200
    assert r10.text == r50.text


# ---------------------------------------------------------------------------
# E) /partials/positions — page param
# ---------------------------------------------------------------------------

def test_partial_positions_page_gt10_clamps(client):
    r10 = client.get("/partials/positions?page=10")
    r99 = client.get("/partials/positions?page=99")
    assert r10.status_code == 200
    assert r99.status_code == 200
    assert r10.text == r99.text


# ---------------------------------------------------------------------------
# F) /partials/audit — page param
# ---------------------------------------------------------------------------

def test_partial_audit_page1_vs_page2(db, client):
    """Seed 25 audit rows; page=2 must differ from page=1."""
    from app import audit
    for i in range(25):
        audit.log(db, actor="test", action=f"action_{i}", entity="test")
    db.commit()  # audit.log only flushes; commit so TestClient sessions can see the rows

    r1 = client.get("/partials/audit?page=1")
    r2 = client.get("/partials/audit?page=2")
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.text != r2.text


def test_partial_audit_page_gt10_clamps(client):
    r10 = client.get("/partials/audit?page=10")
    r20 = client.get("/partials/audit?page=20")
    assert r10.status_code == 200
    assert r20.status_code == 200
    assert r10.text == r20.text


# ---------------------------------------------------------------------------
# G) /partials/kss — page param
# ---------------------------------------------------------------------------

def test_partial_kss_page_gt10_clamps(client):
    r10 = client.get("/partials/kss?page=10")
    r99 = client.get("/partials/kss?page=99")
    assert r10.status_code == 200
    assert r99.status_code == 200
    assert r10.text == r99.text
