"""
Tests for the 2026-09-06 consensus-weight redistribution.

`ml` carried 0.30 — the largest share — while `ml_enabled` was False everywhere, so `MlAgent`
returned confidence 0 and its term dropped out of BOTH sides of the weighted mean. The
consensus that leads `_open_rank_key` was already dip/trend/volatility/liquidity renormalised
over 0.70; the config simply did not say so.

Two things must hold, and they pull in opposite directions:

  1. Redistributing in the SAME proportions must reproduce today's consensus EXACTLY. This is
     a book-keeping correction, not a strategy change, and it ships to a live instance.
  2. The trapdoor must be closed: with the old table, enabling ML would have handed 30% of the
     ranking to an unmeasured model on the very next scan. With the new one it gets nothing
     until someone gives it weight deliberately.
"""

from __future__ import annotations

import pytest

from app.agents.aggregator import DEFAULT_WEIGHTS, aggregate
from app.agents.base import AgentVote

OLD_WEIGHTS = {
    "backtest": 0.0, "dip": 0.25, "trend": 0.20,
    "volatility": 0.15, "liquidity": 0.10, "ml": 0.30,
}


def votes(dip: float, trend: float, vol: float, liq: float,
          ml_score: float = 0.9, ml_conf: float = 0.0) -> list[AgentVote]:
    """A realistic ballot. `ml_conf=0` is what MlAgent returns while ml_enabled is False."""
    return [
        AgentVote("dip", dip, 1.0, ""),
        AgentVote("trend", trend, 1.0, ""),
        AgentVote("volatility", vol, 1.0, ""),
        AgentVote("liquidity", liq, 1.0, ""),
        AgentVote("ml", ml_score, ml_conf, ""),
        AgentVote("backtest", 0.8, 0.9, ""),
    ]


class TestWeightsAreCoherent:
    def test_the_live_agents_sum_to_one(self):
        assert round(sum(v for k, v in DEFAULT_WEIGHTS.items() if k != "backtest"), 3) == 1.0

    def test_ml_no_longer_holds_the_largest_share_of_a_disabled_agent(self):
        assert DEFAULT_WEIGHTS["ml"] == 0.0
        assert DEFAULT_WEIGHTS["dip"] == max(
            v for k, v in DEFAULT_WEIGHTS.items() if k != "backtest")


class TestConsensusIsUnchanged:
    @pytest.mark.parametrize("ballot", [
        (0.9, 0.7, 0.6, 0.8),
        (0.1, 0.2, 0.3, 0.4),
        (0.55, 0.55, 0.55, 0.55),
        (1.0, 0.0, 1.0, 0.0),
    ])
    def test_same_number_to_the_last_decimal_while_ml_is_disabled(self, ballot):
        v = votes(*ballot)
        assert aggregate(v, OLD_WEIGHTS) == aggregate(v, DEFAULT_WEIGHTS)

    def test_holds_when_other_agents_have_partial_confidence(self):
        # Confidence is a second weight, so the equality must survive it being uneven.
        v = [
            AgentVote("dip", 0.9, 0.4, ""),
            AgentVote("trend", 0.3, 1.0, ""),
            AgentVote("volatility", 0.6, 0.2, ""),
            AgentVote("liquidity", 0.8, 0.75, ""),
            AgentVote("ml", 0.95, 0.0, ""),
            AgentVote("backtest", 0.8, 0.9, ""),
        ]
        assert aggregate(v, OLD_WEIGHTS) == aggregate(v, DEFAULT_WEIGHTS)


class TestTheTrapdoorIsClosed:
    def test_enabling_ml_used_to_seize_30_percent_and_now_seizes_nothing(self):
        # An ML model that suddenly starts voting with full confidence and a maximal score.
        v = votes(0.4, 0.4, 0.4, 0.4, ml_score=1.0, ml_conf=1.0)
        muted = votes(0.4, 0.4, 0.4, 0.4, ml_score=1.0, ml_conf=0.0)

        # Old table: the same ballot jumps by ~18 consensus points the moment ML wakes up.
        assert aggregate(v, OLD_WEIGHTS) - aggregate(muted, OLD_WEIGHTS) > 15
        # New table: nothing moves until someone gives ML weight on purpose.
        assert aggregate(v, DEFAULT_WEIGHTS) == aggregate(muted, DEFAULT_WEIGHTS)
