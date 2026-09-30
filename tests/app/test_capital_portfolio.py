"""
Tests for scripts/capital_portfolio_study.py.

WHY this file exists: this is a NEW cash-constrained engine sitting on top of the FROZEN
`app.backtest.simulate_kss` math, and every generalization it adds (partial/starved rung fills,
a shared ledger, session open/close gating) is a place a plausible-looking capital study could
silently be wrong.

  - TestParityWithFrozenSimulator is the critical test: with capital effectively unlimited, the
    new engine's per-bar branching must degenerate EXACTLY to `simulate_kss`'s. If this ever
    fails, `scripts/capital_portfolio_study.py` is the one that is wrong, never
    `app/backtest.py` (frozen — see CLAUDE.md).
  - The ladder-cost helper is the exact number the live app's `ladder_coverage_pct` gate reasons
    about (`app.capital.ladder_budget_exceeded`) — a silent unit error here would misprice every
    `gate=reserve` run.
  - Starvation and cash conservation guard the whole point of this script: a portfolio replay
    that ever lets cash go negative, or "finds" money from nowhere, is not measuring capital
    constraint, it is hiding it.
  - The reservation-gate test locks in the concrete arithmetic the product owner will read this
    study by: $7,000 at 30% coverage on a $28/30-rung/4% ladder funds at most 3 concurrent
    sessions (7000 / 1855.94 = 3.77 -> 3).
"""

from __future__ import annotations

import pytest

from app.backtest import simulate_kss
from scripts.capital_portfolio_study import (
    Config,
    Ledger,
    SessionState,
    _avg_price,
    _cagr_total,
    _detect_bars_per_day,
    _session_lock,
    _sizing_equity,
    _targets,
    _waves_touched,
    full_ladder_cost,
    repay_backstop,
    run_portfolio,
    settle_backstop,
    step_session,
    utilization_stats,
)

DAY_MS = 86_400_000
HOUR_MS = 3_600_000


def _bars(n: int, start_price: float, path, ts0: int = 0) -> list[dict]:
    """Build a synthetic daily candle series. `path(i, open) -> close`; high/low bracket the
    open/close with a small wick so gap-down fills and MAE tracking have something to bite."""
    out = []
    price = start_price
    for i in range(n):
        o = price
        c = path(i, o)
        h = max(o, c) * 1.001
        low = min(o, c) * 0.995
        out.append({"ts": ts0 + i * DAY_MS, "open": o, "high": h, "low": low, "close": c})
        price = c
    return out


def _drive(candles: list[dict], start: int, cfg: Config, ledger: Ledger) -> tuple[dict | None, SessionState]:
    """Run one session through the new engine, bar by bar, until it closes or the data ends —
    the same loop `run_portfolio`'s Step 1 performs for one open session."""
    state = SessionState("X", candles, start, cfg)
    for j in range(start + 1, len(candles)):
        r = step_session(state, candles[j], j, cfg, ledger)
        if r["closed"] is not None:
            return r["closed"], state
    return None, state


def _make_cfg(pessimistic: bool, **kw) -> Config:
    defaults = {
        "capital": 1e12, "gate": "cashflow", "distance_pct": 4.0, "max_waves": 5, "tp_pct": 3.0,
        "tp_step_pct": 0.0, "sl_pct": 0.0, "deadline_days": 1000.0, "trail_after_tp_pct": 0.0,
        "cost_pct": 0.2, "wave0_usd": 28.0,
    }
    defaults.update(kw)
    return Config(pessimistic=pessimistic, **defaults)


class TestParityWithFrozenSimulator:
    """The critical test. With capital unlimited and gate=cashflow, no rung is ever starved or
    partial, so `step_session` must reproduce `simulate_kss` exactly: identical waves_filled,
    identical tp_hit, pnl_pct within 1e-6."""

    def _assert_matches(self, name: str, candles: list[dict], cfg_kwargs: dict):
        for pessimistic in (False, True):
            cfg = _make_cfg(pessimistic, **cfg_kwargs)
            ledger = Ledger(cfg.capital)
            closed, _ = _drive(candles, 0, cfg, ledger)
            ref = simulate_kss(
                candles, 0, distance_pct=cfg.distance_pct, max_waves=cfg.max_waves,
                tp_pct=cfg.tp_pct, deadline_days=cfg.deadline_days, sl_pct=cfg.sl_pct,
                cost_pct=cfg.cost_pct, pessimistic_intrabar=pessimistic,
                wave0_notional_usd=cfg.wave0_usd, tp_step_pct=cfg.tp_step_pct,
                trail_after_tp_pct=cfg.trail_after_tp_pct,
            )
            bound = "pessimistic" if pessimistic else "optimistic"
            assert ref.tp_hit or ref.hit_deadline or ref.stopped, f"{name}/{bound}: reference trial never exited"
            assert closed is not None, f"{name}/{bound}: engine trial never exited"
            assert closed["waves_filled"] == ref.waves_filled, f"{name}/{bound}: waves_filled"
            assert closed["tp_hit"] == ref.tp_hit, f"{name}/{bound}: tp_hit"
            assert abs(closed["pnl_pct"] - ref.pnl_pct) < 1e-6, f"{name}/{bound}: pnl_pct"

    def test_plain_take_profit(self):
        candles = _bars(20, 100.0, lambda i, o: o * 1.02)
        self._assert_matches("plain_tp", candles,
                              {"max_waves": 5, "tp_pct": 3.0, "sl_pct": 8.0})

    def test_deep_ladder_then_take_profit(self):
        def path(i, o):
            return o * 0.94 if i < 8 else o * 1.15
        candles = _bars(20, 100.0, path)
        self._assert_matches("deep_ladder_tp", candles,
                              {"max_waves": 10, "tp_pct": 3.0, "tp_step_pct": 0.5, "sl_pct": 0.0})

    def test_deadline_exit(self):
        candles = _bars(15, 100.0, lambda i, o: o * 0.999)
        self._assert_matches("deadline", candles,
                              {"max_waves": 5, "tp_pct": 10.0, "sl_pct": 0.0, "deadline_days": 5.0})

    def test_trail_armed_exit(self):
        def path(i, o):
            if i < 5:
                return o * 1.03
            if i == 5:
                return o * 1.05
            return o * 0.97
        candles = _bars(20, 100.0, path)
        self._assert_matches("trail", candles,
                              {"max_waves": 5, "tp_pct": 3.0, "sl_pct": 0.0, "trail_after_tp_pct": 3.0})


class TestLadderCostHelper:
    def test_full_ladder_cost_matches_the_live_gate_arithmetic(self):
        cost = full_ladder_cost(4.0, 30, 28.0)
        assert abs(cost - 6186.48) < 0.01

    def test_thirty_percent_coverage_matches_the_owner_scenario(self):
        cost = full_ladder_cost(4.0, 30, 28.0)
        assert abs(cost * 0.30 - 1855.94) < 0.01


