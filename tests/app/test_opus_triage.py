"""P3 (docs/opus-3pct-plan.md §2): Haiku triage — a cheap pre-screen gate in front of the
paid Opus decision call, plus its loop.py wiring and the 5 new runtime knobs."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from pydantic import SecretStr

from app import runtime
from app.config import settings
from app.orchestrator import brain, loop, triage
from app.orchestrator import models as om


def _enable(monkeypatch):
    monkeypatch.setattr(settings, "opus_mode", True)
    monkeypatch.setattr(settings, "anthropic_api_key", SecretStr("sk-ant-test"))


# --- triage.assess() ---------------------------------------------------


def test_assess_fails_open_on_http_error(db, monkeypatch):
    _enable(monkeypatch)

    def boom(sb, ut):
        raise RuntimeError("network down")

    monkeypatch.setattr(triage, "_call_triage", boom)
    out = triage.assess(db)
    assert out == {"act": True, "reason": "triage_error", "ok": False}
    # the call never even reached record_cost — no ledger row for a pure network failure
    assert db.query(om.OpusCostLedger).filter(om.OpusCostLedger.purpose == "triage").count() == 0


def test_assess_parses_and_meters_cost_at_triage_prices(db, monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setattr(settings, "opus_triage_price_in_per_mtok", 1.0)
    monkeypatch.setattr(settings, "opus_triage_price_out_per_mtok", 5.0)
    monkeypatch.setattr(settings, "opus_cost_multiplier", 2.0)
    reply = json.dumps({"act": False, "reason": "nothing changed"})
    monkeypatch.setattr(
        triage, "_call_triage",
        lambda sb, ut: (reply, {"input_tokens": 1_000_000, "output_tokens": 0}),
    )
    out = triage.assess(db)
    assert out == {"act": False, "reason": "nothing changed", "ok": True}
    row = db.query(om.OpusCostLedger).filter(om.OpusCostLedger.purpose == "triage").one()
    # 1M input tokens @ the Haiku price ($1/Mtok) = 1 raw -> x2 billed = 2.0 (NOT the Opus price)
    assert abs(row.billed_cost - 2.0) < 1e-9


def test_assess_bad_json_fails_open_but_still_meters_cost(db, monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setattr(
        triage, "_call_triage",
        lambda sb, ut: ("not json at all", {"input_tokens": 10, "output_tokens": 2}),
    )
    out = triage.assess(db)
    assert out["act"] is True and out["ok"] is False and out["reason"] == "triage_error"
    # the HTTP call itself succeeded (usage was real), so cost IS metered even on parse failure
    assert db.query(om.OpusCostLedger).filter(om.OpusCostLedger.purpose == "triage").count() == 1


# --- loop.py wiring ------------------------------------------------------


def test_loop_holds_on_triage_no_and_does_not_bump_last_decision(db, monkeypatch):
    monkeypatch.setattr(settings, "opus_mode", True)
    monkeypatch.setattr(settings, "anthropic_api_key", SecretStr("k"))
    monkeypatch.setattr(settings, "opus_interval_min", 5)
    monkeypatch.setattr(settings, "opus_triage_enabled", True)
    monkeypatch.setattr(settings, "opus_max_decision_gap_min", 60)
    last_iso = (datetime.utcnow() - timedelta(minutes=10)).isoformat()  # due, but recent
    runtime.set(db, "opus_last_decision_at", last_iso)

    monkeypatch.setattr(triage, "assess", lambda _db: {"act": False, "reason": "quiet", "ok": True})
    called = {"n": 0}
    monkeypatch.setattr(brain, "decide", lambda _db: called.__setitem__("n", called["n"] + 1) or {})

    out = loop.tick(db)
    assert out["skipped"] == "triage_hold"
    assert called["n"] == 0  # the paid brain was never invoked
    assert runtime.get(db, "opus_last_decision_at") == last_iso  # untouched by the hold


def test_loop_forces_full_decision_when_max_gap_exceeded(db, monkeypatch):
    monkeypatch.setattr(settings, "opus_mode", True)
    monkeypatch.setattr(settings, "opus_shadow", True)
    monkeypatch.setattr(settings, "anthropic_api_key", SecretStr("k"))
    monkeypatch.setattr(settings, "opus_interval_min", 5)
    monkeypatch.setattr(settings, "opus_triage_enabled", True)
    monkeypatch.setattr(settings, "opus_max_decision_gap_min", 30)
    runtime.set(db, "opus_last_decision_at",
                (datetime.utcnow() - timedelta(minutes=45)).isoformat())  # past the 30min gap

    monkeypatch.setattr(triage, "assess", lambda _db: {"act": False, "reason": "quiet", "ok": True})
    reply = json.dumps({"intents": [{"action": "hold", "reason": "thin"}]})
    monkeypatch.setattr(brain, "_call_opus",
                        lambda sb, ut: (reply, {"input_tokens": 10, "output_tokens": 2}))

    out = loop.tick(db)
    assert out.get("intents") == 1  # the full decision ran despite triage saying "no"


def test_loop_calls_decide_when_triage_says_act(db, monkeypatch):
    monkeypatch.setattr(settings, "opus_mode", True)
    monkeypatch.setattr(settings, "opus_shadow", True)
    monkeypatch.setattr(settings, "anthropic_api_key", SecretStr("k"))
    monkeypatch.setattr(settings, "opus_interval_min", 5)
    monkeypatch.setattr(settings, "opus_triage_enabled", True)
    runtime.set(db, "opus_last_decision_at", (datetime.utcnow() - timedelta(hours=1)).isoformat())

    monkeypatch.setattr(triage, "assess",
                        lambda _db: {"act": True, "reason": "position needs exit", "ok": True})
    reply = json.dumps({"intents": [{"action": "hold", "reason": "thin"}]})
    monkeypatch.setattr(brain, "_call_opus",
                        lambda sb, ut: (reply, {"input_tokens": 10, "output_tokens": 2}))

    out = loop.tick(db)
    assert out.get("intents") == 1


def test_loop_triage_disabled_behaves_as_before(db, monkeypatch):
    """opus_triage_enabled=False (default) → triage.assess is never even consulted."""
    monkeypatch.setattr(settings, "opus_mode", True)
    monkeypatch.setattr(settings, "opus_shadow", True)
    monkeypatch.setattr(settings, "anthropic_api_key", SecretStr("k"))
    monkeypatch.setattr(settings, "opus_interval_min", 5)
    monkeypatch.setattr(settings, "opus_triage_enabled", False)
    runtime.set(db, "opus_last_decision_at", (datetime.utcnow() - timedelta(hours=1)).isoformat())

    called = {"n": 0}

    def fake_assess(_db):
        called["n"] += 1
        return {"act": False, "reason": "should never be called", "ok": True}

    monkeypatch.setattr(triage, "assess", fake_assess)
    reply = json.dumps({"intents": [{"action": "hold", "reason": "thin"}]})
    monkeypatch.setattr(brain, "_call_opus",
                        lambda sb, ut: (reply, {"input_tokens": 10, "output_tokens": 2}))

    out = loop.tick(db)
    assert called["n"] == 0
    assert out.get("intents") == 1


# --- knob round-trip ------------------------------------------------------


def test_triage_knobs_persist(db):
    """The 5 new knobs round-trip through set_kss_settings + a boot-restore, same path as
    opus_daily_loss_stop_pct (test_opus_godmode.py's sibling)."""
    values = {
        "opus_triage_enabled": True,
        "opus_triage_model": "claude-haiku-4-5-20251001",
        "opus_triage_price_in_per_mtok": 2.0,
        "opus_triage_price_out_per_mtok": 8.0,
        "opus_max_decision_gap_min": 30,
    }
    runtime.set_kss_settings(db, values)
    assert settings.opus_triage_enabled is True
    assert settings.opus_triage_price_in_per_mtok == 2.0
    assert settings.opus_triage_price_out_per_mtok == 8.0
    assert settings.opus_max_decision_gap_min == 30

    # Simulate a fresh process boot: reset to defaults, then restore from runtime_config.
    settings.opus_triage_enabled = False
    settings.opus_max_decision_gap_min = 60
    runtime.sync_from_db(db)
    assert settings.opus_triage_enabled is True
    assert settings.opus_max_decision_gap_min == 30


def test_kss_settings_body_accepts_triage_knobs():
    """Regression: the API/form body must include the new knobs, else
    model_dump(exclude_none=True) silently drops them."""
    from app.routes import KssSettingsBody

    dumped = KssSettingsBody(
        opus_triage_enabled=True, opus_max_decision_gap_min=45,
    ).model_dump(exclude_none=True)
    assert dumped["opus_triage_enabled"] is True
    assert dumped["opus_max_decision_gap_min"] == 45
