"""LiquidityAgent — prefers pairs with healthy traded notional (easy to fill/exit)."""

from __future__ import annotations

from app.agents.base import AgentVote, Candle, clamp

# Target average per-bar notional ($) above which liquidity is considered ample.
_TARGET_NOTIONAL = 1_000_000.0


class LiquidityAgent:
    name = "liquidity"

    def evaluate(self, symbol: str, candles: list[Candle], ctx: dict) -> AgentVote:
        window = candles[-20:]
        if not window:
            return AgentVote(self.name, 0.0, 0.1, "no data")
        avg_notional = sum(c["close"] * c["volume"] for c in window) / len(window)
        score = clamp(avg_notional / _TARGET_NOTIONAL)
        any_vol = any(c["volume"] for c in window)
        conf = min(1.0, (len(window) / 20.0) * (1.0 if any_vol else 0.3))
        return AgentVote(self.name, score, conf, f"avg notional/bar=${avg_notional:,.0f}")
