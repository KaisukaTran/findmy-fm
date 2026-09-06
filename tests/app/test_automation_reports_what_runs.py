"""
`/api/automation` reported the Grok knob, not the Grok gate.

Measured on the live book 2026-09-06: `grok_scanner_enabled=1` and `kss:grok_live_search=True`
were persisted, `/api/automation` answered `"grok_scanner": true`, and `audit_log` held **zero**
Grok rows across the project's whole life. The gate that decides is
`grok.scanner_enabled()` — the knob AND a non-empty `xai_api_key` — and `.env` on the trading
machine has no xAI key at all, so `scanner._scan_once` never entered the branch.

The KSS settings panel already got this right (`{"enabled": knob, "active": gate}` at
routes.py, rendered as "ENABLED (thiếu key)"). The automation payload did not, and it is what an
API client — or an operator asking the box what it is doing — reads.

Same family as the Guardian toggle removed the same day, and as `f7bbb42`: a control that reports
its own setting instead of its own effect.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

import app.portfolio as portfolio
from app.config import settings
from app.main import app as fastapi_app


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(portfolio, "get_current_prices", lambda syms: dict.fromkeys(syms, 60000.0))
    with TestClient(fastapi_app) as c:
        yield c


class TestAutomationReportsTheGateNotTheKnob:
    def test_the_knob_alone_does_not_claim_grok_is_running(self, client, monkeypatch):
        monkeypatch.setattr(settings, "grok_scanner_enabled", True)
        monkeypatch.setattr(settings, "xai_api_key", SecretStr(""))     # the live posture
        body = client.get("/api/automation").json()
        assert body["grok_scanner"] is False, "claimed the gate runs with no xAI key"
        assert body["grok_scanner_enabled"] is True, "the knob's own value must stay visible"

    def test_knob_plus_key_does_report_it_running(self, client, monkeypatch):
        monkeypatch.setattr(settings, "grok_scanner_enabled", True)
        monkeypatch.setattr(settings, "xai_api_key", SecretStr("xai-not-a-real-key"))
        body = client.get("/api/automation").json()
        assert body["grok_scanner"] is True
        assert body["grok_scanner_enabled"] is True

    def test_the_knob_off_is_off_whatever_the_key_says(self, client, monkeypatch):
        monkeypatch.setattr(settings, "grok_scanner_enabled", False)
        monkeypatch.setattr(settings, "xai_api_key", SecretStr("xai-not-a-real-key"))
        body = client.get("/api/automation").json()
        assert body["grok_scanner"] is False
        assert body["grok_scanner_enabled"] is False
