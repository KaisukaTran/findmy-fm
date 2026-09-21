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
    _waves_touched,
    full_ladder_cost,
    run_portfolio,
    step_session,
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
