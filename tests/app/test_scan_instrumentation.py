"""
Tests for the P1 scan instrumentation (2026-09-06).

WHY this file exists — three holes made the book unscoreable, and all three were invisible:

  1. The TA bundle was built ONLY inside `if decision == "trade"`, so every REJECTED coin left
     no evidence at all. "What did the scanner see when it said no?" is precisely the question
     that decides whether a rejection was right, and the data to answer it was discarded.
  2. `ScanRun.params` carried 3 of ~20 knobs, and `_btc_ret` / breadth were computed and thrown
     away — breadth only existed inside the regime ramp, which is off, so no scan in the book
     records what the market was doing. Any study assuming a constant knob set was wrong:
     `min_expectancy_pct` alone moved mid-history.
  3. Grok's verdict lived as free text appended to `reason`, and "no verdict" behaved exactly
     like "endorsed" under fail-open — so a call that TIMED OUT and a call that APPROVED were
     indistinguishable in the book. A gate cannot be measured if its failures look like passes.

None of this changes a trading decision; these tests assert that too.
"""

from __future__ import annotations

import json

import pytest
from test_scanner import scan_env  # noqa: F401  (fixture: fake provider + neutral gates)

from app import models, scanner
from app.config import settings
from app.orchestrator import grok as grok_mod


def _candidates(db):
    return db.query(models.Candidate).all()


@pytest.mark.usefixtures("scan_env")
class TestTaEvidence:
    def test_recorded_for_every_candidate_including_rejected_ones(self, db, monkeypatch):
        # Force a rejection: an unreachable confidence floor makes every candidate a 'skip'.
        monkeypatch.setattr(settings, "min_confidence", 999.0)
        scanner.run_scan(db, mode="semi")

        cands = _candidates(db)
        assert cands, "the scan produced no candidates at all"
        assert {c.decision for c in cands} == {"skip"}
        for c in cands:
            assert c.ta_json, f"{c.symbol} was rejected with no TA evidence recorded"
            ta = json.loads(c.ta_json)
            # The bundle the Grok prompt and every entry veto read from.
            assert {"rsi", "adx", "macd_h", "bb_pct", "atr_pct", "st", "htf"} <= set(ta)

    def test_traded_candidates_keep_the_human_readable_tag(self, db):
        scanner.run_scan(db, mode="semi")
        traded = [c for c in _candidates(db) if c.decision == "trade"]
        assert traded, "fixture should produce at least one tradeable candidate"
        for c in traded:
            assert "TA:" in (c.reason or "")
            assert c.ta_json


@pytest.mark.usefixtures("scan_env")
class TestScanSnapshot:
    def test_params_carry_the_full_knob_set_and_the_market_state(self, db):
        scanner.run_scan(db, mode="semi")
        params = json.loads(db.query(models.ScanRun).one().params)

        # The three that were always there.
        assert {"min_win_rate", "min_confidence", "deadline_days"} <= set(params)
        # The ones whose absence made a historical study unsound.
        for knob in ("min_expectancy_pct", "min_trials", "scan_max_waves", "scan_distance_pct",
                     "max_concurrent_sessions", "rel_strength_enabled", "block_downtrend_adx",
                     "autotune_levels_enabled", "grok_scanner_enabled"):
            assert knob in params, f"{knob} is not snapshotted"
        # Market state, recorded whether or not any gate consumes it.
        assert "btc_ret" in params and "breadth" in params
        assert params["breadth"] is None or 0.0 <= params["breadth"] <= 1.0
        # The weights that decide the ranking are part of the configuration.
        assert isinstance(params["consensus_weights"], dict)
        assert "dip" in params["consensus_weights"]

    def test_breadth_is_recorded_even_though_the_ramp_is_off(self, db, monkeypatch):
        monkeypatch.setattr(settings, "regime_ramp_enabled", False)
        scanner.run_scan(db, mode="semi")
        params = json.loads(db.query(models.ScanRun).one().params)
        assert params["breadth"] is not None


@pytest.mark.usefixtures("scan_env")
class TestGrokVerdictColumn:
    @staticmethod
    def _enable(monkeypatch, reviews):
        # `_review_and_open` imports grok lazily, so the module object is what must be patched.
        monkeypatch.setattr(grok_mod, "scanner_enabled", lambda: True)
        monkeypatch.setattr(grok_mod, "review_candidates", lambda db, items: reviews)

    def test_veto_is_recorded_as_an_enum(self, db, monkeypatch):
        self._enable(monkeypatch, {"BTC": {"endorse": False, "reason": "rsi>75 overbought"}})
        scanner.run_scan(db, mode="semi")
        btc = db.query(models.Candidate).filter_by(symbol="BTC").one()
        assert btc.grok_verdict == "veto"
        assert "Grok veto" in (btc.reason or "")          # free text kept for humans

    def test_endorsement_is_recorded_as_an_enum(self, db, monkeypatch):
        self._enable(monkeypatch, {"BTC": {"endorse": True, "reason": "clean"}})
        scanner.run_scan(db, mode="semi")
        assert db.query(models.Candidate).filter_by(symbol="BTC").one().grok_verdict == "endorse"

    def test_a_failed_call_is_not_recorded_as_an_endorsement(self, db, monkeypatch):
        # An empty review dict is what a timeout, an HTTP error and an unparseable answer all
        # produce. Under fail-open the symbol still opens — but the BOOK must not claim Grok
        # approved it, or the gate's failures are laundered into passes.
        self._enable(monkeypatch, {})
        scanner.run_scan(db, mode="semi")
        btc = db.query(models.Candidate).filter_by(symbol="BTC").one()
        assert btc.grok_verdict in {"unavailable", "absent"}
        assert btc.grok_verdict != "endorse"

    def test_nothing_is_recorded_when_the_gate_is_off(self, db, monkeypatch):
        monkeypatch.setattr(grok_mod, "scanner_enabled", lambda: False)
        scanner.run_scan(db, mode="semi")
        assert db.query(models.Candidate).filter_by(symbol="BTC").one().grok_verdict is None


@pytest.mark.usefixtures("scan_env")
def test_instrumentation_does_not_change_the_decision(db):
    """The evidence is written by code that must not touch the outcome."""
    scanner.run_scan(db, mode="semi")
    before = {(c.symbol, c.decision) for c in _candidates(db)}

    # Same scan, same fake market -> identical decisions. Every trace of the first scan has to
    # go, sessions included: a surviving session pre-blocks its own symbol on the next scan
    # (per-symbol cap), which would look like the instrumentation changed the outcome.
    for model in (models.Candidate, models.ScanRun, models.AgentVoteRecord,
                  models.PendingOrder, models.KssWave, models.KssSession):
        db.query(model).delete()
    db.commit()
    scanner.run_scan(db, mode="semi")
    assert {(c.symbol, c.decision) for c in _candidates(db)} == before
