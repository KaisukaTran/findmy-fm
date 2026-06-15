"""Tests for quant agents and the aggregator (deterministic, synthetic candles)."""

from app.agents import (
    DEFAULT_WEIGHTS,
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
        AgentVote("backtest", 0.9, 1.0, ""),  # S4: weight=0, does not affect consensus
        AgentVote("dip", 0.8, 1.0, ""),
        AgentVote("trend", 0.7, 1.0, ""),
        AgentVote("volatility", 0.7, 1.0, ""),
        AgentVote("liquidity", 1.0, 1.0, ""),
    ]
    consensus = aggregate(votes)
    # S4: backtest excluded; signal agents avg ~0.78 → consensus ~75-80
    assert 0 <= consensus <= 100 and consensus > 60

    ok = decide(consensus, win_rate=88, avg_days_to_tp=6,
                min_confidence=45, min_win_rate=80, deadline_days=30)
    assert ok["decision"] == "trade"

    low_wr = decide(consensus, win_rate=60, avg_days_to_tp=6,
                    min_confidence=70, min_win_rate=80, deadline_days=30)
    assert low_wr["decision"] == "skip"

    too_slow = decide(consensus, win_rate=88, avg_days_to_tp=45,
                      min_confidence=70, min_win_rate=80, deadline_days=30)
    assert too_slow["decision"] == "skip"


def test_decide_expectancy_is_primary_gate():
    # High win-rate but NEGATIVE expectancy (fat-tail losses) → skip on the expectancy gate.
    bad = decide(90, win_rate=95, avg_days_to_tp=5, min_confidence=70, min_win_rate=55,
                 deadline_days=30, expectancy=-0.5, min_expectancy=0.3)
    assert bad["decision"] == "skip" and any("kỳ vọng" in r for r in bad["reasons"])
    # Modest win-rate but positive expectancy + trustworthy sample → trade.
    good = decide(90, win_rate=70, avg_days_to_tp=5, min_confidence=70, min_win_rate=55,
                  deadline_days=30, expectancy=1.2, min_expectancy=0.3,
                  win_rate_lb=65, trials=20, min_trials=8)
    assert good["decision"] == "trade"


# ---------------------------------------------------------------------------
# S4: backtest excluded from consensus, vote row still present
# ---------------------------------------------------------------------------

def test_s4_default_weights_backtest_is_zero():
    """S4 contract: DEFAULT_WEIGHTS must have backtest=0.0."""
    assert DEFAULT_WEIGHTS["backtest"] == 0.0


def test_s4_aggregate_excludes_backtest_vote():
    """A backtest vote with a high score must not change the consensus when weight=0."""
    signal_only = [
        AgentVote("dip", 0.5, 1.0, ""),
        AgentVote("trend", 0.5, 1.0, ""),
        AgentVote("volatility", 0.5, 1.0, ""),
        AgentVote("liquidity", 0.5, 1.0, ""),
        AgentVote("ml", 0.5, 1.0, ""),
    ]
    with_backtest = signal_only + [AgentVote("backtest", 1.0, 1.0, "")]

    c_without = aggregate(signal_only)
    c_with = aggregate(with_backtest)

    # Adding a backtest=1.0 vote must not change the consensus score at all
    assert abs(c_without - c_with) < 1e-9, (
        f"backtest vote changed consensus: {c_without:.4f} vs {c_with:.4f}"
    )


def test_s4_aggregate_custom_weights_override():
    """Passing explicit weights at call time overrides DEFAULT_WEIGHTS."""
    votes = [
        AgentVote("trend", 1.0, 1.0, ""),
        AgentVote("dip", 0.0, 1.0, ""),
        AgentVote("volatility", 0.0, 1.0, ""),
        AgentVote("liquidity", 0.0, 1.0, ""),
        AgentVote("ml", 0.0, 1.0, ""),
    ]
    # All weight on trend (score=1.0) → consensus should be close to 100
    result = aggregate(votes, weights={"trend": 1.0, "dip": 0.0, "volatility": 0.0,
                                       "liquidity": 0.0, "ml": 0.0, "backtest": 0.0})
    assert result > 95.0


def test_s4_runtime_consensus_weights_roundtrip(db):
    """get/set_consensus_weights persist and restore; backtest is always forced to 0."""
    from app import runtime

    saved = runtime.set_consensus_weights(db, {
        "trend": 0.30, "dip": 0.20, "volatility": 0.10, "liquidity": 0.10, "ml": 0.30,
        "backtest": 0.99,   # must be forced to 0
    })
    assert saved["backtest"] == 0.0
    assert abs(saved["trend"] - 0.30) < 1e-9

    loaded = runtime.get_consensus_weights(db)
    assert loaded["backtest"] == 0.0
    assert abs(loaded["trend"] - 0.30) < 1e-9


def test_s4_get_consensus_weights_fallback_to_defaults(db):
    """When no override is stored, get_consensus_weights returns DEFAULT_WEIGHTS."""
    from app import runtime

    # Ensure nothing stored for this key in the clean test db
    weights = runtime.get_consensus_weights(db)
    assert weights == DEFAULT_WEIGHTS
    assert weights["backtest"] == 0.0


def test_s4_min_confidence_persists_via_kss_settings(db):
    """min_confidence is now in KSS_SETTING_FIELDS and survives set/sync cycle."""
    from app import runtime
    from app.config import settings

    runtime.set_kss_settings(db, {"min_confidence": 45.0})
    assert settings.min_confidence == 45.0

    # Simulate restart
    settings.min_confidence = 70.0
    runtime.sync_from_db(db)
    assert settings.min_confidence == 45.0
