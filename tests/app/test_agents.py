"""Tests for quant agents and the aggregator (deterministic, synthetic candles)."""

from app.agents import (
    BacktestAgent,
    DipAgent,
    LiquidityAgent,
    TrendAgent,
    VolatilityAgent,
    aggregate,
    decide,
)
from app.agents.base import AgentVote

_DAY = 86_400_000


def candle(day, close, vol=100000.0, high=None, low=None):
    return {"ts": day * _DAY, "open": close, "high": high or close * 1.001,
            "low": low if low is not None else close * 0.999, "close": close, "volume": vol}


def series(changes, start=100.0, vol=100000.0):
    out, price = [], start
    for d, ch in enumerate(changes):
        price *= (1 + ch)
        out.append(candle(d, price, vol=vol))
    return out


UP = series([0.01] * 60)
UP_GENTLE = series([0.004] * 60)  # controlled uptrend (agent's sweet spot)
DOWN = series([-0.02] * 60)
FLAT = series([0.0] * 60)
CHOPPY = series([0.03 if i % 2 == 0 else -0.03 for i in range(60)])
DIP = series([0.01] * 50 + [-0.014] * 8)  # rally then a ~10% pullback


def test_trend_up_vs_down():
    up = TrendAgent().evaluate("BTC", UP_GENTLE, {}).score
    down = TrendAgent().evaluate("BTC", DOWN, {}).score
    assert up > 0.4 and down < 0.2


def test_dip_prefers_pullback():
    assert DipAgent().evaluate("BTC", DIP, {}).score > DipAgent().evaluate("BTC", UP, {}).score


def test_volatility_band():
    calm = VolatilityAgent().evaluate("BTC", FLAT, {}).score
    lively = VolatilityAgent().evaluate("BTC", CHOPPY, {}).score
    assert calm < 0.2 and lively > calm


def test_liquidity_scales_with_volume():
    rich = LiquidityAgent().evaluate("BTC", series([0.0] * 30, vol=1e6), {}).score
    poor = LiquidityAgent().evaluate("BTC", series([0.0] * 30, vol=0.0), {}).score
    assert rich > poor and poor == 0.0


def test_backtest_agent_uses_win_rate():
    v = BacktestAgent().evaluate("BTC", UP, {"win_rate": 90.0, "trials": 50, "avg_days_to_tp": 5})
    assert abs(v.score - 0.9) < 1e-9 and v.confidence == 1.0
    assert BacktestAgent().evaluate("BTC", UP, {}).score == 0.0


def test_aggregate_and_decide_gates():
    votes = [
        AgentVote("backtest", 0.9, 1.0, ""),
        AgentVote("dip", 0.8, 1.0, ""),
        AgentVote("trend", 0.7, 1.0, ""),
        AgentVote("volatility", 0.7, 1.0, ""),
        AgentVote("liquidity", 1.0, 1.0, ""),
    ]
    consensus = aggregate(votes)
    assert 0 <= consensus <= 100 and consensus > 80

    ok = decide(consensus, win_rate=88, avg_days_to_tp=6,
                min_confidence=70, min_win_rate=80, deadline_days=30)
    assert ok["decision"] == "trade"

    low_wr = decide(consensus, win_rate=60, avg_days_to_tp=6,
                    min_confidence=70, min_win_rate=80, deadline_days=30)
    assert low_wr["decision"] == "skip"

    too_slow = decide(consensus, win_rate=88, avg_days_to_tp=45,
                      min_confidence=70, min_win_rate=80, deadline_days=30)
    assert too_slow["decision"] == "skip"
