"""What does the CURRENT paper configuration do to a $5,000-$7,000 real account?

WHY THIS EXISTS
    Every study in this directory so far (`ladder_depth_study.py`, `ladder_grid_study.py`,
    `ladder_panel_study.py`) measures sessions in ISOLATION with unlimited cash: each trial
    gets its own private wallet, so the per-trial win-rate/expectancy numbers are honest about
    the LADDER MATH but silent about whether the money to run that ladder actually exists. The
    product owner's real account will not be unlimited — he asked what $5,000-$7,000 does.

    That is a different kind of question. With a small account, cash itself becomes the
    binding constraint: deep DCA rungs cannot always be funded, new sessions cannot always
    open even when a good-looking coin is available, and profit stops scaling with the
    per-session statistics once the book is capital-bound rather than opportunity-bound. No
    existing script has a cash ledger, so this one is new: a day-by-day PORTFOLIO replay that
    opens and closes many sessions against one shared pool of real dollars.

WHAT IS REUSED, ON PURPOSE (the frozen math is not re-derived here)
    The per-bar fill/exit semantics are byte-for-byte the same as `app.backtest.simulate_kss`
    (read that file's `simulate_kss` in full before touching this one) — targets, weights,
    the running average, take-profit with `tp_step_pct`, the hard stop, the deadline, Ride &
    Trail v2, and both intra-bar bounds. This script's engine is a GENERALIZATION of that
    function, not a rewrite of its formulas: `step_session` below reproduces its branching
    order exactly (see the docstring on `step_session`), only adding the ability for a rung
    fill to come back PARTIAL or STARVED when the shared ledger cannot afford it. With
    unlimited capital, `step_session` degenerates to `simulate_kss` exactly — that equivalence
    is `tests/app/test_capital_portfolio.py`'s critical test, and if it ever fails the engine
    below is wrong, not the frozen one.

    Loading and candle shaping are reused from existing scripts too: `liquidity_tier_study.load`
    (the sqlite reader) and `ladder_panel_study.to_candles` / `.reserved_capital` (the ladder's
    full pre-booked cost, the same number `app.capital.ladder_budget_exceeded` gates on live).

WHAT IS NEW: THE CASH LEDGER
    One `cash` balance and one `reserved` balance shared across every open session. Each day
    (this dataset is daily bars — `1d` covers 2021-01-01..2026-07-31), open sessions are
    advanced FIRST (exits are never gated, mirroring the live invariant), then new sessions may
    open against whatever cash/headroom remains, then the day's equity is marked to market.
    A rung that cannot be funded either shrinks (`--partial-last-rung`, on by default, mirrors
    the live `kss_partial_last_rung_enabled`) or is skipped and RETRIED on a later bar once cash
    frees up — the ladder's pointer only advances on an actual (full or partial) fill, never on
    a skip, exactly like the live ATOM#16 behaviour it mirrors.

    Two capital gates are modelled, because the live app's own gate
    (`app.capital.ladder_budget_exceeded`) is a PROMISE, not a real-time cash check:
      * `gate=reserve` (default): pre-books `ladder_coverage_pct`% of a session's full
        30-rung cost before it may open — the live default posture (see
        `docs/capital-coverage-2026-09-16.md`). This is the honest live analogue.
      * `gate=cashflow`: no pre-booking, only "does today's wave 0 fit in cash right now" — the
        loosest possible bound, useful as a ceiling on how much `reserve` costs the owner.

WHAT IS NOT MODELLED (say it before someone reads the number as gospel)
    - No entry-quality gate. Every eligible symbol on every eligible day is an equally likely
      candidate, chosen by a seeded shuffle. This is deliberate, not an oversight: the
      project's own 2026-09-10 permutation study (`docs/gate-permutation-2026-09-10...`, see
      `MEMORY.md`) found that NONE of the six live entry gates beat random selection at
      Westfall-Young-corrected significance. Modelling the scanner as random entry is therefore
      the honest, MEASURED choice for this study, not a shortcut taken to save time.
    - No liquidity/volume floor, no per-symbol correlation control beyond "one open session per
      symbol", no slippage beyond `--cost` (the flat round-trip %), no partial-fill queue delay
      (a fundable rung fills the bar it is touched, same as the frozen simulator).
    - The Ride & Trail v2 dynamic exit is included (it is in the frozen simulator), but nothing
      here models pyramid-up or the scanner's regime router — this is the KSS ladder alone.

TRAPS THIS SCRIPT HAD TO AVOID (recorded because they are exactly the kind of bug that makes a
capital study lie by looking plausible)
    - Advancing a starved rung's pointer anyway. That would silently convert "could not afford
      it" into "chose not to fill it", inflating win-rate by skipping over exactly the bars a
      small account cannot use. The pointer only moves on a real (possibly partial) fill.
    - Computing the running average from `weight[i]` (the FULL geometric weight) once any rung
      has been partially filled. This engine tracks actual QUANTITY bought per rung, not the
      nominal weight, and averages on quantity — reducing exactly to the frozen weight-based
      average whenever every fill is full (the parity test depends on this).
    - Treating `reserved` as real cash leaving the wallet. It does not: it is headroom
      accounting only (mirrors `ladder_budget_exceeded`'s worst-case promise), and actual cash
      only moves on a real fill. Confusing the two would make `gate=reserve` double-charge.
    - Letting a session that runs off the end of the data window count as a loss (or a win). It
      is reported separately as "still open at end" with its mark-to-market P&L, never folded
      into realized wins/losses/expectancy — exactly the `data_end` bucket `ladder_depth_study`
      already had to invent for the same reason.

    python scripts/capital_portfolio_study.py --capital 5000 7000 --gate reserve cashflow \
        --since 2023-08-01 --until 2026-07-31 --interval 1d \
        --wave0 28 --waves 30 --distance 4 --tp 5 --tp-step 0.5 --sl 0 --deadline 60 \
        --trail-after-tp 3 --coverage 30 --cost 0.2 --max-sessions 80 --max-new-per-day 5 \
        --seed 7 --out data/research/studies/capital-portfolio-2026-09-20
"""

from __future__ import annotations

import argparse
import math
import random
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from multiprocessing import Pool
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import costengine  # noqa: E402
from app.backtest import _MS_PER_DAY, _fill_price, _targets  # noqa: E402
from scripts.ladder_panel_study import reserved_capital as full_ladder_cost  # noqa: E402
from scripts.ladder_panel_study import to_candles  # noqa: E402
from scripts.liquidity_tier_study import load  # noqa: E402

# ---------------------------------------------------------------------------------------------
# Config / state
# ---------------------------------------------------------------------------------------------


