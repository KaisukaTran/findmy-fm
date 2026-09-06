"""
Grok is asked for events now, not for the technicals the scanner already computed.

MEASURED, on the 5,768 verdicts the old prompt produced in the paper book (2026-09-06):
96.2% of the stated reasons cite indicators the app had calculated and handed over, and **0.0%
mention a news event, an unlock, an exploit, a listing, a regulator or sentiment** — with live
search available the whole time. The most common veto reason, verbatim, is "overbought bb_pct>1".

That was obedience, not echo: the old system prompt cast Grok as "the technical-analysis
gatekeeper" and spelled the rule out — "VETO only on a CONCRETE red flag: overbought (rsi>75 or
bb_pct>1)". It answered the question we asked. Its judgment was never tested.

The new prompt asks only for what a price series cannot contain and adds the verdict the old one
made unsayable: ABSTAIN. These tests pin the two properties that make the shadow measurement
meaningful — an abstention can never block a candidate, and a malformed answer can never veto.
"""

from __future__ import annotations

import json

from app.orchestrator import grok


def _parse(reviews: list[dict]) -> dict[str, dict]:
    return grok._parse_reviews(json.dumps({"reviews": reviews}))


class TestAbstainIsSayableAndHarmless:
    def test_abstain_never_blocks_a_candidate(self):
        out = _parse([{"symbol": "SOL", "verdict": "abstain", "reason": "no information"}])
        assert out["SOL"]["verdict"] == "abstain"
        assert out["SOL"]["endorse"] is True, "not knowing something is not evidence against it"

    def test_a_veto_still_blocks(self):
        out = _parse([{"symbol": "ARB", "verdict": "veto", "reason": "unlock 2026-09-16, 92M ARB"}])
        assert out["ARB"]["verdict"] == "veto"
        assert out["ARB"]["endorse"] is False

    def test_an_endorse_is_kept_distinct_from_an_abstention(self):
        # The measurement depends on telling "had information and used it" from "had nothing".
        out = _parse([{"symbol": "TIA", "verdict": "endorse", "reason": "mainnet upgrade shipped"},
                      {"symbol": "XLM", "verdict": "abstain", "reason": ""}])
        assert out["TIA"]["endorse"] is out["XLM"]["endorse"] is True
        assert out["TIA"]["verdict"] != out["XLM"]["verdict"]


class TestAMalformedAnswerCannotVeto:
    def test_an_unknown_verdict_abstains(self):
        out = _parse([{"symbol": "SOL", "verdict": "maybe?", "reason": "x"}])
        assert out["SOL"]["verdict"] == "abstain"
        assert out["SOL"]["endorse"] is True

    def test_a_missing_verdict_abstains(self):
        out = _parse([{"symbol": "SOL", "reason": "x"}])
        assert out["SOL"]["verdict"] == "abstain"
        assert out["SOL"]["endorse"] is True

    def test_the_legacy_boolean_shape_still_parses(self):
        # The old prompt answered {"endorse": true|false}; verdicts already in the book, and any
        # model that ignores the new schema, must keep their meaning.
        out = _parse([{"symbol": "A", "endorse": False, "reason": "r"},
                      {"symbol": "B", "endorse": True, "reason": "r"}])
        assert (out["A"]["verdict"], out["A"]["endorse"]) == ("veto", False)
        assert (out["B"]["verdict"], out["B"]["endorse"]) == ("endorse", True)


class TestTheQuestionWeAsk:
    """The defect was in the prompt, so the prompt is what regresses. Pin it."""

    def test_the_rule_that_produced_the_echo_is_gone(self):
        # "bb_pct>1" was written by us, then quoted back at us 4,123 times.
        assert "bb_pct>1" not in grok._SCANNER_SYSTEM
        assert "rsi>75" not in grok._SCANNER_SYSTEM
        assert "technical-analysis gatekeeper" not in grok._SCANNER_SYSTEM

    def test_it_asks_for_what_a_chart_cannot_show(self):
        p = grok._SCANNER_SYSTEM.lower()
        for word in ("unlock", "exploit", "delisting", "regulatory", "abstain"):
            assert word in p, word

    def test_the_old_prompt_is_kept_as_the_record(self):
        # It is the evidence for the 96.2% finding; deleting it would erase what was measured.
        assert "bb_pct>1" in grok._SCANNER_SYSTEM_TA_LEGACY
