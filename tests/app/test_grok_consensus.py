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


def test_solo_open_false_matches_default_stats():
    """solo_open defaults False — stats gain the new key, but behaviour is byte-for-byte
    identical to the pre-P2 AND-gate (the other tests above cover the unchanged behaviour)."""
    opus = [{"action": "open", "symbol": "BTC", "notional": 100}]
    grok_i = [{"action": "hold", "reason": "meh"}]
    out = consensus.combine(opus, grok_i)
    assert out["stats"]["solo_open"] is False
    assert [i for i in out["intents"] if i["action"] == "open"] == []  # AND-gate unchanged


# --- P2/O-LIVE: solo_open (OPUS may open without Grok's agreement) -----


def test_solo_open_opus_only_survives():
    opus = [{"action": "open", "symbol": "BTC", "notional": 100, "reason": "trend"}]
    grok_i = [{"action": "hold", "reason": "meh"}]
    out = consensus.combine(opus, grok_i, solo_open=True)
    opens = [i for i in out["intents"] if i["action"] == "open"]
    assert len(opens) == 1 and opens[0]["symbol"] == "BTC"
    assert opens[0]["notional"] == 100
    assert out["stats"]["solo_open"] is True


def test_solo_open_grok_only_dropped():
    """Grok is the risk-skeptic role — it must never open alone, solo or not."""
    opus = [{"action": "hold", "reason": "no edge"}]
    grok_i = [{"action": "open", "symbol": "ETH", "notional": 100, "reason": "alpha"}]
    out = consensus.combine(opus, grok_i, solo_open=True)
    assert [i for i in out["intents"] if i["action"] == "open"] == []


def test_solo_open_agreement_keeps_min_notional():
    """When both agents happen to agree even in solo mode, still size conservatively."""
    opus = [{"action": "open", "symbol": "BTC", "notional": 200, "reason": "trend"}]
    grok_i = [{"action": "open", "symbol": "BTC", "notional": 150, "reason": "ok"}]
    out = consensus.combine(opus, grok_i, solo_open=True)
    opens = [i for i in out["intents"] if i["action"] == "open"]
    assert len(opens) == 1 and opens[0]["notional"] == 150


def test_solo_open_closes_still_union():
    """CLOSE stays a union (either agent can exit) regardless of solo_open."""
    opus = [{"action": "close", "position_id": 5, "reason": "exit"}]
    grok_i = [{"action": "hold", "reason": "ok"}]
    for solo in (False, True):
        out = consensus.combine(opus, grok_i, solo_open=solo)
        closes = [i for i in out["intents"] if i["action"] == "close"]
        assert len(closes) == 1 and closes[0]["position_id"] == 5


# --- P3: 'reduce' (partial take-profit) is risk-reduction too -----------


def test_reduce_survives_from_either_agent():
    opus = [{"action": "reduce", "position_id": 5, "notional": 50, "reason": "bank"}]
    grok_i = [{"action": "hold", "reason": "ok"}]
    out = consensus.combine(opus, grok_i)
    reduces = [i for i in out["intents"] if i["action"] == "reduce"]
    assert len(reduces) == 1 and reduces[0]["position_id"] == 5
    assert reduces[0]["notional"] == 50
    assert out["stats"]["reduces"] == 1


def test_reduce_survives_from_grok_alone_too():
    opus = [{"action": "hold", "reason": "ok"}]
    grok_i = [{"action": "reduce", "position_id": 9, "notional": 20, "reason": "bank"}]
    out = consensus.combine(opus, grok_i)
    reduces = [i for i in out["intents"] if i["action"] == "reduce"]
    assert len(reduces) == 1 and reduces[0]["position_id"] == 9


