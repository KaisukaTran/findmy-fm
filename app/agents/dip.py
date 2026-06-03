"""DipAgent — likes oversold-but-not-collapsing entries (good KSS timing)."""

from __future__ import annotations

from app.agents.base import AgentVote, Candle, clamp, closes, rsi, triangular


class DipAgent:
    name = "dip"

    def evaluate(self, symbol: str, candles: list[Candle], ctx: dict) -> AgentVote:
        cs = closes(candles)
        if len(cs) < 15:
            return AgentVote(self.name, 0.0, 0.1, "insufficient data")
        r = rsi(cs, 14)
        # RSI sweet spot ~40 (a dip), bad when overbought (>70) or knife-falling (<15).
        rsi_score = triangular(r, lo=15, peak=40, hi=70)
        recent_high = max(cs[-30:])
        pull = cs[-1] / recent_high if recent_high else 1.0
        # A modest pullback (~7% off the high) is ideal; a tiny or huge drop is not.
        pull_score = triangular(pull, lo=0.70, peak=0.93, hi=1.00)
        score = 0.7 * rsi_score + 0.3 * pull_score
        return AgentVote(self.name, clamp(score), clamp(len(cs) / 30),
                         f"RSI14={r:.1f}, pullback={(1 - pull) * 100:.1f}% off high")
