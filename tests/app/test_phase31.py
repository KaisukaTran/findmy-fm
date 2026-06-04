"""v3.1: bulk queue actions, auto-approval policy, automation status endpoints."""

import pytest
from fastapi.testclient import TestClient

from app import models, orders
from app.config import settings
from app.main import app as fastapi_app

# --- auto-approval policy + bulk (unit) ---------------------------------


def test_approve_all_and_reject_all(db):
    for i in range(2):
        orders.queue_order(db, symbol="BTC", side="BUY", quantity=0.001, price=50000 + i)
    assert len(orders.approve_all(db)) == 2
    assert db.query(models.Fill).count() == 2

    orders.queue_order(db, symbol="ETH", side="BUY", quantity=0.001, price=1000)
    assert len(orders.reject_all(db, reason="x")) == 1
    assert db.query(models.PendingOrder).filter_by(status=models.PENDING).count() == 0


def test_auto_approve_policy(db, monkeypatch):
    monkeypatch.setattr(settings, "autoapprove_enabled", True)
    monkeypatch.setattr(settings, "autoapprove_max_notional", 50.0)
    monkeypatch.setattr(settings, "autoapprove_sources", ["kss"])

    small_kss, _ = orders.queue_order(db, symbol="BTC", side="BUY", quantity=0.0001, price=100,
                                      source="kss", source_ref="pyramid:1:wave:0")
    big_kss, _ = orders.queue_order(db, symbol="BTC", side="BUY", quantity=1.0, price=100,
                                    source="kss", source_ref="pyramid:1:wave:1")
    manual, _ = orders.queue_order(db, symbol="BTC", side="BUY", quantity=0.0001, price=100)

    approved = orders.auto_approve_by_policy(db)
    assert approved == [small_kss.id]                      # only KSS ≤ $50
    db.refresh(big_kss)
    db.refresh(manual)
    assert big_kss.status == models.PENDING                # too large
    assert manual.status == models.PENDING                 # wrong source


def test_auto_approve_disabled(db, monkeypatch):
    monkeypatch.setattr(settings, "autoapprove_enabled", False)
    orders.queue_order(db, symbol="BTC", side="BUY", quantity=0.0001, price=100, source="kss")
    assert orders.auto_approve_by_policy(db) == []


# --- endpoints (TestClient) ---------------------------------------------


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr("app.portfolio.get_current_prices", lambda syms: dict.fromkeys(syms, 1.0))
    with TestClient(fastapi_app) as c:
        yield c


def test_automation_and_status(client):
    a = client.get("/api/automation").json()
    assert {"scheduler_running", "auto_trade", "autoapprove", "open_sessions"} <= set(a)
    assert client.get("/partials/status").status_code == 200
    assert client.get("/partials/pending").status_code == 200


def test_autoapprove_toggle_and_approve_all_endpoint(client):
    assert client.get("/api/autoapprove").json()["enabled"] is False
    assert client.post("/api/autoapprove", json={"enabled": True, "max_notional": 25}).json()["enabled"] is True
    settings.autoapprove_enabled = False  # reset shared singleton

    client.post("/api/orders", json={"symbol": "BTC", "side": "BUY", "quantity": 0.001, "price": 50000})
    assert len(client.post("/api/pending/approve-all").json()["approved"]) >= 1
