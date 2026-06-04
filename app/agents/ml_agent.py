"""ML agent: wraps the Phase-C logistic-regression win-rate model as a vote."""

from __future__ import annotations

from app import ml
from app.agents.base import AgentVote
from app.data.providers import Candle


class MlAgent:
    """Agent that calls ml.predict and wraps the result as an AgentVote.

    When ML is disabled or no model exists, predict() returns (0.5, 0.0) so
    this vote carries zero weight in the aggregator — completely neutral.
    """

    name = "ml"

    def evaluate(self, symbol: str, candles: list[Candle], ctx: dict) -> AgentVote:
        """Return a vote backed by the trained logistic-regression model."""
        score, conf = ml.predict(candles, model=ctx.get("ml_model"))
        return AgentVote("ml", score, conf, reason=f"ml p(win)={score:.2f}")
