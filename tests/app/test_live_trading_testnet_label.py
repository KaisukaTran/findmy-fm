"""The live-trading panel over-warned: it said "LIVE (tiền thật)" from ``live_trading`` alone,
even on testnet — while the header badge correctly says "LIVE · TESTNET" on the same screen
(``live_trading=true`` + ``live_use_testnet=true`` is the current live posture).

``/api/live-trading`` never returned ``live_use_testnet``, so the partial had no way to tell the
two states apart.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app as fastapi_app


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr("app.portfolio.get_current_prices", lambda syms: dict.fromkeys(syms, 100.0))
    with TestClient(fastapi_app) as c:
        yield c


def test_endpoint_returns_the_testnet_flag(client, monkeypatch):
    monkeypatch.setattr(settings, "live_trading", True)
    monkeypatch.setattr(settings, "live_use_testnet", True)

    body = client.get("/api/live-trading").json()

    assert body["live_use_testnet"] is True


def test_panel_distinguishes_testnet_from_real_money(client, monkeypatch):
    monkeypatch.setattr(settings, "live_trading", True)

    monkeypatch.setattr(settings, "live_use_testnet", True)
    testnet_html = client.get("/partials/live-trading").text

    monkeypatch.setattr(settings, "live_use_testnet", False)
    real_html = client.get("/partials/live-trading").text

    # The distinction lives in the badge, not the static explanatory paragraph below it
    # (which always mentions "tiền thật" as a generic warning about what LIVE does).
    assert "● LIVE · TESTNET" in testnet_html
    assert "● LIVE (tiền thật)" not in testnet_html
    assert "● LIVE (tiền thật)" in real_html
    assert "● LIVE · TESTNET" not in real_html
    assert testnet_html != real_html
