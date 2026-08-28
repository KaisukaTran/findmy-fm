"""
KSS row action buttons:
  - "TP?" (kssCheckTp) is removed from the UI everywhere (the scheduler already runs
    the TP check every cycle; the endpoint stays for the API/tests).
  - trailing session      -> [DCA+] [Chốt lời]  (Dừng hidden — Chốt lời is the safe exit)
  - non-trailing (active)  -> [DCA+] [Dừng]       (no take-profit until it arms)
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.portfolio as portfolio
from app import models
from app.clock import utcnow
from app.main import app as fastapi_app
from app.models import SESSION_ACTIVE


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(portfolio, "get_current_prices",
                        lambda syms: dict.fromkeys(syms, 100.0))
    monkeypatch.setattr("app.kss.pyramid.get_current_prices",
                        lambda syms: dict.fromkeys(syms, 100.0))
    with TestClient(fastapi_app) as c:
        yield c


def _active_session(db, symbol, trail):
    s = models.KssSession(
        symbol=symbol, entry_price=100.0, distance_pct=2.0, max_waves=5,
        isolated_fund=1000.0, tp_pct=3.0, timeout_x_min=30.0, gap_y_min=5.0,
        status=SESSION_ACTIVE, trail_active=trail, created_at=utcnow(),
    )
    db.add(s)
    db.commit()
    return s


def test_tp_button_removed_everywhere(db, client):
    _active_session(db, "AAA", trail=False)
    _active_session(db, "BBB", trail=True)
    body = client.get("/partials/kss").text
    assert 'data-action="kssCheckTp"' not in body
    assert ">TP?<" not in body


def test_trailing_shows_takeprofit_hides_stop(db, client):
    _active_session(db, "AAA", trail=True)
    body = client.get("/partials/kss").text
    assert 'data-action="kssTakeProfit"' in body   # Chốt lời present
    assert 'data-action="kssStop"' not in body      # Dừng hidden while trailing
    assert 'data-action="kssDcaNext"' in body       # DCA+ stays


def test_non_trailing_shows_stop_hides_takeprofit(db, client):
    _active_session(db, "AAA", trail=False)
    body = client.get("/partials/kss").text
    assert 'data-action="kssStop"' in body          # Dừng present
    assert 'data-action="kssTakeProfit"' not in body  # no Chốt lời until it arms
    assert 'data-action="kssDcaNext"' in body       # DCA+ stays