class TestStarvation:
    """Capital just above one wave 0, with a series that would fill several deeper rungs if
    money were free. Every rung beyond wave 0 must starve; deployed_usd must never exceed what
    was actually funded; cash must never go negative."""

    def test_rungs_starve_and_cash_never_goes_negative(self):
        cfg = _make_cfg(False, gate="cashflow", max_waves=8, distance_pct=4.0, wave0_usd=28.0,
                         partial_last_rung=False, deadline_days=1000.0, tp_pct=1000.0)
        capital = cfg.wave0_usd * 1.5  # enough for wave 0, nowhere near a full rung 1
        ledger = Ledger(capital)
        ledger.cash -= cfg.wave0_usd  # mirrors run_portfolio's Step 2 debit on open
        candles = _bars(10, 100.0, lambda i, o: o * 0.94)  # steady dive through several targets
        state = SessionState("X", candles, 0, cfg)
        cash_never_negative = True
        deployed_ok = True
        for j in range(1, len(candles)):
            r = step_session(state, candles[j], j, cfg, ledger)
            if ledger.cash < -1e-9:
                cash_never_negative = False
            if state.deployed_usd > capital + 1e-9:
                deployed_ok = False
            if r["closed"] is not None:
                break
        assert cash_never_negative
        assert deployed_ok
        assert state.rungs_starved > 0
        assert state.next_rung == 1  # never advanced past wave 0 — every attempt starved

    def test_partial_last_rung_spends_exactly_the_remaining_cash(self):
        cfg = _make_cfg(False, gate="cashflow", max_waves=8, distance_pct=4.0, wave0_usd=28.0,
                         partial_last_rung=True, deadline_days=1000.0, tp_pct=1000.0)
        capital = cfg.wave0_usd + 10.0  # $10 left over for rung 1 (which costs far more)
        ledger = Ledger(capital)
        ledger.cash -= cfg.wave0_usd
        candles = _bars(10, 100.0, lambda i, o: o * 0.94)
        state = SessionState("X", candles, 0, cfg)
        for j in range(1, len(candles)):
            r = step_session(state, candles[j], j, cfg, ledger)
            if r["event"] == "partial":
                break
            if r["closed"] is not None:
                break
        assert state.rungs_partial == 1
        assert ledger.cash == 0.0
        assert state.fill_qty[1] > 0.0
        assert state.next_rung == 2  # the partial fill DID advance the pointer (no top-up later)


class TestCashConservation:
    """At every step cash stays non-negative (so cash + deployed >= 0 trivially), and once a
    session's whole life is played out, cash must equal capital plus the dollars it realized —
    to the penny (well within 1e-6)."""

    def test_conservation_across_several_sequential_sessions(self):
        cfg = _make_cfg(False, gate="cashflow", max_waves=6, distance_pct=4.0, wave0_usd=28.0,
                         partial_last_rung=True, deadline_days=6.0, sl_pct=0.0, tp_pct=4.0)
        capital = 500.0
        ledger = Ledger(capital)
        total_realized = 0.0

        def dive_then_pop(i, o):
            return o * 0.95 if i < 3 else o * 1.10

        for k in range(3):  # three sessions back to back, sharing one ledger
            candles = _bars(15, 100.0 + k, dive_then_pop, ts0=k * 15 * DAY_MS)
            assert ledger.cash >= 0.0
            ledger.cash -= cfg.wave0_usd
            assert ledger.cash >= -1e-9
            closed, state = _drive(candles, 0, cfg, ledger)
            assert ledger.cash >= -1e-9
            assert closed is not None, "test data must drive every session to a real exit"
            ledger.cash += closed["deployed_usd"] * (1 + closed["pnl_pct"] / 100.0)
            total_realized += closed["pnl_usd"]
            assert ledger.cash >= -1e-9

        assert abs(ledger.cash - (capital + total_realized)) < 1e-6

    def test_full_portfolio_run_never_goes_cash_negative(self):
        """A cheap end-to-end smoke check that the day-loop itself preserves the same
        invariant — the unit test above pins the engine; this pins the day-loop wiring
        around it (the open/close/mark-to-market bookkeeping in `run_portfolio`)."""
        symbols = [f"SYM{i}" for i in range(6)]

        def path(i, o):
            return o * (1.01 if (i % 5) else 0.9)

        series = {sym: _bars(90, 100.0 + i, path) for i, sym in enumerate(symbols)}
        cfg = Config(capital=1000.0, gate="cashflow", pessimistic=False, distance_pct=4.0,
                     max_waves=6, tp_pct=4.0, sl_pct=0.0, deadline_days=20.0, wave0_usd=28.0,
                     warmup=0, max_sessions=10, max_new_per_day=6, seed=3)
        report = run_portfolio(series, cfg)
        assert all(row["cash"] >= -1e-6 for row in report["equity_curve"])
        assert all(row["equity"] >= -1e-6 for row in report["equity_curve"])


class TestReservationGate:
    """At capital $7,000, gate=reserve, coverage 30%, wave0 $28, 30 rungs: at most 3 concurrent
    sessions can ever be open (7000 / 1855.94 = 3.77 -> 3), and a session's reservation must be
    released on exit — proven by more than 3 sessions opening in total over the run (turnover
    is only possible if freed reservations are reused)."""

    def test_at_most_three_concurrent_and_reservations_are_released(self):
        symbols = [f"SYM{i}" for i in range(12)]

        def flat(i, o):
            return o * 0.999  # drifts gently down; never reaches the (very high) take-profit

        series = {sym: _bars(60, 100.0 + i, flat) for i, sym in enumerate(symbols)}
        cfg = Config(
            capital=7000.0, gate="reserve", pessimistic=False, distance_pct=4.0, max_waves=30,
            tp_pct=1000.0, sl_pct=0.0, deadline_days=3.0, wave0_usd=28.0, coverage_pct=30.0,
            warmup=0, max_sessions=80, max_new_per_day=12, seed=11,
        )
        report = run_portfolio(series, cfg)
        assert max(row["open_n"] for row in report["equity_curve"]) <= 3
        assert report["totals"]["sessions_opened"] > 3  # proves reservations got released and reused


class TestSessionStateHelpers:
    """Small direct checks on the quantity-based average — the generalization that makes a
    partial fill fold correctly into the running price, which the parity test above can't see
    (it never triggers a partial fill)."""

    def test_average_price_is_quantity_weighted_not_slot_weighted(self):
        cfg = _make_cfg(False, max_waves=3, distance_pct=4.0, wave0_usd=28.0)
        candles = _bars(5, 100.0, lambda i, o: o)
        state = SessionState("X", candles, 0, cfg)
        assert _avg_price(state) == 100.0
        assert _waves_touched(state) == 1
        # Simulate a partial fill of rung 1 at price 96, half the full quantity.
        full_qty = 2 * state.unit_qty
        state.fill_qty[1] = full_qty / 2
        state.fill_prices[1] = 96.0
        expected = (state.unit_qty * 100.0 + (full_qty / 2) * 96.0) / (state.unit_qty + full_qty / 2)
        assert abs(_avg_price(state) - expected) < 1e-9
        assert _waves_touched(state) == 2


