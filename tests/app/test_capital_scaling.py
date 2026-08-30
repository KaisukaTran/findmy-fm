"""How many sessions, and how big, for a given amount of capital.

Kai adds capital monthly and needs the sizing to follow deterministically, written down
precisely enough that a later Claude/xAI API call can apply it without re-deriving anything.

It scales SIZE ONLY — never take-profit, stop-loss, DCA spacing, wave count or entry filters.

THE FIRST VERSION CALIBRATED AGAINST THE WRONG LIMIT, and a cross-check caught it. The app has
three brakes and they do not fire in the order I assumed:

* `consecutive_losses >= max_consecutive_losses` (default **4**) — N sessions stopping together
  IS a streak of N, so this fires at 4 sessions whatever the dollar loss. It binds FIRST.
* `daily_loss_pct > daily_loss_hard_pct` (default 5%) — and the breaker measures GROSS realised
  loss over CURRENT mark-to-market equity, so it sees a bigger number than this model computes.
* `drawdown_pct > max_drawdown_pct` (15%), which includes unrealised.

The correlated stop day is a STRESS ASSUMPTION, not a measurement of our book. Our own paper
book's worst day was 5 losing exits, 0.86% gross, and it still closed net positive. The
"13 of 16 symbols" figure comes from a walk-forward BACKTEST over 16 symbols of daily history —
real evidence that stops cluster, but not evidence that our book has ever done it.
"""

from __future__ import annotations

import inspect

import pytest

from app import capital

# The live configuration these numbers were measured against (2026-08-30).
LIVE = {"ladder_ratio": 5.841, "stop_fraction": 0.083, "daily_loss_limit": 0.05,
        "backup_fraction": 0.25, "min_notional": 10.0, "max_consecutive_losses": 4,
        "universe_size": 100}


def test_the_streak_limit_binds_before_the_money_limit():
    """The correction. Four sessions stopping together is a streak of four, which freezes the
    app on `max_consecutive_losses` at 3.88% of equity — long before the 5% daily-loss leg the
    first version of this rule calibrated against."""
    got = capital.recommend_sessions(2002.21, first_wave_usd=40.0, **LIVE)

    assert got.sessions == 3
    assert got.binding == "consecutive_loss_streak"
    assert got.detail["streak_cap"] < got.detail["risk_cap"]


def test_the_money_limit_still_applies_when_the_streak_limit_is_lifted():
    """Raise the streak tolerance and the daily-loss leg takes over — the arithmetic that was
    right all along, just second in line."""
    got = capital.recommend_sessions(2002.21, first_wave_usd=40.0,
                                     **{**LIVE, "max_consecutive_losses": 50})

    assert got.sessions == 4
    assert got.binding == "daily_loss_limit"


def test_adding_capital_moves_the_money_limit_but_never_the_streak_limit():
    """What Kai asked for, honestly stated: money buys headroom under one brake and none at all
    under the other. Past a point, more capital cannot buy more sessions — only bigger ones."""
    small = capital.recommend_sessions(2000.0, first_wave_usd=40.0,
                                       **{**LIVE, "max_consecutive_losses": 50})
    large = capital.recommend_sessions(5000.0, first_wave_usd=40.0,
                                       **{**LIVE, "max_consecutive_losses": 50})
    assert large.sessions > small.sessions

    capped_small = capital.recommend_sessions(2000.0, first_wave_usd=40.0, **LIVE)
    capped_large = capital.recommend_sessions(50_000.0, first_wave_usd=40.0, **LIVE)
    assert capped_small.sessions == capped_large.sessions == 3


def test_the_universe_caps_concurrency_however_much_capital_there_is():
    got = capital.recommend_sessions(5_000_000.0, first_wave_usd=40.0,
                                     **{**LIVE, "max_consecutive_losses": 500,
                                        "universe_size": 100})

    assert got.sessions == 100 and got.binding == "universe_size"


def test_the_deploy_cap_truncates_the_ladder_and_the_rule_must_know():
    """`max_session_deploy_usd` is enforced on both the auto-chain and manual DCA+. A rule that
    ignores it recommends a wave whose last rungs the app silently refuses — a session that
    cannot average down and dies at the stop on a worse average."""
    uncapped = capital.ladder_usd(68.76, 5.841)
    capped = capital.ladder_usd(68.76, 5.841, session_deploy_cap=240.0)

    assert uncapped == pytest.approx(401.6, abs=0.5)
    assert capped == 240.0


def test_a_recommended_wave_never_exceeds_the_deploy_cap():
    w = capital.recommend_first_wave(5000.0, sessions=6, session_deploy_cap=240.0,
                                     ladder_ratio=5.841, stop_fraction=0.083,
                                     daily_loss_limit=0.05, backup_fraction=0.25,
                                     min_notional=10.0)

    assert w * 5.841 <= 240.0 + 1e-6, "the app would truncate anything larger"


def test_a_wave_that_would_land_under_the_minimum_notional_is_refused_not_returned():
    """Returning $4.13 for a $300 book was a number the venue rejects — the two directions of
    the rule then disagreed, which the old round-trip test did not catch."""
    w = capital.recommend_first_wave(300.0, sessions=6, ladder_ratio=5.841, stop_fraction=0.083,
                                     daily_loss_limit=0.05, backup_fraction=0.25,
                                     min_notional=10.0)

    assert w == 0.0


def test_a_wave_below_the_minimum_notional_yields_no_sessions():
    got = capital.recommend_sessions(2000.0, first_wave_usd=4.0, **LIVE)

    assert got.sessions == 0 and got.binding == "first_wave_below_min_notional"


