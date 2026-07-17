"""O-5: cost-aware control (decision throttle, pacing) + loop.tick orchestration."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from pydantic import SecretStr

from app import runtime
from app.config import settings
from app.orchestrator import brain, grok, loop, policy, service
from app.orchestrator import models as om


def test_decision_gap_doubles_when_budget_hot(db, monkeypatch):
    monkeypatch.setattr(settings, "opus_interval_min", 5)
    monkeypatch.setattr(settings, "opus_daily_cost_cap_usd", 10.0)
    assert service.decision_gap_min(db) == 5.0
    db.add(om.OpusCostLedger(billed_cost=8.0))  # 80% of cap
    db.commit()
    assert service.decision_gap_min(db) == 10.0


def test_pacing_behind_when_kpi_below_target(db, monkeypatch):
    monkeypatch.setattr(settings, "opus_kpi_target_pct", 1.0)
    monkeypatch.setattr(settings, "opus_allocation_usd", 1000.0)
    p = service.pacing(db)
    assert p["behind_pace"] is True  # KPI 0 < 1%
    assert set(p) == {"kpi_pct", "target_pct", "behind_pace", "spend_ratio", "daily_loss_stop_active"}


def test_tick_off_is_noop(db, monkeypatch):
    monkeypatch.setattr(settings, "opus_mode", False)
    assert loop.tick(db) == {"skipped": "off"}


def test_tick_pauses_at_cost_cap(db, monkeypatch):
    monkeypatch.setattr(settings, "opus_mode", True)
    monkeypatch.setattr(settings, "opus_daily_cost_cap_usd", 5.0)
    db.add(om.OpusCostLedger(billed_cost=6.0))
    db.commit()
    called = {"n": 0}
    monkeypatch.setattr(brain, "decide", lambda _db: called.__setitem__("n", called["n"] + 1) or {})
    out = loop.tick(db)
    assert out["skipped"] == "cost_cap"
    assert called["n"] == 0  # brain never called when capped


def test_tick_throttles_recent_decision(db, monkeypatch):
    monkeypatch.setattr(settings, "opus_mode", True)
    monkeypatch.setattr(settings, "anthropic_api_key", SecretStr("k"))
    monkeypatch.setattr(settings, "opus_interval_min", 5)
    runtime.set(db, "opus_last_decision_at", datetime.utcnow().isoformat())  # just decided
    called = {"n": 0}
    monkeypatch.setattr(brain, "decide", lambda _db: called.__setitem__("n", 1) or {"ok": False, "intents": []})
    out = loop.tick(db)
    assert out["skipped"] == "throttled"
    assert called["n"] == 0


def test_tick_decides_when_due(db, monkeypatch):
    monkeypatch.setattr(settings, "opus_mode", True)
    monkeypatch.setattr(settings, "opus_shadow", True)
    monkeypatch.setattr(settings, "anthropic_api_key", SecretStr("k"))
    monkeypatch.setattr(settings, "opus_interval_min", 5)
    # last decision long ago → due now
    runtime.set(db, "opus_last_decision_at", (datetime.utcnow() - timedelta(hours=1)).isoformat())
    reply = json.dumps({"intents": [{"action": "hold", "reason": "thin"}]})
    monkeypatch.setattr(brain, "_call_opus", lambda sb, ut: (reply, {"input_tokens": 10, "output_tokens": 2}))
    out = loop.tick(db)
    assert out.get("intents") == 1
    assert out.get("shadow") is True  # shadow → not executed
    # the decision timestamp advanced
    assert runtime.get(db, "opus_last_decision_at") is not None


def test_loop_passes_solo_open_knob_to_consensus(db, monkeypatch):
    """P2: loop.tick must pass settings.opus_solo_open into consensus.combine — with it on,
    OPUS's own open reaches policy even though Grok proposed a DIFFERENT symbol (the
    AND-gate would otherwise drop it)."""
    monkeypatch.setattr(settings, "opus_mode", True)
    monkeypatch.setattr(settings, "anthropic_api_key", SecretStr("k"))
    monkeypatch.setattr(settings, "opus_interval_min", 5)
    monkeypatch.setattr(settings, "opus_solo_open", True)
    monkeypatch.setattr(settings, "grok_enabled", True)
    monkeypatch.setattr(settings, "xai_api_key", SecretStr("k"))
    runtime.set(db, "opus_last_decision_at", (datetime.utcnow() - timedelta(hours=1)).isoformat())

    monkeypatch.setattr(brain, "decide", lambda _db: {
        "ok": True, "billed_cost": 0.0,
        "intents": [{"action": "open", "symbol": "BTC", "notional": 100, "reason": "trend"}],
    })
    monkeypatch.setattr(grok, "decide", lambda _db: {
        "ok": True, "billed_cost": 0.0,
        "intents": [{"action": "open", "symbol": "ETH", "notional": 100, "reason": "alpha"}],
    })
    captured: dict = {}

    def fake_apply(_db, intents):
        captured["intents"] = intents
        return {"executed": [], "rejected": [], "shadow": False}

    monkeypatch.setattr(policy, "apply_intents", fake_apply)

    out = loop.tick(db)
    assert out["grok"] is True
    opens = [i for i in captured["intents"] if i["action"] == "open"]
    assert any(i["symbol"] == "BTC" for i in opens)  # OPUS's solo open reached policy
    assert not any(i["symbol"] == "ETH" for i in opens)  # Grok-only open still dropped
