"""VolatilityAgent — wants enough movement to reach TP, but not crash-level vol."""

from __future__ import annotations

from app.agents.base import AgentVote, Candle, clamp, closes, realized_vol_pct, triangular


class VolatilityAgent:
    name = "volatility"

    def evaluate(self, symbol: str, candles: list[Candle], ctx: dict) -> AgentVote:
        cs = closes(candles)
        if len(cs) < 10:
            return AgentVote(self.name, 0.0, 0.1, "insufficient data")
        vol = realized_vol_pct(cs, 30)  # daily return stdev, %
        # Too calm -> TP never hit; too wild -> deadline/drawdown risk. Peak ~3%/day.
        score = triangular(vol, lo=0.3, peak=3.0, hi=12.0)
        return AgentVote(self.name, clamp(score), clamp(len(cs) / 30),
                         f"realized vol={vol:.2f}%/bar")