@dataclass
class Config:
    """Every knob a single run needs. One `Config` = one (capital, gate, bound) combination."""

    capital: float
    gate: str  # "reserve" | "cashflow"
    pessimistic: bool  # the intra-bar bound for THIS run

    distance_pct: float = 4.0
    max_waves: int = 30
    tp_pct: float = 5.0
    tp_step_pct: float = 0.5
    sl_pct: float = 0.0
    deadline_days: float = 60.0
    trail_after_tp_pct: float = 3.0
    cost_pct: float = 0.2
    wave0_usd: float = 28.0

    # --- market-wide crash brake (all zero = OFF = byte-identical to an unbraked run) ---
    # Depths are FRACTIONS of `max_waves`, not rung counts, so a threshold survives a change of
    # ladder length. Breadth is the share of currently-OPEN sessions at or past that depth.
    brake_warn_depth_frac: float = 0.0
    brake_warn_breadth_pct: float = 0.0
    brake_halt_depth_frac: float = 0.0
    brake_halt_breadth_pct: float = 0.0
    brake_resume_days: float = 3.0   # how long the owner takes to confirm and lift the halt
    brake_suspend_deadline: bool = True  # halted time does not count against a session's deadline
    # Universe-breadth brake. Session-breadth (above) is unusable at small capital: at $5,000 the
    # book holds a MEDIAN OF 2 open sessions, so "30% of sessions" is one unlucky coin, and the
    # brake halts the book on noise. The scanned universe has hundreds of members no matter how
    # much capital there is, so a market-wide signal keeps its sample size. Drop is measured
    # against each symbol's own high over `brake_universe_lookback` bars. 0 = off.
    brake_universe_drop_pct: float = 0.0
    brake_universe_breadth_pct: float = 0.0
    brake_universe_lookback: int = 24

    # Mirrors app.capital_scale.first_wave_usd: when > 0, each NEW session's first wave is this
    # % of the book's equity at the close of the previous bar, floored at `wave0_floor` (the
    # exchange minimum). 0 = the fixed `wave0_usd` (every run before 2026-09-21 — byte-identical).
    # The app anchors equity with a 10% deadband; this uses the previous bar's equity directly,
    # which differs from the app only by that smoothing.
    wave0_pct: float = 0.0
    wave0_floor: float = 10.0
    wave0_cap: float = 0.0   # mirrors app first_wave_max_usd: >0 caps the %-sized wave in dollars

    warmup: int = 24
    max_sessions: int = 80
    max_new_per_day: int = 5
    coverage_pct: float = 30.0
    # Mirrors app.scanner's equity_backup_pct: the reserve-gate budget is (100-this)% of
    # mark-to-market equity, never the raw cash balance. 0 = off (whole equity deployable) —
    # kept off by default so every pre-2026-09-28 caller of this module (study_wave_cap.py,
    # capital_montecarlo.py, the study_k7_* family, TestReservationGate) is unaffected; the
    # 2026-09-28 capital-utilization study passes the live value (24.8) explicitly.
    equity_backup_pct: float = 0.0
    partial_last_rung: bool = True
    # 2026-09-28: when a rung is due and the account's own cash cannot fund it (after this bar's
    # exits are booked), draw the shortfall from an outside fund instead of starving/partialling
    # it. Off by default (byte-identical to every run before this date). See
    # docs/capital-utilization-2026-09-28/report.md for the repayment rule and what it costs.
    backstop: bool = False
    # Mirrors costengine.tp_fee_buffer_pct(): added to the take-profit TRIGGER (so a TP is
    # slightly harder to reach) and therefore to the realized pnl at that trigger, exactly the
    # way evaluate.py boosts tp_pct before calling the frozen simulator. 0 = off (byte-identical
    # to every run before this date, incl. the parity test's cost_pct-only accounting).
    tp_fee_buffer_pct: float = 0.0
    # 2026-09-28 (verification pass): mirrors app.scanner._session_lock's DEPTH TRIGGER
    # (scanner.py:1340-1342, live `deep_ladder_lock_rungs` = 4 on paper): a session whose
    # `current_wave` (== `next_rung` here: rungs filled) reached this locks its WHOLE fund
    # against the reserve-gate budget. 0 = off (byte-identical to every earlier run).
    deep_lock_rungs: int = 0
    # Mirrors app.orders._apply_cash_cap + capital_scale.cash_floor_usd (orders.py:153-172):
    # every BUY is trimmed so cash never drops below this % of the owner's equity (live
    # `cash_floor_pct` = 20 with capital scaling on). 0 = off (byte-identical to earlier runs).
    cash_floor_pct: float = 0.0
    seed: int = 7

    def label(self) -> str:
        bound = "pessimistic" if self.pessimistic else "optimistic"
        return f"cap{self.capital:g}_{self.gate}_{bound}"


class SessionState:
    """One open pyramid session's mutable state. Quantity-based (not weight-based) so a
    partially-funded rung still folds correctly into the running average — see the module
    docstring's "TRAPS" section. Reduces exactly to `simulate_kss`'s weight-based bookkeeping
    whenever every rung fills in full.
    """

    __slots__ = (
        "symbol", "candles", "start", "j", "entry", "entry_ts", "targets", "max_waves",
        "unit_qty", "fill_qty", "fill_prices", "next_rung", "deployed_usd", "fund",
        "mae_pct", "armed", "floor", "peak", "eff_tp_at_arm", "rungs_starved", "rungs_partial",
        "last_close", "capital_days_acc", "prev_ts", "open_month", "halt_credit_days",
    )

    def __init__(self, symbol: str, candles: list[dict], start: int, cfg: Config,
                 wave0: float | None = None, fund: float = 0.0):
        self.symbol = symbol
        self.candles = candles
        self.start = start
        self.j = start + 1
        entry = candles[start]["close"]
        self.entry = entry
        self.entry_ts = candles[start]["ts"]
        self.targets = _targets(entry, cfg.distance_pct, cfg.max_waves)
        self.max_waves = cfg.max_waves
        w0 = cfg.wave0_usd if wave0 is None else wave0
        self.unit_qty = w0 / entry if entry > 0 else 0.0
        self.fill_qty = [0.0] * cfg.max_waves
        self.fill_prices = [entry] + list(self.targets[1:])
        self.fill_qty[0] = self.unit_qty  # wave 0 always fills at entry, weight 1
        self.next_rung = 1
        self.deployed_usd = w0
        # Full ladder cost at THIS session's own wave0 (mirrors the live `isolated_fund` a
        # session is opened with) — the number `_session_lock` reserves coverage_pct of.
        self.fund = fund
        self.mae_pct = 0.0
        self.armed = False
        self.floor = 0.0
        self.peak = 0.0
        self.eff_tp_at_arm = 0.0
        self.rungs_starved = 0
        self.rungs_partial = 0
        self.last_close = entry
        self.capital_days_acc = 0.0
        self.prev_ts = self.entry_ts
        self.open_month = ""
        self.halt_credit_days = 0.0  # wall-clock days spent under a halt, refunded to the deadline


class Ledger:
    """The shared cash ledger. `reserved` is headroom accounting only (mirrors the live
    `ladder_budget_exceeded` promise) — actual cash only ever moves on a real fill."""

    def __init__(self, capital: float):
        self.cash = capital
        self.reserved = 0.0
        self.realized_usd = 0.0
        self.rungs_starved_total = 0
        self.rungs_partial_total = 0
        self.starved_usd_total = 0.0  # dollars NOT deployed because of a starved/partial rung
        # --- crash brake ---
        self.halted = False
        self.halt_until_ts = 0        # resume time once the owner confirms (brake_resume_days)
        self.halt_episodes = 0
        self.halt_bars = 0            # bars spent halted, for the report
        self.warn_bars = 0
        self.rungs_blocked = 0        # rungs the halt refused (distinct from cash starvation)
        self.blocked_usd = 0.0
        self.opens_blocked = 0
        # --- external backstop (2026-09-28) ---
        self.external_outstanding = 0.0    # currently owed to the outside fund
        self.external_draw_total = 0.0     # cumulative GROSS draws (never reduced by repayment)
        self.external_draw_events = 0
        self.external_peak_outstanding = 0.0
        self.external_draw_log: list[tuple[str, str, float]] = []  # (date, "*", usd) per settlement
        self.external_peak_date = ""
        # Cash floor for THIS bar (set by run_portfolio from the previous bar's own unit-NAV).
        self.floor = 0.0
        # Backstopped rung cost this bar that own cash above the floor could not pay YET. It is
        # settled by `settle_backstop` only after every session's exits for the bar are credited.
        self.bar_debt = 0.0


def _waves_touched(state: SessionState) -> int:
    return sum(1 for q in state.fill_qty if q > 0)


def _avg_price(state: SessionState) -> float:
    num = sum(q * p for q, p in zip(state.fill_qty, state.fill_prices, strict=True))
    den = sum(state.fill_qty)
    return num / den if den > 0 else state.entry


def _eff_tp(cfg: Config, k: int) -> float:
    return cfg.tp_pct + cfg.tp_step_pct * max(0, k - 1) + cfg.tp_fee_buffer_pct


def _session_lock(state: SessionState, cfg: Config) -> float:
    """Capital ONE open session holds against the reserve-gate budget.

    Byte-for-byte the live `app.scanner._session_lock` formula (Fix A2, 2026-09-21): cash
    already spent PLUS the untouched `coverage_pct` pre-booking of the session's own full
    ladder (`state.fund`), capped at that full reservation. `coverage_pct` outside (0, 100] is
    100 in production (a gate that can be silently disabled by a bad value is not a gate) —
    this study's grid intentionally runs a literal 0 as "spent-only, no forward pre-booking" to
    answer the owner's question, so 0 is accepted here rather than clamped; see the report's
    assumptions for why that one value diverges from what the live app would actually do with
    `ladder_coverage_pct=0`.

    `cfg.deep_lock_rungs` > 0 models `deep_ladder_lock_rungs` (live value 4 on paper
    2026-09-28): a session that has filled that many rungs (`next_rung` == the live
    `current_wave`, the index of the next queued rung) locks its WHOLE reservation, exactly
    like scanner.py:1340-1342. (The first draft of this study omitted it; added in the
    2026-09-28 verification pass.)
    """
    fund = state.fund
    if fund <= 0:
        return state.deployed_usd
    if cfg.deep_lock_rungs > 0 and state.next_rung >= cfg.deep_lock_rungs:
        return fund  # scanner.py:1340-1342 — the depth trigger takes priority
    frac = cfg.coverage_pct / 100.0 if 0.0 <= cfg.coverage_pct <= 100.0 else 1.0
    return min(fund, state.deployed_usd + frac * fund)


