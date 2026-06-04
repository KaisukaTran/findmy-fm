"""Combine agent votes into a consensus % and a trade/skip decision.

Consensus weights each agent's score by a fixed weight AND its own confidence,
so low-confidence (thin-data) votes count less. The backtest agent carries the
most weight because it is the only voter tied to a measured win-rate.
"""

from __future__ import annotations

from app.agents.base import AgentVote

DEFAULT_WEIGHTS = {
    "backtest": 0.40,
    "dip": 0.20,
    "trend": 0.15,
    "volatility": 0.15,
    "liquidity": 0.10,
    "ml": 0.25,
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
) -> dict:
    """
    Decide trade vs skip. Capital-preservation posture — a pair must clear ALL gates:
      consensus ≥ min_confidence, win_rate ≥ min_win_rate, time-to-TP ≤ deadline,
      loss_rate ≤ max_loss_rate, and net edge (TP − round-trip cost) ≥ min_net_edge.
    """
    reasons: list[str] = []
    if consensus_pct < min_confidence:
        reasons.append(f"consensus {consensus_pct:.1f}% < {min_confidence:.1f}%")
    if win_rate < min_win_rate:
        reasons.append(f"win-rate {win_rate:.1f}% < {min_win_rate:.1f}%")
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
        "loss_rate": loss_rate,
        "net_edge": net_edge,
        "avg_days_to_tp": avg_days_to_tp,
        "reasons": reasons or ["all gates passed"],
    }
