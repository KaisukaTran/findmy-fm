"""TrendAgent — favors a controlled uptrend/range, penalizes free-fall and blow-off tops."""

from __future__ import annotations

from app.agents.base import AgentVote, Candle, clamp, closes, sma, triangular


class TrendAgent:
    name = "trend"

    def evaluate(self, symbol: str, candles: list[Candle], ctx: dict) -> AgentVote:
        cs = closes(candles)
        if len(cs) < 10:
            return AgentVote(self.name, 0.0, 0.1, "insufficient data")
        ref = sma(cs, 50)
        last = cs[-1]
        ratio = last / ref if ref else 1.0
        # Peak favorability in a gentle uptrend (~+5% over the mean); 0 in deep
        # downtrends (<0.85) or overextended spikes (>1.30).
        score = triangular(ratio, lo=0.85, peak=1.05, hi=1.30)
        conf = clamp(len(cs) / 50)
        return AgentVote(self.name, score, conf,
                         f"price/SMA50={ratio:.3f} (regime {'up' if ratio >= 1 else 'down'})")
