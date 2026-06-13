"""Deterministic quant agents that score a pair's suitability for a KSS session."""

from app.agents.aggregator import DEFAULT_WEIGHTS, aggregate, decide
from app.agents.backtest_agent import BacktestAgent
from app.agents.base import Agent, AgentVote
from app.agents.dip import DipAgent
from app.agents.liquidity import LiquidityAgent
from app.agents.ml_agent import MlAgent
from app.agents.trend import TrendAgent
from app.agents.volatility import VolatilityAgent

# Agents whose votes feed the aggregator (BacktestAgent is added separately as it
# needs the precomputed win-rate in ctx).
SIGNAL_AGENTS: list[Agent] = [TrendAgent(), DipAgent(), VolatilityAgent(), LiquidityAgent(), MlAgent()]

__all__ = [
    "Agent", "AgentVote", "TrendAgent", "DipAgent", "VolatilityAgent",
    "LiquidityAgent", "BacktestAgent", "MlAgent", "SIGNAL_AGENTS", "aggregate", "decide",
    "DEFAULT_WEIGHTS",
]
