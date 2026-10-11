"""Level 2: portfolio runs. Loads 1h once, runs a list of jobs, checkpoints rows to JSON.

job spec: cfgname|anchor|bound|seed      (cfgname in CONFIGS)
    python portfolio.py out.json "paper_cov1|avg|pess|0,paper_cov1|entry|pess|0"
"""
from __future__ import annotations

import gc
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import tp_anchor_engine as eng  # noqa: E402
import scripts.capital_portfolio_study as cps  # noqa: E402
from app import costengine  # noqa: E402
from scripts.capital_hourly_grid import listing_ts_from_1d  # noqa: E402
from scripts.ladder_panel_study import to_candles  # noqa: E402
from scripts.liquidity_tier_study import load  # noqa: E402

DB = Path(r"D:\FINDMY\data\research\market.db")

# Effective paper config (runtime_config of data/findmy.db, 2026-10-11) -> see report.md section 1.
BASE = dict(
    capital=7000.0, gate="reserve", distance_pct=7.0, max_waves=10, tp_pct=5.0, tp_step_pct=0.5,
    sl_pct=0.0, deadline_days=60.0, trail_after_tp_pct=0.0, wave0_pct=0.4, wave0_cap=40.0,
    wave0_floor=10.0, wave0_usd=28.0, warmup=24, deep_lock_rungs=4, partial_last_rung=True,
    backstop=False, cash_floor_pct=0.0, max_new_per_day=40,
)
CONFIGS = {
    # current paper: coverage 1, floor 0, backup 10, <=150 sessions (runtime_config)
    "paper_cov1": dict(coverage_pct=1.0, equity_backup_pct=10.0, max_sessions=150),
    # the 2026-09-29 recommendation (cov30), same everything else
    "paper_cov30": dict(coverage_pct=30.0, equity_backup_pct=10.0, max_sessions=150),
    # candidate-starved variant: 5 new sessions / day
    "paper_cov1_n5": dict(coverage_pct=1.0, equity_backup_pct=10.0, max_sessions=150, max_new_per_day=5),
    "paper_cov30_n5": dict(coverage_pct=30.0, equity_backup_pct=10.0, max_sessions=150, max_new_per_day=5),
    # exact 2026-09-29 study config (cov1 floor0 cell): backup 24.8, 80 sessions -> sanity vs report.md
    "rep_cov1_floor0": dict(coverage_pct=1.0, equity_backup_pct=24.8, max_sessions=80),
    "rep_cov30_floor0": dict(coverage_pct=30.0, equity_backup_pct=24.8, max_sessions=80),
}

KEYS = [
    "cagr_own_pct", "cagr_pct", "final_nav_own", "final_equity", "realized_usd",
    "max_drawdown_unit_nav_pct", "max_drawdown_pct", "sessions_opened", "sessions_closed",
    "sessions_still_open_at_end", "open_at_end_unrealized_usd", "util_avg_pct", "avg_utilization_pct",
    "rungs_starved", "starved_rungs_distinct", "rungs_partial", "avg_waves_filled",
    "deadline_exits", "deadline_exits_usd", "delisted_exits", "delisted_exits_usd",
    "capital_days", "pct_per_capital_day", "worst_session_usd", "ended_below_start_capital",
]


_CAPTURE: dict = {}
_orig_build = cps._build_report


def _capturing_build(cfg, ledger, open_sessions, equity_curve, monthly, closed_log, ladder_cost):
    _CAPTURE["closed_log"] = closed_log
    _CAPTURE["open"] = [(st.symbol, st.next_rung, st.deployed_usd, st.entry_ts, st.capital_days_acc)
                        for st in open_sessions.values()]
    return _orig_build(cfg, ledger, open_sessions, equity_curve, monthly, closed_log, ladder_cost)


cps._build_report = _capturing_build


def closed_stats(cl):
    out = {}
    for reason in ("tp", "deadline", "delisted"):
        s = [c for c in cl if c["reason"] == reason]
        out[f"n_{reason}"] = len(s)
        out[f"usd_{reason}"] = round(sum(c["pnl_usd"] for c in s), 2)
        out[f"days_{reason}"] = round(sum(c["days"] for c in s) / len(s), 2) if s else None
    out["mean_days_all"] = round(sum(c["days"] for c in cl) / len(cl), 2) if cl else None
    out["mean_waves_closed"] = round(sum(c["waves_filled"] for c in cl) / len(cl), 3) if cl else None
    out["waves_hist"] = {k: sum(1 for c in cl if c["waves_filled"] == k) for k in range(1, 11)}
    return out


def make_cfg(name, anchor, bound, seed, cost, buf):
    kw = dict(BASE)
    kw.update(CONFIGS[name])
    return eng.TPConfig(pessimistic=(bound == "pess"), seed=seed, cost_pct=cost, tp_fee_buffer_pct=buf,
                        tp_anchor=anchor, **kw)


def main():
    out = Path(sys.argv[1])
    jobs = [j for j in sys.argv[2].split(",") if j]
    interval = "1h"
    if len(sys.argv) > 3:
        interval = sys.argv[3]
    t = time.time()
    raw = load(DB, interval)
    series = {s: to_candles(b) for s, b in raw.items()}
    del raw
    gc.collect()
    print(f"loaded {len(series)} syms in {time.time()-t:.0f}s", flush=True)
    listing = listing_ts_from_1d(DB) if interval != "1d" else None
    cost = costengine.round_trip_cost_pct()
    buf = costengine.tp_fee_buffer_pct()
    since, until = cps._to_ts("2024-01-01"), cps._to_ts("2026-07-31")
    eng.install()
    res = json.loads(out.read_text()) if out.exists() else {}
    for spec in jobs:
        if spec in res:
            continue
        name, anchor, bound, seed = spec.split("|")
        cfg = make_cfg(name, anchor, bound, int(seed), cost, buf)
        t0 = time.time()
        rep = cps.run_portfolio(series, cfg, since, until, listing_ts=listing)
        tt = rep["totals"]
        row = {k: tt.get(k) for k in KEYS}
        row["ended_below_start_capital"] = 1.0 if tt["ended_below_start_capital"] else 0.0
        for y in ("2024", "2025", "2026"):
            yr = next((r for r in rep["yearly"] if r["year"] == y), {})
            row[f"y{y}.return_pct"] = yr.get("return_pct_on_start_capital")
            row[f"y{y}.realized_usd"] = yr.get("realized_usd")
            row[f"y{y}.closed"] = yr.get("closed")
        row["opened_by_year"] = {y: sum(m["opened"] for m in rep["monthly"] if m["month"][:4] == y) for y in ("2024", "2025", "2026")}
        row.update(closed_stats(_CAPTURE["closed_log"]))
        op = _CAPTURE["open"]
        row["open_end_n"] = len(op)
        row["open_end_deployed"] = round(sum(o[2] for o in op), 2)
        row["open_end_waves_hist"] = {k: sum(1 for o in op if o[1] == k) for k in range(1, 11)}
        row["open_end_mean_age_days"] = round(sum((until - o[3]) / 86400000 for o in op) / len(op), 1) if op else None
        row["secs"] = round(time.time() - t0, 1)
        res[spec] = row
        out.write_text(json.dumps(res))
        print(f"{spec} {row['secs']}s CAGR {row['cagr_own_pct']} DD {row['max_drawdown_unit_nav_pct']} "
              f"closed {row['sessions_closed']} open_end {row['sessions_still_open_at_end']}", flush=True)


if __name__ == "__main__":
    main()
