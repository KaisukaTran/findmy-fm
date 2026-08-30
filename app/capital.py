"""How much capital to run, and in how many pieces — derived, not guessed.

Kai tops the account up monthly and needs the sizing to follow deterministically, written down
precisely enough that a later Claude/xAI API call can apply it without re-deriving anything.
This module is that rule as executable arithmetic; ``docs/capital-scaling-policy.md`` is the
same rule in prose, with the evidence and the caveats.

**It scales SIZE ONLY.** Take-profit, stop-loss, DCA spacing, wave count, deadlines and entry
filters are never decided here. That split is the conclusion of
``docs/capital-scaling-2026-08-23.md`` and of every production bot surveyed there: sizing is
auditable arithmetic; shape fitted to history is overfitting. A test asserts on this module's
public API surface — not on its source text — that no shape parameter ever appears.

WHAT ACTUALLY STOPS US, in the order it fires. A cross-check found the first version of this
module calibrated against the SECOND leg:

1. **A consecutive-loss streak.** ``circuit.evaluate`` freezes at
   ``consecutive_losses >= max_consecutive_losses`` (default 4). N sessions stopping together
   IS a streak of N, so this fires at N=4 regardless of how little money was lost.
2. **The daily loss limit**, ``daily_loss_hard_pct`` (default 5%). Reached later, and measured
   on GROSS realised loss divided by CURRENT mark-to-market equity — both of which make the
   breaker see a bigger number than this model computes.
3. The deployable-budget reserve, which the prior analysis measured as not existing in practice
   (peak deployment 97.4% of equity). Kept as a check, believed weakly.

Nothing here mutates settings: it returns a recommendation naming the binding constraint, and a
human decides. Nothing in ``app/`` imports it — that is deliberate.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

# Spend only this much of a limit on the modelled worst day, so a day slightly worse than
# modelled — slippage past the stop, a gap through it — still does not trip the breaker.
DEFAULT_SAFETY_MARGIN = 0.8


class CapitalInputError(ValueError):
    """An input that would make the answer meaningless. Raised, never silently absorbed."""


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
    """What an open book costs if every session stops on the same day."""

    worst_day_pct: float
    limit_pct: float
    within_limit: bool
    breaches_by_pct: float
    committed_usd: float
    freezes_on_loss_streak: bool


def _require_positive(equity: float) -> None:
    """Fail closed. A zero or negative equity read made every configuration look safe."""
    if equity <= 0:
        raise CapitalInputError(f"equity must be positive, got {equity!r}")


def ladder_usd(first_wave_usd: float, ladder_ratio: float,
               session_deploy_cap: float = 0.0) -> float:
    """USD one session commits once its ladder has filled, as the app will ACTUALLY allow it.

    ``session_deploy_cap`` is ``max_session_deploy_usd`` (0 = no cap). The app enforces it on
    both the auto-chain and manual DCA+, so a recommendation that ignores it produces sessions
    whose last rungs are silently refused — a truncated ladder that cannot average down and
    dies at the stop on a worse average.
    """
    full = first_wave_usd * ladder_ratio
    return min(full, session_deploy_cap) if session_deploy_cap > 0 else full


def worst_correlated_day_pct(
    equity: float, sessions: int, first_wave_usd: float, ladder_ratio: float,
    stop_fraction: float, session_deploy_cap: float = 0.0,
) -> float:
    """Percent of equity lost if EVERY open session stops out on the same day.

    A stress assumption, not a measurement of this book — see the policy doc. It is
    conservative in dollars (measured fill fraction is ~33% of the reservation in a normal
    regime) and optimistic in one way that matters: a freed slot can be refilled the same day,
    so N sessions is ONE generation, not a day's worth.
    """
    _require_positive(equity)
    committed = sessions * ladder_usd(first_wave_usd, ladder_ratio, session_deploy_cap)
    return committed * stop_fraction / equity * 100.0


def equity_per_extra_session(
    first_wave_usd: float, ladder_ratio: float, stop_fraction: float,
    daily_loss_limit: float, safety_margin: float = DEFAULT_SAFETY_MARGIN,
    session_deploy_cap: float = 0.0,
) -> float:
    """Equity that buys one more session under the DAILY-LOSS leg alone.

    Useful for planning a top-up, but it is NOT the whole rule: the consecutive-loss streak
    usually binds first and does not move with equity at all. Always confirm with
    ``recommend_sessions``.
    """
    denom = safety_margin * daily_loss_limit
    if denom <= 0:
        return math.inf
    return ladder_usd(first_wave_usd, ladder_ratio, session_deploy_cap) * stop_fraction / denom


def recommend_sessions(
    equity: float,
    *,
    first_wave_usd: float,
    ladder_ratio: float,
    stop_fraction: float,
    daily_loss_limit: float,
    max_consecutive_losses: int,
    backup_fraction: float,
    universe_size: int,
    min_notional: float = 0.0,
    session_deploy_cap: float = 0.0,
    safety_margin: float = DEFAULT_SAFETY_MARGIN,
) -> Sizing:
    """How many concurrent sessions this capital supports, and what limited it.

    Every constraint is REQUIRED except the two with a natural "off" value, because an
    omitted one silently returns a larger, unsafe number — the failure mode of the first
    version, where ``universe_size`` defaulted to None and the answer overshot by 3 sessions.
    """
    _require_positive(equity)
    if first_wave_usd < min_notional:
        # A wave 0 the venue refuses makes the count meaningless: the slot is held by an order
        # that can never fill.
        return Sizing(0, first_wave_usd, "first_wave_below_min_notional", 0.0, 0.0,
                      {"min_notional": min_notional})

    lad = ladder_usd(first_wave_usd, ladder_ratio, session_deploy_cap)
    # N sessions stopping together IS a streak of N, so staying one under the limit is what
    # keeps a fully correlated day from freezing the app by itself.
    streak_cap = float(max(0, max_consecutive_losses - 1))
    risk_cap = (safety_margin * daily_loss_limit * equity) / (lad * stop_fraction) \
        if lad > 0 and stop_fraction > 0 else math.inf
    budget_cap = ((1.0 - backup_fraction) * equity) / lad if lad > 0 else math.inf

    limit, binding = min(
        [(streak_cap, "consecutive_loss_streak"),
         (risk_cap, "daily_loss_limit"),
         (budget_cap, "deployable_budget"),
         (float(universe_size), "universe_size")],
        key=lambda c: c[0],
    )
    # Nudge before flooring: `recommend_first_wave` solves the same equation backwards, so a
    # size it returns lands on an exact integer in arithmetic but 5.999999... in floating point.
    sessions = max(0, int(math.floor(limit + 1e-9)))
    return Sizing(
        sessions=sessions,
        first_wave_usd=first_wave_usd,
        binding=binding,
        worst_day_pct=round(worst_correlated_day_pct(
            equity, sessions, first_wave_usd, ladder_ratio, stop_fraction,
            session_deploy_cap), 4),
        committed_usd=round(sessions * lad, 2),
        detail={"streak_cap": streak_cap, "risk_cap": round(risk_cap, 4),
                "budget_cap": round(budget_cap, 4), "universe_cap": float(universe_size),
                "ladder_usd": round(lad, 2), "safety_margin": safety_margin},
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
    session_deploy_cap: float = 0.0,
    safety_margin: float = DEFAULT_SAFETY_MARGIN,
) -> float:
    """The other lever: hold the session count and size each one to fit.

    Returns 0.0 when no size satisfies every constraint at once — in particular when the
    answer would fall under ``min_notional`` (the venue would refuse wave 0) or when
    ``session_deploy_cap`` truncates the ladder. Returning a number the app cannot build was
    the defect here: two rows of the policy doc's table recommended waves whose ladders exceed
    ``max_session_deploy_usd`` and would have run truncated.
    """
    _require_positive(equity)
    if sessions <= 0 or ladder_ratio <= 0 or stop_fraction <= 0:
        return 0.0
    by_risk = (safety_margin * daily_loss_limit * equity) / (sessions * ladder_ratio * stop_fraction)
    by_budget = ((1.0 - backup_fraction) * equity) / (sessions * ladder_ratio)
    wave = min(by_risk, by_budget)
    if session_deploy_cap > 0:
        wave = min(wave, session_deploy_cap / ladder_ratio)
    return 0.0 if wave < min_notional else wave


def audit_book(
    equity: float,
    *,
    ladder_usds: list[float],
    stop_fraction: float,
    daily_loss_limit: float,
    max_consecutive_losses: int,
) -> Audit:
    """What the sessions ACTUALLY OPEN cost on a correlated stop day. **Use this to audit.**

    A single ``ladder_ratio`` does not exist in the running system: ``autotune_levels_enabled``
    gives each symbol its own DCA spacing, and sessions opened at different times were sized
    against different wave-0 settings. Measured live 2026-08-30 with six open — five legacy
    ladders near $140 and one WLD ladder of $223.86 — the real total was $931.89 and a
    correlated day 3.86%, while a uniform ratio at today's $40 wave said 5.81% and would have
    raised a false alarm.

    Feed it ``min(isolated_fund, max_session_deploy_usd)`` per session, and treat a session
    touched by manual DCA+, orphan adoption or a merge as suspect: ``isolated_fund`` is a
    PLANNING cap that those paths rewrite (DCA+ shrinks it to spent-so-far; orphan adoption
    inflates it by ``scan_fund``), so it is not always the ladder cost.
    """
    _require_positive(equity)
    if any(x < 0 for x in ladder_usds):
        raise CapitalInputError("a ladder cost cannot be negative")
    committed = sum(ladder_usds)
    worst = committed * stop_fraction / equity * 100.0
    limit_pct = daily_loss_limit * 100.0
    return Audit(
        worst_day_pct=round(worst, 4),
        limit_pct=limit_pct,
        within_limit=worst <= limit_pct,
        breaches_by_pct=round(max(0.0, worst - limit_pct), 4),
        committed_usd=round(committed, 2),
        # The leg that fires first: this many sessions stopping together is a streak of that
        # length, whatever the dollar loss. It is why a book can sit "inside the limit" and
        # still freeze the app.
        freezes_on_loss_streak=len(ladder_usds) >= max_consecutive_losses,
    )


def _model_uniform_book(
    equity: float,
    *,
    sessions: int,
    first_wave_usd: float,
    ladder_ratio: float,
    stop_fraction: float,
    daily_loss_limit: float,
    max_consecutive_losses: int,
    session_deploy_cap: float = 0.0,
) -> Audit:
    """Model a HYPOTHETICAL book of uniform sessions — for planning only, never for auditing.

    Private on purpose. Its public predecessor was picked over ``audit_book`` and produced the
    false 5.81% alarm that this module's own history records.
    """
    _require_positive(equity)
    lad = ladder_usd(first_wave_usd, ladder_ratio, session_deploy_cap)
    return audit_book(equity, ladder_usds=[lad] * sessions, stop_fraction=stop_fraction,
                      daily_loss_limit=daily_loss_limit,
                      max_consecutive_losses=max_consecutive_losses)