class TestEquityBackedReserveGate:
    """2026-09-28: the reserve gate must price its budget off (100-equity_backup_pct)% of
    mark-to-market EQUITY, and lock EVERY open session's own `min(fund, spent+coverage*fund)` —
    not the old cash-based proxy — exactly `app.scanner._can_open`/`_session_lock`. A nonzero
    backup must admit STRICTLY FEWER concurrent sessions than the same book with backup=0,
    because the same coverage-based lock now has to fit inside a smaller budget."""

    def _series(self):
        symbols = [f"SYM{i}" for i in range(12)]

        def flat(i, o):
            return o * 0.999  # drifts gently down; never reaches the (very high) take-profit

        return {sym: _bars(60, 100.0 + i, flat) for i, sym in enumerate(symbols)}

    def _cfg(self, **kw):
        defaults = {
            "capital": 7000.0, "gate": "reserve", "pessimistic": False, "distance_pct": 4.0,
            "max_waves": 30, "tp_pct": 1000.0, "sl_pct": 0.0, "deadline_days": 3.0,
            "wave0_usd": 28.0, "coverage_pct": 30.0, "warmup": 0, "max_sessions": 80,
            "max_new_per_day": 12, "seed": 11,
        }
        defaults.update(kw)
        return Config(**defaults)

    def test_backup_reserve_admits_fewer_concurrent_sessions(self):
        series = self._series()
        no_backup = run_portfolio(series, self._cfg(equity_backup_pct=0.0))
        with_backup = run_portfolio(series, self._cfg(equity_backup_pct=25.0))
        max_no_backup = max(row["open_n"] for row in no_backup["equity_curve"])
        max_with_backup = max(row["open_n"] for row in with_backup["equity_curve"])
        assert max_no_backup <= 3    # 7000 / 1855.94 = 3.77 -> 3 (documented in TestReservationGate)
        assert max_with_backup <= 2  # 7000*0.75 = 5250 budget -> only 2 fit under the same lock
        assert max_with_backup < max_no_backup

    def test_session_lock_uses_spent_plus_coverage_capped_at_fund(self):
        cfg = self._cfg(coverage_pct=30.0)
        candles = _bars(10, 100.0, lambda i, o: o)
        state = SessionState("X", candles, 0, cfg, wave0=28.0,
                              fund=full_ladder_cost(4.0, 30, 28.0))
        # Nothing spent beyond wave0: lock = wave0 + 30% of the full ladder.
        assert abs(_session_lock(state, cfg) - (28.0 + 0.30 * state.fund)) < 1e-6
        # Mid-life: lock still tracks spend PLUS the untouched coverage pre-booking...
        state.deployed_usd = state.fund * 0.5
        assert abs(_session_lock(state, cfg) - (state.fund * 0.5 + 0.30 * state.fund)) < 1e-6
        # ...until spend alone pushes past the cap, where it can never exceed the full fund.
        state.deployed_usd = state.fund * 0.9
        assert _session_lock(state, cfg) == state.fund
        state.deployed_usd = state.fund * 1.5
        assert _session_lock(state, cfg) == state.fund


class TestExternalBackstop:
    """2026-09-28: with `backstop=True`, a rung the account's own cash cannot afford fills IN
    FULL from an outside fund instead of starving — and cash must still never go negative. The
    no-backstop run on the SAME price path must show the starvation the backstop eliminates,
    so the cost of NOT having it is visible (the owner's stated purpose)."""

    def _dive_then_rally(self, i, o):
        return o * 0.94 if i < 8 else o * 1.5

    def test_backstop_fills_every_rung_and_never_goes_cash_negative(self):
        cfg = Config(capital=100.0, gate="cashflow", pessimistic=False, distance_pct=4.0,
                     max_waves=6, tp_pct=50.0, sl_pct=0.0, deadline_days=90.0, wave0_usd=28.0,
                     partial_last_rung=False, backstop=True, seed=1)
        candles = _bars(20, 100.0, self._dive_then_rally)
        ledger = Ledger(cfg.capital)
        ledger.cash -= cfg.wave0_usd
        state = SessionState("X", candles, 0, cfg, wave0=cfg.wave0_usd)
        for j in range(1, len(candles)):
            r = step_session(state, candles[j], j, cfg, ledger)
            # run_portfolio settles the bar's backstop debt after all exits (2026-09-28 fix)
            settle_backstop(ledger, f"d{j}")
            assert ledger.cash >= -1e-9
            if r["closed"] is not None:
                break
        assert state.rungs_starved == 0
        assert ledger.external_draw_total > 0
        assert ledger.external_draw_events > 0
        assert ledger.external_peak_outstanding > 0

    def test_no_backstop_same_path_starves_instead(self):
        cfg = Config(capital=100.0, gate="cashflow", pessimistic=False, distance_pct=4.0,
                     max_waves=6, tp_pct=50.0, sl_pct=0.0, deadline_days=90.0, wave0_usd=28.0,
                     partial_last_rung=False, backstop=False, seed=1)
        candles = _bars(20, 100.0, self._dive_then_rally)
        ledger = Ledger(cfg.capital)
        ledger.cash -= cfg.wave0_usd
        state = SessionState("X", candles, 0, cfg, wave0=cfg.wave0_usd)
        for j in range(1, len(candles)):
            r = step_session(state, candles[j], j, cfg, ledger)
            if r["closed"] is not None:
                break
        assert state.rungs_starved > 0
        assert ledger.external_draw_total == 0.0

    def test_full_portfolio_run_repays_the_backstop_from_later_cash_surplus(self):
        symbols = [f"SYM{i}" for i in range(4)]
        series = {sym: _bars(60, 100.0 + i, self._dive_then_rally) for i, sym in enumerate(symbols)}
        cfg = Config(capital=100.0, gate="cashflow", pessimistic=False, distance_pct=4.0,
                     max_waves=6, tp_pct=10.0, sl_pct=0.0, deadline_days=90.0, wave0_usd=28.0,
                     partial_last_rung=False, backstop=True, warmup=0, max_sessions=4,
                     max_new_per_day=4, seed=5)
        report = run_portfolio(series, cfg)
        assert all(row["cash"] >= -1e-6 for row in report["equity_curve"])
        assert all(row["external_outstanding"] >= -1e-6 for row in report["equity_curve"])
        t = report["totals"]
        assert t["external_peak_outstanding_usd"] > 0
        # The rally after day 8 realizes a large profit; some of that surplus cash must have
        # gone to repaying the outside fund rather than sitting idle or funding new opens only.
        assert t["external_outstanding_at_end_usd"] < t["external_peak_outstanding_usd"]


class TestTpFeeBuffer:
    """2026-09-28: `tp_fee_buffer_pct` (mirrors `costengine.tp_fee_buffer_pct()`, ~0.24pp in
    production) raises the TAKE-PROFIT TRIGGER, not just the realized pnl — exactly how
    `evaluate.py` boosts `tp_pct` before calling the frozen simulator. A bar that would clear a
    bare 3% target must NOT exit when the buffer pushes the real trigger to 3.5%."""

    def test_buffer_delays_the_exit_and_raises_realized_pnl(self):
        def path(i, o):
            return o * 1.03 if i == 1 else o * 1.01  # +3% then +~4.1% cumulative

        candles = _bars(5, 100.0, path)

        cfg_no_buffer = _make_cfg(False, max_waves=3, tp_pct=3.0, tp_step_pct=0.0,
                                   cost_pct=0.2, tp_fee_buffer_pct=0.0, deadline_days=1000.0)
        ledger = Ledger(cfg_no_buffer.capital)
        closed_no_buf, _ = _drive(candles, 0, cfg_no_buffer, ledger)
        assert closed_no_buf is not None
        assert closed_no_buf["days"] == 1.0
        assert abs(closed_no_buf["pnl_pct"] - 2.8) < 1e-6  # 3.0 - 0.2

        cfg_buffer = _make_cfg(False, max_waves=3, tp_pct=3.0, tp_step_pct=0.0,
                                cost_pct=0.2, tp_fee_buffer_pct=0.5, deadline_days=1000.0)
        ledger2 = Ledger(cfg_buffer.capital)
        closed_buf, _ = _drive(candles, 0, cfg_buffer, ledger2)
        assert closed_buf is not None
        assert closed_buf["days"] > closed_no_buf["days"]  # the +3% bar alone could not trigger it
        assert abs(closed_buf["pnl_pct"] - 3.3) < 1e-6  # (3.0 + 0.5) - 0.2


