"""
scripts/measure_entry_gates.py — does each of the scanner's six entry gates earn its keep?

WHY. `app/evaluate.py` measured the KSS strategy with ROLLING entries — no entry selection
at all — and scored it at roughly −0.002 to −0.003 profit per dollar-day at every parameter
set tried. The live scanner never trades that way: it stacks six vetoes/gates on top before
it opens anything. So if the strategy has an edge, the edge is in the SELECTION. Nobody had
measured whether any of the six actually helps. This script does exactly that, and nothing
else — it is measurement only and changes no scanner behaviour.

The six, as they appear in `app/scanner.py`:

  1. downtrend          `_downtrend_veto(ta)`                       — HTF+ST down, ADX ≥ 25
  2. falling_knife      `_falling_knife_veto(ta)`                   — ST down AND MACD hist < 0
  3. rel_strength       `_rel_strength_veto(candles, btc_ret)`      — weaker than BTC by > margin
  4. mae_quartile       `_drop_worst_mae_quartile(...)`             — cross-sectional, per scan
  5. backtest_evidence  `decide(...)` expectancy / win-rate / trials / loss-rate / net-edge
  6. consensus          `aggregate(votes)` vs `min_confidence`

METHOD. Roll an entry across history for every symbol exactly as `estimate_win_rate` does
(same `spacing_days` → same `eff_step`). At each candidate entry index `i`, ask every gate
what it WOULD have answered using ONLY candles `[..i]`, then measure the forward outcome with
`backtest.simulate_kss` from `i`. Compare profit-per-dollar-day (plus stop rate, expectancy
and win rate) for the entries a gate VETOED against the ones it PASSED. A gate earns its keep
only when the entries it vetoed did measurably WORSE than the ones it passed.

THE TRAP THIS CODE IS BUILT AROUND: LOOK-AHEAD. Every helper in `app/ta/*` and
`app/backtest.py` takes a whole candle series and reports "as of the last bar". Computing
them once over the full history and reading index `i` uses the future — the same class of bug
that had `simulate_kss` testing its take-profit against the entry bar's OWN high and reporting
a 100% win rate in a market that fell 48%. Defences here:

  - `causal_window(candles, i, lookback_bars)` is the ONLY way a gate ever sees price data,
    and it slices `[..i]` BEFORE anything is computed. It also ROLLS (caps at
    `backtest_lookback_days`) because production never sees more history than that either.
  - BTC's reference return for gate 3 comes from `btc_window_at(btc, ts, ...)`, sliced by
    TIMESTAMP (never by index — symbol arrays need not start on the same bar).
  - `tests/app/test_measure_entry_gates.py::test_gate_snapshot_is_immune_to_the_future`
    appends a violently different future and asserts every gate answer at `i` is unchanged.

WHAT THIS IS NOT FAITHFUL TO, deliberately:
  - The gate is fed production's own backtest call (NOMINAL `tp_pct`), because that is what
    the live gate actually sees. The OUTCOME is scored the honest way `app/evaluate.py`
    established: effective take-profit (`tp_pct + costengine.tp_fee_buffer_pct()`) and the
    full round-trip cost. Gate behaviour measured as-shipped; economics measured honestly.
  - Consensus weights come from `DEFAULT_WEIGHTS`, not `runtime.get_consensus_weights(db)` —
    this script never opens a database.
  - Gates that ship OFF (`rel_strength_enabled`, `mae_quartile_gate_enabled`) are switched on
    in-process by `gates_forced_on()` so the harness can measure what they WOULD have done.
    Nothing is written anywhere; the toggle is restored on the way out.

This script NEVER writes to the database — no session, no commit. The one `audit.log` call
inside the real `_drop_worst_mae_quartile` is absorbed by a null stand-in so gate 4 can be
measured with the REAL function instead of a second, possibly-divergent copy of its rule.

Usage:
    .venv/Scripts/python.exe scripts/measure_entry_gates.py
        [--symbols BTC,ETH,SOL,...] [--timeframe 1d] [--lookback-days 1000]
        [--warmup-days 365] [--spacing-days 7] [--distance 2.0] [--tp 3.0] [--waves 10]
        [--wave0-usd 100] [--bootstrap 2000] [--seed 20260831]
"""

from __future__ import annotations

import argparse
import bisect
import math
import random
import sys
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

# Make the project root importable when running as a standalone script.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import costengine  # noqa: E402
from app.agents import SIGNAL_AGENTS, BacktestAgent, aggregate, decide  # noqa: E402
from app.agents.aggregator import DEFAULT_WEIGHTS  # noqa: E402
from app.backtest import estimate_win_rate, simulate_kss  # noqa: E402
from app.config import settings  # noqa: E402
from app.data.providers import Candle, data_provider  # noqa: E402
from app.scanner import (  # noqa: E402
    _MIN_CANDLES,
    _days_to_bars,
    _downtrend_veto,
    _drop_worst_mae_quartile,
    _falling_knife_veto,
    _nbar_return,
    _rel_strength_veto,
)
from app.ta import bundle as ta_bundle  # noqa: E402

