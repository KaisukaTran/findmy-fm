"""What configuration keeps >= 50% of the KSS paper book's money WORKING in normal times, and what
does it cost? (docs/capital-utilization-2026-09-28/)

CONTEXT. The paper account (~$7,000) sits at ~93% cash: 15 open sessions have spent only $577,
but production's session gate (`app.scanner._session_lock`, Fix A2 2026-09-21) reserves for
EVERY open session `min(fund, spent + ladder_coverage_pct% x fund)` against a budget of
`(100 - equity_backup_pct)% x equity` -- so a book of small, shallow sessions can fully occupy
the deploy budget on PROMISED future rungs while almost none of it is actually spent. The owner
wants >50% of the money working, and is willing to backstop deep rungs from an outside fund if
the book's own cash runs short.

ENGINE. `scripts/capital_portfolio_study.py` (per-bar math byte-for-byte the frozen
`app.backtest.simulate_kss`; parity locked by tests/app/test_capital_portfolio.py).

HISTORY. The first draft (Sonnet, 2026-09-28) ranked configs by FULL-HISTORY AVERAGE utilization
(deployed cost / equity). That number is inflated by bear markets: when deep rungs fill and
equity shrinks, a config 'uses' money by holding losers. The 2026-09-28 verification pass (Opus)
therefore reports, per seed: median DAILY utilization, utilization on NORMAL days (own unit-NAV
within 5% of its high-water mark), and the recent regime (2026-01..2026-07) -- and fixed these
engine gaps against production (see the test classes added that day):
  * `deep_ladder_lock_rungs` (live 4) was not modelled -> added (`deep_lock_rungs`).
  * the hard cash floor (`cash_floor_pct` 20% of anchored equity, orders.py:_apply_cash_cap) was
    not modelled -> added (`cash_floor_pct`); it also moves WHEN the backstop must step in.
  * backstop draws happened mid-bar, before other sessions' same-bar exits were credited ->
    now settled after all exits (`settle_backstop`).
  * repayment held back each session's whole lock INCLUDING already-spent cash -> now only the
    future (un-spent) part (`repay_backstop`).
  * the budget and the %-wave0 were sized on GROSS equity (incl. the outside fund's money) ->
    now on own unit-NAV (`_sizing_equity`).
  * 'CAGR on own + peak draw' dropped every repaid dollar -> fixed (`_cagr_total`).
  * sessions on delisted coins (173/641 symbols end early) stayed open forever -> now realized
    at the last close (`_close_delisted`).
  * coverage 0 is not a deployable value (scanner.py:1344/1371 treat 0 as 100%); the grid's
    floor is now 1% (routes.py/UI minimum).

STAGES
  grid      every (coverage, wave0 %, cap, rungs) cell x {pess, opt} x {backstop off, on} x N
            seeds; per-seed metric rows are kept (no single-seed point estimate is ever cited).
  followup  for named cells: max_new_per_day 5 (vs the grid's 40), the cash floor at 0, and the
            deep-lock trigger off -- pessimistic bound, both backstop modes, N seeds.

    python scripts/capital_utilization_grid.py --stage grid --seeds 30 --workers 8
    python scripts/capital_utilization_grid.py --stage followup --cells cov30_w0.4_cap40_r10 ...
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
SINCE = "2021-01-01"
UNTIL = "2026-07-31"

# Fixed production knobs, verified 2026-09-28 against app/config.py and the runtime_config rows
# of data/findmy.db (read-only): scan_distance_pct 7, scan_tp_pct 5, tp_step 0.5, sl 0,
# deadline 60d, kss_trail_after_tp_pct 0, max_concurrent_sessions 80, equity_backup_pct 24.8,
# deep_ladder_lock_rungs 4, capital_scale_enabled True, cash_floor_pct 20 (config default; no
# runtime override), first_wave_pct 0.4, first_wave_max_usd 40, scan_min_notional 10.
FIXED = {
    "capital": CAPITAL, "distance_pct": 7.0, "tp_pct": 5.0, "tp_step_pct": 0.5, "sl_pct": 0.0,
    "deadline_days": 60.0, "trail_after_tp_pct": 0.0, "wave0_floor": 10.0, "max_sessions": 80,
    "equity_backup_pct": 24.8, "deep_lock_rungs": 4, "cash_floor_pct": 20.0, "warmup": 24,
    "gate": "reserve",
}
MAX_NEW_PER_DAY = 40   # 5 per 15-min scan in production; 40/day is a moderate daily ceiling
MAX_NEW_SENS = 5

CURRENT = (30.0, 0.4, 40.0, 10)   # coverage, wave0 %, cap ($, 0 = none), rungs
COVERAGE_GRID = [30.0, 15.0, 5.0, 1.0]
WAVE0_PCT_GRID = [0.4, 0.8, 1.2]
CAP_GRID = [40.0, 0.0]
RUNGS_GRID = [10, 7, 5]


def cell_key(coverage: float, wave0_pct: float, cap: float, rungs: int) -> str:
    cap_label = "nocap" if cap <= 0 else f"cap{cap:g}"
    return f"cov{coverage:g}_w{wave0_pct:g}_{cap_label}_r{rungs}"


def parse_key(key: str) -> tuple[float, float, float, int]:
    cov, w, cap, r = key.split("_")
    return (float(cov[3:]), float(w[1:]), 0.0 if cap == "nocap" else float(cap[3:]), int(r[1:]))


def grid_cells() -> list[tuple[float, float, float, int]]:
    cells = []
    for cov in COVERAGE_GRID:
        for w in WAVE0_PCT_GRID:
            for cap in CAP_GRID:
                # 1.2% x $7,000 = $84 and 0.8% = $56 are both capped to $40 until own NAV drops
                # below $5,000 -- w1.2+cap40 is w0.8+cap40 in all but the deepest drawdowns.
                if cap > 0 and w >= 1.2:
                    continue
                for r in RUNGS_GRID:
                    cells.append((cov, w, cap, r))
    return cells


def make_config(cell, *, pessimistic: bool, backstop: bool, seed: int, cost: float, buf: float,
                **over) -> Config:
    coverage, wave0_pct, cap, rungs = cell
    kw = {
        "gate": FIXED["gate"], "pessimistic": pessimistic, "capital": FIXED["capital"],
        "distance_pct": FIXED["distance_pct"], "max_waves": rungs, "tp_pct": FIXED["tp_pct"],
        "tp_step_pct": FIXED["tp_step_pct"], "sl_pct": FIXED["sl_pct"],
        "deadline_days": FIXED["deadline_days"], "trail_after_tp_pct": FIXED["trail_after_tp_pct"],
        "cost_pct": cost, "tp_fee_buffer_pct": buf, "wave0_usd": 28.0, "wave0_pct": wave0_pct,
        "wave0_cap": cap, "wave0_floor": FIXED["wave0_floor"], "warmup": FIXED["warmup"],
        "max_sessions": FIXED["max_sessions"], "max_new_per_day": MAX_NEW_PER_DAY,
        "coverage_pct": coverage, "equity_backup_pct": FIXED["equity_backup_pct"],
        "deep_lock_rungs": FIXED["deep_lock_rungs"], "cash_floor_pct": FIXED["cash_floor_pct"],
        "partial_last_rung": True, "backstop": backstop, "seed": seed,
    }
    kw.update(over)
    return Config(**kw)


# ---------------------------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------------------------

_SERIES: dict = {}
_SINCE_TS: int | None = None
_UNTIL_TS: int | None = None

TOTAL_KEYS = [
    "util_avg_pct", "util_median_daily_pct", "util_normal_days_pct", "normal_days_share_pct",
    "util_recent_pct", "util_recent_median_pct", "own_util_median_daily_pct",
    "own_util_normal_days_pct", "own_util_recent_pct", "cagr_own_pct", "cagr_total_pct",
    "final_nav_own", "max_drawdown_unit_nav_pct", "max_drawdown_unit_nav_date",
    "ended_below_start_capital", "sessions_opened", "sessions_still_open_at_end",
    "avg_waves_filled", "rungs_starved", "rungs_partial", "starved_usd", "worst_session_usd",
    "deadline_exits", "deadline_exits_usd", "deadline_losses", "deadline_losses_usd",
    "delisted_exits", "delisted_exits_usd", "open_at_end_unrealized_usd",
    "external_draw_total_usd", "external_peak_outstanding_usd", "external_peak_date",
    "external_outstanding_at_end_usd", "external_draw_events",
]


def _pool_init(series, since_ts, until_ts):
    global _SERIES, _SINCE_TS, _UNTIL_TS
    _SERIES, _SINCE_TS, _UNTIL_TS = series, since_ts, until_ts


def _run_one(job: tuple[str, Config]) -> tuple[str, dict]:
    label, cfg = job
    rep = run_portfolio(_SERIES, cfg, _SINCE_TS, _UNTIL_TS)
    t = rep["totals"]
    row = {k: t[k] for k in TOTAL_KEYS}
    row["ended_below_start_capital"] = 1.0 if t["ended_below_start_capital"] else 0.0
    row["seed"] = cfg.seed
    for wname, w in rep["windows"].items():
        for k, v in w.items():
            row[f"{wname}.{k}"] = v
    return label, row


def _run_batch(pool, jobs, tag):
    t0 = time.time()
    out: dict[str, dict] = {}
    for i, (label, row) in enumerate(pool.imap_unordered(_run_one, jobs, chunksize=2), 1):
        out[label] = row
        if i % max(1, len(jobs) // 40) == 0 or i == len(jobs):
            print(f"  [{tag}] {i}/{len(jobs)}  ({time.time()-t0:,.0f}s)", file=sys.stderr, flush=True)
    return out


def _group(rows: dict[str, dict]) -> dict[str, list[dict]]:
    """'<group>|s<seed>' -> {group: [row, ...]} sorted by seed."""
    groups: dict[str, list[dict]] = {}
    for label, row in rows.items():
        g = label.rsplit("|", 1)[0]
        groups.setdefault(g, []).append(row)
    for g in groups:
        groups[g].sort(key=lambda r: r["seed"])
    return groups


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--db", default="data/research/market.db")
    p.add_argument("--out", default="docs/capital-utilization-2026-09-28")
    p.add_argument("--stage", choices=["grid", "followup"], required=True)
    p.add_argument("--cells", nargs="*", default=[])
    p.add_argument("--seeds", type=int, default=30)
    p.add_argument("--workers", type=int, default=8)
    args = p.parse_args(argv)

    cost = costengine.round_trip_cost_pct()
    buf = costengine.tp_fee_buffer_pct()
    print(f"cost_pct={cost:.3f}%  tp_fee_buffer_pct={buf:.3f}%", file=sys.stderr)
    raw = load(Path(args.db), "1d")
    series = {sym: to_candles(bars) for sym, bars in raw.items()}
    print(f"{len(series)} symbols", file=sys.stderr)

    jobs: list[tuple[str, Config]] = []
    if args.stage == "grid":
        for cell in grid_cells():
            key = cell_key(*cell)
            for pess in (True, False):
                for bs in (False, True):
                    for seed in range(args.seeds):
                        cfg = make_config(cell, pessimistic=pess, backstop=bs, seed=seed,
                                          cost=cost, buf=buf)
                        b = "pess" if pess else "opt"
                        jobs.append((f"{key}|{b}|bs{int(bs)}|s{seed}", cfg))
        # the first draft's engine assumptions for the current posture (no floor, no deep lock)
        for pess in (True, False):
            for seed in range(args.seeds):
                cfg = make_config(CURRENT, pessimistic=pess, backstop=False, seed=seed, cost=cost,
                                  buf=buf, cash_floor_pct=0.0, deep_lock_rungs=0)
                b = "pess" if pess else "opt"
                jobs.append((f"{cell_key(*CURRENT)}~nofloor_nodeeplock|{b}|bs0|s{seed}", cfg))
        out_name = "results_verified.json"
    else:
        variants = {
            "n5": {"max_new_per_day": MAX_NEW_SENS},
            "floor0": {"cash_floor_pct": 0.0},
            "deeplock0": {"deep_lock_rungs": 0},
        }
        for key in args.cells:
            cell = parse_key(key)
            for vname, over in variants.items():
                for bs in (False, True):
                    for seed in range(args.seeds):
                        cfg = make_config(cell, pessimistic=True, backstop=bs, seed=seed,
                                          cost=cost, buf=buf, **over)
                        jobs.append((f"{key}~{vname}|pess|bs{int(bs)}|s{seed}", cfg))
        out_name = "results_followup.json"

    print(f"{len(jobs)} runs, {args.workers} workers", file=sys.stderr)
    with Pool(args.workers, initializer=_pool_init,
              initargs=(series, _to_ts(SINCE), _to_ts(UNTIL))) as pool:
        rows = _run_batch(pool, jobs, args.stage)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(), "stage": args.stage,
        "seeds": list(range(args.seeds)), "cost_pct": cost, "tp_fee_buffer_pct": buf,
        "fixed": FIXED, "max_new_per_day": MAX_NEW_PER_DAY, "since": SINCE, "until": UNTIL,
        "groups": _group(rows),
    }
    (out_dir / out_name).write_text(json.dumps(result, default=str), encoding="utf-8")
    print(f"wrote {out_dir / out_name}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