# ---------------------------------------------------------------------------------------------
# 2026-09-28 verification pass (Opus): production-parity fixes found while auditing the
# capital-utilization study. Each class pins one bug that made the study's ranking wrong.
# ---------------------------------------------------------------------------------------------


class TestDeepLadderLock:
    """`app.scanner._session_lock` (scanner.py:1340-1342): once a session's `current_wave`
    (== this engine's `next_rung`: the index of the next queued rung = rungs filled) reaches
    `deep_ladder_lock_rungs` (live value 4), the session locks its WHOLE reservation. Omitting
    this lets a stressed book keep opening sessions the live app would refuse."""

    def test_deep_session_locks_the_full_fund(self):
        cfg = Config(capital=7000.0, gate="reserve", pessimistic=False, distance_pct=7.0,
                     max_waves=10, coverage_pct=30.0, deep_lock_rungs=4)
        candles = _bars(10, 100.0, lambda i, o: o)
        fund = full_ladder_cost(7.0, 10, 28.0)
        state = SessionState("X", candles, 0, cfg, wave0=28.0, fund=fund)
        state.next_rung = 3
        assert abs(_session_lock(state, cfg) - min(fund, 28.0 + 0.3 * fund)) < 1e-9
        state.next_rung = 4
        assert _session_lock(state, cfg) == fund

    def test_zero_disables_the_trigger(self):
        cfg = Config(capital=7000.0, gate="reserve", pessimistic=False, distance_pct=7.0,
                     max_waves=10, coverage_pct=30.0, deep_lock_rungs=0)
        candles = _bars(10, 100.0, lambda i, o: o)
        fund = full_ladder_cost(7.0, 10, 28.0)
        state = SessionState("X", candles, 0, cfg, wave0=28.0, fund=fund)
        state.next_rung = 9
        assert _session_lock(state, cfg) < fund


class TestCashFloor:
    """`app.orders._apply_cash_cap` (orders.py:153-172): every BUY is trimmed so cash never
    drops below `cash_floor_usd` (capital-scaled: 20% of anchored equity on paper). The engine
    must honour `ledger.floor` the same way: a rung only spends `cash - floor`."""

    def test_rung_never_spends_below_the_floor(self):
        cfg = _make_cfg(False, gate="cashflow", max_waves=8, distance_pct=4.0, wave0_usd=28.0,
                         partial_last_rung=True, deadline_days=1000.0, tp_pct=1000.0)
        ledger = Ledger(1000.0)
        ledger.cash = 100.0
        ledger.floor = 80.0
        candles = _bars(10, 100.0, lambda i, o: o * 0.94)
        state = SessionState("X", candles, 0, cfg)
        for j in range(1, len(candles)):
            step_session(state, candles[j], j, cfg, ledger)
            assert ledger.cash >= 80.0 - 1e-9
        assert state.rungs_partial == 1  # the $20 above the floor went into one partial rung

    def test_backstop_covers_the_part_below_the_floor(self):
        cfg = _make_cfg(False, gate="cashflow", max_waves=3, distance_pct=4.0, wave0_usd=28.0,
                         partial_last_rung=True, deadline_days=1000.0, tp_pct=1000.0,
                         backstop=True)
        ledger = Ledger(1000.0)
        ledger.cash = 100.0
        ledger.floor = 80.0
        candles = _bars(3, 100.0, lambda i, o: o * 0.95)
        state = SessionState("X", candles, 0, cfg)
        step_session(state, candles[1], 1, cfg, ledger)  # rung 1 = 2 x 28 x ~0.96 ~ $53.8
        assert state.next_rung == 2
        assert abs(ledger.cash - 80.0) < 1e-9
        settle_backstop(ledger, "2026-01-01")
        assert ledger.external_outstanding > 30.0
        assert abs(ledger.cash - 80.0) < 1e-9


class TestBackstopSettlesAfterTheBarsExits:
    """The shortfall is drawn only AFTER every session's exits for that bar are credited - a
    rung whose cash arrives from another session's take-profit on the same bar must not borrow."""

    def _setup(self):
        cfg = _make_cfg(False, gate="cashflow", max_waves=3, distance_pct=4.0, wave0_usd=28.0,
                         partial_last_rung=True, deadline_days=1000.0, tp_pct=1000.0,
                         backstop=True)
        ledger = Ledger(1000.0)
        ledger.cash = 10.0
        candles = _bars(3, 100.0, lambda i, o: o * 0.95)
        state = SessionState("X", candles, 0, cfg)
        step_session(state, candles[1], 1, cfg, ledger)
        return ledger, state

    def test_rung_fills_in_full_and_debt_is_pending_not_drawn(self):
        ledger, state = self._setup()
        assert state.next_rung == 2 and state.rungs_partial == 0 and state.rungs_starved == 0
        assert ledger.cash == 0.0
        assert ledger.bar_debt > 40.0
        assert ledger.external_outstanding == 0.0

    def test_same_bar_exit_cash_pays_the_debt_without_a_draw(self):
        ledger, _ = self._setup()
        debt = ledger.bar_debt
        ledger.cash += 100.0  # another session's take-profit credited on the same bar
        settle_backstop(ledger, "2026-01-01")
        assert ledger.external_outstanding == 0.0 and ledger.external_draw_events == 0
        assert abs(ledger.cash - (100.0 - debt)) < 1e-9
        assert ledger.bar_debt == 0.0

    def test_no_exit_cash_means_the_whole_debt_is_drawn(self):
        ledger, _ = self._setup()
        debt = ledger.bar_debt
        settle_backstop(ledger, "2026-01-01")
        assert abs(ledger.external_outstanding - debt) < 1e-9
        assert ledger.external_peak_outstanding == ledger.external_outstanding
        assert ledger.external_peak_date == "2026-01-01"
        assert ledger.cash == 0.0


class TestBackstopRepayment:
    """Repay the outside fund from cash above what the open book still needs IN FUTURE - the
    un-spent part of each session's lock - plus the cash floor. The old rule kept the whole lock
    (spent + coverage) in cash, double-counting money that already left the wallet."""

    def test_forward_need_excludes_already_spent_cash(self):
        cfg = Config(capital=7000.0, gate="reserve", pessimistic=False, distance_pct=7.0,
                     max_waves=10, coverage_pct=30.0, backstop=True)
        candles = _bars(10, 100.0, lambda i, o: o)
        state = SessionState("X", candles, 0, cfg, wave0=28.0, fund=1000.0)
        state.deployed_usd = 800.0  # lock = min(1000, 800 + 300) = 1000; forward need = 200
        ledger = Ledger(7000.0)
        ledger.cash = 500.0
        ledger.external_outstanding = 400.0
        repay_backstop(ledger, {"X": state}, cfg)
        assert abs(ledger.external_outstanding - 100.0) < 1e-9  # repaid 300 = 500 - 200
        assert abs(ledger.cash - 200.0) < 1e-9