def _tp_factor(cfg: Config, k: int) -> float:
    return 1 + _eff_tp(cfg, k) / 100


def settle_backstop(ledger: Ledger, date: str) -> float:
    """Settle this bar's backstopped rung debt AFTER every session's exits were credited: pay
    it from own cash above the floor first, draw only the remainder from the outside fund.
    Returns the amount drawn."""
    if ledger.bar_debt <= 0:
        ledger.bar_debt = 0.0
        return 0.0
    pay = min(ledger.bar_debt, max(0.0, ledger.cash - ledger.floor))
    ledger.cash -= pay
    draw = ledger.bar_debt - pay
    ledger.bar_debt = 0.0
    if draw <= 1e-12:
        return 0.0
    ledger.external_outstanding += draw
    ledger.external_draw_total += draw
    ledger.external_draw_events += 1
    ledger.external_draw_log.append((date, "*", round(draw, 6)))
    if ledger.external_outstanding > ledger.external_peak_outstanding:
        ledger.external_peak_outstanding = ledger.external_outstanding
        ledger.external_peak_date = date
    return draw


def repay_backstop(ledger: Ledger, open_sessions: dict, cfg: Config) -> float:
    """Repay the outside fund from cash above (floor + what the open book still needs IN
    FUTURE). Future need = the un-spent part of each session's lock (`lock - deployed`); spent
    cash already left the wallet and must not be held back a second time (the first draft held
    back the WHOLE lock, spent part included, and so repaid far too slowly). gate=cashflow
    pre-books nothing, so its future need is 0. Returns the amount repaid."""
    if ledger.external_outstanding <= 0:
        return 0.0
    if cfg.gate == "reserve":
        forward = sum(max(0.0, _session_lock(st, cfg) - st.deployed_usd)
                      for st in open_sessions.values())
    else:
        forward = 0.0
    surplus = max(0.0, ledger.cash - ledger.floor - forward)
    repay = min(surplus, ledger.external_outstanding)
    if repay > 0:
        ledger.cash -= repay
        ledger.external_outstanding -= repay
    return repay


def _sizing_equity(equity_curve: list[dict], capital: float) -> float:
    """The equity the book sizes itself on (reserve-gate budget, %-of-equity wave0, cash
    floor): the OWNER's unit-NAV at the previous bar's close — never gross equity, which with
    the backstop drawn includes the outside fund's money."""
    return equity_curve[-1]["nav_own"] if equity_curve else capital


def _cagr_total(final_nav_own: float, peak_draw: float, capital: float, years: float) -> float:
    """CAGR on (own capital + peak outside draw): what the owner would have had to hold from
    day one to never need the backstop. End wealth = own unit-NAV + the whole outside fund."""
    base = capital + peak_draw
    end = final_nav_own + peak_draw
    if base <= 0 or end <= 0 or years <= 0:
        return 0.0
    return ((end / base) ** (1 / years) - 1) * 100


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _median(xs: list[float]) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    m = len(s) // 2
    return s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2


def utilization_stats(equity_curve: list[dict], recent_since: str = "2026-01-01",
                      normal_band_pct: float = 5.0) -> dict:
    """Utilization measured so a bear market cannot pass for 'money working'.

    `utilization_pct` = deployed cost / own unit-NAV; `own_utilization_pct` excludes the part
    financed by the outside fund. 'Normal' days = unit-NAV within `normal_band_pct`% of its own
    running high-water mark (deep rungs filling while equity shrinks are NOT normal days)."""
    util = [r["utilization_pct"] for r in equity_curve]
    own = [r.get("own_utilization_pct", r["utilization_pct"]) for r in equity_curve]
    hwm = float("-inf")
    normal_u: list[float] = []
    normal_o: list[float] = []
    for r, u, o in zip(equity_curve, util, own, strict=True):
        hwm = max(hwm, r["nav_own"])
        if r["nav_own"] >= hwm * (1 - normal_band_pct / 100.0):
            normal_u.append(u)
            normal_o.append(o)
    recent_u = [u for r, u in zip(equity_curve, util, strict=True) if r["date"] >= recent_since]
    recent_o = [o for r, o in zip(equity_curve, own, strict=True) if r["date"] >= recent_since]
    return {
        "util_avg_pct": round(_mean(util), 3),
        "util_median_daily_pct": round(_median(util), 3),
        "util_normal_days_pct": round(_mean(normal_u), 3),
        "normal_days_share_pct": round(100.0 * len(normal_u) / len(util), 3) if util else 0.0,
        "util_recent_pct": round(_mean(recent_u), 3),
        "util_recent_median_pct": round(_median(recent_u), 3),
        "own_util_median_daily_pct": round(_median(own), 3),
        "own_util_normal_days_pct": round(_mean(normal_o), 3),
        "own_util_recent_pct": round(_mean(recent_o), 3),
    }


def window_stats(equity_curve: list[dict], draw_log: list, start: str, end: str) -> dict:
    """Behaviour inside [start, end]: own unit-NAV return and peak-to-trough drawdown within
    the window, the depth below the ALL-TIME high-water mark reached in it, mean utilization,
    and outside-fund draws / peak outstanding."""
    before = [r for r in equity_curve if r["date"] < start]
    rows = [r for r in equity_curve if start <= r["date"] <= end]
    if not rows:
        return {}
    nav0 = before[-1]["nav_own"] if before else rows[0]["nav_own"]
    hwm_all = max([r["nav_own"] for r in before], default=rows[0]["nav_own"])
    peak = nav0
    worst_in = 0.0
    worst_vs_hwm = 0.0
    for r in rows:
        peak = max(peak, r["nav_own"])
        hwm_all = max(hwm_all, r["nav_own"])
        if peak > 0:
            worst_in = min(worst_in, r["nav_own"] / peak - 1)
        if hwm_all > 0:
            worst_vs_hwm = min(worst_vs_hwm, r["nav_own"] / hwm_all - 1)
    draws = sum(usd for d, _s, usd in draw_log if start <= d <= end)
    return {
        "nav_return_pct": round((rows[-1]["nav_own"] / nav0 - 1) * 100, 3) if nav0 > 0 else 0.0,
        "max_dd_in_window_pct": round(-worst_in * 100, 3),
        "max_below_hwm_pct": round(-worst_vs_hwm * 100, 3),
        "util_avg_pct": round(_mean([r["utilization_pct"] for r in rows]), 3),
        "external_draws_usd": round(draws, 2),
        "external_peak_outstanding_usd": round(max(r["external_outstanding"] for r in rows), 2),
    }


def _close_delisted(state: SessionState, cfg: Config) -> dict:
    """A symbol whose data ended (delisted) is realized at its last close — never held open
    forever with a slot, a budget lock and a cost basis that would read as utilization. The
    first draft left such sessions open to the end of the run (173 of 641 symbols end early)."""
    avg = _avg_price(state)
    last = state.last_close
    pnl_pct = round((last - avg) / avg * 100.0 - cfg.cost_pct, 4) if avg > 0 else -cfg.cost_pct
    return {
        "reason": "delisted", "tp_hit": False, "pnl_pct": pnl_pct,
        "pnl_usd": round(state.deployed_usd * pnl_pct / 100.0, 6), "exit_price": last,
        "days": round((state.prev_ts - state.entry_ts) / _MS_PER_DAY, 2),
        "waves_filled": _waves_touched(state), "deployed_usd": round(state.deployed_usd, 6),
        "capital_days": round(state.capital_days_acc, 6),
    }