def test_close_beats_reduce_on_same_position():
    """The same position drawing both a close and a reduce (from either agent) → close wins,
    the stronger of the two risk-reduction actions."""
    opus = [{"action": "reduce", "position_id": 7, "notional": 30, "reason": "bank"}]
    grok_i = [{"action": "close", "position_id": 7, "reason": "exit"}]
    out = consensus.combine(opus, grok_i)
    matching = [i for i in out["intents"] if i.get("position_id") == 7]
    assert len(matching) == 1 and matching[0]["action"] == "close"
    assert out["stats"]["closes"] == 1
    assert out["stats"]["reduces"] == 0

    # order-independent: close first, reduce second — still wins.
    out2 = consensus.combine(grok_i, opus)
    matching2 = [i for i in out2["intents"] if i.get("position_id") == 7]
    assert len(matching2) == 1 and matching2[0]["action"] == "close"


def test_reduce_solo_open_irrelevant():
    """solo_open only governs opens; a reduce survives identically either way."""
    opus = [{"action": "reduce", "position_id": 3, "notional": 40, "reason": "bank"}]
    grok_i = [{"action": "hold", "reason": "ok"}]
    for solo in (False, True):
        out = consensus.combine(opus, grok_i, solo_open=solo)
        reduces = [i for i in out["intents"] if i["action"] == "reduce"]
        assert len(reduces) == 1


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


def test_scanner_review_routes_to_live_search_and_scales_tokens(db, monkeypatch):
    """When grok_live_search is on, review_candidates uses the Agent-Tools (search) path and
    scales the output-token budget to fit one verdict per candidate (no JSON truncation)."""
    monkeypatch.setattr(settings, "grok_live_search", True)
    monkeypatch.setattr(settings, "grok_max_tokens", 100)
    monkeypatch.setattr(settings, "grok_price_in_per_mtok", 3.0)
    monkeypatch.setattr(settings, "grok_price_out_per_mtok", 15.0)
    monkeypatch.setattr(settings, "opus_cost_multiplier", 1.0)
    monkeypatch.setattr(grok, "scanner_enabled", lambda: True)

    captured: dict = {}

    def _fake_search(systxt, usertxt, *, max_tokens=None):
        captured["called"] = "search"
        captured["max_tokens"] = max_tokens
        return json.dumps({"reviews": []}), {"prompt_tokens": 1, "completion_tokens": 1}

    def _fake_chat(systxt, usertxt, *, max_tokens=None):
        captured["called"] = "chat"
        return json.dumps({"reviews": []}), {"prompt_tokens": 1, "completion_tokens": 1}

    monkeypatch.setattr(grok, "_call_grok_search", _fake_search)
    monkeypatch.setattr(grok, "_call_grok", _fake_chat)
    grok.review_candidates(db, [{"symbol": f"C{i}"} for i in range(30)])
    assert captured["called"] == "search"            # live-search path taken
    assert captured["max_tokens"] >= 30 * 40          # scaled past the 100 default to fit 30 verdicts


def test_call_grok_search_uses_responses_api_with_tools(monkeypatch):
    """_call_grok_search posts to /v1/responses with the web_search + x_search server-side tools,
    honours max_output_tokens, and extracts the final message text + normalised usage."""
    monkeypatch.setattr(settings, "xai_api_key", SecretStr("k"))
    monkeypatch.setattr(settings, "grok_search_max_results", 5)
    captured: dict = {}

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "output": [
                    {"type": "reasoning"},
                    {"type": "web_search_call"},
                    {"type": "message",
                     "content": [{"type": "output_text", "text": '{"reviews":[]}'}]},
                ],
                "usage": {"input_tokens": 10, "output_tokens": 3},
            }

    monkeypatch.setattr(
        "app.orchestrator.grok.httpx.post",
        lambda url, headers=None, json=None, timeout=None:
            captured.update(url=url, body=json) or _Resp(),
    )
    raw, usage = grok._call_grok_search("sys", "usr", max_tokens=777)
    assert captured["url"].endswith("/v1/responses")
    assert {t["type"] for t in captured["body"]["tools"]} >= {"web_search", "x_search"}
    assert captured["body"]["max_output_tokens"] == 777
    assert raw == '{"reviews":[]}'
    assert usage == {"prompt_tokens": 10, "completion_tokens": 3}