class TestBorrowedMoneyNeverSizesTheBook:
    """With the backstop drawn, gross equity includes the outside fund's money. The reserve-gate
    budget and the %-of-equity wave0 must follow the OWNER's unit-NAV, or a drawdown that forces a
    draw would perversely let the book open MORE/BIGGER sessions on borrowed money."""

    def test_sizing_equity_is_nav_own(self):
        curve = [{"equity": 9000.0, "nav_own": 6000.0}]
        assert _sizing_equity(curve, 7000.0) == 6000.0
        assert _sizing_equity([], 7000.0) == 7000.0


class TestCagrOnOwnPlusPeakDraw:
    """Return on own + peak draw: the owner's total wealth at the end is his own unit-NAV plus
    the whole outside fund (the part still lent to the book sits inside gross equity; the part
    already repaid is back in the fund). The old formula used final GROSS equity only, which
    dropped every dollar already repaid and understated the return."""

    def test_formula(self):
        # own NAV 14,000, peak draw 3,000 (fully repaid) over 1 year on 7,000 own capital
        got = _cagr_total(final_nav_own=14000.0, peak_draw=3000.0, capital=7000.0, years=1.0)
        assert abs(got - ((17000.0 / 10000.0) - 1) * 100) < 1e-9


class TestDelistedCoinIsClosed:
    """A symbol whose data ends (delisted) must be closed at its last close and counted - never
    left open forever holding a slot, a budget lock and a cost basis that reads as utilization."""

    def test_session_on_a_dead_coin_is_realized_at_its_last_close(self):
        dead = _bars(5, 100.0, lambda i, o: o * 0.7)
        alive = _bars(30, 50.0, lambda i, o: o)
        # ALIVE starts one day later, so DEAD is the only candidate on day 0.
        series = {"DEAD": dead, "ALIVE": [dict(b, ts=b["ts"] + DAY_MS) for b in alive]}
        cfg = Config(capital=1000.0, gate="cashflow", pessimistic=False, distance_pct=50.0,
                     max_waves=2, tp_pct=1000.0, sl_pct=0.0, deadline_days=1000.0,
                     wave0_usd=28.0, warmup=0, max_sessions=1, max_new_per_day=1, seed=0)
        report = run_portfolio(series, cfg)
        t = report["totals"]
        assert t["delisted_exits"] == 1
        assert t["delisted_exits_usd"] < 0
        # after the delisting the slot is free again, so ALIVE got opened
        assert t["sessions_opened"] == 2


class TestUtilizationStats:
    """Median daily utilization, utilization on 'normal' days (unit-NAV within 5% of its own
    high-water mark) and over a recent window - a bear market that fills deep rungs and shrinks
    equity must not read as 'money working'."""

    def test_normal_days_exclude_drawdown_days(self):
        curve = [
            {"date": "2021-01-01", "nav_own": 100.0, "utilization_pct": 20.0, "own_utilization_pct": 20.0},
            {"date": "2021-01-02", "nav_own": 110.0, "utilization_pct": 30.0, "own_utilization_pct": 30.0},
            {"date": "2021-01-03", "nav_own": 70.0, "utilization_pct": 150.0, "own_utilization_pct": 90.0},
            {"date": "2021-01-04", "nav_own": 106.0, "utilization_pct": 40.0, "own_utilization_pct": 40.0},
        ]
        s = utilization_stats(curve, recent_since="2021-01-03")
        assert s["util_normal_days_pct"] == 30.0          # median of 20, 30, 40 (day 3 is -36%)
        assert s["normal_days_share_pct"] == 75.0
        assert s["util_median_daily_pct"] == 35.0         # median of 20, 30, 150, 40
        assert s["util_recent_pct"] == 95.0               # mean of 150, 40
        assert s["own_util_normal_days_pct"] == 30.0


# ---------------------------------------------------------------------------------------------
# 2026-09-28b: cash-floor release policies (docs/cash-floor-release-2026-09-28/). Today NOTHING
# ever spends the hard floor (`cash_floor_pct`); it only blocks a BUY and fires `rung_starved`.
# These tests pin policy A (automatic conditional release) and policy B (manual/Telegram release
# with an approval delay), both default-OFF (`floor_release_trigger == ""` / `_manual_delay_days
# == 0`) so every test above this point — proving byte-identical behaviour with the new fields at
# their defaults — is the real parity guard for this whole feature.
# ---------------------------------------------------------------------------------------------


def _floor_cfg(**kw) -> Config:
    defaults = {
        "capital": 1000.0, "gate": "cashflow", "distance_pct": 4.0, "max_waves": 8,
        "wave0_usd": 28.0, "partial_last_rung": True, "deadline_days": 1000.0, "tp_pct": 1000.0,
        "sl_pct": 0.0,
    }
    defaults.update(kw)
    return Config(pessimistic=False, **defaults)


class TestFloorReleaseWave0NeverTouchesFloor:
    """New sessions must NEVER use the floor under policy A or B — only the rung loop
    (next_rung >= 1) was touched; `run_portfolio`'s Step 2 wave-0 open gate is untouched code, so
    this pins the OUTCOME: with the floor eating nearly all of a tiny book, and release turned
    fully on, not a single session should ever open."""

    def test_no_session_opens_when_floor_blocks_wave0_even_with_release_maxed_out(self):
        symbols = [f"SYM{i}" for i in range(4)]
        series = {sym: _bars(30, 100.0 + i, lambda i, o: o * 0.999) for i, sym in enumerate(symbols)}
        cfg = Config(
            capital=100.0, gate="cashflow", pessimistic=False, distance_pct=4.0, max_waves=8,
            wave0_usd=28.0, deadline_days=1000.0, tp_pct=1000.0, warmup=0, max_sessions=10,
            max_new_per_day=4, seed=1, cash_floor_pct=90.0,  # floor = 90% of $100 = $90
            floor_release_trigger="starved", floor_release_min_wave=1, floor_release_frac_pct=100.0,
        )
        report = run_portfolio(series, cfg)
        # cash(100) - floor(90) = 10 < wave0(28): every candidate is refused at open, every day.
        assert report["totals"]["sessions_opened"] == 0
        assert all(row["cash"] >= 90.0 - 1e-6 for row in report["equity_curve"])


