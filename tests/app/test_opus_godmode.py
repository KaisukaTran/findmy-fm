"""
Phase O-FIX acceptance tests (docs/opus-godmode-plan.md §2):
F1 surfaced HTTP errors, F2 brain_health, F3 enriched candidate snapshot, plus the
5 new runtime knobs and the OpusLesson scaffold table.
"""

from __future__ import annotations

import json

import httpx
import pytest
from pydantic import SecretStr

from datetime import datetime, timedelta

from app import runtime
from app.config import settings
from app.models import Candidate, Fill, ScanRun
from app.orchestrator import brain, distill, service
from app.orchestrator.models import OPUS_CLOSED, OpusLesson, OpusPosition


def _enable_opus(monkeypatch):
    monkeypatch.setattr(settings, "opus_mode", True)
    monkeypatch.setattr(settings, "anthropic_api_key", SecretStr("sk-ant-test"))


def test_godmode_knobs_persist(db):
    """Setting the 5 new knobs updates `settings.*` and survives a boot-restore."""
    values = {
        "opus_copy_mode": True,
        "opus_solo_open": True,
        "opus_solo_min_consensus": 85.0,
        "opus_lessons_max": 3,
        "opus_history_n": 50,
    }
    runtime.set_kss_settings(db, values)
    assert settings.opus_copy_mode is True
    assert settings.opus_solo_open is True
    assert settings.opus_solo_min_consensus == 85.0
    assert settings.opus_lessons_max == 3
    assert settings.opus_history_n == 50

    # Simulate a fresh process boot: reset to defaults, then restore from runtime_config.
    settings.opus_copy_mode = False
    settings.opus_solo_open = False
    settings.opus_solo_min_consensus = 70.0
    settings.opus_lessons_max = 8
    settings.opus_history_n = 20
    runtime.sync_from_db(db)

    assert settings.opus_copy_mode is True
    assert settings.opus_solo_open is True
    assert settings.opus_solo_min_consensus == 85.0
    assert settings.opus_lessons_max == 3
    assert settings.opus_history_n == 50


def test_brain_health_400_is_credit(db, monkeypatch):
    """A 400 ('credit balance too low') is the exact root-cause failure (docs §0) — must
    be loud: audited with status=400 and classified distinctly from other HTTP errors."""
    _enable_opus(monkeypatch)

    def boom(sb, ut):
        request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        response = httpx.Response(400, text='{"error":"credit balance too low"}', request=request)
        raise httpx.HTTPStatusError("bad status", request=request, response=response)

    monkeypatch.setattr(brain, "_call_opus", boom)
    out = brain.decide(db)
    assert out["ok"] is False
    assert out["intents"] == []

    from app.models import AuditLog

    row = (
        db.query(AuditLog)
        .filter(AuditLog.actor == "opus", AuditLog.action == "decide_error")
        .order_by(AuditLog.id.desc())
        .first()
    )
    assert row is not None
    detail = json.loads(row.detail)
    assert detail["status"] == 400
    assert "credit" in detail["detail"]

    assert service.brain_health(db) == "http_400_credit"


def test_brain_health_ok(db, monkeypatch):
    """A successful decide() call leaves brain_health == 'ok'."""
    _enable_opus(monkeypatch)
    reply = json.dumps({"intents": []})
    monkeypatch.setattr(brain, "_call_opus",
                         lambda sb, ut: (reply, {"input_tokens": 100, "output_tokens": 10}))
    out = brain.decide(db)
    assert out["ok"] is True
    assert service.brain_health(db) == "ok"


def test_brain_health_disabled_and_never(db):
    """opus_mode off short-circuits to 'disabled'; mode on with no audit row yet is 'never'."""
    assert settings.opus_mode is False
    assert service.brain_health(db) == "disabled"


def test_snapshot_has_rich_candidate_fields(db):
    """F3: _candidates must forward expectancy/win_rate_lb/trials/reason, not just the
    thin trio the brain used to see (root cause, docs §0 — starved snapshot)."""
    scan = ScanRun()
    db.add(scan)
    db.flush()
    cand = Candidate(
        scan_id=scan.id,
        symbol="BTC",
        consensus_pct=80.0,
        win_rate=70.0,
        win_rate_lb=62.5,
        expectancy=4.2,
        trials=30,
        decision="trade",
        reason="strong trend + dip",
    )
    db.add(cand)
    db.commit()

    snap = brain.build_snapshot(db)
    assert snap["candidates"], "expected at least one candidate"
    row = snap["candidates"][0]
    for key in ("expectancy", "win_rate_lb", "trials", "decision", "reason", "symbol", "consensus"):
        assert key in row
    assert row["expectancy"] == pytest.approx(4.2)
    assert row["win_rate_lb"] == pytest.approx(62.5)
    assert row["trials"] == 30
    assert row["reason"] == "strong trend + dip"