# ---------------------------------------------------------------------------------------------
# The per-bar engine — mirrors app.backtest.simulate_kss's branching order exactly.
# ---------------------------------------------------------------------------------------------


def _bar_len_days(candles: list[dict], idx: int) -> float:
    """Duration of candles[idx] in days, derived from consecutive `ts` — same convention as
    `simulate_kss`'s private helper of the same name (falls back to the preceding gap for the
    final candle, which has no next `ts` to measure forward from)."""
    if idx + 1 < len(candles):
        return (candles[idx + 1]["ts"] - candles[idx]["ts"]) / _MS_PER_DAY
    if idx > 0:
        return (candles[idx]["ts"] - candles[idx - 1]["ts"]) / _MS_PER_DAY
    return 0.0


def step_session(state: SessionState, bar: dict, idx: int, cfg: Config, ledger: Ledger) -> dict:
    """Advance one open session by exactly one bar.

    Structure copied bar-for-bar from `simulate_kss` (see that function's docstring for why the
    order matters): pessimistic bound checks take-profit against the PRE-fill average first;
    then rungs whose target the bar traded through attempt to fill (this is the ONLY part that
    is new — a fill may come back full, partial, or not at all, depending on `ledger.cash`);
    then both bounds check the exit conditions against the POST-fill average, in the bound's own
    order. A bar that arms the Ride & Trail v2 stop never exits on that same bar, not even at
    the deadline — copied from `just_armed` in the frozen function.

    Returns ``{"closed": <dict|None>, "event": "starved"|"partial"|None, "event_usd": float}``.
    ``closed`` is a dict with pnl_pct/pnl_usd/exit reason/waves_filled/deployed_usd/capital_days
    /tp_hit when the session exited this bar, else None (still open).
    """
    days = (bar["ts"] - state.entry_ts) / _MS_PER_DAY
    if ledger.halted and cfg.brake_suspend_deadline:
        # Suspend the deadline for as long as the halt lasts, so a brake that stops the buying
        # does not then force-sell the same position at the bottom on a timer. Credit accrues
        # per bar; `days` below is the deadline's clock, not the calendar's.
        state.halt_credit_days += _bar_len_days(state.candles, idx)
    days -= state.halt_credit_days
    bar_open = bar.get("open", bar["close"])
    just_armed = False
    event: str | None = None
    event_usd = 0.0

    # Capital-days bookkeeping: close out the interval since the previous bar boundary at the
    # capital level that was actually deployed throughout it (same convention as simulate_kss).
    state.capital_days_acc += state.deployed_usd * (bar["ts"] - state.prev_ts) / _MS_PER_DAY
    state.prev_ts = bar["ts"]
    state.last_close = bar["close"]

    def close(reason: str, exit_price: float, pnl_pct: float) -> dict:
        pnl_usd = round(state.deployed_usd * pnl_pct / 100.0, 6)
        # Mirrors simulate_kss's close_capital(): the closing bar's own duration counts too,
        # held at the capital level deployed as of the exit (post-fill for this bar).
        capital_days = state.capital_days_acc + state.deployed_usd * _bar_len_days(state.candles, idx)
        return {
            "reason": reason,
            "tp_hit": reason in ("tp", "trail"),
            "pnl_pct": pnl_pct,
            "pnl_usd": pnl_usd,
            "exit_price": exit_price,
            "days": round(days, 2),
            "waves_filled": _waves_touched(state),
            "deployed_usd": round(state.deployed_usd, 6),
            "capital_days": round(capital_days, 6),
        }

    if cfg.pessimistic:
        pre_avg = _avg_price(state)
        if pre_avg > 0:
            dd = (bar["low"] - pre_avg) / pre_avg * 100.0
            state.mae_pct = min(state.mae_pct, dd)
            k = _waves_touched(state)
            if not state.armed and bar["high"] >= pre_avg * _tp_factor(cfg, k):
                if cfg.trail_after_tp_pct <= 0:
                    closed = close("tp", pre_avg * _tp_factor(cfg, k), round(_eff_tp(cfg, k) - cfg.cost_pct, 4))
                    return {"closed": closed, "event": event, "event_usd": event_usd}
                state.armed = True
                just_armed = True
                state.floor = pre_avg * _tp_factor(cfg, k)
                state.peak = bar["high"]
                state.eff_tp_at_arm = _eff_tp(cfg, k)

    # Fill deeper rungs whose target the bar traded through. THIS is the new part: a fill only
    # happens if the ledger can afford it (full or, with --partial-last-rung, whatever cash
    # remains). A starved/partial rung STOPS the loop for this bar (cash is now known to be
    # short or exhausted) and does NOT advance the pointer on a starve — the ladder is retried
    # at the same target on a later bar, exactly the live ATOM#16 behaviour this mirrors.
    while (not state.armed and state.next_rung < cfg.max_waves
           and bar["low"] <= state.targets[state.next_rung]):
        n = state.next_rung
        price = _fill_price(state.targets[n], bar_open)
        full_qty = (n + 1) * state.unit_qty
        full_cost = full_qty * price
        if ledger.halted:
            # The crash brake refuses new exposure. It never reaches the exit checks below —
            # those run exactly as they always did, halted or not.
            ledger.rungs_blocked += 1
            ledger.blocked_usd += full_cost
            event = "blocked"
            event_usd = round(full_cost, 6)
            break
        # Spendable cash = cash above the hard floor (app.orders._apply_cash_cap). With the
        # floor at 0 (default) this is exactly `ledger.cash` — the parity path is untouched.
        avail = ledger.cash - ledger.floor
        if avail >= full_cost:
            ledger.cash -= full_cost
            state.fill_qty[n] = full_qty
            state.fill_prices[n] = price
            state.deployed_usd += full_cost
            state.next_rung += 1
        elif cfg.backstop:
            # The rung fills in FULL (the whole point of this mode). Own cash above the floor
            # pays what it can now; the rest becomes `bar_debt`, which `settle_backstop` pays
            # from same-bar exit proceeds first and only then draws from the outside fund —
            # so a rung is never financed externally while another session's take-profit on
            # the very same bar would have covered it.
            pay = max(0.0, avail)
            ledger.cash -= pay
            ledger.bar_debt += full_cost - pay
            state.fill_qty[n] = full_qty
            state.fill_prices[n] = price
            state.deployed_usd += full_cost
            state.next_rung += 1
        elif cfg.partial_last_rung and avail > 0 and avail >= min(cfg.wave0_floor, full_cost):
            # Production trims to the cash above the floor and refuses a slice below
            # `scan_min_notional` (== wave0_floor here) outright (orders.py:162-166).
            spent = avail
            got_qty = spent / price
            ledger.cash -= spent
            state.fill_qty[n] = got_qty
            state.fill_prices[n] = price
            state.deployed_usd += spent
            state.next_rung += 1
            state.rungs_partial += 1
            event = "partial"
            event_usd = round(full_cost - spent, 6)
            break  # cash is now exhausted; no further rung can fill this bar
        else:
            state.rungs_starved += 1
            event = "starved"
            event_usd = round(full_cost, 6)
            break  # leave the ladder where it is — retried next bar if cash frees up

    avg = _avg_price(state)
    if avg > 0:
        dd = (bar["low"] - avg) / avg * 100.0
        state.mae_pct = min(state.mae_pct, dd)

    if cfg.pessimistic:
        if state.armed and not just_armed:
            stop = max(state.floor, state.peak * (1 - cfg.trail_after_tp_pct / 100))
            if bar["low"] <= stop:
                gross = (stop / avg - 1) * 100.0
                closed = close("trail", stop, round(gross - cfg.cost_pct, 4))
                return {"closed": closed, "event": event, "event_usd": event_usd}
            state.peak = max(state.peak, bar["high"])
        elif not state.armed:
            if cfg.sl_pct > 0 and bar["low"] <= avg * (1 - cfg.sl_pct / 100):
                closed = close("sl", avg * (1 - cfg.sl_pct / 100), round(-cfg.sl_pct - cfg.cost_pct, 4))
                return {"closed": closed, "event": event, "event_usd": event_usd}
        if not just_armed and days >= cfg.deadline_days:
            last = bar["close"]
            closed = close("deadline", last, round((last - avg) / avg * 100.0 - cfg.cost_pct, 4))
            return {"closed": closed, "event": event, "event_usd": event_usd}
    else:
        if state.armed and not just_armed:
            state.peak = max(state.peak, bar["high"])
            stop = max(state.floor, state.peak * (1 - cfg.trail_after_tp_pct / 100))
            if bar["low"] <= stop:
                gross = (stop / avg - 1) * 100.0
                closed = close("trail", stop, round(gross - cfg.cost_pct, 4))
                return {"closed": closed, "event": event, "event_usd": event_usd}
        elif not state.armed:
            if cfg.sl_pct > 0 and bar["low"] <= avg * (1 - cfg.sl_pct / 100):
                closed = close("sl", avg * (1 - cfg.sl_pct / 100), round(-cfg.sl_pct - cfg.cost_pct, 4))
                return {"closed": closed, "event": event, "event_usd": event_usd}
            k = _waves_touched(state)
            if bar["high"] >= avg * _tp_factor(cfg, k):
                if cfg.trail_after_tp_pct <= 0:
                    closed = close("tp", avg * _tp_factor(cfg, k), round(_eff_tp(cfg, k) - cfg.cost_pct, 4))
                    return {"closed": closed, "event": event, "event_usd": event_usd}
                state.armed = True
                just_armed = True
                state.floor = avg * _tp_factor(cfg, k)
                state.peak = bar["high"]
                state.eff_tp_at_arm = _eff_tp(cfg, k)
        if not just_armed and days >= cfg.deadline_days:
            last = bar["close"]
            closed = close("deadline", last, round((last - avg) / avg * 100.0 - cfg.cost_pct, 4))
            return {"closed": closed, "event": event, "event_usd": event_usd}

    return {"closed": None, "event": event, "event_usd": event_usd}


