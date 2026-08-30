"""How many sessions, and how big, for a given amount of capital.

Kai adds capital monthly and needs the sizing to follow it deterministically — and needs the
rule written down precisely enough that a Claude or xAI API call can apply it later without
re-deriving anything. This is that rule, as executable arithmetic rather than prose.

It scales SIZE ONLY. The shape of the strategy — take-profit, stop-loss, DCA spacing, entry
filters — is never touched here. That split is the conclusion of `docs/capital-scaling-2026-08-23.md`
and of how every production bot surveyed there works: sizing is auditable arithmetic, shape
fitted to history is overfitting.

THE BINDING CONSTRAINT IS A CORRELATED STOP DAY, not the deployable budget. Crypto stops out
together: measured on our own universe, **13 of 16 symbols stopped on the same day**
(2026-06-05). So the worst case is not "one position fails", it is "every open position fails at
once", and it must stay under `daily_loss_hard_pct` — the level at which the app freezes itself.

    worst correlated day = N x first_wave x ladder_ratio x stop_fraction
    require:               worst day <= safety_margin x daily_loss_limit x equity

Measured against the live config on 2026-08-30 (E=$2002, w=$40, ladder 5.841x, stop 8.3%,
limit 5%, margin 0.8): the risk cap allows 4.13 sessions while the budget cap allows 6.43 — the
risk cap binds, and it is what set the live cap of 4. It also says one extra session is earned
per **$484.82** of added equity, and that running 6 sessions at $40 wave-0 puts a correlated day
at 5.81% of equity, i.e. past the 5% limit and into the app's own circuit breaker.
"""

from __future__ import annotations

import pytest

from app import capital

# The live configuration these numbers were measured against.
LIVE = {"ladder_ratio": 5.841, "stop_fraction": 0.083, "daily_loss_limit": 0.05,
        "backup_fraction": 0.25, "min_notional": 10.0}


def test_the_live_configuration_reproduces_the_cap_that_was_set():
    """A rule that cannot explain the number already in production is not a rule."""
    got = capital.recommend_sessions(2002.21, first_wave_usd=40.0, **LIVE)

    assert got.sessions == 4
    assert got.binding == "correlated_stop_day"
    assert got.worst_day_pct == pytest.approx(3.88, abs=0.05)


def test_the_risk_cap_binds_before_the_budget_cap():
    """The deployable budget would allow 6. Stopping at 4 is the correlated-day limit, and
    reporting WHICH constraint bound it is the whole point — otherwise nobody can tell whether
    to add capital or loosen a limit."""
    got = capital.recommend_sessions(2002.21, first_wave_usd=40.0, **LIVE)

    assert got.detail["risk_cap"] < got.detail["budget_cap"]


def test_sessions_grow_one_step_per_fixed_slice_of_equity():
    """What Kai actually asked for: add capital, get another session, predictably. With the
    wave-0 size held fixed the rule is linear in equity — one more session per $484.82."""
    step = capital.equity_per_extra_session(first_wave_usd=40.0, **LIVE)

    assert step == pytest.approx(484.82, abs=0.5)
    assert capital.recommend_sessions(2000.0, first_wave_usd=40.0, **LIVE).sessions == 4
    assert capital.recommend_sessions(2500.0, first_wave_usd=40.0, **LIVE).sessions == 5
    assert capital.recommend_sessions(3000.0, first_wave_usd=40.0, **LIVE).sessions == 6
    assert capital.recommend_sessions(5000.0, first_wave_usd=40.0, **LIVE).sessions == 10


def test_the_other_direction_resize_to_keep_a_chosen_session_count():
    """The alternative lever: hold the session count and shrink each one instead. Running 6
    sessions on $2002 is safe at a $27.53 wave-0 and unsafe at $40 — same diversification,
    inside the breaker instead of past it."""
    w = capital.recommend_first_wave(2002.21, sessions=6, **LIVE)

    assert w == pytest.approx(27.53, abs=0.05)
    back = capital.recommend_sessions(2002.21, first_wave_usd=w, **LIVE)
    assert back.sessions == 6, "the two directions must agree"


def test_a_configuration_past_the_limit_is_reported_as_such():
    """6 sessions at a $40 wave-0 is what is running now. The rule must say plainly that a
    correlated day costs 5.81% against a 5% limit — not quietly round it down."""
    audit = capital.audit_current(2002.21, sessions=6, first_wave_usd=40.0, **LIVE)

    assert audit.worst_day_pct == pytest.approx(5.81, abs=0.05)
    assert audit.within_limit is False
    assert audit.breaches_by_pct > 0


def test_a_configuration_inside_the_limit_passes():
    audit = capital.audit_current(2002.21, sessions=4, first_wave_usd=40.0, **LIVE)

    assert audit.within_limit is True


# --- guardrails --------------------------------------------------------------


def test_a_wave_below_the_minimum_notional_is_refused():
    """Below the venue's floor the first order cannot trade at all, so the session count is
    meaningless — a $10 ladder that sends a $1 wave 0 is a slot that never fills."""
    got = capital.recommend_sessions(2000.0, first_wave_usd=4.0, **LIVE)

    assert got.sessions == 0
    assert got.binding == "first_wave_below_min_notional"


def test_too_little_capital_yields_no_sessions_rather_than_a_fractional_one():
    got = capital.recommend_sessions(300.0, first_wave_usd=40.0, **LIVE)

    assert got.sessions == 0


def test_the_universe_caps_concurrency_however_much_capital_there_is():
    """One session per symbol, so the candidate pool is a hard ceiling no amount of money
    lifts. Above it the answer is bigger sessions, not more of them."""
    got = capital.recommend_sessions(500_000.0, first_wave_usd=40.0, universe_size=100, **LIVE)

    assert got.sessions == 100
    assert got.binding == "universe_size"


def test_a_deeper_ladder_earns_fewer_sessions_for_the_same_money():
    """The ladder ratio is what converts a wave-0 size into capital at risk, so a deeper
    pyramid must reduce the session count — this is the coupling that a fixed cap hides."""
    shallow = capital.recommend_sessions(4000.0, first_wave_usd=40.0,
                                         **{**LIVE, "ladder_ratio": 2.94})
    deep = capital.recommend_sessions(4000.0, first_wave_usd=40.0,
                                      **{**LIVE, "ladder_ratio": 9.70})

    assert shallow.sessions > deep.sessions


def test_the_safety_margin_is_honoured_and_adjustable():
    """margin 1.0 spends the whole limit and leaves nothing for slippage past the modelled
    stop; the default 0.8 keeps a fifth of it in hand."""
    tight = capital.recommend_sessions(2002.21, first_wave_usd=40.0, safety_margin=1.0, **LIVE)
    default = capital.recommend_sessions(2002.21, first_wave_usd=40.0, **LIVE)

    assert tight.sessions == 5 and default.sessions == 4


def test_nothing_here_touches_the_shape_of_the_strategy():
    """Sizing only. If this module ever grows a take-profit or a stop-loss knob, that is the
    overfitting failure `docs/capital-scaling-2026-08-23.md` exists to prevent."""
    import inspect

    src = inspect.getsource(capital)
    for forbidden in ("tp_pct", "take_profit", "distance_pct", "min_expectancy", "win_rate"):
        assert forbidden not in src, f"sizing must not decide strategy shape ({forbidden})"
