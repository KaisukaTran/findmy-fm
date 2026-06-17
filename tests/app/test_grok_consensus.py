"""Grok co-pilot + OPUS consensus: open needs both, close needs either; cost metered."""

from __future__ import annotations

import json

from pydantic import SecretStr

from app.config import settings
from app.orchestrator import consensus, grok
from app.orchestrator import models as om

# --- consensus rules (pure) --------------------------------------------


def test_open_requires_both_agree():
    opus = [{"action": "open", "symbol": "BTC", "notional": 200, "reason": "trend"},
            {"action": "open", "symbol": "ETH", "notional": 100, "reason": "alpha"}]
    grok_i = [{"action": "open", "symbol": "BTC", "notional": 150, "reason": "ok"},
              {"action": "open", "symbol": "SOL", "notional": 100, "reason": "x"}]
    out = consensus.combine(opus, grok_i)
    opens = [i for i in out["intents"] if i["action"] == "open"]
    assert len(opens) == 1 and opens[0]["symbol"] == "BTC"     # only the agreed symbol
    assert opens[0]["notional"] == 150                          # min (more conservative)
    assert out["stats"]["agreed_open"] == 1


def test_close_needs_either():
    opus = [{"action": "close", "position_id": 5, "reason": "exit"}]
    grok_i = [{"action": "hold", "reason": "ok"}]
    out = consensus.combine(opus, grok_i)
    closes = [i for i in out["intents"] if i["action"] == "close"]
    assert len(closes) == 1 and closes[0]["position_id"] == 5
    assert out["stats"]["closes"] == 1


def test_no_agreement_no_open():
    out = consensus.combine(
        [{"action": "open", "symbol": "BTC", "notional": 100}],
        [{"action": "open", "symbol": "ETH", "notional": 100}],
    )
    assert [i for i in out["intents"] if i["action"] == "open"] == []


# --- grok agent (mocked call) ------------------------------------------


def test_grok_enabled_gate(monkeypatch):
    monkeypatch.setattr(settings, "opus_mode", True)
    monkeypatch.setattr(settings, "grok_enabled", True)
    monkeypatch.setattr(settings, "xai_api_key", SecretStr(""))
    assert grok.enabled() is False
    monkeypatch.setattr(settings, "xai_api_key", SecretStr("k"))
    assert grok.enabled() is True
    monkeypatch.setattr(settings, "grok_enabled", False)
    assert grok.enabled() is False


def test_grok_decide_parses_and_meters_cost(db, monkeypatch):
    monkeypatch.setattr(settings, "opus_mode", True)
    monkeypatch.setattr(settings, "grok_enabled", True)
    monkeypatch.setattr(settings, "xai_api_key", SecretStr("k"))
    monkeypatch.setattr(settings, "grok_price_in_per_mtok", 3.0)
    monkeypatch.setattr(settings, "grok_price_out_per_mtok", 15.0)
    monkeypatch.setattr(settings, "opus_cost_multiplier", 2.0)
    reply = json.dumps({"intents": [{"action": "open", "symbol": "btc", "notional": 100, "reason": "y"}]})
    monkeypatch.setattr(grok, "_call_grok",
                        lambda sysm, ut: (reply, {"prompt_tokens": 1_000_000, "completion_tokens": 0}))
    out = grok.decide(db)
    assert out["ok"] and out["intents"][0]["symbol"] == "BTC"
    assert abs(out["billed_cost"] - 6.0) < 1e-9          # 1M in @ $3/Mtok = 3 raw → ×2 = 6
    assert db.query(om.OpusCostLedger).filter(om.OpusCostLedger.purpose == "grok_decision").count() == 1


def test_grok_disabled_is_noop(db, monkeypatch):
    monkeypatch.setattr(settings, "grok_enabled", False)
    assert grok.decide(db)["ok"] is False


def test_scanner_review_uses_live_search_and_scaled_tokens(db, monkeypatch):
    """review_candidates passes grok_live_search through and scales the output-token budget to
    fit one verdict per candidate (so a long list never truncates into invalid JSON)."""
    monkeypatch.setattr(settings, "grok_live_search", True)
    monkeypatch.setattr(settings, "grok_max_tokens", 100)
    monkeypatch.setattr(settings, "grok_price_in_per_mtok", 3.0)
    monkeypatch.setattr(settings, "grok_price_out_per_mtok", 15.0)
    monkeypatch.setattr(settings, "opus_cost_multiplier", 1.0)
    monkeypatch.setattr(grok, "scanner_enabled", lambda: True)

    captured: dict = {}

    def _fake(systxt, usertxt, *, max_tokens=None, live_search=False):
        captured["max_tokens"] = max_tokens
        captured["live_search"] = live_search
        return json.dumps({"reviews": []}), {"prompt_tokens": 1, "completion_tokens": 1}

    monkeypatch.setattr(grok, "_call_grok", _fake)
    grok.review_candidates(db, [{"symbol": f"C{i}"} for i in range(30)])
    assert captured["live_search"] is True
    assert captured["max_tokens"] >= 30 * 40  # scaled past the 100 default to fit 30 verdicts


def test_call_grok_attaches_live_search_params(monkeypatch):
    """_call_grok adds xAI search_parameters only when live_search=True, and honours max_tokens."""
    monkeypatch.setattr(settings, "xai_api_key", SecretStr("k"))
    monkeypatch.setattr(settings, "grok_search_max_results", 5)
    captured: dict = {}

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": "{}"}}], "usage": {}}

    monkeypatch.setattr(
        "app.orchestrator.grok.httpx.post",
        lambda url, headers=None, json=None, timeout=None: captured.update(body=json) or _Resp(),
    )
    grok._call_grok("sys", "usr", max_tokens=777, live_search=True)
    assert captured["body"]["max_tokens"] == 777
    sp = captured["body"]["search_parameters"]
    assert sp["mode"] == "auto" and sp["max_search_results"] == 5
    grok._call_grok("sys", "usr", live_search=False)
    assert "search_parameters" not in captured["body"]  # off path stays a plain chat call