# ---------------------------------------------------------------------------------------------
# The portfolio day-loop
# ---------------------------------------------------------------------------------------------


def _date_str(ts: int) -> str:
    return datetime.fromtimestamp(ts / 1000, timezone.utc).strftime("%Y-%m-%d")


def _max_drawdown_pct(equities: list[float]) -> float:
    """Worst peak-to-trough decline over a daily equity curve, as a POSITIVE percentage."""
    peak = float("-inf")
    worst = 0.0
    for e in equities:
        peak = max(peak, e)
        if peak > 0:
            worst = min(worst, (e - peak) / peak * 100.0)
    return round(-worst, 4)


def _max_dd_trough_date(equity_curve: list[dict]) -> str:
    """Date of the trough of the worst own-unit-NAV drawdown."""
    peak = float("-inf")
    worst = 0.0
    when = ""
    for r in equity_curve:
        peak = max(peak, r["nav_own"])
        if peak > 0 and (r["nav_own"] - peak) / peak < worst:
            worst = (r["nav_own"] - peak) / peak
            when = r["date"]
    return when


def universe_breadth(series: dict[str, list[dict]], drop_pct: float, lookback: int
                     ) -> dict[int, float]:
    """ts -> % of symbols trading at least `drop_pct` below their own high of the last
    `lookback` bars. Independent of the book, so one computation serves every run."""
    hit: dict[int, int] = defaultdict(int)
    seen: dict[int, int] = defaultdict(int)
    thresh = 1 - drop_pct / 100.0
    for bars in series.values():
        highs: list[float] = []
        for i, c in enumerate(bars):
            seen[c["ts"]] += 1
            lo = max(0, i - lookback)
            highs = [b["high"] for b in bars[lo:i]] or [c["high"]]
            peak = max(highs)
            if peak > 0 and c["low"] <= peak * thresh:
                hit[c["ts"]] += 1
    return {ts: 100.0 * hit[ts] / n for ts, n in seen.items() if n}