class TestFloorReleasePolicyA:
    """Automatic conditional release: an eligible, trigger-active rung may spend the floor down
    to `(1 - frac/100) * floor` instead of starving/partialling at the plain floor."""

    def _dive(self, n=10):
        return _bars(n, 100.0, lambda i, o: o * 0.94)

    def test_release_funds_a_rung_the_plain_floor_would_only_partially_fill(self):
        cfg = self._floor_a_cfg(floor_release_min_wave=1, floor_release_frac_pct=50.0)
        ledger = Ledger(1000.0)
        ledger.cash = 100.0
        ledger.floor = 80.0  # plain avail = 20 (would starve/partial); release avail = 100-40 = 60
        candles = self._dive()
        state = SessionState("X", candles, 0, cfg)
        step_session(state, candles[1], 1, cfg, ledger)
        assert state.next_rung == 2          # rung 1 filled IN FULL (cost ~$53.8 < $60 release room)
        assert state.rungs_partial == 0
        assert state.rungs_starved == 0
        assert ledger.cash < 80.0             # it DID dip below the plain floor...
        assert ledger.cash >= 40.0 - 1e-9     # ...but never past (1-0.5)*80 = 40
        assert ledger.floor_release_usd_total > 0.0
        assert ledger.floor_release_events == 1

    def test_min_wave_gate_blocks_release_below_k(self):
        """K=4: wave 1 does not qualify, so it starves/partials exactly like the plain floor."""
        cfg = self._floor_a_cfg(floor_release_min_wave=4, floor_release_frac_pct=100.0)
        ledger = Ledger(1000.0)
        ledger.cash = 100.0
        ledger.floor = 80.0
        candles = self._dive()
        state = SessionState("X", candles, 0, cfg)
        step_session(state, candles[1], 1, cfg, ledger)
        assert state.rungs_partial == 1       # same outcome as TestCashFloor's no-release case
        assert ledger.cash == 80.0
        assert ledger.floor_release_usd_total == 0.0

    def test_crash_trigger_inactive_behaves_like_no_release(self):
        cfg = self._floor_a_cfg(floor_release_min_wave=1, floor_release_frac_pct=100.0,
                                trigger="crash")
        ledger = Ledger(1000.0)
        ledger.cash = 100.0
        ledger.floor = 80.0
        ledger.crash_release_active_until_ts = 0  # never fired
        candles = self._dive()
        state = SessionState("X", candles, 0, cfg)
        step_session(state, candles[1], 1, cfg, ledger)
        assert state.rungs_partial == 1
        assert ledger.cash == 80.0
        assert ledger.floor_release_usd_total == 0.0

    def test_crash_trigger_active_releases(self):
        cfg = self._floor_a_cfg(floor_release_min_wave=1, floor_release_frac_pct=100.0,
                                trigger="crash")
        ledger = Ledger(1000.0)
        ledger.cash = 100.0
        ledger.floor = 80.0
        candles = self._dive()
        ledger.crash_release_active_until_ts = candles[1]["ts"] + 1  # active on this bar
        state = SessionState("X", candles, 0, cfg)
        step_session(state, candles[1], 1, cfg, ledger)
        assert state.next_rung == 2
        assert ledger.floor_release_usd_total > 0.0

    def _floor_a_cfg(self, *, floor_release_min_wave, floor_release_frac_pct, trigger="starved"):
        return _floor_cfg(floor_release_trigger=trigger, floor_release_min_wave=floor_release_min_wave,
                          floor_release_frac_pct=floor_release_frac_pct)


class TestFloorReleasePolicyB:
    """Manual release: a starved, eligible rung raises a REQUEST instead of releasing inline; it
    is only bought `manual_delay_days` later, and only if approved AND the rung's price is still
    touched that day (limit semantics — reuses `_fill_price`)."""

    def _touch_then_hold(self, entry: float, target1: float, low_day2: float) -> list[dict]:
        """day0 = the session's own start bar (flat at `entry`); day1 touches `target1`
        intraday (starves under the plain floor, raising a request); day2 is the D=1 decision
        day, whose low is `low_day2` (below target1 = still touched/fillable, above = missed)."""
        return [
            {"ts": 0, "open": entry, "high": entry * 1.001, "low": entry * 0.995, "close": entry},
            {"ts": DAY_MS, "open": entry, "high": entry * 1.001, "low": target1 - 1.0, "close": entry},
            {"ts": 2 * DAY_MS, "open": entry, "high": entry * 1.001, "low": low_day2, "close": entry},
        ]

    def _cfg_and_state(self, entry=100.0, **over):
        # partial_last_rung=False: a true STARVE (not a plain-floor partial fill) must happen on
        # day 1 for a request to be worth raising — otherwise the plain floor's own leftover cash
        # would silently fund a partial fill and there would be nothing left to release.
        cfg = _floor_cfg(distance_pct=4.0, max_waves=8, partial_last_rung=False,
                         floor_release_manual_delay_days=1.0, floor_release_min_wave=1,
                         floor_release_frac_pct=50.0, **over)
        target1 = _targets(entry, cfg.distance_pct, cfg.max_waves)[1]
        return cfg, target1

    def test_request_is_not_filled_the_same_day_it_is_raised(self):
        cfg, target1 = self._cfg_and_state()
        candles = self._touch_then_hold(100.0, target1, low_day2=target1 - 1.0)
        ledger = Ledger(1000.0)
        ledger.cash, ledger.floor = 100.0, 80.0  # avail=20 < rung cost: starves, request raised
        state = SessionState("X", candles, 0, cfg)
        step_session(state, candles[1], 1, cfg, ledger)
        assert state.next_rung == 1                    # NOT filled yet
        assert state.rungs_starved == 1
        assert ledger.manual_requests_sent == 1
        assert state.pending_release is not None

    def test_approved_and_still_touched_fills_on_the_decision_day(self):
        cfg, target1 = self._cfg_and_state()
        candles = self._touch_then_hold(100.0, target1, low_day2=target1 - 1.0)  # still touched
        ledger = Ledger(1000.0)
        ledger.cash, ledger.floor = 100.0, 80.0
        state = SessionState("X", candles, 0, cfg)
        step_session(state, candles[1], 1, cfg, ledger)   # day 1: raises the request
        step_session(state, candles[2], 2, cfg, ledger)   # day 2: the decision day
        assert state.next_rung == 2
        assert state.pending_release is None
        assert ledger.manual_requests_approved == 1
        assert ledger.floor_release_usd_total > 0.0
        assert ledger.cash >= 80.0 * (1 - 0.5) - 1e-9    # never past the R=50% release cap

    def test_missed_when_price_has_rebounded_by_the_decision_day(self):
        cfg, target1 = self._cfg_and_state()
        candles = self._touch_then_hold(100.0, target1, low_day2=target1 + 1.0)  # rebounded
        ledger = Ledger(1000.0)
        ledger.cash, ledger.floor = 100.0, 80.0
        state = SessionState("X", candles, 0, cfg)
        step_session(state, candles[1], 1, cfg, ledger)
        step_session(state, candles[2], 2, cfg, ledger)
        assert state.next_rung == 1                     # never filled
        assert ledger.manual_requests_missed_price == 1
        assert ledger.manual_requests_approved == 0
        assert ledger.cash == 100.0                      # untouched — no spend happened at all

    def test_crash_only_owner_denies_without_the_crash_signal(self):
        cfg, target1 = self._cfg_and_state(floor_release_manual_owner="crash_only")
        candles = self._touch_then_hold(100.0, target1, low_day2=target1 - 1.0)
        ledger = Ledger(1000.0)
        ledger.cash, ledger.floor = 100.0, 80.0
        ledger.crash_release_active_until_ts = 0  # never fired
        state = SessionState("X", candles, 0, cfg)
        step_session(state, candles[1], 1, cfg, ledger)
        step_session(state, candles[2], 2, cfg, ledger)
        assert state.next_rung == 1
        assert ledger.manual_requests_denied == 1
        assert ledger.manual_requests_approved == 0

    def test_crash_only_owner_approves_while_the_crash_signal_is_active(self):
        cfg, target1 = self._cfg_and_state(floor_release_manual_owner="crash_only")
        candles = self._touch_then_hold(100.0, target1, low_day2=target1 - 1.0)
        ledger = Ledger(1000.0)
        ledger.cash, ledger.floor = 100.0, 80.0
        ledger.crash_release_active_until_ts = candles[2]["ts"] + 1  # active on the decision day
        state = SessionState("X", candles, 0, cfg)
        step_session(state, candles[1], 1, cfg, ledger)
        step_session(state, candles[2], 2, cfg, ledger)
        assert state.next_rung == 2
        assert ledger.manual_requests_approved == 1