_MS_PER_DAY = 86_400_000

DEFAULT_SYMBOLS: list[str] = ["BTC", "ETH", "SOL", "BNB", "XRP", "ADA", "DOGE", "LINK"]
# The gates are computed from the SAME candles the live scanner backtests, so the measurement
# has to run on that timeframe: `rel_strength_lookback_bars` (7) means 7 DAYS on daily bars and
# 7 hours on hourly ones, and `htf_trend`'s factor-7 down-sample is a week vs. most of a day.
DEFAULT_TIMEFRAME = settings.backtest_timeframe
DEFAULT_LOOKBACK_DAYS = 1000  # ~3y of daily bars: as much decorrelated history as Binance has
DEFAULT_WARMUP_DAYS = 365  # first slice used only as gate history, never as an entry
DEFAULT_WAVE0_USD = 100.0
DEFAULT_BOOTSTRAP_ROUNDS = 2000
DEFAULT_SEED = 20260831

# Below this many entries on EITHER side, a gate cannot be judged — "a gate that vetoes 3
# entries out of 400 cannot be judged, and you must say so rather than report a number".
MIN_GROUP_N = 30

GATE_NAMES: tuple[str, ...] = (
    "downtrend", "falling_knife", "rel_strength",
    "mae_quartile", "backtest_evidence", "consensus",
)

# Two extra, synthetic rows: the gates measured TOGETHER. Per-gate lift answers "does this one
# discriminate"; the stack answers the question the whole exercise turns on — whether the
# selection layer as a whole beats no selection at all.
STACK_SHIPPED = "STACK (as shipped)"
STACK_ALL = "STACK (all six)"

# Which gates are actually live at the model defaults (reported next to each result so a
# "helps"/"hurts" verdict is read against whether it is currently switched on).
GATE_SHIPS_ON: dict[str, bool] = {
    "downtrend": settings.block_downtrend_adx > 0,
    "falling_knife": settings.entry_momentum_gate,
    "rel_strength": settings.rel_strength_enabled,
    "mae_quartile": settings.mae_quartile_gate_enabled,
    "backtest_evidence": True,
    "consensus": True,
}


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EntryOutcome:
    """Forward economics of ONE rolled entry — the thing a gate is judged against."""

    symbol: str
    index: int
    ts: int
    pnl_usd: float  # realized dollars: pnl_pct/100 × capital deployed at exit
    capital_days: float  # dollars actually tied up, integrated over time
    pnl_pct: float
    stopped: bool
    tp_hit: bool


@dataclass(frozen=True)
class GateSnapshot:
    """What every gate WOULD have answered at one entry, from causal data only.

    True = the gate VETOES this entry. `worst_mae` is carried because gate 4 is
    cross-sectional and cannot be decided from one symbol alone.
    """

    downtrend: bool
    falling_knife: bool
    rel_strength: bool
    backtest_evidence: bool
    consensus: bool
    worst_mae: float


@dataclass(frozen=True)
class GroupStats:
    n: int
    profit_per_dollar_day: float  # Σpnl_usd / Σcapital_days — a ratio of SUMS
    expectancy: float  # mean net pnl %/trial
    stop_rate: float  # % of trials that hit the hard stop
    win_rate: float  # % of trials that reached take-profit
    pnl_usd: float
    capital_days: float


@dataclass(frozen=True)
class GateResult:
    name: str
    ships_on: bool
    vetoed: GroupStats
    passed: GroupStats
    lift: float  # passed.ppdd − vetoed.ppdd; POSITIVE = the gate vetoed the worse entries
    ci: tuple[float, float] | None
    verdict: str


# ---------------------------------------------------------------------------
# Causal slicing — nothing downstream may look at price data any other way
# ---------------------------------------------------------------------------


def causal_window(candles: Sequence[Candle], index: int, lookback_bars: int) -> list[Candle]:
    """The candles a gate at `index` is allowed to see: `[..index]`, capped to the last
    `lookback_bars` of them.

    The cap is not an optimisation — it is faithfulness. Production fetches exactly
    `_days_to_bars(backtest_lookback_days, backtest_timeframe)` bars every scan, so a late
    entry must not be judged on more history than the live scanner would have had.
    """
    if index < 0 or not candles:
        return []
    stop = min(index + 1, len(candles))
    start = max(0, stop - lookback_bars) if lookback_bars > 0 else 0
    return list(candles[start:stop])


