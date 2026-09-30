"""Hourly-bar re-run of the current paper posture over 2024-01..2026-07 (docs/capital-hourly-2026-09-29/).

WHY THIS EXISTS
    docs/cash-floor-release-2026-09-28/report.md found that on DAILY bars the two intra-bar
    bounds (pessimistic: a rung fills before a take-profit on the same bar; optimistic: the
    reverse) disagree wildly for 2024-01..2026-07 -- pessimistic gives the current posture
    CAGR ~ -25%/yr, DD 74%; optimistic ~ +86%/yr, because on a 1d bar the ordering decides
    whether a deep rung fills before the take-profit that would have closed the session first.
    Whole-period (2021-2026) profit was dominated by 2021, so the daily bound gap on 2024-2026
    alone is the open question this grid answers. Hourly bars shrink the window in which that
    ordering ambiguity can matter from a whole day to a whole hour.

ENGINE. Same `scripts/capital_portfolio_study.py` as the 2026-09-28 studies, now fixed
    (2026-09-29) to convert every day-denominated knob correctly when fed hourly bars instead
    of daily: `max_new_per_day` is a calendar-day budget (not a per-bar one), the
    universe-breadth/crash-release lookback windows scale by bars-per-day, and CAGR/drawdown/
    yearly stats are computed on one equity sample PER CALENDAR DAY (the day's last bar), not
    per bar -- see `_detect_bars_per_day`, `TestHourlyBarsReduceToDaily` in
    tests/app/test_capital_portfolio.py. On 1d input every one of those is a no-op: this script
    produces byte-identical numbers to a `capital_portfolio_study.py`/`cash_floor_release_grid.py`
    run on the same daily inputs (the `--interval 1d` sanity cells below exist to demonstrate
    exactly that, side by side with the 1h numbers).

UNIVERSE. `data/research/market.db`'s `1h` table was, before this script's data pull, only the
    150 symbols selected by 2025+ volume (survivorship-biased: no delisted coins, nothing listed
    after that ranking was taken). `docs/capital-hourly-2026-09-29/build_universe.py` (run once,
    not part of this script) filled in 1h candles for every USDT spot symbol with ANY `1d` bar
    in [2024-01-01, 2026-07-31] that did not already have them, from data.binance.vision -- see
    that file's own docstring for the exact query and the delisted-symbol count.

POSTURE (mirrors docs/cash-floor-release-2026-09-28/report.md's FIXED, same $7,000 book):
    capital $7,000; gate=reserve; wave0 0.4% of equity, capped $40, floor $10; 10 rungs @ 7%;
    TP 5% + 0.5%/rung (+ costengine's live fee buffer); SL 0; 60-day deadline; no trail;
    <=80 sessions; equity_backup 24.8%; deep-ladder lock at 4; backstop OFF throughout.
    warmup 24 = listing age >= 24 DAYS on both intervals (on 1h measured from the 1d table's
    first bar via `listing_ts`; before the 2026-09-29 verification it was 24 BARS = 24 hours on
    1h, which admitted fresh listings the day after they listed. Production: 30 daily candles).

CELLS (per the 2026-09-29 request):
    cov1_floor20, cov1_floor0, cov30_floor20, cov30_floor0 -- the coverage x cash-floor 2x2 --
    plus A_crash_K1_R100_W7 (floor-release policy A, K=1, R=100%, W=7 days, crash lag 1 BAR --
    one hour on 1h, still causal: bar t-1's breadth is complete at its close; the breadth itself
    is each bar's low vs the trailing 24 h high on 1h, vs the previous day's high on 1d; note
    `crash_release_active_days` counts active BARS, i.e. hours on 1h) on coverage 1 / floor 20, the one cell the 2026-09-28
    verification found to have a real (if modest) effect.
    x max_new_per_day in {5, 40} (production sees 22-48/day through the entry gate; 5 and 40
    bracket "starved for candidates" and "never candidate-constrained")
    x bound in {pessimistic, optimistic}
    x 30 seeds (0..29)
    x interval in {1h, 1d} -- the 1d cells are the requested sanity check that the 1h bounds sit
      inside the 1d bounds on the SAME code path, not a rerun of the 2026-09-28 daily study
      (that one ran 2021-2026 and 641 symbols; this repeats only 2024-2026 on whatever symbols
      have both 1d and 1h data, for a fair side-by-side).

    python scripts/capital_hourly_grid.py --seeds 30 --workers 6 \
        --out docs/capital-hourly-2026-09-29

Memory/time: the hourly universe has ~22,600 bars/symbol x ~590 symbols. `run_portfolio`'s
outer loop is one pass over the UNION of all bar timestamps (not symbols x bars), so it is
~22,600 iterations regardless of interval choice inside that iteration the per-bar candidate
scan is O(#symbols); a single 1h/40-open-rate run took ~[fill in from the observed timing] on
this box. Each worker process holds one copy of `series` (loaded once in `_pool_init`, shared
via fork/COW on POSIX -- on Windows `multiprocessing.Pool` re-pickles it per worker instead, so
worker count is capped at 6 here specifically to keep resident memory under the project's
12 GB ceiling while the paper app is running on the same box; raise `--workers` only on a box
that is not also serving paper traffic).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from multiprocessing import Pool
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import costengine  # noqa: E402
from scripts.capital_portfolio_study import Config, _to_ts, run_portfolio  # noqa: E402
from scripts.ladder_panel_study import to_candles  # noqa: E402
from scripts.liquidity_tier_study import load  # noqa: E402

CAPITAL = 7000.0
SINCE = "2024-01-01"
UNTIL = "2026-07-31"

FIXED = {
    "capital": CAPITAL, "gate": "reserve", "distance_pct": 7.0, "max_waves": 10,
    "tp_pct": 5.0, "tp_step_pct": 0.5, "sl_pct": 0.0, "deadline_days": 60.0,
    "trail_after_tp_pct": 0.0, "wave0_pct": 0.4, "wave0_cap": 40.0, "wave0_floor": 10.0,
    "warmup": 24, "max_sessions": 80, "equity_backup_pct": 24.8, "deep_lock_rungs": 4,
    "partial_last_rung": True, "backstop": False,
}

CRASH_DROP_PCT = 20.0
CRASH_BREADTH_PCT = 60.0

CELLS: dict[str, dict] = {
    "cov1_floor20": {"coverage_pct": 1.0, "cash_floor_pct": 20.0},
    "cov1_floor0": {"coverage_pct": 1.0, "cash_floor_pct": 0.0},
    "cov30_floor20": {"coverage_pct": 30.0, "cash_floor_pct": 20.0},
    "cov30_floor0": {"coverage_pct": 30.0, "cash_floor_pct": 0.0},
    "cov1_A_crash_K1_R100_W7": {
        "coverage_pct": 1.0, "cash_floor_pct": 20.0,
        "floor_release_trigger": "crash", "floor_release_min_wave": 1,
        "floor_release_frac_pct": 100.0, "floor_release_crash_drop_pct": CRASH_DROP_PCT,
        "floor_release_crash_breadth_pct": CRASH_BREADTH_PCT,
        "floor_release_crash_window_days": 7.0, "floor_release_crash_lag_bars": 1,
    },
}

TOTAL_KEYS = [
    "cagr_own_pct", "cagr_pct", "final_nav_own", "max_drawdown_unit_nav_pct",
    "max_drawdown_pct", "max_drawdown_unit_nav_date", "ended_below_start_capital",
    "sessions_opened", "sessions_still_open_at_end", "avg_waves_filled",
    "rungs_starved", "rungs_partial", "starved_usd", "starved_rungs_distinct",
    "starved_distinct_usd", "worst_session_usd", "deadline_exits", "deadline_exits_usd",
    "deadline_losses", "deadline_losses_usd", "delisted_exits", "delisted_exits_usd",
    "open_at_end_unrealized_usd", "util_avg_pct", "util_median_daily_pct",
    "util_normal_days_pct", "normal_days_share_pct", "floor_release_usd",
    "floor_release_events", "crash_release_episodes", "crash_release_active_days",
]


def make_config(*, pessimistic: bool, seed: int, cost: float, buf: float,
                max_new_per_day: int, over: dict) -> Config:
    kw = {
        "gate": FIXED["gate"], "pessimistic": pessimistic, "capital": FIXED["capital"],
        "distance_pct": FIXED["distance_pct"], "max_waves": FIXED["max_waves"],
        "tp_pct": FIXED["tp_pct"], "tp_step_pct": FIXED["tp_step_pct"], "sl_pct": FIXED["sl_pct"],
        "deadline_days": FIXED["deadline_days"], "trail_after_tp_pct": FIXED["trail_after_tp_pct"],
        "cost_pct": cost, "tp_fee_buffer_pct": buf, "wave0_usd": 28.0,
        "wave0_pct": FIXED["wave0_pct"], "wave0_cap": FIXED["wave0_cap"],
        "wave0_floor": FIXED["wave0_floor"], "warmup": FIXED["warmup"],
        "max_sessions": FIXED["max_sessions"], "max_new_per_day": max_new_per_day,
        "equity_backup_pct": FIXED["equity_backup_pct"], "deep_lock_rungs": FIXED["deep_lock_rungs"],
        "partial_last_rung": FIXED["partial_last_rung"], "backstop": FIXED["backstop"],
        "seed": seed,
    }
    kw.update(over)
    return Config(**kw)


_SERIES: dict = {}
_SINCE_TS: int | None = None
_UNTIL_TS: int | None = None
_LISTING: dict | None = None


def _pool_init(series, since_ts, until_ts, listing=None):
    global _SERIES, _SINCE_TS, _UNTIL_TS, _LISTING
    _SERIES, _SINCE_TS, _UNTIL_TS, _LISTING = series, since_ts, until_ts, listing


def listing_ts_from_1d(db_path: Path) -> dict[str, int]:
    """symbol -> first 1d bar ts: the listing date (or 2021-01-01 for older coins). On hourly
    bars `warmup` is a listing age in DAYS measured from this (2026-09-29 verification: the 1h
    table starts at 2024-01, so its own first bar is not the listing)."""
    import sqlite3
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return dict(db.execute(
            "SELECT symbol, MIN(ts) FROM candles WHERE interval='1d' GROUP BY symbol").fetchall())
    finally:
        db.close()


def _year_slice(rep: dict, year: str) -> dict:
    for row in rep["yearly"]:
        if row["year"] == year:
            return row
    return {}


def _run_one(job: tuple[str, Config]) -> tuple[str, dict]:
    label, cfg = job
    rep = run_portfolio(_SERIES, cfg, _SINCE_TS, _UNTIL_TS, listing_ts=_LISTING)
    t = rep["totals"]
    row = {k: t.get(k) for k in TOTAL_KEYS}
    row["ended_below_start_capital"] = 1.0 if t["ended_below_start_capital"] else 0.0
    row["seed"] = cfg.seed
    row["pessimistic"] = cfg.pessimistic
    for y in ("2024", "2025", "2026"):
        yr = _year_slice(rep, y)
        row[f"y{y}.return_pct"] = yr.get("return_pct_on_start_capital")
        row[f"y{y}.max_dd_pct"] = yr.get("max_drawdown_pct")
    w = rep["windows"].get("post_2025_10_10", {})
    for k, v in w.items():
        row[f"crash2025.{k}"] = v
    return label, row


def _run_batch(pool, jobs, tag, checkpoint_path: Path | None = None, all_rows: dict | None = None):
    t0 = time.time()
    out: dict[str, dict] = {}
    for i, (label, row) in enumerate(pool.imap_unordered(_run_one, jobs, chunksize=1), 1):
        out[label] = row
        step = max(1, len(jobs) // 40)
        if i % step == 0 or i == len(jobs):
            print(f"  [{tag}] {i}/{len(jobs)}  ({time.time()-t0:,.0f}s)", file=sys.stderr, flush=True)
            _checkpoint(checkpoint_path, all_rows, tag, out)
    return out


def _run_sequential(jobs, tag, checkpoint_path: Path | None = None, all_rows: dict | None = None):
    """No Pool at all: one process, one copy of `series` in memory. Required for the 1h
    dataset (~6-8 GB per copy of `series` -- see the module docstring's memory note; a
    `multiprocessing.Pool` on Windows re-pickles a full copy per worker via `spawn`, so even
    `Pool(1)` would briefly hold two full copies and any `Pool(N>1)` would need N+1, blowing
    past the project's 12 GB ceiling while the paper app shares the box)."""
    t0 = time.time()
    out: dict[str, dict] = {}
    for i, job in enumerate(jobs, 1):
        label, row = _run_one(job)
        out[label] = row
        step = max(1, len(jobs) // 40)
        if i % step == 0 or i == len(jobs):
            print(f"  [{tag}] {i}/{len(jobs)}  ({time.time()-t0:,.0f}s)", file=sys.stderr, flush=True)
            _checkpoint(checkpoint_path, all_rows, tag, out)
    return out


def _checkpoint(checkpoint_path: Path | None, all_rows: dict | None, tag: str, out: dict) -> None:
    """Write whatever is done so far to disk. A 1h grid can run for hours in one sequential
    process; without this, checking progress mid-run or recovering from an interruption would
    mean losing every completed job."""
    if checkpoint_path is None or all_rows is None:
        return
    snapshot = {**all_rows, tag: out}
    checkpoint_path.write_text(json.dumps(
        {"generated_at": datetime.now(timezone.utc).isoformat(), "partial": True, "rows": snapshot},
        indent=1))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--db", default="data/research/market.db")
    p.add_argument("--out", default="docs/capital-hourly-2026-09-29")
    p.add_argument("--seeds", type=int, default=30)
    p.add_argument("--workers", type=int, default=6, help="used for any interval without its own --workers-<interval>")
    p.add_argument("--workers-1h", type=int, default=None,
                   help="1h's `series` is ~6-8 GB/copy (measured 2026-09-29 at 505/599 symbols: "
                        "5.87 GB RSS) -- a Windows multiprocessing.Pool re-pickles a full copy "
                        "PER WORKER, so anything above 1 risks the box's 12 GB ceiling. Default 1 "
                        "(sequential, no Pool at all -- see `_run_sequential`).")
    p.add_argument("--workers-1d", type=int, default=None,
                   help="1d's `series` is ~1.2M rows, small enough for the 8-worker precedent "
                        "the 2026-09-28 studies already used safely. Default 6.")
    p.add_argument("--since", default=SINCE)
    p.add_argument("--until", default=UNTIL)
    p.add_argument("--intervals", default="1h,1d")
    p.add_argument("--max-new-per-day", default="5,40")
    p.add_argument("--cells", default="", help="comma-separated subset of CELLS; default = all")
    p.add_argument("--tag", default="grid")
    p.add_argument("--seed-start", type=int, default=0)
    p.add_argument("--bounds", default="pess,opt")
    args = p.parse_args(argv)

    cost = costengine.round_trip_cost_pct()
    buf = costengine.tp_fee_buffer_pct()
    print(f"cost_pct={cost:.3f}%  tp_fee_buffer_pct={buf:.3f}%", file=sys.stderr)

    cell_names = list(CELLS) if not args.cells else args.cells.split(",")
    max_news = [int(x) for x in args.max_new_per_day.split(",")]
    intervals = args.intervals.split(",")

    result_all = {}
    for interval in intervals:
        t_load = time.time()
        raw = load(Path(args.db), interval)
        series = {sym: to_candles(bars) for sym, bars in raw.items()}
        del raw  # the tuple copy is ~as large as `series` on 1h; never hold both during the runs
        listing = None if interval == "1d" else listing_ts_from_1d(Path(args.db))
        print(f"[{interval}] {len(series)} symbols, "
              f"{sum(len(v) for v in series.values()):,} bars, load {time.time()-t_load:.1f}s",
              file=sys.stderr)

        jobs: list[tuple[str, Config]] = []
        for cell in cell_names:
            over = CELLS[cell]
            for max_new in max_news:
                for b in args.bounds.split(","):
                    pess = b == "pess"
                    for seed in range(args.seed_start, args.seed_start + args.seeds):
                        label = f"{interval}|{cell}|n{max_new}|{b}|s{seed}"
                        jobs.append((label, make_config(
                            pessimistic=pess, seed=seed, cost=cost, buf=buf,
                            max_new_per_day=max_new, over=over)))
        workers = (args.workers_1h if interval == "1h" else
                  args.workers_1d if interval == "1d" else None)
        if workers is None:
            workers = args.workers
        print(f"[{interval}] {len(jobs)} runs, {workers} workers", file=sys.stderr)
        out_dir = Path(args.out)
        out_dir.mkdir(parents=True, exist_ok=True)
        checkpoint_path = out_dir / f"results_{args.tag}.partial.json"
        t0 = time.time()
        if workers <= 1:
            _pool_init(series, _to_ts(args.since), _to_ts(args.until), listing)
            rows = _run_sequential(jobs, interval, checkpoint_path, result_all)
        else:
            with Pool(workers, initializer=_pool_init,
                      initargs=(series, _to_ts(args.since), _to_ts(args.until), listing)) as pool:
                rows = _run_batch(pool, jobs, interval, checkpoint_path, result_all)
        print(f"[{interval}] all done in {time.time()-t0:,.0f}s", file=sys.stderr)
        result_all[interval] = rows
        del series  # 1h's copy is multi-GB; drop it before the next interval loads its own

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "seeds": list(range(args.seed_start, args.seed_start + args.seeds)), "cost_pct": cost, "tp_fee_buffer_pct": buf,
        "fixed": FIXED, "cells": CELLS, "since": args.since, "until": args.until,
        "max_new_per_day": max_news, "intervals": intervals, "rows": result_all,
    }
    out_path = out_dir / f"results_{args.tag}.json"
    out_path.write_text(json.dumps(result, indent=1))
    print(f"wrote {out_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
