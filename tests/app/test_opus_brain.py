"""O-2: OPUS brain — snapshot, gating, strict parse, cost metering (Opus call mocked)."""

from __future__ import annotations

import json

from pydantic import SecretStr

from app.config import settings
from app.orchestrator import brain
from app.orchestrator import models as om


def _enable(monkeypatch):
    monkeypatch.setattr(settings, "opus_mode", True)
    monkeypatch.setattr(settings, "anthropic_api_key", SecretStr("sk-ant-test"))


def test_enabled_requires_mode_and_key(monkeypatch):
    monkeypatch.setattr(settings, "opus_mode", False)
    monkeypatch.setattr(settings, "anthropic_api_key", SecretStr("k"))
    assert brain.enabled() is False
    monkeypatch.setattr(settings, "opus_mode", True)
    monkeypatch.setattr(settings, "anthropic_api_key", SecretStr(""))
    assert brain.enabled() is False
    monkeypatch.setattr(settings, "anthropic_api_key", SecretStr("k"))
    assert brain.enabled() is True


def test_build_snapshot_shape(db, monkeypatch):
    monkeypatch.setattr(settings, "opus_allocation_usd", 2000.0)
    snap = brain.build_snapshot(db)
    assert set(snap) >= {"account", "kpi", "limits", "open_positions", "candidates", "prices"}
    assert snap["account"]["allocation"] == 2000.0
    assert snap["open_positions"] == []  # none yet


def test_decide_disabled_is_noop(db, monkeypatch):
    monkeypatch.setattr(settings, "opus_mode", False)
    out = brain.decide(db)
    assert out["ok"] is False and out["intents"] == []


def test_decide_parses_and_meters_cost(db, monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setattr(settings, "opus_price_in_per_mtok", 10.0)
    monkeypatch.setattr(settings, "opus_price_out_per_mtok", 10.0)
    monkeypatch.setattr(settings, "opus_cost_multiplier", 2.0)
    reply = json.dumps({"intents": [
        {"action": "open", "symbol": "btc", "notional": 150, "reason": "trend"},
        {"action": "hold", "reason": "rest thin"},
        {"action": "bogus", "reason": "ignored"},
    ]})
    monkeypatch.setattr(brain, "_call_opus",
                        lambda sb, ut: (reply, {"input_tokens": 1_000_000, "output_tokens": 0}))
    out = brain.decide(db)
    assert out["ok"] is True
    actions = [i["action"] for i in out["intents"]]
    assert actions == ["open", "hold"]            # bogus dropped
    assert out["intents"][0]["symbol"] == "BTC"   # upper-cased
    # 1M input @10/Mtok = 10 raw → 20 billed
    assert abs(out["billed_cost"] - 20.0) < 1e-9
    assert db.query(om.OpusCostLedger).count() == 1


def test_decide_bad_json_is_safe(db, monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setattr(brain, "_call_opus",
                        lambda sb, ut: ("not json at all", {"input_tokens": 100, "output_tokens": 5}))
    out = brain.decide(db)
    assert out["ok"] is False and out["intents"] == []
    # cost is still metered even on parse failure
    assert db.query(om.OpusCostLedger).count() == 1


def test_decide_call_error_fails_safe(db, monkeypatch):
    _enable(monkeypatch)

    def boom(sb, ut):
        raise RuntimeError("network")

    monkeypatch.setattr(brain, "_call_opus", boom)
    out = brain.decide(db)
    assert out["ok"] is False and out["intents"] == []
    assert db.query(om.OpusCostLedger).count() == 0  # no usage → no cost row