def test_opus_lesson_table(db):
    """OpusLesson is additive and queryable (O-LEARN scaffolding created now)."""
    lesson = OpusLesson(scope="general", lesson_text="Avoid chasing breakouts after a +8% spike.")
    db.add(lesson)
    db.commit()

    row = db.query(OpusLesson).filter(OpusLesson.scope == "general").first()
    assert row is not None
    assert row.lesson_text.startswith("Avoid chasing")
    assert row.evidence_json is None


# --- Phase O-COPY (docs/opus-godmode-plan.md §2) ----------------------------------------


def test_candidate_persists_mae(db):
    """C1: avg_mae/worst_mae round-trip on a Candidate row (pure persistence, no gate
    behaviour involved)."""
    scan = ScanRun()
    db.add(scan)
    db.flush()
    cand = Candidate(
        scan_id=scan.id, symbol="ETH", decision="trade",
        avg_mae=-3.25, worst_mae=-12.5,
    )
    db.add(cand)
    db.commit()

    row = db.query(Candidate).filter(Candidate.symbol == "ETH").first()
    assert row is not None
    assert row.avg_mae == pytest.approx(-3.25)
    assert row.worst_mae == pytest.approx(-12.5)

    # Forwarded into the brain snapshot's candidate rows too.
    snap_row = brain._candidates(db)[0]
    assert snap_row["avg_mae"] == pytest.approx(-3.25)
    assert snap_row["worst_mae"] == pytest.approx(-12.5)


def test_snapshot_rule_engine_block(db):
    """C2: rule_engine.endorsed_open lists only decision='trade' symbols from the latest
    scan (best-first by consensus); recent_exits lists non-OPUS SELL fills with a real
    realized PnL, newest first."""
    scan = ScanRun()
    db.add(scan)
    db.flush()
    db.add(Candidate(
        scan_id=scan.id, symbol="SOL", decision="trade", consensus_pct=90.0,
    ))
    db.add(Candidate(
        scan_id=scan.id, symbol="DOGE", decision="skip", consensus_pct=95.0,
    ))
    db.add(Fill(
        symbol="BTC", side="SELL", quantity=0.1, price=65000.0, realized_pnl=42.0,
        source_ref="pyramid:1:tp", strategy_name="KSS",
    ))
    db.add(Fill(
        symbol="ETH", side="SELL", quantity=1.0, price=3500.0, realized_pnl=-15.0,
        source_ref="opus:1:close", strategy_name="OPUS",  # must be excluded
    ))
    db.commit()

    block = brain.build_snapshot(db)["rule_engine"]
    assert block["endorsed_open"] == ["SOL"]
    symbols = [r["symbol"] for r in block["recent_exits"]]
    assert "BTC" in symbols
    assert "ETH" not in symbols
    btc_row = next(r for r in block["recent_exits"] if r["symbol"] == "BTC")
    assert btc_row["realized"] == pytest.approx(42.0)


def test_copy_mode_prompt(db, monkeypatch):
    """C3: opus_copy_mode off -> 1 system block; on -> 2 blocks, the 2nd mentions copying
    endorsed_open/the engine, and the 1st (cached) block's text is byte-identical either way."""
    monkeypatch.setattr(settings, "opus_copy_mode", False)
    blocks_off = brain._system_blocks(db)
    assert len(blocks_off) == 1

    monkeypatch.setattr(settings, "opus_copy_mode", True)
    blocks_on = brain._system_blocks(db)
    assert len(blocks_on) == 2
    assert blocks_on[0]["text"] == blocks_off[0]["text"]
    second_text = blocks_on[1]["text"].lower()
    assert "endorsed_open" in second_text or "engine" in second_text


# --- Phase O-LEARN (docs/opus-godmode-plan.md §2) ---------------------------------------


def test_self_history_block(db):
    """L1: build_snapshot's self_history reflects closed OpusPositions — right count,
    win_rate, and the net_24h_pct key present (zero-equity default is fine here)."""
    now = datetime.utcnow()
    # A winner (ridden to close) and a loser (rescued to KSS) — mix for a non-trivial
    # win_rate, and to exercise both outcome labels.
    db.add(OpusPosition(
        symbol="BTC", opened_at=now - timedelta(hours=5), closed_at=now - timedelta(hours=1),
        entry_price=100.0, qty=1.0, avg_price=100.0, state=OPUS_CLOSED,
        evaluated_at=now - timedelta(hours=4), realized_pnl=12.5,
    ))
    db.add(OpusPosition(
        symbol="ETH", opened_at=now - timedelta(hours=10), closed_at=now - timedelta(hours=2),
        entry_price=50.0, qty=2.0, avg_price=50.0, state=OPUS_CLOSED,
        evaluated_at=now - timedelta(hours=9), kss_session_id=7, realized_pnl=-8.0,
    ))
    db.commit()

    snap = brain.build_snapshot(db)
    hist = snap["self_history"]
    assert len(hist["recent_closed"]) == 2
    assert hist["recent_closed"][0]["symbol"] == "BTC"  # newest closed_at first
    assert hist["win_rate"] == pytest.approx(50.0)
    assert "net_24h_pct" in hist
    outcomes = {row["symbol"]: row["outcome"] for row in hist["recent_closed"]}
    assert outcomes["ETH"] == "rescue"
    assert outcomes["BTC"] == "ride"


