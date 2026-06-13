"""BacktestAgent — turns the historical KSS win-rate into the dominant safety vote."""

from __future__ import annotations

from app.agents.base import AgentVote, Candle, clamp


class BacktestAgent:
    name = "backtest"

    def evaluate(self, symbol: str, candles: list[Candle], ctx: dict) -> AgentVote:
        wr = ctx.get("win_rate")
        if wr is None:
            return AgentVote(self.name, 0.0, 0.0, "no backtest")
        # Score from the Wilson lower bound (win_rate_lb) when available — aligns the
        # consensus vote with the same conservative estimate that the hard gates use.
        # Falls back to the point win_rate when lb is absent or None (e.g. legacy ctx).
        _lb = ctx.get("win_rate_lb")
        wr_lb: float = _lb if isinstance(_lb, (int, float)) else wr
        trials = ctx.get("trials", 0)
        avg_days = ctx.get("avg_days_to_tp")
        score = clamp(wr_lb / 100)
        conf = clamp(trials / 30)  # more historical trials -> more trustworthy
        days = f", avg {avg_days}d to TP" if avg_days is not None else ""
        return AgentVote(self.name, score, conf,
                         f"backtest win-rate {wr:.1f}% (lb={wr_lb:.1f}%) over {trials} trials{days}")