class TestCrashReleaseTriggerBookkeeping:
    """`run_portfolio` computes the crash-release breadth series and flips the trigger on/off day
    by day; this is the day-loop wiring the unit tests above (which set `crash_release_active_
    until_ts` directly) don't exercise."""

    def test_a_sharp_synchronized_drop_fires_the_trigger_and_it_stays_active_for_the_window(self):
        symbols = [f"SYM{i}" for i in range(40)]

        def flat(i, o):
            return o * 1.0

        series = {sym: _bars(20, 100.0, flat) for sym in symbols}
        # Day 5: every symbol gaps 30% below its own day-4 high in one bar.
        crash_day = 5
        for sym in symbols:
            b = series[sym][crash_day]
            prev_high = series[sym][crash_day - 1]["high"]
            b["low"] = prev_high * 0.70
            b["close"] = prev_high * 0.72
            b["open"] = prev_high * 0.72
        cfg = Config(
            capital=100_000.0, gate="cashflow", pessimistic=False, distance_pct=4.0, max_waves=5,
            wave0_usd=28.0, deadline_days=1000.0, tp_pct=1000.0, warmup=0, max_sessions=1,
            max_new_per_day=0, seed=0,  # no sessions open at all — isolates the trigger bookkeeping
            floor_release_trigger="crash", floor_release_min_wave=1, floor_release_frac_pct=50.0,
            floor_release_crash_drop_pct=20.0, floor_release_crash_breadth_pct=60.0,
            floor_release_crash_window_days=3.0,
        )
        report = run_portfolio(series, cfg)
        t = report["totals"]
        assert t["crash_release_episodes"] == 1
        assert t["crash_release_active_days"] >= 4          # the crash day itself + the 3-day window
        assert len(t["crash_release_fired_dates"]) == 1


# ---------------------------------------------------------------------------------------------
# 2026-09-28c: adversarial verification of the cash-floor release study (Opus). Three bugs in
# the first draft: (1) `floor_release_usd` re-counted the deficit the book was ALREADY carrying
# below the floor on every later fill, so a $7k book "released" $0.5-7.7M; (2) the crash trigger
# used bar t's own low and the whole day's cross-section to release a rung filled on that same
# bar t (lookahead); (3) `starved_usd` sums the same rung once per day it is retried, which is
# not a count of rungs. These tests pin the corrected definitions.
# ---------------------------------------------------------------------------------------------


class TestFloorReleaseAccountingIsCappedBySpend:
    def test_release_usd_counts_only_the_part_of_this_fill_below_the_floor(self):
        cfg = _floor_cfg(floor_release_trigger="starved", floor_release_min_wave=1,
                         floor_release_frac_pct=100.0)
        ledger = Ledger(1000.0)
        ledger.cash = 60.0    # ALREADY $20 below the floor (yesterday's release, floor re-sized)
        ledger.floor = 80.0
        candles = _bars(10, 100.0, lambda i, o: o * 0.94)
        state = SessionState("X", candles, 0, cfg)
        step_session(state, candles[1], 1, cfg, ledger)
        spent = 60.0 - ledger.cash
        assert state.next_rung == 2 and spent > 0
        # Every dollar of this fill came from below the floor -- but not a cent more than it.
        assert abs(ledger.floor_release_usd_total - spent) < 1e-9
        assert ledger.floor_release_events == 1


class TestDistinctStarvedRungs:
    def test_a_rung_retried_on_three_bars_is_one_distinct_starved_rung(self):
        cfg = _floor_cfg(partial_last_rung=False)
        ledger = Ledger(1000.0)
        ledger.cash, ledger.floor = 100.0, 95.0   # $5 spendable: rung 1 can never fill
        candles = _bars(10, 100.0, lambda i, o: o * 0.97)
        state = SessionState("X", candles, 0, cfg)
        for j in range(1, 5):
            step_session(state, candles[j], j, cfg, ledger)
        assert state.next_rung == 1
        assert state.rungs_starved >= 3                 # retried every bar it was touched
        assert ledger.starved_rungs_distinct == 1       # ...but it is ONE rung
        first_cost = 2 * state.unit_qty * state.targets[1]
        assert abs(ledger.starved_distinct_usd - first_cost) / first_cost < 0.05


class TestCrashReleaseTriggerIsLagged:
    """The breadth of bar t needs bar t's low and every symbol's bar t; it is only KNOWN at the
    close. With `floor_release_crash_lag_bars=1` (the default) the trigger can first release a
    rung on bar t+1; 0 reproduces the first draft's same-bar (lookahead) behaviour."""

    def _series(self):
        symbols = [f"SYM{i}" for i in range(40)]
        series = {sym: _bars(20, 100.0, lambda i, o: o * 1.0) for sym in symbols}
        for sym in symbols:
            b = series[sym][5]
            prev_high = series[sym][4]["high"]
            b["low"], b["close"], b["open"] = prev_high * 0.70, prev_high * 0.72, prev_high * 0.72
        return series

    def _cfg(self, lag):
        return Config(
            capital=100_000.0, gate="cashflow", pessimistic=False, distance_pct=4.0, max_waves=5,
            wave0_usd=28.0, deadline_days=1000.0, tp_pct=1000.0, warmup=0, max_sessions=1,
            max_new_per_day=0, seed=0, floor_release_trigger="crash", floor_release_min_wave=1,
            floor_release_frac_pct=50.0, floor_release_crash_drop_pct=20.0,
            floor_release_crash_breadth_pct=60.0, floor_release_crash_window_days=3.0,
            floor_release_crash_lag_bars=lag,
        )

    def test_lag_one_activates_the_bar_after_the_crash(self):
        rep = run_portfolio(self._series(), self._cfg(1))
        assert rep["totals"]["crash_release_fired_dates"] == ["1970-01-07"]  # bar 6, not bar 5

    def test_lag_zero_is_the_same_bar_lookahead_version(self):
        rep = run_portfolio(self._series(), self._cfg(0))
        assert rep["totals"]["crash_release_fired_dates"] == ["1970-01-06"]  # bar 5

    def test_default_is_lagged(self):
        assert Config(capital=1.0, gate="cashflow", pessimistic=False).floor_release_crash_lag_bars == 1


def _bars_to_hourly(daily: list[dict]) -> list[dict]:
    """Expand a daily candle series into 24 hourly bars/day with IDENTICAL trading content: the
    day's full O/H/L/C lands entirely on hour 0 (so every fill/TP/deadline check that would have
    happened on the daily bar happens on that hour, at the same intrabar bound), and hours
    1..23 are flat at the day's close (zero range, no new information). Used to prove
    `run_portfolio` reduces the same way on hourly bars as on daily ones for every
    day-denominated knob."""
    out = []
    for day in daily:
        day_start = day["ts"]
        out.append({"ts": day_start, "open": day["open"], "high": day["high"],
                    "low": day["low"], "close": day["close"]})
        for h in range(1, 24):
            c = day["close"]
            out.append({"ts": day_start + h * HOUR_MS, "open": c, "high": c, "low": c, "close": c})
    return out


