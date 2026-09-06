"""
Shadow mode: ask Grok, record the answer, act on none of it.

An LLM cannot be back-tested. Asked "should I have bought SOL on 2025-03-04", a model may
already know how that trade ended — from training data, or from the live search this gate can
use — and it will look brilliant for the wrong reason. The only honest measurement is forward,
and a forward measurement needs a control arm: the scanner must keep deciding exactly as it would
with Grok switched off, while Grok's verdict is written down beside it.

That is what these tests pin. In shadow, a veto is RECORDED and NOT APPLIED — including under
`fail_mode="closed"`, where an unreviewed candidate would otherwise be blocked. If shadow ever
started blocking, the control arm would quietly disappear and the resulting numbers would look
fine while measuring nothing.

Scored afterwards by scripts/grok_shadow_eval.py, which compares arms WITHIN a single scan —
the previous attempt at this question compared two date ranges and called the difference Grok.
"""

from __future__ import annotations

import pytest
from test_scanner import scan_env  # noqa: F401  (fixture: fake provider + neutral gates)

from app import models, scanner
from app.config import settings
from app.orchestrator import grok as grok_mod


@pytest.mark.usefixtures("scan_env")
class TestShadowRecordsButDoesNotAct:
    @staticmethod
    def _grok(monkeypatch, reviews, *, shadow: bool, fail_mode: str = "open"):
        monkeypatch.setattr(grok_mod, "scanner_enabled", lambda: True)
        monkeypatch.setattr(grok_mod, "review_candidates", lambda db, items: reviews)
        monkeypatch.setattr(settings, "grok_scanner_shadow", shadow)
        monkeypatch.setattr(settings, "grok_scanner_fail_mode", fail_mode)

    def _btc(self, db):
        return db.query(models.Candidate).filter_by(symbol="BTC").one()

    def test_a_shadow_veto_is_recorded(self, db, monkeypatch):
        self._grok(monkeypatch, {"BTC": {"endorse": False, "verdict": "veto",
                                         "reason": "unlock 2026-09-16"}}, shadow=True)
        scanner.run_scan(db, mode="semi")
        btc = self._btc(db)
        assert btc.grok_verdict == "veto"
        assert "shadow, not applied" in (btc.reason or "")

    def test_a_shadow_veto_does_not_block_the_session(self, db, monkeypatch):
        # The control arm: with shadow on, the book must look exactly as it does with Grok off.
        self._grok(monkeypatch, {"BTC": {"endorse": False, "verdict": "veto", "reason": "r"}},
                   shadow=True)
        scanner.run_scan(db, mode="semi")
        shadowed = db.query(models.KssSession).filter_by(symbol="BTC").count()

        db.query(models.KssWave).delete()      # waves first: they FK the sessions below
        db.query(models.KssSession).delete()
        db.query(models.Candidate).delete()
        db.commit()
        monkeypatch.setattr(grok_mod, "scanner_enabled", lambda: False)
        scanner.run_scan(db, mode="semi")
        control = db.query(models.KssSession).filter_by(symbol="BTC").count()
        assert shadowed == control, "shadow mode changed what the scanner did"

    def test_the_same_veto_DOES_block_when_shadow_is_off(self, db, monkeypatch):
        # Guard against the fix that quietly disables the gate for everyone.
        self._grok(monkeypatch, {"BTC": {"endorse": False, "verdict": "veto", "reason": "r"}},
                   shadow=False)
        scanner.run_scan(db, mode="semi")
        assert self._btc(db).grok_verdict == "veto"
        assert db.query(models.KssSession).filter_by(symbol="BTC").count() == 0

    def test_closed_fail_mode_is_also_suspended_in_shadow(self, db, monkeypatch):
        # fail_mode="closed" blocks anything without an explicit endorsement. In shadow that
        # would block on Grok's silence — the loudest possible way to lose the control arm.
        self._grok(monkeypatch, {}, shadow=True, fail_mode="closed")
        scanner.run_scan(db, mode="semi")
        assert db.query(models.KssSession).filter_by(symbol="BTC").count() > 0


@pytest.mark.usefixtures("scan_env")
class TestAbstain:
    def test_an_abstention_is_recorded_as_itself_and_opens_normally(self, db, monkeypatch):
        # "I know nothing about this asset" must be distinguishable from "I approve of it" —
        # that distinction is the measurement — while behaving identically: not knowing
        # something is not evidence against it.
        monkeypatch.setattr(grok_mod, "scanner_enabled", lambda: True)
        monkeypatch.setattr(grok_mod, "review_candidates", lambda db, items:
                            {"BTC": {"endorse": True, "verdict": "abstain", "reason": ""}})
        monkeypatch.setattr(settings, "grok_scanner_shadow", False)
        scanner.run_scan(db, mode="semi")
        btc = db.query(models.Candidate).filter_by(symbol="BTC").one()
        assert btc.grok_verdict == "abstain"
        assert db.query(models.KssSession).filter_by(symbol="BTC").count() > 0


@pytest.mark.usefixtures("scan_env")
class TestThePayloadNoLongerCarriesTheAnswer:
    def test_grok_is_not_handed_the_indicators_it_was_told_to_ignore(self, db, monkeypatch):
        # 96.2% of the old verdicts quoted the TA bundle back at us because we shipped it. The
        # new prompt forbids chart reasoning; sending the chart anyway is an invitation to break
        # it, and six times the token cost.
        seen: list[dict] = []

        def _capture(db_, items):
            seen.extend(items)
            return {}

        monkeypatch.setattr(grok_mod, "scanner_enabled", lambda: True)
        monkeypatch.setattr(grok_mod, "review_candidates", _capture)
        monkeypatch.setattr(settings, "grok_scanner_shadow", True)
        scanner.run_scan(db, mode="semi")

        assert seen, "Grok was never called"
        for item in seen:
            assert set(item) == {"symbol", "price"}, item
