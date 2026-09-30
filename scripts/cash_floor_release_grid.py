"""Does spending the 20% cash floor ever beat leaving it alone? (docs/cash-floor-release-2026-09-28/)

CONTEXT. Today NOTHING ever spends the hard cash floor (`app.orders._apply_cash_cap`,
`cash_floor_pct` = 20% of anchored equity on paper): it only trims/blocks a BUY and fires
`rung_starved`. The owner asked to measure two policies that let it be spent on purpose:

  A - automatic conditional release: a DCA rung at or past wave K that would otherwise starve
      may spend the floor down to (1 - R) x floor, but only while a TRIGGER is active
      ("starved" = the starve itself is the trigger, i.e. always-on once K is reached; "crash"
      = a market-wide crash signal mirroring app/crash_watch.py must be active).
  B - manual (Telegram) release: same K/R and "starved" trigger as A, but the release is a
      REQUEST that only fills D days later, and only if the owner approves AND the rung's price
      is still touched that day (limit semantics; reuses the engine's own `_fill_price`).

ENGINE. `scripts/capital_portfolio_study.py`'s `floor_release_*` / cash-floor-release Config
fields (2026-09-28b) - default OFF, so every earlier study (incl. this repo's own
capital-utilization-2026-09-28 grid) is untouched. Parity + the new mechanics are pinned by
`tests/app/test_capital_portfolio.py`'s `TestFloorRelease*` / `TestCrashReleaseTriggerBookkeeping`
classes.

POSTURE. The book this grid measures is the CURRENT paper posture (2026-09-28): $7,000, reserve
gate at coverage 1%, wave0 0.4% of equity capped $40, 10 rungs @ 7% distance, TP 5% + 0.5%/rung
(+0.24% fee buffer), SL 0, 60-day deadline, no trail, <=80 sessions, equity_backup 24.8%,
deep-ladder lock at 4 rungs, cash floor 20% baseline. Backstop (the external-fund knob) is OFF
throughout this whole grid — the owner confirmed no automatic outside money for this question.

STAGES
  grid   every policy cell x {pess, opt} x N seeds, plus the floor20 (current) and floor0 (upper
         bound) baselines. Per-seed summary rows only (no equity curve) - see TOTAL_KEYS.
  sens   5-opens/day sensitivity for the two baselines plus the best A and best B cell named on
         the command line (picked after reading the `grid` stage's results).

    python scripts/cash_floor_release_grid.py --stage grid --seeds 30 --workers 8
    python scripts/cash_floor_release_grid.py --stage sens --best-a A_starved_K1_R100 \
        --best-b B_D1_K1_R100_always --seeds 30 --workers 8

  verify (2026-09-28c, adversarial check) the named cells + both baselines on any seed range,
         period and crash-trigger lag, pessimistic bound only unless --bounds both:
    python scripts/cash_floor_release_grid.py --stage verify --tag oos_lag1 --seed-start 30 \
        --seeds 30 --crash-lag 1 --cells A_crash_K6_R50_W7,A_crash_K4_R100_W7 --workers 8

NOTE (2026-09-28c): results.json / results_sens.json were produced BEFORE the verification fixes:
the crash trigger then released on the SAME bar whose breadth fired it (lookahead; `--crash-lag 0`
reproduces it, the engine default is now 1), `floor_release_usd` double-counted the deficit the
book already carried below the floor, and `starved_usd` sums daily retries of the same rung.
See report.md.
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

# The CURRENT paper posture, 2026-09-28 (docs/paper-7k-reset-2026-09-21.md config + the
# 2026-09-28 coverage-1% recommendation actually running — see MEMORY.md capital-scaling entry).
FIXED = {
    "capital": CAPITAL, "gate": "reserve", "distance_pct": 7.0, "max_waves": 10,
    "tp_pct": 5.0, "tp_step_pct": 0.5, "sl_pct": 0.0, "deadline_days": 60.0,
    "trail_after_tp_pct": 0.0, "wave0_pct": 0.4, "wave0_cap": 40.0, "wave0_floor": 10.0,
    "warmup": 24, "max_sessions": 80, "coverage_pct": 1.0, "equity_backup_pct": 24.8,
    "deep_lock_rungs": 4, "partial_last_rung": True, "backstop": False,
}
MAX_NEW_PER_DAY = 40
MAX_NEW_SENS = 5

K_GRID = [1, 4, 6]
R_GRID = [50.0, 100.0]
W_GRID = [3.0, 7.0]
D_GRID = [1.0, 2.0]
OWNER_GRID = ["always", "crash_only"]
CRASH_DROP_PCT = 20.0     # mirrors crash_alert_drop_pct's default (app/config.py)
CRASH_BREADTH_PCT = 60.0  # mirrors crash_alert_breadth_pct's default


def policy_cells() -> dict[str, dict]:
    """group label -> the Config kwargs that differ from FIXED/baseline."""
    cells: dict[str, dict] = {}
    for k in K_GRID:
        for r in R_GRID:
            cells[f"A_starved_K{k:g}_R{r:g}"] = {
                "floor_release_trigger": "starved", "floor_release_min_wave": k,
                "floor_release_frac_pct": r,
            }
            for w in W_GRID:
                cells[f"A_crash_K{k:g}_R{r:g}_W{w:g}"] = {
                    "floor_release_trigger": "crash", "floor_release_min_wave": k,
                    "floor_release_frac_pct": r, "floor_release_crash_drop_pct": CRASH_DROP_PCT,
                    "floor_release_crash_breadth_pct": CRASH_BREADTH_PCT,
                    "floor_release_crash_window_days": w,
                }
            for d in D_GRID:
                for owner in OWNER_GRID:
                    otag = "always" if owner == "always" else "crashonly"
                    cells[f"B_D{d:g}_K{k:g}_R{r:g}_{otag}"] = {
                        "floor_release_manual_delay_days": d, "floor_release_min_wave": k,
                        "floor_release_frac_pct": r, "floor_release_manual_owner": owner,
                        "floor_release_crash_drop_pct": CRASH_DROP_PCT,
                        "floor_release_crash_breadth_pct": CRASH_BREADTH_PCT,
                    }
    return cells


def make_config(*, pessimistic: bool, seed: int, cost: float, buf: float, cash_floor_pct: float,
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
        "coverage_pct": FIXED["coverage_pct"], "equity_backup_pct": FIXED["equity_backup_pct"],
        "deep_lock_rungs": FIXED["deep_lock_rungs"], "cash_floor_pct": cash_floor_pct,
        "partial_last_rung": FIXED["partial_last_rung"], "backstop": FIXED["backstop"],
        "seed": seed,
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
    "cagr_own_pct", "cagr_total_pct", "final_nav_own", "max_drawdown_unit_nav_pct",
    "max_drawdown_pct", "max_drawdown_unit_nav_date", "ended_below_start_capital",
    "sessions_opened", "sessions_still_open_at_end", "avg_waves_filled",
    "rungs_starved", "rungs_partial", "starved_usd", "worst_session_usd",
    "deadline_exits", "deadline_exits_usd", "deadline_losses", "deadline_losses_usd",
    "delisted_exits", "delisted_exits_usd", "open_at_end_unrealized_usd",
    "floor_release_usd", "floor_release_events", "manual_requests_sent",
    "manual_requests_approved", "manual_requests_denied", "manual_requests_missed_price",
    "manual_requests_missed_cash", "crash_release_episodes", "crash_release_active_days",
    "crash_release_fired_dates", "starved_rungs_distinct", "starved_distinct_usd",
]


def _days_floor_below_50pct(curve: list[dict]) -> int:
    return sum(1 for r in curve if r["floor"] > 0 and r["cash"] < 0.5 * r["floor"])


def _days_floor_at_zero(curve: list[dict], eps: float = 1.0) -> int:
    return sum(1 for r in curve if r["floor"] > 0 and r["cash"] <= eps)


def _recovery_days_from_max_dd(curve: list[dict]) -> float | None:
    """Calendar days from the trough of the worst own-unit-NAV drawdown back to the PRIOR peak.
    None if the book never recovered by the end of the run."""
    if not curve:
        return None
    peak = curve[0]["nav_own"]
    peak_i = 0
    worst_dd = 0.0
    trough_i = 0
    trough_peak_i = 0
    for i, r in enumerate(curve):
        if r["nav_own"] > peak:
            peak = r["nav_own"]
            peak_i = i
        if peak > 0:
            dd = r["nav_own"] / peak - 1
            if dd < worst_dd:
                worst_dd = dd
                trough_i = i
                trough_peak_i = peak_i
    if worst_dd == 0.0:
        return 0.0
    target = curve[trough_peak_i]["nav_own"]
    for j in range(trough_i, len(curve)):
        if curve[j]["nav_own"] >= target:
            from datetime import date as _d
            d0 = _d.fromisoformat(curve[trough_i]["date"])
            d1 = _d.fromisoformat(curve[j]["date"])
            return float((d1 - d0).days)
    return None  # never recovered inside the data window


def _pool_init(series, since_ts, until_ts):
    global _SERIES, _SINCE_TS, _UNTIL_TS
    _SERIES, _SINCE_TS, _UNTIL_TS = series, since_ts, until_ts


def _run_one(job: tuple[str, Config]) -> tuple[str, dict]:
    label, cfg = job
    rep = run_portfolio(_SERIES, cfg, _SINCE_TS, _UNTIL_TS)
    t = rep["totals"]
    row = {k: t.get(k) for k in TOTAL_KEYS}
    row["ended_below_start_capital"] = 1.0 if t["ended_below_start_capital"] else 0.0
    row["seed"] = cfg.seed
    row["pessimistic"] = cfg.pessimistic
    row["days_floor_below_50pct"] = _days_floor_below_50pct(rep["equity_curve"])
    row["days_floor_at_zero"] = _days_floor_at_zero(rep["equity_curve"])
    row["recovery_days_from_max_dd"] = _recovery_days_from_max_dd(rep["equity_curve"])
    for wname in ("y2022", "post_2025_10_10"):
        w = rep["windows"].get(wname, {})
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
    """label = '<name>|<pess|opt>|s<seed>' -> {'<name>|<pess|opt>': [row, ...]} — keeps the
    bound in the group key (a prior draft stripped 2 segments instead of 1 here and silently
    merged the pessimistic/optimistic rows of every cell into one list; row['pessimistic'] now
    also records it directly as a second line of defence)."""
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
    p.add_argument("--out", default="docs/cash-floor-release-2026-09-28")
    p.add_argument("--stage", choices=["grid", "sens", "verify"], required=True)
    p.add_argument("--seeds", type=int, default=30)
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--best-a", default=None)
    p.add_argument("--best-b", default=None)
    # --- verify stage only ---
    p.add_argument("--cells", default="", help="comma-separated policy cell names")
    p.add_argument("--seed-start", type=int, default=0)
    p.add_argument("--since", default=SINCE)
    p.add_argument("--until", default=UNTIL)
    p.add_argument("--crash-lag", type=int, default=1)
    p.add_argument("--max-new", type=int, default=MAX_NEW_PER_DAY)
    p.add_argument("--bounds", choices=["pess", "both"], default="pess")
    p.add_argument("--tag", default="verify")
    args = p.parse_args(argv)

    cost = costengine.round_trip_cost_pct()
    buf = costengine.tp_fee_buffer_pct()
    print(f"cost_pct={cost:.3f}%  tp_fee_buffer_pct={buf:.3f}%", file=sys.stderr)
    raw = load(Path(args.db), "1d")
    series = {sym: to_candles(bars) for sym, bars in raw.items()}
    print(f"{len(series)} symbols", file=sys.stderr)

    jobs: list[tuple[str, Config]] = []
    if args.stage != "verify":
        args.since, args.until = SINCE, UNTIL
    if args.stage == "grid":
        for pess in (True, False):
            for seed in range(args.seeds):
                b = "pess" if pess else "opt"
                jobs.append((f"floor20|{b}|s{seed}", make_config(
                    pessimistic=pess, seed=seed, cost=cost, buf=buf, cash_floor_pct=20.0,
                    max_new_per_day=MAX_NEW_PER_DAY, over={})))
                jobs.append((f"floor0|{b}|s{seed}", make_config(
                    pessimistic=pess, seed=seed, cost=cost, buf=buf, cash_floor_pct=0.0,
                    max_new_per_day=MAX_NEW_PER_DAY, over={})))
                for group, over in policy_cells().items():
                    jobs.append((f"{group}|{b}|s{seed}", make_config(
                        pessimistic=pess, seed=seed, cost=cost, buf=buf, cash_floor_pct=20.0,
                        max_new_per_day=MAX_NEW_PER_DAY, over=over)))
        out_name = "results.json"
    elif args.stage == "sens":
        variants = {"floor20": {}, "floor0": {}}
        if args.best_a:
            variants[args.best_a] = policy_cells()[args.best_a]
        if args.best_b:
            variants[args.best_b] = policy_cells()[args.best_b]
        for name, over in variants.items():
            floor = 0.0 if name == "floor0" else 20.0
            for pess in (True, False):
                for seed in range(args.seeds):
                    b = "pess" if pess else "opt"
                    jobs.append((f"{name}~n5|{b}|s{seed}", make_config(
                        pessimistic=pess, seed=seed, cost=cost, buf=buf, cash_floor_pct=floor,
                        max_new_per_day=MAX_NEW_SENS, over=over)))
        out_name = "results_sens.json"
    else:  # verify
        cells = policy_cells()
        variants = {"floor20": {}, "floor0": {}}
        for name in filter(None, args.cells.split(",")):
            variants[name] = cells[name]
        bounds = (True,) if args.bounds == "pess" else (True, False)
        for name, over in variants.items():
            floor = 0.0 if name == "floor0" else 20.0
            over = {**over, "floor_release_crash_lag_bars": args.crash_lag}
            for pess in bounds:
                for seed in range(args.seed_start, args.seed_start + args.seeds):
                    b = "pess" if pess else "opt"
                    jobs.append((f"{name}|{b}|s{seed}", make_config(
                        pessimistic=pess, seed=seed, cost=cost, buf=buf, cash_floor_pct=floor,
                        max_new_per_day=args.max_new, over=over)))
        out_name = f"verify_{args.tag}.json"

    print(f"{len(jobs)} runs, {args.workers} workers", file=sys.stderr)
    t0 = time.time()
    with Pool(args.workers, initializer=_pool_init,
              initargs=(series, _to_ts(args.since), _to_ts(args.until))) as pool:
        rows = _run_batch(pool, jobs, args.stage)
    print(f"all done in {time.time()-t0:,.0f}s", file=sys.stderr)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(), "stage": args.stage,
        "seeds": (list(range(args.seed_start, args.seed_start + args.seeds))
                  if args.stage == "verify" else list(range(args.seeds))),
        "cost_pct": cost, "tp_fee_buffer_pct": buf,
        "fixed": FIXED, "since": args.since, "until": args.until,
        "crash_lag_bars": args.crash_lag if args.stage == "verify" else "engine default",
        "max_new_per_day": args.max_new if args.stage == "verify" else None,
        "crash_drop_pct": CRASH_DROP_PCT, "crash_breadth_pct": CRASH_BREADTH_PCT,
        "groups": _group(rows),
    }
    (out_dir / out_name).write_text(json.dumps(result, default=str), encoding="utf-8")
    print(f"wrote {out_dir / out_name}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
