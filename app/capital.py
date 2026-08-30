"""How much capital to run, and in how many pieces — derived, not guessed.

Kai tops the account up monthly and needs the sizing to follow deterministically, written down
precisely enough that a later Claude/xAI API call can apply it without re-deriving anything.
This module is that rule as executable arithmetic; ``docs/capital-scaling-policy.md`` is the
same rule in prose, with the evidence.

**It scales SIZE ONLY.** Take-profit, stop-loss, DCA spacing and entry filters are never decided
here. That split is the conclusion of ``docs/capital-scaling-2026-08-23.md`` and of every
production bot surveyed there: sizing is auditable arithmetic; shape fitted to history is
overfitting. A test asserts this module never mentions a shape parameter.

The binding constraint is a **correlated stop day**, not the deployable budget. Crypto stops out
together — measured on our own universe, 13 of 16 symbols stopped on 2026-06-05 — so the case to
survive is every open position failing at once:

    worst_day = sessions x first_wave x ladder_ratio x stop_fraction
    require     worst_day <= safety_margin x daily_loss_limit x equity

Everything else follows from solving that for one unknown. Nothing here mutates settings: it
returns a recommendation, and a human (or an API) decides whether to apply it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

# Spend only this much of the daily-loss limit on the modelled worst day, so a day that is
# slightly worse than modelled — slippage past the stop, a gap through it — still does not trip
# the breaker. 1.0 spends the whole limit and leaves nothing in hand.
DEFAULT_SAFETY_MARGIN = 0.8


@dataclass(frozen=True)
class Sizing:
    """A recommended session count, and which constraint decided it."""

    sessions: int
    first_wave_usd: float
    binding: str
    worst_day_pct: float
    committed_usd: float
    detail: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Audit:
    """What a configuration already in use actually costs on a correlated stop day."""

    worst_day_pct: float
    limit_pct: float
    within_limit: bool
    breaches_by_pct: float
    committed_usd: float


def _ladder_cost(first_wave_usd: float, ladder_ratio: float) -> float:
    """USD one session commits once its whole ladder has filled."""
    return first_wave_usd * ladder_ratio


def worst_correlated_day_pct(
    equity: float, sessions: int, first_wave_usd: float,
    ladder_ratio: float, stop_fraction: float,
) -> float:
    """Percent of equity lost if EVERY open session stops out on the same day.

    Not a tail scenario invented for safety: measured on this universe, 13 of 16 symbols
    stopped on one day. Diversifying across more symbols does not reduce this — only the total
    committed capital does, which is why the constraint is on `sessions x ladder`, never on
    the session count alone.
    """
    if equity <= 0:
        return 0.0
    committed = sessions * _ladder_cost(first_wave_usd, ladder_ratio)
    return committed * stop_fraction / equity * 100.0


def equity_per_extra_session(
    first_wave_usd: float, ladder_ratio: float, stop_fraction: float,
    daily_loss_limit: float, safety_margin: float = DEFAULT_SAFETY_MARGIN,
    **_ignored,
) -> float:
    """Equity that buys exactly one more concurrent session, holding the wave-0 size fixed.

    This is the monthly top-up rule in one number: the risk cap is linear in equity, so adding
    this much capital earns one more session and nothing else has to change.
    """
    denom = safety_margin * daily_loss_limit
    if denom <= 0:
        return math.inf
    return _ladder_cost(first_wave_usd, ladder_ratio) * stop_fraction / denom


def recommend_sessions(
    equity: float,
    *,
    first_wave_usd: float,
    ladder_ratio: float,
    stop_fraction: float,
    daily_loss_limit: float,
    backup_fraction: float,
    min_notional: float = 0.0,
    universe_size: int | None = None,
    safety_margin: float = DEFAULT_SAFETY_MARGIN,
) -> Sizing:
    """How many concurrent sessions this much capital supports, and what limited it.

    Reporting the binding constraint is half the value: it says whether the next step is to add
    capital, widen the universe, or accept a smaller wave — three very different actions that a
    bare number hides.
    """
    if first_wave_usd < min_notional:
        # A wave 0 the venue will refuse makes the session count meaningless — the slot would
        # be held by an order that can never fill.
        return Sizing(0, first_wave_usd, "first_wave_below_min_notional", 0.0, 0.0,
                      {"min_notional": min_notional})

    ladder = _ladder_cost(first_wave_usd, ladder_ratio)
    risk_cap = (safety_margin * daily_loss_limit * equity) / (ladder * stop_fraction) \
        if ladder > 0 and stop_fraction > 0 else math.inf
    budget_cap = ((1.0 - backup_fraction) * equity) / ladder if ladder > 0 else math.inf
    caps: list[tuple[float, str]] = [
        (risk_cap, "correlated_stop_day"),
        (budget_cap, "deployable_budget"),
    ]
    if universe_size is not None:
        # One session per symbol, so the candidate pool is a ceiling no amount of money lifts.
        # Above it the answer is bigger sessions, not more of them.
        caps.append((float(universe_size), "universe_size"))

    limit, binding = min(caps, key=lambda c: c[0])
    # Nudge before flooring: `recommend_first_wave` solves this same equation backwards, so a
    # size it returns lands on an exact integer cap in arithmetic but 5.999999... in floating
    # point — and the two directions have to agree, or the rule contradicts itself.
    sessions = max(0, int(math.floor(limit + 1e-9)))
    return Sizing(
        sessions=sessions,
        first_wave_usd=first_wave_usd,
        binding=binding,
        worst_day_pct=round(worst_correlated_day_pct(
            equity, sessions, first_wave_usd, ladder_ratio, stop_fraction), 4),
        committed_usd=round(sessions * ladder, 2),
        detail={"risk_cap": round(risk_cap, 4), "budget_cap": round(budget_cap, 4),
                "ladder_usd": round(ladder, 2), "safety_margin": safety_margin},
    )


def recommend_first_wave(
    equity: float,
    *,
    sessions: int,
    ladder_ratio: float,
    stop_fraction: float,
    daily_loss_limit: float,
    backup_fraction: float,
    min_notional: float = 0.0,
    safety_margin: float = DEFAULT_SAFETY_MARGIN,
    **_ignored,
) -> float:
    """The other lever: hold the session count and size each one to fit instead.

    Use this when diversification across more symbols is wanted at a capital level that cannot
    afford them at the current wave size — the same risk, split more ways.
    """
    if sessions <= 0 or ladder_ratio <= 0 or stop_fraction <= 0:
        return 0.0
    by_risk = (safety_margin * daily_loss_limit * equity) / (sessions * ladder_ratio * stop_fraction)
    by_budget = ((1.0 - backup_fraction) * equity) / (sessions * ladder_ratio)
    return min(by_risk, by_budget)


def audit_current(
    equity: float,
    *,
    sessions: int,
    first_wave_usd: float,
    ladder_ratio: float,
    stop_fraction: float,
    daily_loss_limit: float,
    safety_margin: float = DEFAULT_SAFETY_MARGIN,
    **_ignored,
) -> Audit:
    """What the configuration in use right now costs on a correlated stop day.

    Deliberately reports the raw number against the RAW limit, not the margin-reduced one: the
    question this answers is "does a bad day trip the breaker", and the breaker does not know
    about our safety margin.
    """
    worst = worst_correlated_day_pct(equity, sessions, first_wave_usd, ladder_ratio, stop_fraction)
    limit_pct = daily_loss_limit * 100.0
    return Audit(
        worst_day_pct=round(worst, 4),
        limit_pct=limit_pct,
        within_limit=worst <= limit_pct,
        breaches_by_pct=round(max(0.0, worst - limit_pct), 4),
        committed_usd=round(sessions * _ladder_cost(first_wave_usd, ladder_ratio), 2),
    )
