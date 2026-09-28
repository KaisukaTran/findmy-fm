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

from app.backtest import simulate_kss
from scripts.capital_portfolio_study import (
    Config,
    Ledger,
    SessionState,
    _avg_price,
    _cagr_total,
    _session_lock,
    _sizing_equity,
    _waves_touched,
    full_ladder_cost,
    repay_backstop,
    run_portfolio,
    settle_backstop,
    step_session,
    utilization_stats,
)

DAY_MS = 86_400_000


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
