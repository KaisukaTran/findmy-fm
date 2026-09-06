"""``kss_live_stop_orders`` promised a resting exchange-side STOP-MARKET that does not exist.

``app/kss/service.py:_maintain_live_stop`` passes every gate and then hits a bare ``return`` — its
own docstring said "Deliberately a no-op here". The knob is OFF today, so nobody is harmed yet;
the danger is switching it on before real money believing there is server-side gap protection.

Same family as the Guardian toggle and ``acaec7f``: a control that reports its own SETTING
instead of its own EFFECT — here the effect is "nothing happens", so the fix is to make the API
refuse to lie about it, and say so everywhere a human reads the knob.
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


class TestApiRejectsEnablingTheStub:
    def test_enabling_kss_live_stop_orders_is_rejected(self, client):
        r = client.post("/api/kss-settings", json={"kss_live_stop_orders": True})
        assert r.status_code == 400
        assert "_maintain_live_stop" in r.json()["detail"]

    def test_explicitly_disabling_it_is_still_allowed(self, client):
        r = client.post("/api/kss-settings", json={"kss_live_stop_orders": False})
        assert r.status_code == 200

    def test_unrelated_knob_edits_are_unaffected(self, client):
        r = client.post("/api/kss-settings", json={"sl_pct": 9.0})
        assert r.status_code == 200


def test_field_description_promises_no_resting_exchange_order():
    field = type(settings).model_fields["kss_live_stop_orders"]
    desc = (field.description or "").lower()
    assert "chưa dựng" in desc or "not implemented" in desc
    # must not still promise a resting exchange order as if it exists today
    assert "duy trì lệnh stop-market treo sẵn trên sàn" not in desc


def test_maintain_live_stop_docstring_says_stub_first():
    from app.kss.service import _maintain_live_stop

    first_line = (_maintain_live_stop.__doc__ or "").strip().splitlines()[0]
    assert first_line.upper().startswith("STUB")