def btc_window_at(btc: Sequence[Candle], ts: int, lookback_bars: int) -> list[Candle]:
    """BTC's candles at or before `ts`, capped to `lookback_bars` — the causal input to
    `_rel_strength_veto`'s benchmark.

    Sliced by TIMESTAMP, never by index: symbol arrays need not start on the same bar or have
    the same length, and lining them up positionally would quietly compare different weeks.
    """
    if not btc:
        return []
    stop = bisect.bisect_right([c["ts"] for c in btc], ts)
    if stop <= 0:
        return []
    start = max(0, stop - lookback_bars) if lookback_bars > 0 else 0
    return list(btc[start:stop])


def rolled_entry_indices(
    candles: Sequence[Candle], first_index: int, spacing_days: float
) -> list[int]:
    """Entry indices to test, mirroring `app.backtest.estimate_win_rate`'s own roll: the same
    `spacing_days → eff_step` conversion and the same `len(candles) - 1` stop, so the harness
    samples history exactly where production's walk-forward backtest samples it."""
    n = len(candles)
    if n < 2 or first_index >= n - 1:
        return []
    span_days = (candles[-1]["ts"] - candles[0]["ts"]) / _MS_PER_DAY / max(n - 1, 1)
    eff_step = 1
    if spacing_days > 0 and span_days > 0:
        eff_step = max(1, round(spacing_days / span_days))
    return list(range(max(first_index, 0), n - 1, eff_step))


# ---------------------------------------------------------------------------
# Gate evaluation (calls the REAL scanner functions — never a paraphrase of them)
# ---------------------------------------------------------------------------


@contextmanager
def gates_forced_on() -> Iterator[None]:
    """Switch the gates that ship OFF on, for the duration of the measurement.

    Two of the six (`rel_strength_enabled`, `mae_quartile_gate_enabled`) default to False and
    return "no veto" unconditionally, so measuring what they WOULD have done means enabling
    them. This is process-local to a script that never runs a scan and never writes to a
    database, and every touched setting is restored on the way out — including when the body
    raises.
    """
    saved = {
        "entry_momentum_gate": settings.entry_momentum_gate,
        "rel_strength_enabled": settings.rel_strength_enabled,
        "mae_quartile_gate_enabled": settings.mae_quartile_gate_enabled,
        "block_downtrend_adx": settings.block_downtrend_adx,
    }
    try:
        settings.entry_momentum_gate = True
        settings.rel_strength_enabled = True
        settings.mae_quartile_gate_enabled = True
        if settings.block_downtrend_adx <= 0:
            settings.block_downtrend_adx = 25.0
        yield
    finally:
        for key, value in saved.items():
            setattr(settings, key, value)


def backtest_gate_vetoes(wr: dict, tp_pct: float) -> bool:
    """Gate 5 — the expectancy / win-rate / trials / loss-rate / net-edge evidence.

    Calls the REAL `app.agents.aggregator.decide` with the consensus NEUTRALISED (forced to a
    passing value), so the answer is the backtest evidence alone. Without that neutralisation
    this gate's measured lift would be contaminated by gate 6, which shares the same call.
    """
    d = decide(
        100.0,  # consensus neutralised — gate 6 is measured separately
        wr["win_rate"], wr["avg_days_to_tp"],
        min_confidence=settings.min_confidence,
        min_win_rate=settings.min_win_rate,
        deadline_days=settings.deadline_days,
        loss_rate=wr["loss_rate"], max_loss_rate=settings.max_loss_rate,
        net_edge=costengine.net_edge_pct(tp_pct), min_net_edge=settings.min_net_edge,
        win_rate_lb=wr["win_rate_lb"], trials=wr["trials"], min_trials=settings.min_trials,
        expectancy=wr["expectancy"], min_expectancy=settings.min_expectancy_pct,
    )
    return d["decision"] != "trade"


def consensus_gate_vetoes(consensus_pct: float) -> bool:
    """Gate 6 — the agent-consensus vote against `min_confidence`.

    Mirror of `backtest_gate_vetoes`: the REAL `decide`, with every piece of backtest evidence
    neutralised so only the consensus threshold can fire.
    """
    d = decide(
        consensus_pct,
        100.0, 0.0,
        min_confidence=settings.min_confidence,
        min_win_rate=settings.min_win_rate,
        deadline_days=settings.deadline_days,
        loss_rate=0.0, max_loss_rate=settings.max_loss_rate,
        net_edge=float("inf"), min_net_edge=settings.min_net_edge,
        win_rate_lb=100.0, trials=None, min_trials=0,
        expectancy=None, min_expectancy=settings.min_expectancy_pct,
    )
    return d["decision"] != "trade"