def run_portfolio(series: dict[str, list[dict]], cfg: Config,
                   since_ts: int | None = None, until_ts: int | None = None,
                   breadth: dict[int, float] | None = None) -> dict:
    """One full day-by-day replay for one (capital, gate, bound) combination.

    `series` holds each symbol's FULL candle history (so `--warmup` counts real prior bars, not
    bars-since-window-start) but only timestamps inside [since_ts, until_ts] are iterated — a
    session opened near the end of the window simply never sees bars past `until_ts` and is
    reported as still open, never folded into realized wins/losses (see module docstring).
    """
    ts_index: dict[str, dict[int, int]] = {
        sym: {c["ts"]: i for i, c in enumerate(bars)} for sym, bars in series.items()
    }
    all_ts = sorted({
        c["ts"] for bars in series.values() for c in bars
        if (since_ts is None or c["ts"] >= since_ts) and (until_ts is None or c["ts"] <= until_ts)
    })

    rng = random.Random(cfg.seed)
    ledger = Ledger(cfg.capital)
    open_sessions: dict[str, SessionState] = {}
    equity_curve: list[dict] = []
    monthly: dict[str, dict] = defaultdict(lambda: {
        "opened": 0, "closed": 0, "wins": 0, "losses": 0, "flats": 0, "realized_usd": 0.0,
        "rungs_starved": 0, "rungs_partial": 0,
    })
    closed_log: list[dict] = []
    # Informational only now (reporting baseline "ladder full cost" at the CLI's flat wave0):
    # the reserve gate itself prices every session at its OWN wave0 (see Step 2), which differs
    # per session once `wave0_pct` makes wave0 track compounding equity.
    ladder_cost = full_ladder_cost(cfg.distance_pct, cfg.max_waves, cfg.wave0_usd)

    uni_on = cfg.brake_universe_drop_pct > 0 and cfg.brake_universe_breadth_pct > 0
    if uni_on and breadth is None:
        breadth = universe_breadth(series, cfg.brake_universe_drop_pct,
                                   cfg.brake_universe_lookback)
    brake_on = cfg.brake_halt_breadth_pct > 0 and cfg.brake_halt_depth_frac > 0
    warn_depth = math.ceil(cfg.brake_warn_depth_frac * cfg.max_waves)
    halt_depth = math.ceil(cfg.brake_halt_depth_frac * cfg.max_waves)

    for ts in all_ts:
        date = _date_str(ts)
        month = date[:7]

        # --- Step 0: the crash brake, judged on the state the PREVIOUS bar left behind. ---
        # The lag is real, not a modelling shortcut: no brake can know a candle's depth before
        # the candle happens, and the live app is in exactly the same position on its cycle.
        if uni_on:
            wide = breadth.get(ts, 0.0)
            if ledger.halted:
                if ts >= ledger.halt_until_ts and wide < cfg.brake_universe_breadth_pct:
                    ledger.halted = False   # owner confirmed AND the market has calmed
            elif wide >= cfg.brake_universe_breadth_pct:
                ledger.halted = True
                ledger.halt_episodes += 1
                ledger.halt_until_ts = ts + int(cfg.brake_resume_days * _MS_PER_DAY)
            if ledger.halted:
                ledger.halt_bars += 1
        if brake_on and open_sessions:
            n_open = len(open_sessions)
            deep_warn = sum(1 for st in open_sessions.values() if _waves_touched(st) >= warn_depth)
            deep_halt = sum(1 for st in open_sessions.values() if _waves_touched(st) >= halt_depth)
            if 100.0 * deep_warn / n_open >= cfg.brake_warn_breadth_pct:
                ledger.warn_bars += 1
            if ledger.halted:
                if ts >= ledger.halt_until_ts:
                    ledger.halted = False          # the owner confirmed; buying resumes
            elif 100.0 * deep_halt / n_open >= cfg.brake_halt_breadth_pct:
                ledger.halted = True
                ledger.halt_episodes += 1
                ledger.halt_until_ts = ts + int(cfg.brake_resume_days * _MS_PER_DAY)
            if ledger.halted:
                ledger.halt_bars += 1

        # The hard cash floor for today's buys, from yesterday's own unit-NAV (the live floor is
        # scaled off anchored equity the same way; 0 when cash_floor_pct is off).
        sizing_eq = _sizing_equity(equity_curve, cfg.capital)
        ledger.floor = max(0.0, cfg.cash_floor_pct / 100.0 * sizing_eq)

        # --- Step 1: manage open sessions FIRST, always (exits are never gated). ---
        for sym in list(open_sessions.keys()):
            idx = ts_index[sym].get(ts)
            state = open_sessions[sym]
            if idx is None:
                if ts > series[sym][-1]["ts"]:
                    # The symbol's data ended before today: delisted. Realize at last close.
                    r = {"closed": _close_delisted(state, cfg), "event": None, "event_usd": 0.0}
                else:
                    continue  # a mid-series gap day; the session simply waits
            else:
                bar = series[sym][idx]
                r = step_session(state, bar, idx, cfg, ledger)
            if r["event"] == "starved":
                monthly[month]["rungs_starved"] += 1
                ledger.rungs_starved_total += 1
                ledger.starved_usd_total += r["event_usd"]
            elif r["event"] == "blocked":
                pass  # already counted on the ledger; it is a policy refusal, not a cash miss
            elif r["event"] == "partial":
                monthly[month]["rungs_partial"] += 1
                ledger.rungs_partial_total += 1
                ledger.starved_usd_total += r["event_usd"]
            if r["closed"] is not None:
                c = r["closed"]
                ledger.cash += c["deployed_usd"] * (1 + c["pnl_pct"] / 100.0)
                ledger.realized_usd += c["pnl_usd"]
                monthly[month]["closed"] += 1
                monthly[month]["realized_usd"] += c["pnl_usd"]
                if c["pnl_usd"] > 0:
                    monthly[month]["wins"] += 1
                else:
                    monthly[month]["losses"] += 1
                closed_log.append({
                    "symbol": sym, "open_month": state.open_month, "close_month": month,
                    "year": date[:4], **c,
                })
                del open_sessions[sym]
            else:
                state.j = idx + 1

        # --- Step 1.25: settle this bar's backstopped rungs now that EVERY exit is credited. ---
        # --- Step 1.5: then repay the outside fund from cash above the floor plus what the
        # open book still needs in future (see `repay_backstop`), before deploying further. ---
        if cfg.backstop:
            settle_backstop(ledger, date)
            repay_backstop(ledger, open_sessions, cfg)

        # --- Step 2: open new sessions (a halt blocks these too). ---
        if ledger.halted:
            ledger.opens_blocked += 1
            candidates = []
        else:
            candidates = [
                sym for sym, idx_map in ts_index.items()
                if sym not in open_sessions and ts in idx_map and idx_map[ts] >= cfg.warmup
            ]
        rng.shuffle(candidates)
        new_today = 0
        # RESERVE-GATE budget: byte-for-byte app.scanner._can_open — (100-equity_backup_pct)%
        # of mark-to-market equity (the PREVIOUS bar's, the same lag `wave0_pct` already uses;
        # a scan cannot know today's still-unresolved candle either), minus every OPEN session's
        # `_session_lock` (not just newly-opened ones — Fix A2, 2026-09-21). Recomputed fresh
        # each day from `open_sessions` (cheap: <= max_sessions terms) and then updated
        # incrementally as sessions open today, mirroring the live app re-querying active
        # sessions from the DB between candidates in the same scan.
        locked_today = 0.0
        budget = 0.0
        if cfg.gate == "reserve":
            locked_today = sum(_session_lock(st, cfg) for st in open_sessions.values())
            budget = max(0.0, sizing_eq) * max(0.0, 100.0 - cfg.equity_backup_pct) / 100.0
        for sym in candidates:
            if len(open_sessions) >= cfg.max_sessions or new_today >= cfg.max_new_per_day:
                break
            if cfg.wave0_pct > 0:
                w0 = cfg.wave0_pct / 100.0 * sizing_eq
                if cfg.wave0_cap > 0:
                    w0 = min(w0, cfg.wave0_cap)
                w0 = max(cfg.wave0_floor, w0)
            else:
                w0 = cfg.wave0_usd
            fund = full_ladder_cost(cfg.distance_pct, cfg.max_waves, w0)
            if cfg.gate == "reserve":
                cov_frac = cfg.coverage_pct / 100.0 if 0.0 <= cfg.coverage_pct <= 100.0 else 1.0
                new_need = cov_frac * fund
                if locked_today + new_need > budget:
                    continue
            if ledger.cash - ledger.floor < w0:
                continue  # wave 0 is a BUY too: the hard cash floor applies (orders.py:153)
            idx = ts_index[sym][ts]
            state = SessionState(sym, series[sym], idx, cfg, wave0=w0, fund=fund)
            state.open_month = month
            ledger.cash -= w0
            open_sessions[sym] = state
            monthly[month]["opened"] += 1
            new_today += 1
            if cfg.gate == "reserve":
                locked_today += _session_lock(state, cfg)

        # --- Step 3: mark to market. ---
        deployed_total = 0.0
        unreal_usd = 0.0
        for state in open_sessions.values():
            avg = _avg_price(state)
            unreal_pct = (state.last_close / avg - 1) * 100.0 if avg > 0 else 0.0
            deployed_total += state.deployed_usd
            unreal_usd += state.deployed_usd * unreal_pct / 100.0
        equity = ledger.cash + deployed_total + unreal_usd
        # Unit-NAV: what the OWNER's capital is worth once the outside fund's outstanding draw
        # (a liability, not a windfall) is backed out. Equals `equity` whenever backstop is off
        # or nothing is currently drawn.
        nav_own = equity - ledger.external_outstanding
        utilization_pct = (
            100.0 * deployed_total / nav_own if nav_own > 0
            else (100.0 * deployed_total / equity if equity > 0 else 0.0)
        )
        # The share of the OWNER's money at work: deployed cost minus the part the outside fund
        # is currently financing.
        own_deployed = max(0.0, deployed_total - ledger.external_outstanding)
        own_utilization_pct = 100.0 * own_deployed / nav_own if nav_own > 0 else 0.0
        equity_curve.append({
            "date": date, "equity": round(equity, 2), "nav_own": round(nav_own, 2),
            "cash": round(ledger.cash, 2), "deployed": round(deployed_total, 2),
            "open_n": len(open_sessions),
            "external_outstanding": round(ledger.external_outstanding, 2),
            "utilization_pct": round(utilization_pct, 4),
            "own_utilization_pct": round(own_utilization_pct, 4),
        })

    return _build_report(cfg, ledger, open_sessions, equity_curve, monthly, closed_log, ladder_cost)


# ---------------------------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------------------------