# --- auditing the book that actually exists ----------------------------------


def test_the_audit_reads_the_ladders_actually_open():
    """Measured live: five legacy ladders near $140 plus one WLD ladder of $223.86 = $931.89,
    a correlated day of 3.86% — while a uniform 5.841x ratio at today's $40 wave said 5.81%
    and raised a false alarm."""
    got = capital.audit_book(2002.30, ladder_usds=[144.09, 139.82, 144.07, 142.19, 137.84, 223.86],
                             stop_fraction=0.083, daily_loss_limit=0.05, max_consecutive_losses=4)

    assert got.committed_usd == pytest.approx(931.87, abs=0.05)
    assert got.worst_day_pct == pytest.approx(3.86, abs=0.02)
    assert got.within_limit is True


def test_a_book_inside_the_money_limit_can_still_freeze_the_app():
    """The whole point of the correction: six sessions cost only 3.86% of equity, but six
    stopping together is a streak of six against a limit of four. `within_limit` alone would
    have told the operator everything was fine."""
    got = capital.audit_book(2002.30, ladder_usds=[144.09, 139.82, 144.07, 142.19, 137.84, 223.86],
                             stop_fraction=0.083, daily_loss_limit=0.05, max_consecutive_losses=4)

    assert got.within_limit is True
    assert got.freezes_on_loss_streak is True


def test_a_small_book_neither_breaches_nor_freezes():
    got = capital.audit_book(2002.30, ladder_usds=[144.09, 139.82, 144.07],
                             stop_fraction=0.083, daily_loss_limit=0.05, max_consecutive_losses=4)

    assert got.within_limit is True and got.freezes_on_loss_streak is False


def test_a_book_past_the_money_limit_is_reported_as_such():
    got = capital.audit_book(2002.30, ladder_usds=[233.65] * 6, stop_fraction=0.083,
                             daily_loss_limit=0.05, max_consecutive_losses=4)

    assert got.within_limit is False and got.breaches_by_pct > 0


# --- fail closed -------------------------------------------------------------


def test_a_non_positive_equity_raises_instead_of_reporting_safe():
    """A failed equity read made every configuration look safe — the worst possible direction
    for a safety check."""
    for call in (
        lambda: capital.recommend_sessions(0.0, first_wave_usd=40.0, **LIVE),
        lambda: capital.audit_book(0.0, ladder_usds=[1e9], stop_fraction=0.083,
                                   daily_loss_limit=0.05, max_consecutive_losses=4),
    ):
        with pytest.raises(capital.CapitalInputError):
            call()


def test_a_negative_ladder_cost_is_refused_not_netted_off():
    with pytest.raises(capital.CapitalInputError):
        capital.audit_book(2000.0, ladder_usds=[-5000.0, 5000.0], stop_fraction=0.083,
                           daily_loss_limit=0.05, max_consecutive_losses=4)


def test_a_misspelled_knob_raises_instead_of_silently_using_the_default():
    """`**_ignored` absorbed `saftey_margin` and returned a number 25% wrong with no error —
    unacceptable in a module whose stated purpose is to be applied by an agent verbatim."""
    with pytest.raises(TypeError):
        capital.recommend_sessions(2000.0, first_wave_usd=40.0, saftey_margin=1.0, **LIVE)
    with pytest.raises(TypeError):
        capital.audit_book(2000.0, ladder_usds=[100.0], stop_fraction=0.083,
                           daily_loss_limit=0.05, max_consecutive_losses=4, typo=1)


def test_every_constraint_must_be_supplied():
    """An omitted constraint silently returns a LARGER, unsafe number. The first version
    defaulted `universe_size` to None and overshot by three sessions."""
    with pytest.raises(TypeError):
        capital.recommend_sessions(2000.0, first_wave_usd=40.0, ladder_ratio=5.841,
                                   stop_fraction=0.083, daily_loss_limit=0.05,
                                   backup_fraction=0.25)  # no universe_size, no streak limit


# --- the boundary this module must never cross -------------------------------


_SHAPE = ("tp_pct", "take_profit", "sl_pct", "stop_loss_pct", "distance_pct", "max_waves",
          "deadline_days", "min_expectancy", "win_rate", "trail", "min_confidence")


def test_no_public_function_decides_the_shape_of_the_strategy():
    """Asserted on the public API SURFACE, not on source text.

    The first version grepped the module for banned words and was defeated three ways in
    review: `sl_pct` and `max_waves` were missing from the list entirely, a split string
    literal slipped through, and moving the logic into a sibling module bypassed it completely
    (`inspect.getsource` reads one file). Names and signatures are what a caller can actually
    reach, so that is what this checks.
    """
    for name in dir(capital):
        if name.startswith("_"):
            continue
        obj = getattr(capital, name)
        if not callable(obj) or not hasattr(obj, "__code__"):
            continue
        assert not any(s in name.lower() for s in _SHAPE), f"{name} names a shape parameter"
        for param in inspect.signature(obj).parameters:
            assert not any(s in param.lower() for s in _SHAPE), \
                f"{name}({param}=...) takes a shape parameter"


def test_the_module_cannot_reach_anything_it_could_mutate():
    """It must be structurally incapable of applying itself, not merely asked not to.

    Checked by parsing the imports rather than grepping the text — a source-text check matches
    prose in a docstring and misses `from x import y as settings`, which is exactly the kind of
    hole review found in this file's first version.
    """
    import ast

    tree = ast.parse(inspect.getsource(capital))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")

    assert not [m for m in imported if m.startswith("app")], (
        f"sizing advice must not import from the app — it would gain a way to apply itself: "
        f"{imported}")