def gate_snapshot(
    window: Sequence[Candle],
    btc_window: Sequence[Candle],
    *,
    distance_pct: float,
    tp_pct: float,
    max_waves: int,
    pessimistic_intrabar: bool,
) -> GateSnapshot | None:
    """Every gate's answer at one entry, from the causal `window` alone.

    Returns None for a window the live scanner would have skipped outright (fewer than
    `_MIN_CANDLES` bars) — judging a gate on a TA bundle full of neutral fallbacks would
    measure the fallbacks, not the gate.

    The backtest here is production's OWN call: nominal `tp_pct`, `walk_forward_split`,
    `backtest_trial_spacing_days`, real round-trip cost. That is what the live gate sees.
    """
    if len(window) < _MIN_CANDLES:
        return None
    window = list(window)

    ta = ta_bundle.build(window)  # db/symbol omitted → Tier 1 only, no DB, no network
    downtrend = _downtrend_veto(ta) is not None
    falling_knife = _falling_knife_veto(ta) is not None

    btc_ret = _nbar_return(list(btc_window), settings.rel_strength_lookback_bars)
    rel_strength = _rel_strength_veto(window, btc_ret) is not None

    wr = estimate_win_rate(
        window, distance_pct, max_waves, tp_pct, settings.deadline_days,
        split=settings.walk_forward_split, sl_pct=settings.sl_pct,
        cost_pct=costengine.round_trip_cost_pct(),
        spacing_days=settings.backtest_trial_spacing_days,
        pessimistic_intrabar=pessimistic_intrabar,
    )

    ctx = {"win_rate": wr["win_rate"], "win_rate_lb": wr["win_rate_lb"],
           "trials": wr["trials"], "avg_days_to_tp": wr["avg_days_to_tp"], "ml_model": None}
    votes = [a.evaluate("", window, ctx) for a in SIGNAL_AGENTS]
    votes.append(BacktestAgent().evaluate("", window, ctx))
    consensus = aggregate(votes, weights=DEFAULT_WEIGHTS)

    return GateSnapshot(
        downtrend=downtrend,
        falling_knife=falling_knife,
        rel_strength=rel_strength,
        backtest_evidence=backtest_gate_vetoes(wr, tp_pct),
        consensus=consensus_gate_vetoes(consensus),
        worst_mae=wr["worst_mae"],
    )


class _NullSession:
    """Stand-in for the SQLAlchemy Session that `_drop_worst_mae_quartile` hands to
    `audit.log`. Swallowing the write lets gate 4 be measured with the REAL scanner function
    instead of a second copy of its quartile rule — and keeps this script database-free."""

    def add(self, _obj: object) -> None:  # pragma: no cover - trivial
        pass

    def flush(self) -> None:  # pragma: no cover - trivial
        pass


@dataclass
class _StubCandidate:
    """Minimal duck-type of `app.models.Candidate` — `_drop_worst_mae_quartile` writes a
    decision and appends to a reason, and reads nothing else."""

    decision: str = "trade"
    reason: str = ""


def mark_mae_quartile_drops(rows: Iterable[tuple[str, float]]) -> set[str]:
    """Gate 4 — which keys of one cross-section the REAL `_drop_worst_mae_quartile` drops.

    `rows` is `(key, worst_mae)` for the symbols evaluated at ONE entry timestamp: this gate
    is relative, so it only means anything against a whole scan's worth of candidates. Returns
    the set of dropped keys — empty when there are fewer than 4 candidates, which is the live
    function's own no-op rule.

    The gate ships OFF (`mae_quartile_gate_enabled=False`), so this forces it on for the call:
    the question being measured is what it WOULD have dropped.
    """
    rows = list(rows)
    to_open = [{"cand": _StubCandidate(), "symbol": key, "worst_mae": mae} for key, mae in rows]
    with gates_forced_on():
        kept = _drop_worst_mae_quartile(_NullSession(), to_open)  # type: ignore[arg-type]
    kept_keys = {c["symbol"] for c in kept}
    return {key for key, _ in rows} - kept_keys


# ---------------------------------------------------------------------------
# Forward outcome
# ---------------------------------------------------------------------------