def _build_report(cfg: Config, ledger: Ledger, open_sessions: dict[str, SessionState],
                   equity_curve: list[dict], monthly: dict[str, dict], closed_log: list[dict],
                   ladder_cost: float) -> dict:
    months = sorted(monthly)
    by_month_equity: dict[str, dict] = {}
    for row in equity_curve:
        by_month_equity[row["date"][:7]] = row  # last write per month = month-end snapshot

    monthly_out = []
    cum_realized = 0.0
    for m in months:
        d = monthly[m]
        cum_realized += d["realized_usd"]
        n_closed = d["wins"] + d["losses"] + d["flats"]
        end = by_month_equity.get(m, {"equity": None, "open_n": None, "deployed": None})
        monthly_out.append({
            "month": m, "opened": d["opened"], "closed": d["closed"], "wins": d["wins"],
            "losses": d["losses"], "flats": d["flats"],
            "win_rate_pct": round(100 * d["wins"] / n_closed, 2) if n_closed else 0.0,
            "realized_usd": round(d["realized_usd"], 2), "cum_realized_usd": round(cum_realized, 2),
            "equity_end": end["equity"], "open_at_end": end["open_n"], "deployed_end": end["deployed"],
            "rungs_starved": d["rungs_starved"], "rungs_partial": d["rungs_partial"],
        })

    years = sorted({row["date"][:4] for row in equity_curve})
    yearly_out = []
    prior_year_equity = cfg.capital
    for y in years:
        rows = [r for r in equity_curve if r["date"][:4] == y]
        closes = [c for c in closed_log if c["year"] == y]
        opens = sum(monthly[m]["opened"] for m in months if m[:4] == y)
        wins = sum(1 for c in closes if c["pnl_usd"] > 0)
        losses = sum(1 for c in closes if c["pnl_usd"] <= 0)
        realized = sum(c["pnl_usd"] for c in closes)
        equity_end = rows[-1]["equity"] if rows else prior_year_equity
        yearly_out.append({
            "year": y, "opened": opens, "closed": len(closes), "wins": wins, "losses": losses,
            "win_rate_pct": round(100 * wins / len(closes), 2) if closes else 0.0,
            "realized_usd": round(realized, 2),
            "return_pct_on_start_capital": round(100 * (equity_end - prior_year_equity) / prior_year_equity, 2)
            if prior_year_equity else 0.0,
            "equity_end": equity_end,
            "max_drawdown_pct": _max_drawdown_pct([r["equity"] for r in rows]) if rows else 0.0,
        })
        prior_year_equity = equity_end

    all_waves = [c["waves_filled"] for c in closed_log] + [_waves_touched(s) for s in open_sessions.values()]
    capital_days_closed = sum(c["capital_days"] for c in closed_log)
    final_equity = equity_curve[-1]["equity"] if equity_curve else cfg.capital
    final_nav_own = equity_curve[-1]["nav_own"] if equity_curve else cfg.capital
    span_days = max(1, len(equity_curve) - 1)
    years_elapsed = span_days / 365.0
    cagr = (((final_equity / cfg.capital) ** (1 / years_elapsed)) - 1) * 100 if final_equity > 0 and years_elapsed > 0 else 0.0
    # Own-capital CAGR uses unit-NAV (backs out the outside fund's outstanding draw — a
    # liability, never a windfall). "Total capital" CAGR prices the GROSS equity against what
    # the owner would have had to hold from day one to never need the backstop at all: starting
    # capital plus the PEAK outstanding draw this run ever reached.
    # A book whose own NAV ended at or below zero lost everything (and, with the backstop, owes
    # the outside fund): -100%, never the 0.0 the first draft reported for it.
    cagr_own = (((final_nav_own / cfg.capital) ** (1 / years_elapsed)) - 1) * 100 \
        if final_nav_own > 0 and years_elapsed > 0 else -100.0
    capital_incl_backstop = cfg.capital + ledger.external_peak_outstanding
    # Fixed 2026-09-28: end wealth is own NAV + the WHOLE outside fund (the repaid part is back
    # in the fund) — the first draft used final GROSS equity and dropped the repaid dollars.
    cagr_total = _cagr_total(final_nav_own, ledger.external_peak_outstanding, cfg.capital,
                             years_elapsed)

    util_series = [r["utilization_pct"] for r in equity_curve]
    util_sorted = sorted(util_series)

    def _pctile(arr: list[float], p: float) -> float:
        if not arr:
            return 0.0
        k = max(0, min(len(arr) - 1, int(round(p * (len(arr) - 1)))))
        return arr[k]

    deadline_closes = [c for c in closed_log if c["reason"] == "deadline"]
    deadline_losses = [c for c in deadline_closes if c["pnl_usd"] < 0]
    delisted_closes = [c for c in closed_log if c["reason"] == "delisted"]
    open_unreal_usd = sum(
        s.deployed_usd * ((s.last_close / _avg_price(s) - 1) if _avg_price(s) > 0 else 0.0)
        for s in open_sessions.values()
    )
    windows = {
        "y2022": window_stats(equity_curve, ledger.external_draw_log, "2022-01-01", "2022-12-31"),
        "crash_2025q4": window_stats(equity_curve, ledger.external_draw_log, "2025-10-01", "2025-12-31"),
        "post_2025_10_10": window_stats(equity_curve, ledger.external_draw_log, "2025-10-10", "2026-07-31"),
        "recent_2026": window_stats(equity_curve, ledger.external_draw_log, "2026-01-01", "2026-07-31"),
    }

    # External draws, aggregated by month (the raw per-fill log can run to thousands of rows
    # across an 80-session book; a monthly total is enough to answer "was it 2022? 2025-10-10?"
    # without ballooning results.json across a whole grid of seeds).
    draws_by_month: dict[str, dict] = defaultdict(lambda: {"usd": 0.0, "events": 0})
    draws_oct_2025_crash = {"usd": 0.0, "events": 0}  # 2025-10-05..2025-10-15, the 10/10 crash week
    for d, _sym, usd in ledger.external_draw_log:
        draws_by_month[d[:7]]["usd"] += usd
        draws_by_month[d[:7]]["events"] += 1
        if "2025-10-05" <= d <= "2025-10-15":
            draws_oct_2025_crash["usd"] += usd
            draws_oct_2025_crash["events"] += 1
    external_draws_by_month = [
        {"month": m, "usd": round(v["usd"], 2), "events": v["events"]}
        for m, v in sorted(draws_by_month.items())
    ]

    totals = {
        "sessions_opened": sum(monthly[m]["opened"] for m in months),
        "sessions_closed": len(closed_log),
        "sessions_still_open_at_end": len(open_sessions),
        "realized_usd": round(ledger.realized_usd, 2),
        "final_equity": final_equity,
        "final_nav_own": final_nav_own,
        "cagr_pct": round(cagr, 2),
        "cagr_own_pct": round(cagr_own, 2),
        "cagr_total_pct": round(cagr_total, 2),
        "capital_incl_peak_backstop": round(capital_incl_backstop, 2),
        "max_drawdown_pct": _max_drawdown_pct([r["equity"] for r in equity_curve]),
        "max_drawdown_unit_nav_pct": _max_drawdown_pct([r["nav_own"] for r in equity_curve]),
        "avg_utilization_pct": round(sum(util_series) / len(util_series), 3) if util_series else 0.0,
        "utilization_p10_pct": round(_pctile(util_sorted, 0.10), 3),
        "utilization_p90_pct": round(_pctile(util_sorted, 0.90), 3),
        "external_draw_total_usd": round(ledger.external_draw_total, 2),
        "external_peak_outstanding_usd": round(ledger.external_peak_outstanding, 2),
        "external_outstanding_at_end_usd": round(ledger.external_outstanding, 2),
        "external_draw_events": ledger.external_draw_events,
        "external_draws_oct2025_crash_usd": round(draws_oct_2025_crash["usd"], 2),
        "external_draws_oct2025_crash_events": draws_oct_2025_crash["events"],
        "deadline_exits": len(deadline_closes),
        "deadline_exits_usd": round(sum(c["pnl_usd"] for c in deadline_closes), 2),
        "deadline_losses": len(deadline_losses),
        "deadline_losses_usd": round(sum(c["pnl_usd"] for c in deadline_losses), 2),
        "delisted_exits": len(delisted_closes),
        "delisted_exits_usd": round(sum(c["pnl_usd"] for c in delisted_closes), 2),
        "open_at_end_unrealized_usd": round(open_unreal_usd, 2),
        "external_peak_date": ledger.external_peak_date,
        "max_drawdown_unit_nav_date": _max_dd_trough_date(equity_curve),
        **utilization_stats(equity_curve),
        "ended_below_start_capital": final_nav_own < cfg.capital,
        "rungs_starved": ledger.rungs_starved_total,
        "rungs_partial": ledger.rungs_partial_total,
        "starved_usd": round(ledger.starved_usd_total, 2),
        "avg_waves_filled": round(sum(all_waves) / len(all_waves), 3) if all_waves else 0.0,
        "deepest_ladder_reached": max(all_waves) if all_waves else 0,
        "worst_session_usd": round(min((c["pnl_usd"] for c in closed_log), default=0.0), 2),
        "capital_days": round(capital_days_closed, 2),
        "pct_per_capital_day": round(100 * ledger.realized_usd / capital_days_closed, 5) if capital_days_closed else 0.0,
        "ladder_full_cost_usd": round(ladder_cost, 2),
        "brake_episodes": ledger.halt_episodes,
        "brake_bars_halted": ledger.halt_bars,
        "brake_bars_warned": ledger.warn_bars,
        "brake_rungs_blocked": ledger.rungs_blocked,
        "brake_blocked_usd": round(ledger.blocked_usd, 2),
        "ladder_reserve_usd": round(cfg.coverage_pct / 100.0 * ladder_cost, 2),
    }

    return {
        "config": {
            "capital": cfg.capital, "gate": cfg.gate, "bound": "pessimistic" if cfg.pessimistic else "optimistic",
            "distance_pct": cfg.distance_pct, "max_waves": cfg.max_waves, "tp_pct": cfg.tp_pct,
            "tp_step_pct": cfg.tp_step_pct, "sl_pct": cfg.sl_pct, "deadline_days": cfg.deadline_days,
            "trail_after_tp_pct": cfg.trail_after_tp_pct, "cost_pct": cfg.cost_pct, "wave0_usd": cfg.wave0_usd,
            "warmup": cfg.warmup, "max_sessions": cfg.max_sessions, "max_new_per_day": cfg.max_new_per_day,
            "coverage_pct": cfg.coverage_pct, "partial_last_rung": cfg.partial_last_rung, "seed": cfg.seed,
            "equity_backup_pct": cfg.equity_backup_pct, "backstop": cfg.backstop,
            "tp_fee_buffer_pct": cfg.tp_fee_buffer_pct, "wave0_pct": cfg.wave0_pct,
            "wave0_cap": cfg.wave0_cap, "wave0_floor": cfg.wave0_floor,
            "deep_lock_rungs": cfg.deep_lock_rungs, "cash_floor_pct": cfg.cash_floor_pct,
        },
        "monthly": monthly_out,
        "yearly": yearly_out,
        "totals": totals,
        "windows": windows,
        "external_draws_by_month": external_draws_by_month,
        "equity_curve": equity_curve,
    }