class TestHourlyBarsReduceToDaily:
    """2026-09-29: the day-by-day loop was written and tested against `1d` data only, where
    "one bar" and "one calendar day" are the same thing. Three places silently depended on
    that: `max_new_per_day`'s reset, the universe-breadth brake's and the cash-floor crash
    trigger's lookback windows (both counted in bars), and CAGR/drawdown/yearly stats (which
    need one equity sample per day, not per bar). These tests pin the fix — a portfolio replay
    on hourly bars must gate opens per calendar day and report the same day-level numbers a
    daily replay of the identical trading content would."""

    def test_bars_per_day_detected_from_spacing(self):
        daily_ts = [i * DAY_MS for i in range(50)]
        hourly_ts = [i * HOUR_MS for i in range(50)]
        assert _detect_bars_per_day(daily_ts) == 1.0
        assert _detect_bars_per_day(hourly_ts) == 24.0
        assert _detect_bars_per_day([0]) == 1.0  # too short to detect anything: fall back safely

    def test_max_new_per_day_is_a_calendar_day_budget_not_a_per_bar_one(self):
        """Before the fix, `new_today` was reset to 0 on EVERY bar. On daily data that is
        harmless (every bar starts a new date), but on hourly data it let up to
        `max_new_per_day` sessions open in a single HOUR — 24x the intended rate."""
        symbols = [f"SYM{i}" for i in range(100)]

        def flat(i, o):
            return o * 0.999  # drifts down slowly; never closes within the test window

        n_days = 4
        series = {
            sym: _bars_to_hourly(_bars(n_days, 100.0 + i, flat))
            for i, sym in enumerate(symbols)
        }
        cfg = Config(capital=1_000_000.0, gate="cashflow", pessimistic=False, distance_pct=4.0,
                     max_waves=6, tp_pct=1000.0, sl_pct=0.0, deadline_days=1000.0, wave0_usd=28.0,
                     warmup=0, max_sessions=80, max_new_per_day=3, seed=5)
        report = run_portfolio(series, cfg)
        assert report["totals"]["sessions_opened"] == 3 * n_days

    def test_cagr_and_drawdown_match_between_daily_and_hourly_representations(self):
        """Same trading content (see `_bars_to_hourly`), different bar granularity: CAGR,
        drawdown and the equity-curve length (one row per calendar day) must come out the same
        either way — the report must not silently treat 24x more rows as 24x more elapsed time."""
        def path(i, o):
            return o * (1.15 if (i % 10 == 0) else 0.97)  # occasional pump, steady bleed

        daily = _bars(120, 100.0, path)
        hourly = _bars_to_hourly(daily)
        cfg_kwargs = {
            "capital": 1000.0, "gate": "cashflow", "pessimistic": True, "distance_pct": 4.0,
            "max_waves": 6, "tp_pct": 6.0, "sl_pct": 0.0, "deadline_days": 25.0,
            "wave0_usd": 28.0, "warmup": 0, "max_sessions": 5, "max_new_per_day": 1, "seed": 9,
        }
        daily_report = run_portfolio({"X": daily}, Config(**cfg_kwargs))
        hourly_report = run_portfolio({"X": hourly}, Config(**cfg_kwargs))
        assert daily_report["totals"]["cagr_pct"] == pytest.approx(
            hourly_report["totals"]["cagr_pct"], abs=0.05)
        assert daily_report["totals"]["max_drawdown_pct"] == pytest.approx(
            hourly_report["totals"]["max_drawdown_pct"], abs=0.05)
        assert len(daily_report["equity_curve"]) == len(hourly_report["equity_curve"])

    def test_universe_breadth_lookback_scales_to_the_same_number_of_days(self):
        """The universe-breadth brake's `lookback` is written in BARS against a daily
        assumption (default 24 == 24 DAYS). On hourly bars this must become
        24 days * 24 bars/day, not stay 24 bars (== 1 day) — otherwise the brake compares each
        bar to little more than its own last hour instead of its trailing month."""
        from scripts.capital_portfolio_study import universe_breadth

        def path(i, o):
            return o * 0.995

        daily = {"X": _bars(40, 100.0, path)}
        hourly = {"X": _bars_to_hourly(daily["X"])}
        daily_breadth = universe_breadth(daily, drop_pct=10.0, lookback=24)
        hourly_breadth = universe_breadth(hourly, drop_pct=10.0, lookback=round(24 * 24))
        # Compare on the calendar days both series share: the day-0 hourly bar (hour 0) carries
        # the same O/H/L/C as the whole daily bar, so the two breadth readings must agree there.
        for day in daily["X"]:
            assert hourly_breadth[day["ts"]] == pytest.approx(daily_breadth[day["ts"]], abs=1e-9)


class TestWarmupIsListingAgeInDaysOnHourlyBars:
    """2026-09-29 verification: `warmup` (default 24) was counted in BARS. On daily data that is
    24 days of listing age -- close to production's `scanner._MIN_CANDLES` = 30 daily candles.
    On hourly data it silently became 24 HOURS, so a coin was a candidate one day after it
    listed. On hourly bars it must mean 24 calendar days since the symbol's LISTING (passed in
    as `listing_ts`, e.g. from the 1d table -- the 1h series itself may start at the dataset's
    own first month, long after the real listing)."""

    def _series(self, n_days: int) -> dict:
        flat = lambda i, o: o  # noqa: E731 - never reaches TP/deadline below
        return {
            "OLD": _bars_to_hourly(_bars(n_days, 100.0, flat)),
            "NEW": _bars_to_hourly(_bars(n_days, 50.0, flat)),
        }

    def _cfg(self) -> Config:
        return Config(capital=1_000_000.0, gate="cashflow", pessimistic=False, distance_pct=4.0,
                      max_waves=6, tp_pct=1000.0, sl_pct=0.0, deadline_days=1000.0,
                      wave0_usd=28.0, warmup=24, max_sessions=80, max_new_per_day=10, seed=3)

    def test_fresh_listing_waits_24_days_old_listing_is_eligible_at_once(self):
        series = self._series(30)
        listing = {"OLD": series["OLD"][0]["ts"] - 400 * DAY_MS, "NEW": series["NEW"][0]["ts"]}
        rep = run_portfolio(series, self._cfg(), listing_ts=listing)
        open_n = [r["open_n"] for r in rep["equity_curve"]]
        assert open_n[:24] == [1] * 24   # OLD from day 0; NEW not before 24 days of age
        assert open_n[24:] == [2] * 6

    def test_without_listing_the_series_start_counts_as_listing(self):
        rep = run_portfolio(self._series(30), self._cfg())
        open_n = [r["open_n"] for r in rep["equity_curve"]]
        assert open_n[:24] == [0] * 24
        assert open_n[24:] == [2] * 6

    def test_daily_bars_keep_the_bar_count(self):
        flat = lambda i, o: o  # noqa: E731
        series = {"OLD": _bars(30, 100.0, flat), "NEW": _bars(30, 50.0, flat)}
        listing = {"OLD": -400 * DAY_MS, "NEW": 0}
        rep = run_portfolio(series, self._cfg(), listing_ts=listing)
        open_n = [r["open_n"] for r in rep["equity_curve"]]
        assert open_n[:24] == [0] * 24   # 1d path unchanged: idx >= warmup, listing ignored
        assert open_n[24:] == [2] * 6