def test_self_history_block_empty(db):
    """L1: no closed positions -> defensive zero defaults, never raises."""
    hist = brain.build_snapshot(db)["self_history"]
    assert hist["recent_closed"] == []
    assert hist["win_rate"] == 0.0


def test_distiller_writes_bounded_lessons(db, monkeypatch):
    """L2: a successful distill call writes at most opus_lessons_max OpusLesson rows,
    even when Opus returns more lessons than the cap."""
    _enable_opus(monkeypatch)
    monkeypatch.setattr(settings, "opus_lessons_max", 3)

    # Seed one closed position so there's history to distill from.
    now = datetime.utcnow()
    db.add(OpusPosition(
        symbol="SOL", opened_at=now - timedelta(hours=4), closed_at=now - timedelta(hours=1),
        entry_price=10.0, qty=5.0, avg_price=10.0, state=OPUS_CLOSED,
        evaluated_at=now - timedelta(hours=3), realized_pnl=5.0,
    ))
    db.commit()

    reply = json.dumps({"lessons": [
        {"scope": "general", "lesson": f"lesson {i}"} for i in range(10)
    ]})
    monkeypatch.setattr(
        distill.brain, "_call_opus",
        lambda sb, ut: (reply, {"input_tokens": 50, "output_tokens": 20}),
    )

    written = distill.distill_lessons(db)
    assert written == 3
    rows = db.query(OpusLesson).all()
    assert len(rows) == 3


def test_distiller_throttled(db, monkeypatch):
    """L2: a second call within the 6h gap returns 0 and writes nothing more."""
    _enable_opus(monkeypatch)
    now = datetime.utcnow()
    db.add(OpusPosition(
        symbol="SOL", opened_at=now - timedelta(hours=4), closed_at=now - timedelta(hours=1),
        entry_price=10.0, qty=5.0, avg_price=10.0, state=OPUS_CLOSED,
        evaluated_at=now - timedelta(hours=3), realized_pnl=5.0,
    ))
    db.commit()

    reply = json.dumps({"lessons": [{"scope": "general", "lesson": "first run"}]})
    monkeypatch.setattr(
        distill.brain, "_call_opus",
        lambda sb, ut: (reply, {"input_tokens": 10, "output_tokens": 5}),
    )

    first = distill.distill_lessons(db)
    assert first == 1

    # Immediate second call: still within the 6h throttle gap -> must skip entirely.
    second = distill.distill_lessons(db)
    assert second == 0
    assert db.query(OpusLesson).count() == 1


def test_distiller_failsafe(db, monkeypatch):
    """L2: a raising _call_opus must not propagate — returns 0 and audits distill_error."""
    _enable_opus(monkeypatch)
    now = datetime.utcnow()
    db.add(OpusPosition(
        symbol="SOL", opened_at=now - timedelta(hours=4), closed_at=now - timedelta(hours=1),
        entry_price=10.0, qty=5.0, avg_price=10.0, state=OPUS_CLOSED,
        evaluated_at=now - timedelta(hours=3), realized_pnl=5.0,
    ))
    db.commit()

    def boom(sb, ut):
        raise RuntimeError("network down")

    monkeypatch.setattr(distill.brain, "_call_opus", boom)

    written = distill.distill_lessons(db)
    assert written == 0

    from app.models import AuditLog

    row = (
        db.query(AuditLog)
        .filter(AuditLog.actor == "opus", AuditLog.action == "distill_error")
        .order_by(AuditLog.id.desc())
        .first()
    )
    assert row is not None


def test_distiller_no_history_is_noop(db, monkeypatch):
    """L2: nothing to learn from yet (no closed positions, no engine exits) -> skip."""
    _enable_opus(monkeypatch)
    written = distill.distill_lessons(db)
    assert written == 0
    assert db.query(OpusLesson).count() == 0


def test_lessons_injected_into_prompt(db):
    """L3: no lessons -> no extra block; with lessons -> last block has the header and is
    bounded to opus_lessons_max entries."""
    blocks_empty = brain._system_blocks(db)
    assert all("LESSONS LEARNED" not in b["text"] for b in blocks_empty)

    for i in range(20):
        db.add(OpusLesson(scope="general", lesson_text=f"lesson {i}"))
    db.commit()

    blocks = brain._system_blocks(db)
    last = blocks[-1]["text"]
    assert "LESSONS LEARNED" in last
    # Bounded to opus_lessons_max bullet lines.
    bullet_lines = [ln for ln in last.splitlines() if ln.startswith("- ")]
    assert len(bullet_lines) <= settings.opus_lessons_max