def entry_outcome(
    candles: Sequence[Candle],
    index: int,
    symbol: str,
    *,
    distance_pct: float,
    tp_pct: float,
    max_waves: int,
    pessimistic_intrabar: bool,
    wave0_notional_usd: float,
) -> EntryOutcome | None:
    """Forward economics of the entry at `index`, or None for an incomplete trial.

    Scored the honest way `app/evaluate.py` established — the EFFECTIVE take-profit that
    actually rests on the book (`tp_pct + costengine.tp_fee_buffer_pct()`) and the full
    round-trip cost, not the nominal target a config dials in. A trial that neither reached
    take-profit nor stopped nor hit the deadline before history ran out has no verdict and is
    excluded, exactly as `estimate_win_rate` excludes it.
    """
    if index < 0 or index >= len(candles) - 1:
        return None
    res = simulate_kss(
        list(candles), index, distance_pct, max_waves,
        tp_pct + costengine.tp_fee_buffer_pct(), settings.deadline_days,
        sl_pct=settings.sl_pct, cost_pct=costengine.round_trip_cost_pct(),
        pessimistic_intrabar=pessimistic_intrabar, wave0_notional_usd=wave0_notional_usd,
    )
    if not (res.tp_hit or res.hit_deadline or res.stopped):
        return None
    return EntryOutcome(
        symbol=symbol, index=index, ts=candles[index]["ts"],
        pnl_usd=res.pnl_pct / 100.0 * res.exit_capital,
        capital_days=res.capital_days,
        pnl_pct=res.pnl_pct, stopped=res.stopped, tp_hit=res.tp_hit,
    )


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------


