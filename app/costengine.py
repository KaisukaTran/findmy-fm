"""
Transaction-cost awareness for the loss-minimizing posture.

A KSS round trip pays a taker fee on entry and exit plus slippage both ways.
A micro-trade is only worth taking when the take-profit target clears that cost
with a margin (`min_net_edge`). Costs are percentages, so they are size-agnostic;
`scan_min_notional` separately rejects dust orders an exchange would refuse.
"""

from __future__ import annotations

from app.config import settings


def round_trip_cost_pct() -> float:
    """Total cost of a round trip, in percent of notional."""
    return 2 * settings.taker_fee_pct + 2 * settings.slippage_pct


def round_trip_fee_pct() -> float:
    """Total exchange FEE for a round trip (buy + sell), in percent of notional — fees only,
    no slippage. = 2x the highest Binance spot fee."""
    return 2 * settings.binance_max_fee_pct


def tp_fee_buffer_pct() -> float:
    """Extra take-profit % added on top of a session's tp_pct so every TP clears its fees with
    a margin: ``tp_fee_coverage`` (default 1.2 = 120%) x the round-trip fee. Applies to BOTH
    paper and live (the strategy's TP target/trigger add this)."""
    return settings.tp_fee_coverage * round_trip_fee_pct()


def net_edge_pct(tp_pct: float) -> float:
    """Take-profit target minus round-trip cost — the realistic edge."""
    return tp_pct - round_trip_cost_pct()


def covers_costs(tp_pct: float, min_net_edge: float | None = None) -> bool:
    """True if the TP target beats round-trip cost by at least the required margin."""
    margin = settings.min_net_edge if min_net_edge is None else min_net_edge
    return net_edge_pct(tp_pct) >= margin


def min_profit_pct() -> float:
    """
    Minimum worthwhile take-profit %, in percent of notional.

    A round trip pays a buy fee and a sell fee, so 2x the highest Binance spot
    fee is the floor below which a "profit" would not even clear the fees. A
    session's tp_pct is raised to this floor so the (frozen) TP trigger never
    fires on a gain smaller than it.
    """
    return 2 * settings.binance_max_fee_pct


def notional_ok(notional: float) -> bool:
    """Reject dust micro-trades below the configured minimum notional."""
    return notional >= settings.scan_min_notional