def _fmt_totals(label: str, r: dict) -> str:
    t = r["totals"]
    return (
        f"{label}\n"
        f"  sessions opened {t['sessions_opened']:,}  closed {t['sessions_closed']:,}  "
        f"still open {t['sessions_still_open_at_end']:,}\n"
        f"  realized {t['realized_usd']:+,.2f}$   final equity {t['final_equity']:+,.2f}$   "
        f"CAGR {t['cagr_pct']:+.2f}%   max DD {t['max_drawdown_pct']:.2f}%\n"
        f"  rungs starved {t['rungs_starved']:,}  partial {t['rungs_partial']:,}  "
        f"(${t['starved_usd']:,.0f} not deployed)   avg waves {t['avg_waves_filled']:.2f}   "
        f"deepest {t['deepest_ladder_reached']}   worst session {t['worst_session_usd']:+,.2f}$\n"
        f"  ladder full cost ${t['ladder_full_cost_usd']:,.2f}   reserve/session "
        f"${t['ladder_reserve_usd']:,.2f}   %/capital-day {t['pct_per_capital_day']:+.5f}\n"
    )


# ---------------------------------------------------------------------------------------------
# CLI / multiprocessing
# ---------------------------------------------------------------------------------------------

_WORKER_SERIES: dict[str, list[dict]] = {}
_WORKER_SINCE_TS: int | None = None
_WORKER_UNTIL_TS: int | None = None


def _init_worker(series: dict[str, list[dict]], since_ts: int | None, until_ts: int | None) -> None:
    global _WORKER_SERIES, _WORKER_SINCE_TS, _WORKER_UNTIL_TS
    _WORKER_SERIES = series
    _WORKER_SINCE_TS = since_ts
    _WORKER_UNTIL_TS = until_ts


def _run_job(cfg: Config) -> tuple[str, dict]:
    t0 = time.time()
    report = run_portfolio(_WORKER_SERIES, cfg, _WORKER_SINCE_TS, _WORKER_UNTIL_TS)
    print(f"  {cfg.label():34}  ({time.time() - t0:,.1f}s)  "
          f"realized {report['totals']['realized_usd']:+,.0f}$  "
          f"equity {report['totals']['final_equity']:+,.0f}$  "
          f"starved {report['totals']['rungs_starved']:,}", file=sys.stderr)
    return cfg.label(), report


def _to_ts(day: str | None) -> int | None:
    if not day:
        return None
    dt = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--db", default="data/research/market.db")
    p.add_argument("--interval", default="1d")
    p.add_argument("--capital", type=float, nargs="+", default=[5000.0, 7000.0])
    p.add_argument("--gate", choices=["reserve", "cashflow"], nargs="+", default=["reserve", "cashflow"])
    p.add_argument("--bounds", choices=["optimistic", "pessimistic", "both"], default="both")
    p.add_argument("--since", default=None, help="YYYY-MM-DD")
    p.add_argument("--until", default=None, help="YYYY-MM-DD")
    p.add_argument("--wave0", type=float, default=28.0)
    p.add_argument("--waves", type=int, default=30)
    p.add_argument("--distance", type=float, default=4.0)
    p.add_argument("--tp", type=float, default=5.0)
    p.add_argument("--tp-step", type=float, default=0.5)
    p.add_argument("--sl", type=float, default=0.0)
    p.add_argument("--deadline", type=float, default=60.0)
    p.add_argument("--trail-after-tp", type=float, default=3.0)
    p.add_argument("--coverage", type=float, default=30.0)
    p.add_argument("--cost", type=float, default=None, help="round-trip %% (default: costengine)")
    p.add_argument("--no-partial-last-rung", action="store_true")
    p.add_argument("--max-sessions", type=int, default=80)
    p.add_argument("--max-new-per-day", type=int, default=5)
    p.add_argument("--warmup", type=int, default=24)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--out", default=None)
    p.add_argument("--equity-backup-pct", type=float, default=0.0,
                    help="mirrors app.scanner equity_backup_pct (live value 24.8); 0=off")
    p.add_argument("--tp-fee-buffer", type=float, default=0.0,
                    help="mirrors costengine.tp_fee_buffer_pct(); 0=off")
    p.add_argument("--backstop", action="store_true",
                    help="draw the shortfall from an outside fund instead of starving a rung")
    p.add_argument("--wave0-pct", type=float, default=0.0,
                    help="mirrors app.config first_wave_pct: wave0 as %% of equity (capital_scale); 0=use --wave0")
    p.add_argument("--wave0-cap", type=float, default=0.0,
                    help="mirrors app.config first_wave_max_usd; 0=no cap")
    args = p.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    cost = args.cost if args.cost is not None else costengine.round_trip_cost_pct()
    raw = load(Path(args.db), args.interval)
    series = {sym: to_candles(bars) for sym, bars in raw.items()}
    since_ts = _to_ts(args.since)
    until_ts = _to_ts(args.until)

    bounds = [False, True] if args.bounds == "both" else [args.bounds == "pessimistic"]
    jobs = [
        Config(
            capital=cap, gate=gate, pessimistic=pess,
            distance_pct=args.distance, max_waves=args.waves, tp_pct=args.tp,
            tp_step_pct=args.tp_step, sl_pct=args.sl, deadline_days=args.deadline,
            trail_after_tp_pct=args.trail_after_tp, cost_pct=cost, wave0_usd=args.wave0,
            warmup=args.warmup, max_sessions=args.max_sessions, max_new_per_day=args.max_new_per_day,
            coverage_pct=args.coverage, partial_last_rung=not args.no_partial_last_rung, seed=args.seed,
            equity_backup_pct=args.equity_backup_pct, tp_fee_buffer_pct=args.tp_fee_buffer,
            backstop=args.backstop, wave0_pct=args.wave0_pct, wave0_cap=args.wave0_cap,
        )
        for cap in args.capital for gate in args.gate for pess in bounds
    ]
    print(f"{len(series)} symbols loaded; {len(jobs)} runs "
          f"(capital={args.capital} x gate={args.gate} x bounds={args.bounds}); cost {cost:.2f}%\n",
          file=sys.stderr)

    t0 = time.time()
    results: dict[str, dict] = {}
    with Pool(min(args.workers, len(jobs)), initializer=_init_worker,
              initargs=(series, since_ts, until_ts)) as pool:
        for label, report in pool.imap_unordered(_run_job, jobs):
            results[label] = report
    print(f"\nall runs done in {time.time() - t0:,.0f}s", file=sys.stderr)

    out = Path(args.out) if args.out else Path(f"data/research/studies/capital-portfolio-{datetime.now(timezone.utc):%Y-%m-%d}")
    out.parent.mkdir(parents=True, exist_ok=True)
    import json
    out.with_suffix(".json").write_text(json.dumps(results, indent=1, default=str), encoding="utf-8")
    md_lines = ["# Capital portfolio study\n", f"cost={cost:.2f}%  seed={args.seed}  "
                f"since={args.since}  until={args.until}\n"]
    for label in sorted(results):
        md_lines.append(_fmt_totals(label, results[label]))
    out.with_suffix(".md").write_text("\n".join(md_lines), encoding="utf-8")
    print(f"wrote {out.with_suffix('.json')} and {out.with_suffix('.md')}")
    for label in sorted(results):
        print(_fmt_totals(label, results[label]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
