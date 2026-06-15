"""
Phase 6 go-live infrastructure tests.

Live placement is SHIPPED OFF: paper is the default. These pin the gating —
the live path runs only with the master flag + keys, BUYs are re-gated by the
breaker and the notional cap, and SELL exits are never gated. The exchange call
itself is monkeypatched so no test touches the network.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import execution, orders, runtime
from app.config import settings
from app.main import app as fastapi_app


@pytest.fixture(autouse=True)
def _stub_prices(monkeypatch):
    monkeypatch.setattr("app.orders.get_current_prices", lambda syms: dict.fromkeys(syms, 100.0))


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr("app.portfolio.get_current_prices", lambda syms: dict.fromkeys(syms, 100.0))
    with TestClient(fastapi_app) as c:
        yield c


def test_live_enabled_requires_flag_and_keys(monkeypatch):
    monkeypatch.setattr(settings, "live_trading", False)
    monkeypatch.setattr(execution, "live_key_present", lambda: True)
    assert execution.live_enabled() is False                 # flag off

    monkeypatch.setattr(settings, "live_trading", True)
    monkeypatch.setattr(execution, "live_key_present", lambda: False)
    assert execution.live_enabled() is False                 # no keys

    monkeypatch.setattr(execution, "live_key_present", lambda: True)
    assert execution.live_enabled() is True                  # both → on


def test_approve_uses_paper_by_default(db, monkeypatch):
    monkeypatch.setattr(settings, "live_trading", False)
    # If the live path were taken this would explode — it must not be.
    monkeypatch.setattr(execution, "place_live_order",
                        lambda *a, **k: pytest.fail("paper mode must not place a live order"))
    b, _ = orders.queue_order(db, symbol="BTC", side="BUY", quantity=0.1, price=100.0)
    fill = orders.approve_order(db, b.id)
    assert fill.id is not None and fill.price > 0            # paper-executed


def test_live_path_used_when_enabled(db, monkeypatch):
    monkeypatch.setattr(execution, "live_enabled", lambda: True)
    monkeypatch.setattr(settings, "live_max_order_notional", 1000.0)
    called = {}
    monkeypatch.setattr(
        execution, "place_live_order",
        lambda pair, side, qty, price, ot, **k: called.update(pair=pair, side=side)
        or {"price": 101.0, "quantity": qty, "fee": 0.2, "raw_id": "X1"},
    )
    b, _ = orders.queue_order(db, symbol="BTC", side="BUY", quantity=0.1, price=100.0)
    fill = orders.approve_order(db, b.id)
    assert fill.price == 101.0 and fill.fee == 0.2          # values came from the live fill
    assert called["side"] == "BUY"


def test_live_buy_blocked_over_notional_cap(db, monkeypatch):
    monkeypatch.setattr(execution, "live_enabled", lambda: True)
    monkeypatch.setattr(settings, "live_max_order_notional", 5.0)   # 0.1 * 100 = 10 > 5
    placed = {"n": 0}
    monkeypatch.setattr(execution, "place_live_order",
                        lambda *a, **k: placed.update(n=placed["n"] + 1) or {"price": 1, "quantity": 1, "fee": 0})
    b, _ = orders.queue_order(db, symbol="BTC", side="BUY", quantity=0.1, price=100.0)
    with pytest.raises(ValueError, match="notional"):
        orders.approve_order(db, b.id)
    assert placed["n"] == 0                                  # never placed a real order


def test_live_buy_blocked_when_frozen(db, monkeypatch):
    monkeypatch.setattr(execution, "live_enabled", lambda: True)
    monkeypatch.setattr(settings, "live_max_order_notional", 1000.0)
    monkeypatch.setattr(execution, "place_live_order",
                        lambda *a, **k: pytest.fail("frozen breaker must block a live BUY"))
    runtime.freeze(db, reason="test")
    b, _ = orders.queue_order(db, symbol="BTC", side="BUY", quantity=0.1, price=100.0)
    with pytest.raises(ValueError, match="frozen"):
        orders.approve_order(db, b.id)


def test_live_sell_exit_not_gated_when_frozen_or_over_cap(db, monkeypatch):
    """SELL exits must place even when frozen and above the BUY notional cap."""
    monkeypatch.setattr(execution, "live_enabled", lambda: True)
    monkeypatch.setattr(settings, "live_max_order_notional", 1.0)   # tiny — would block a BUY
    monkeypatch.setattr(
        execution, "place_live_order",
        lambda pair, side, qty, price, ot, **k: {"price": 100.0, "quantity": qty, "fee": 0.1, "raw_id": "S1"},
    )
    runtime.freeze(db, reason="test")
    s, _ = orders.queue_order(db, symbol="BTC", side="SELL", quantity=0.1, price=100.0)
    fill = orders.approve_order(db, s.id, reviewer="dashboard")
    assert fill.price == 100.0                               # the exit went through


def test_live_trading_route_requires_typed_confirm(client, monkeypatch):
    from app import execution as ex2
    monkeypatch.setattr(ex2, "live_key_present", lambda: True)
    # wrong / missing confirm phrase → 400, stays paper
    r = client.post("/api/live-trading", json={"enabled": True, "confirm": "yes"})
    assert r.status_code == 400
    r = client.post("/api/live-trading", json={"enabled": True})
    assert r.status_code == 400
    assert client.get("/api/live-trading").json()["live_trading"] is False


def test_live_trading_route_blocks_enable_without_keys(client, monkeypatch):
    from app import execution as ex2
    monkeypatch.setattr(ex2, "live_key_present", lambda: False)
    r = client.post("/api/live-trading", json={"enabled": True, "confirm": "LIVE-TRADING"})
    assert r.status_code == 400 and "key" in r.json()["detail"].lower()


def test_live_trading_route_enable_and_disable(client, monkeypatch):
    from app import execution as ex2
    monkeypatch.setattr(ex2, "live_key_present", lambda: True)
    r = client.post("/api/live-trading", json={"enabled": True, "confirm": "LIVE-TRADING"})
    assert r.status_code == 200 and r.json()["live_trading"] is True
    # disabling never needs a confirm
    r = client.post("/api/live-trading", json={"enabled": False})
    assert r.status_code == 200 and r.json()["live_trading"] is False


def test_validate_at_boot_messages(monkeypatch):
    monkeypatch.setattr(settings, "live_trading", False)
    assert execution.validate_at_boot() is None

    monkeypatch.setattr(settings, "live_trading", True)
    monkeypatch.setattr(execution, "live_key_present", lambda: False)
    assert "no exchange API key" in execution.validate_at_boot()

    monkeypatch.setattr(execution, "live_key_present", lambda: True)
    assert "LIVE_TRADING active" in execution.validate_at_boot()