def group_stats(outcomes: Sequence[EntryOutcome]) -> GroupStats:
    """Aggregate one side of a gate split.

    `profit_per_dollar_day` is Σpnl_usd / Σcapital_days — a ratio of SUMS, matching
    `app.evaluate.ConfigScore`. Averaging the per-trial ratios instead would weight a $100
    one-day trial the same as a $600 thirty-day one and can flip the sign.
    """
    n = len(outcomes)
    if n == 0:
        return GroupStats(0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    pnl = sum(o.pnl_usd for o in outcomes)
    cap = sum(o.capital_days for o in outcomes)
    return GroupStats(
        n=n,
        profit_per_dollar_day=(pnl / cap) if cap else 0.0,
        expectancy=sum(o.pnl_pct for o in outcomes) / n,
        stop_rate=sum(1 for o in outcomes if o.stopped) / n * 100,
        win_rate=sum(1 for o in outcomes if o.tp_hit) / n * 100,
        pnl_usd=pnl,
        capital_days=cap,
    )


def _ppdd(outcomes: Sequence[EntryOutcome]) -> float | None:
    cap = sum(o.capital_days for o in outcomes)
    return (sum(o.pnl_usd for o in outcomes) / cap) if cap else None


def stack_names(only_shipped: bool) -> tuple[str, ...]:
    """Which gates a stacked row combines: the ones actually switched on, or all six."""
    if only_shipped:
        return tuple(n for n in GATE_NAMES if GATE_SHIPS_ON[n])
    return GATE_NAMES


def stacked_vetoed(veto_by_gate: dict[str, bool], names: Sequence[str]) -> bool:
    """A stacked row vetoes an entry when ANY member gate vetoes it.

    That mirrors the live scanner, which short-circuits: the first veto flips the candidate to
    'skip' and the remaining gates are never consulted. It is emphatically not a vote.
    """
    return any(veto_by_gate[n] for n in names)


def overlap_block_clusters(deadline_days: float, spacing_days: float) -> int:
    """How many consecutive entry dates one trial's lifetime spans.

    A trial runs until take-profit, stop or the `deadline_days` timeout, while entries are
    rolled every `spacing_days`. With production's 30-day deadline and 7-day spacing, the bars
    inside one trial are also inside the next four — five overlapping trials share price
    history and are not five independent observations. This is the block length the bootstrap
    must resample in one piece.
    """
    if spacing_days <= 0 or deadline_days <= 0:
        return 1
    return max(1, math.ceil(deadline_days / spacing_days))


def cluster_bootstrap_lift_ci(
    passed: Sequence[EntryOutcome],
    vetoed: Sequence[EntryOutcome],
    *,
    rounds: int = DEFAULT_BOOTSTRAP_ROUNDS,
    seed: int = DEFAULT_SEED,
    level: float = 0.95,
    block: int = 1,
) -> tuple[float, float] | None:
    """Percentile confidence interval for `passed.ppdd − vetoed.ppdd`, resampling contiguous
    BLOCKS of entry-date clusters (a circular moving-block bootstrap).

    Individual entries are NOT independent, in two separate ways, and both have to be handled
    or the interval is fiction:

      - CROSS-SECTIONALLY: eight crypto symbols entered on the same day are one market
        observation, not eight. So the unit of resampling is an entry DATE, carrying that
        date's whole cross-section.
      - SERIALLY: the trial spacing (7 days) is shorter than the deadline (30 days), so one
        trial's price history is shared with the next four. `block` (see
        `overlap_block_clusters`) keeps those neighbours together instead of letting the
        resample break a regime into independent-looking pieces.

    `block=1` is the plain cluster bootstrap. Blocks wrap around the end of the series
    (circular), which keeps every date equally likely to appear rather than under-weighting
    the tail.

    Deterministic for a given `seed`. Returns None when either side is empty, when no resample
    produced a usable pair of groups, or when there is nothing to resample.
    """
    if not passed or not vetoed:
        return None
    by_cluster: dict[int, tuple[list[EntryOutcome], list[EntryOutcome]]] = {}
    for outcome in passed:
        by_cluster.setdefault(outcome.ts, ([], []))[0].append(outcome)
    for outcome in vetoed:
        by_cluster.setdefault(outcome.ts, ([], []))[1].append(outcome)
    clusters = [by_cluster[ts] for ts in sorted(by_cluster)]
    if not clusters:
        return None

    rng = random.Random(seed)
    k = len(clusters)
    b = max(1, min(block, k))
    n_blocks = math.ceil(k / b)
    lifts: list[float] = []
    for _ in range(max(1, rounds)):
        pick: list[tuple[list[EntryOutcome], list[EntryOutcome]]] = []
        for _ in range(n_blocks):
            start = rng.randrange(k)
            pick.extend(clusters[(start + j) % k] for j in range(b))
        pick = pick[:k]
        p_side = [o for c in pick for o in c[0]]
        v_side = [o for c in pick for o in c[1]]
        p_ppdd, v_ppdd = _ppdd(p_side), _ppdd(v_side)
        if p_ppdd is None or v_ppdd is None:
            continue
        lifts.append(p_ppdd - v_ppdd)
    if not lifts:
        return None
    lifts.sort()
    tail = (1.0 - level) / 2.0
    lo = lifts[min(len(lifts) - 1, max(0, int(tail * len(lifts))))]
    hi = lifts[min(len(lifts) - 1, max(0, int((1.0 - tail) * len(lifts)) - 1))]
    return (lo, hi)


def verdict(
    passed_n: int,
    vetoed_n: int,
    lift: float,
    ci: tuple[float, float] | None,
    min_n: int = MIN_GROUP_N,
) -> str:
    """Classify a gate from its INTERVAL, never from the point estimate.

    "cannot judge" when either side is too thin to support any claim (a gate that vetoes 3
    entries out of 400 has no measurable effect, and reporting its lift as a number would be
    the misleading answer), or when no interval could be computed.
    """
    if passed_n < min_n or vetoed_n < min_n or ci is None:
        return "cannot judge"
    if ci[0] > 0:
        return "helps"
    if ci[1] < 0:
        return "hurts"
    return "noise"


# ---------------------------------------------------------------------------
# The measurement
# ---------------------------------------------------------------------------


def measure(
    candles_by_symbol: dict[str, list[Candle]],
    *,
    distance_pct: float,
    tp_pct: float,
    max_waves: int,
    lookback_bars: int,
    warmup_bars: int,
    spacing_days: float,
    wave0_notional_usd: float = DEFAULT_WAVE0_USD,
    pessimistic_intrabar: bool = False,
    bootstrap_rounds: int = DEFAULT_BOOTSTRAP_ROUNDS,
    seed: int = DEFAULT_SEED,
) -> tuple[list[GateResult], GroupStats]:
    """Roll entries across every symbol, ask every gate what it would have said, and split
    the forward outcomes by each answer.

    Returns `(per-gate results, the ungated baseline)` — the baseline being every counted
    entry, i.e. exactly what `app/evaluate.py` measures with no selection at all.

    Pure apart from the `gates_forced_on()` toggle: candles are injected, nothing is fetched,
    nothing is written.
    """
    btc = candles_by_symbol.get("BTC", [])
    snapshots: dict[tuple[str, int], GateSnapshot] = {}
    outcomes: dict[tuple[str, int], EntryOutcome] = {}

    with gates_forced_on():
        for symbol, candles in candles_by_symbol.items():
            for i in rolled_entry_indices(candles, warmup_bars, spacing_days):
                outcome = entry_outcome(
                    candles, i, symbol, distance_pct=distance_pct, tp_pct=tp_pct,
                    max_waves=max_waves, pessimistic_intrabar=pessimistic_intrabar,
                    wave0_notional_usd=wave0_notional_usd,
                )
                if outcome is None:
                    continue  # incomplete trial — no verdict, so no evidence either way
                snap = gate_snapshot(
                    causal_window(candles, i, lookback_bars),
                    btc_window_at(btc, candles[i]["ts"], lookback_bars),
                    distance_pct=distance_pct, tp_pct=tp_pct, max_waves=max_waves,
                    pessimistic_intrabar=pessimistic_intrabar,
                )
                if snap is None:
                    continue
                snapshots[(symbol, i)] = snap
                outcomes[(symbol, i)] = outcome

        # Gate 4 is CROSS-SECTIONAL: it ranks one scan's candidates against each other, so it
        # has to be resolved per entry timestamp, over every symbol evaluated at that instant.
        quartile_dropped: set[tuple[str, int]] = set()
        by_ts: dict[int, list[tuple[str, int]]] = {}
        for key, outcome in outcomes.items():
            by_ts.setdefault(outcome.ts, []).append(key)
        for keys in by_ts.values():
            rows = [(f"{sym}|{idx}", snapshots[(sym, idx)].worst_mae) for sym, idx in keys]
            dropped = mark_mae_quartile_drops(rows)
            quartile_dropped |= {
                (sym, idx) for sym, idx in keys if f"{sym}|{idx}" in dropped
            }

    baseline = group_stats(list(outcomes.values()))
    # Trials overlap in time (deadline 30d vs. spacing 7d), so neighbouring entry dates carry
    # shared price history — the bootstrap resamples them in one piece.
    block = overlap_block_clusters(settings.deadline_days, spacing_days)

    # One veto table per entry: gate name -> did it veto. Everything below reads from this, so
    # a single gate and a stack of gates are scored by exactly the same code path.
    veto_by_key: dict[tuple[str, int], dict[str, bool]] = {}
    for key, snap in snapshots.items():
        veto_by_key[key] = {
            "downtrend": snap.downtrend,
            "falling_knife": snap.falling_knife,
            "rel_strength": snap.rel_strength,
            "mae_quartile": key in quartile_dropped,
            "backtest_evidence": snap.backtest_evidence,
            "consensus": snap.consensus,
        }

    def _score(name: str, ships_on: bool, is_vetoed) -> GateResult:
        vetoed = [o for key, o in outcomes.items() if is_vetoed(key)]
        passed = [o for key, o in outcomes.items() if not is_vetoed(key)]
        v_stats, p_stats = group_stats(vetoed), group_stats(passed)
        ci = cluster_bootstrap_lift_ci(passed, vetoed, rounds=bootstrap_rounds, seed=seed,
                                       block=block)
        lift = p_stats.profit_per_dollar_day - v_stats.profit_per_dollar_day
        return GateResult(name=name, ships_on=ships_on, vetoed=v_stats, passed=p_stats,
                          lift=lift, ci=ci, verdict=verdict(p_stats.n, v_stats.n, lift, ci))

    results = [
        _score(name, GATE_SHIPS_ON[name], lambda key, _n=name: veto_by_key[key][_n])
        for name in GATE_NAMES
    ]
    for label, only_shipped in ((STACK_SHIPPED, True), (STACK_ALL, False)):
        names = stack_names(only_shipped)
        results.append(_score(
            label, only_shipped,
            lambda key, _names=names: stacked_vetoed(veto_by_key[key], _names),
        ))
    return results, baseline


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

_HEADER = (
    f"{'gate':<18}{'ships':>6}{'veto_n':>8}{'pass_n':>8}"
    f"{'veto_$/d-day':>14}{'pass_$/d-day':>14}{'lift':>12}"
    f"{'95% CI (cluster bootstrap)':>30}  verdict"
)
_ROW = (
    "{name:<18}{ships:>6}{vn:>8d}{pn:>8d}"
    "{vppd:>14.6f}{pppd:>14.6f}{lift:>+12.6f}"
    "{ci:>30}  {verdict}"
)
_DETAIL_HEADER = (
    f"{'gate':<18}{'group':>8}{'n':>7}{'stop%':>8}{'win%':>8}{'E%':>9}"
    f"{'pnl_usd':>12}{'cap_days':>12}"
)
_DETAIL_ROW = (
    "{name:<18}{group:>8}{n:>7d}{stop:>8.1f}{win:>8.1f}{exp:>+9.3f}"
    "{pnl:>12.2f}{cap:>12.1f}"
)


def format_report(results: Sequence[GateResult], baseline: GroupStats, title: str) -> str:
    """Render the lift table plus a per-group breakdown. Sample sizes are printed on every
    row: a lift without its n is not a finding."""
    lines = [title, "=" * len(_HEADER)]
    lines.append(
        f"baseline (no selection at all): n={baseline.n}  "
        f"$/dollar-day={baseline.profit_per_dollar_day:+.6f}  "
        f"stop={baseline.stop_rate:.1f}%  win={baseline.win_rate:.1f}%  "
        f"E={baseline.expectancy:+.3f}%"
    )
    lines.append("")
    lines.append(_HEADER)
    lines.append("-" * len(_HEADER))
    for r in results:
        ci = f"[{r.ci[0]:+.6f}, {r.ci[1]:+.6f}]" if r.ci else "n/a"
        lines.append(_ROW.format(
            name=r.name, ships="on" if r.ships_on else "off",
            vn=r.vetoed.n, pn=r.passed.n,
            vppd=r.vetoed.profit_per_dollar_day, pppd=r.passed.profit_per_dollar_day,
            lift=r.lift, ci=ci, verdict=r.verdict,
        ))
    lines.append("")
    lines.append(_DETAIL_HEADER)
    lines.append("-" * len(_DETAIL_HEADER))
    for r in results:
        for group, s in (("vetoed", r.vetoed), ("passed", r.passed)):
            lines.append(_DETAIL_ROW.format(
                name=r.name if group == "vetoed" else "", group=group, n=s.n,
                stop=s.stop_rate, win=s.win_rate, exp=s.expectancy,
                pnl=s.pnl_usd, cap=s.capital_days,
            ))
    lines.append("")
    # ASCII only: this prints to a Windows console on cp1252, where a U+2212 minus sign dies.
    lines.append("lift = pass_$/d-day - veto_$/d-day; POSITIVE means the gate vetoed the "
                 "WORSE entries (it earns its keep).")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Network I/O (kept apart from everything above, which is pure)
# ---------------------------------------------------------------------------


def fetch_candles(
    symbols: list[str], timeframe: str, lookback_days: int
) -> dict[str, list[Candle]]:
    """Fetch OHLCV per symbol via the configured provider, reusing `app.scanner._days_to_bars`
    for the calendar-days → bars conversion and letting `CcxtProvider.get_ohlcv` do its own
    paging past Binance's 1000-kline cap. Symbols with no data are dropped, not crashed on."""
    limit = _days_to_bars(lookback_days, timeframe)
    provider = data_provider()
    out: dict[str, list[Candle]] = {}
    for symbol in symbols:
        candles = provider.get_ohlcv(symbol, timeframe, limit)
        if candles:
            out[symbol] = candles
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Measure whether each scanner entry gate improves outcomes."
    )
    p.add_argument("--symbols", type=str, default=",".join(DEFAULT_SYMBOLS))
    p.add_argument("--timeframe", type=str, default=DEFAULT_TIMEFRAME,
                   help="Candle timeframe (default: settings.backtest_timeframe).")
    p.add_argument("--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS,
                   help="Calendar days of history to fetch.")
    p.add_argument("--warmup-days", type=int, default=DEFAULT_WARMUP_DAYS,
                   help="Leading days used only as gate history, never as an entry.")
    p.add_argument("--spacing-days", type=float, default=None,
                   help="Entry spacing (default: settings.backtest_trial_spacing_days).")
    p.add_argument("--distance", type=float, default=settings.scan_distance_pct)
    p.add_argument("--tp", type=float, default=settings.scan_tp_pct)
    p.add_argument("--waves", type=int, default=settings.scan_max_waves)
    p.add_argument("--wave0-usd", type=float, default=DEFAULT_WAVE0_USD)
    p.add_argument("--bootstrap", type=int, default=DEFAULT_BOOTSTRAP_ROUNDS)
    p.add_argument("--seed", type=int, default=DEFAULT_SEED)
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    spacing = (settings.backtest_trial_spacing_days
               if args.spacing_days is None else args.spacing_days)
    lookback_bars = _days_to_bars(settings.backtest_lookback_days, args.timeframe)
    warmup_bars = _days_to_bars(args.warmup_days, args.timeframe)

    print(f"Fetching {args.timeframe} candles, {args.lookback_days}d: {', '.join(symbols)}")
    candles_by_symbol = fetch_candles(symbols, args.timeframe, args.lookback_days)
    missing = [s for s in symbols if s not in candles_by_symbol]
    if missing:
        print(f"  (no data for: {', '.join(missing)})")
    if not candles_by_symbol:
        print("No candle data — nothing to measure.")
        return
    for sym, cs in candles_by_symbol.items():
        print(f"  {sym}: {len(cs)} bars")

    print(f"\nparams: distance={args.distance}% tp={args.tp}% waves={args.waves} "
          f"spacing={spacing}d warmup={warmup_bars} bars lookback={lookback_bars} bars "
          f"sl={settings.sl_pct}% deadline={settings.deadline_days}d "
          f"cost={costengine.round_trip_cost_pct():.2f}% "
          f"effective_tp={args.tp + costengine.tp_fee_buffer_pct():.2f}%")

    for pessimistic in (False, True):
        results, baseline = measure(
            candles_by_symbol,
            distance_pct=args.distance, tp_pct=args.tp, max_waves=args.waves,
            lookback_bars=lookback_bars, warmup_bars=warmup_bars, spacing_days=spacing,
            wave0_notional_usd=args.wave0_usd, pessimistic_intrabar=pessimistic,
            bootstrap_rounds=args.bootstrap, seed=args.seed,
        )
        bound = "PESSIMISTIC (high-then-low)" if pessimistic else "OPTIMISTIC (low-then-high)"
        print()
        print(format_report(results, baseline, f"intra-bar bound: {bound}"))


if __name__ == "__main__":
    main()
