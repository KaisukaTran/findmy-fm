"""Combine agent votes into a consensus % and a trade/skip decision.

Consensus weights each agent's score by a fixed weight AND its own confidence,
so low-confidence (thin-data) votes count less.

S4: the backtest agent's vote is EXCLUDED from the consensus score (weight 0) so
the consensus becomes a pure market-context signal from {trend, dip, volatility,
liquidity, ml}.  The backtest evidence (E, win_lb, loss_rate, days) is OWNED by
the hard gates in ``decide`` — keeping it in the consensus caused the score to be
largely the backtest agreeing with itself and prevented the 5 signal agents from
ever vetoing a trade.  The vote row is still persisted for audit purposes.
"""

from __future__ import annotations

from app.agents.base import AgentVote

DEFAULT_WEIGHTS = {
    "backtest": 0.0,   # S4: excluded from consensus; evidence lives in the hard gates
    "dip": 0.25,
    "trend": 0.20,
    "volatility": 0.15,
    "liquidity": 0.10,
    "ml": 0.30,
}


def aggregate(votes: list[AgentVote], weights: dict[str, float] | None = None) -> float:
    """Return a consensus confidence in [0, 100]."""
    w = weights or DEFAULT_WEIGHTS
    num = 0.0
    den = 0.0
    for v in votes:
        weight = w.get(v.name, 0.0) * v.confidence
        num += weight * v.score
        den += weight
    return round((num / den * 100) if den else 0.0, 2)


def decide(
    consensus_pct: float,
    win_rate: float,
    avg_days_to_tp: float | None,
    *,
    min_confidence: float,
    min_win_rate: float,
    deadline_days: float,
    loss_rate: float = 0.0,
    max_loss_rate: float = 100.0,
    net_edge: float = 1.0,
    min_net_edge: float = 0.0,
    win_rate_lb: float | None = None,
    trials: int | None = None,
    min_trials: int = 0,
    expectancy: float | None = None,
    min_expectancy: float = 0.0,
) -> dict:
    """
    Decide trade vs skip. Capital-preservation posture — a pair must clear ALL gates:
      consensus ≥ min_confidence, expectancy ≥ min_expectancy (PRIMARY), win-rate ≥
      min_win_rate, time-to-TP ≤ deadline, loss_rate ≤ max_loss_rate, net edge ≥ min_net_edge.

    `expectancy` (mean net PnL %/trade, SL- and fee-aware) is the primary gate: a pair only
    trades when the math has positive net edge, so a high win-rate hiding fat-tail losses is
    rejected. The win-rate gate compares against `win_rate_lb` (Wilson lower bound) when
    supplied — high AND statistically trustworthy. `min_trials` rejects thin backtest evidence.
    """
    reasons: list[str] = []
    wr_gated = win_rate if win_rate_lb is None else win_rate_lb
    if consensus_pct < min_confidence:
        reasons.append(f"consensus {consensus_pct:.1f}% < {min_confidence:.1f}%")
    if trials is not None and min_trials > 0 and trials < min_trials:
        reasons.append(f"chỉ {trials} lần thử backtest < {min_trials} (chưa đủ tin cậy)")
    if expectancy is not None and expectancy < min_expectancy:
        reasons.append(f"kỳ vọng {expectancy:+.2f}% < {min_expectancy:.2f}% (net edge âm/thấp)")
    if wr_gated < min_win_rate:
        label = "win-rate (cận dưới)" if win_rate_lb is not None else "win-rate"
        reasons.append(f"{label} {wr_gated:.1f}% < {min_win_rate:.1f}%")
    if loss_rate > max_loss_rate:
        reasons.append(f"loss-rate {loss_rate:.1f}% > {max_loss_rate:.1f}%")
    if net_edge < min_net_edge:
        reasons.append(f"net edge {net_edge:.2f}% < {min_net_edge:.2f}% (cost too high)")
    if avg_days_to_tp is None:
        reasons.append("no estimated time-to-TP")
    elif avg_days_to_tp > deadline_days:
        reasons.append(f"avg {avg_days_to_tp:.1f}d to TP > deadline {deadline_days}d")

    decision = "trade" if not reasons else "skip"
    return {
        "decision": decision,
        "consensus_pct": consensus_pct,
        "win_rate": win_rate,
        "expectancy": expectancy,
        "loss_rate": loss_rate,
        "net_edge": net_edge,
        "avg_days_to_tp": avg_days_to_tp,
        "reasons": reasons or ["all gates passed"],
    }
