"""Verification runner: load 1h ONCE (single process), run a list of jobs, checkpoint JSON.

job spec: cell|n|bound|seed|mode   mode: old (24 BARS warmup, emulated) | fix (24 DAYS since listing)
"""
from __future__ import annotations

import gc
import json
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(r"D:\FINDMY")
sys.path.insert(0, str(ROOT))

import scripts.capital_portfolio_study as cps  # noqa: E402
from app import costengine  # noqa: E402
from scripts.capital_hourly_grid import (  # noqa: E402
    CELLS,
    TOTAL_KEYS,
    listing_ts_from_1d,
    make_config,
)
from scripts.ladder_panel_study import to_candles  # noqa: E402
from scripts.liquidity_tier_study import load  # noqa: E402

DB = ROOT / "data/research/market.db"
DAY = 86_400_000

_CAPTURE: dict = {}
_orig_build = cps._build_report


def _capturing_build(cfg, ledger, open_sessions, equity_curve, monthly, closed_log, ladder_cost):
    _CAPTURE["closed_log"] = closed_log
    _CAPTURE["open"] = {s: (st.next_rung, st.deployed_usd, st.entry_ts) for s, st in open_sessions.items()}
    return _orig_build(cfg, ledger, open_sessions, equity_curve, monthly, closed_log, ladder_cost)


cps._build_report = _capturing_build


def main():
    out = Path(sys.argv[1])
    interval = sys.argv[2]
    jobs = sys.argv[3].split(",")
    trace = "--trace" in sys.argv
    t = time.time()
    raw = load(DB, interval)
    series = {s: to_candles(b) for s, b in raw.items()}
    del raw
    gc.collect()
    print(f"loaded {len(series)} in {time.time()-t:.0f}s", flush=True)
    listing_fix = listing_ts_from_1d(DB) if interval != "1d" else None
    listing_old = {s: b[0]["ts"] - 23 * DAY for s, b in series.items() if b} if interval != "1d" else None
    cost = costengine.round_trip_cost_pct()
    buf = costengine.tp_fee_buffer_pct()
    since, until = cps._to_ts("2024-01-01"), cps._to_ts("2026-07-31")
    res = json.loads(out.read_text()) if out.exists() else {}
    for spec in jobs:
        if spec in res:
            continue
        cell, n, bound, seed, mode = spec.split("|")
        cfg = make_config(pessimistic=bound == "pess", seed=int(seed), cost=cost, buf=buf,
                          max_new_per_day=int(n), over=CELLS[cell])
        lst = listing_fix if mode == "fix" else listing_old
        t0 = time.time()
        rep = cps.run_portfolio(series, cfg, since, until, listing_ts=lst)
        tt = rep["totals"]
        row = {k: tt.get(k) for k in TOTAL_KEYS}
        row["ended_below_start_capital"] = 1.0 if tt["ended_below_start_capital"] else 0.0
        row["seed"] = int(seed)
        for y in ("2024", "2025", "2026"):
            yr = next((r for r in rep["yearly"] if r["year"] == y), {})
            row[f"y{y}.return_pct"] = yr.get("return_pct_on_start_capital")
        if trace:
            cl = _CAPTURE["closed_log"]
            by = defaultdict(lambda: [0, 0.0, 0.0])
            for c in cl:
                k = (c["year"], c["reason"])
                by[k][0] += 1
                by[k][1] += c["pnl_usd"]
                by[k][2] += c["deployed_usd"]
            row["by_year_reason"] = {f"{a}|{b}": [v[0], round(v[1], 2), round(v[2], 2)] for (a, b), v in sorted(by.items())}
            wf = Counter((c["year"], c["waves_filled"]) for c in cl)
            row["waves_by_year"] = {f"{a}|{b}": v for (a, b), v in sorted(wf.items())}
            dl = [c for c in cl if c["reason"] == "deadline"]
            row["deadline_waves"] = dict(Counter(c["waves_filled"] for c in dl))
            row["deadline_pnl_pct_mean"] = round(sum(c["pnl_pct"] for c in dl) / len(dl), 3) if dl else None
            row["deadline_deployed_mean"] = round(sum(c["deployed_usd"] for c in dl) / len(dl), 2) if dl else None
            row["monthly"] = [{k: m[k] for k in ("month", "opened", "closed", "wins", "losses", "realized_usd",
                                                  "equity_end", "open_at_end", "deployed_end", "rungs_starved")}
                              for m in rep["monthly"]]
            eq = rep["equity_curve"]
            row["eq_sample"] = [r for r in eq if r["date"][8:] in ("01", "15")]
            fresh = [c for c in cl if lst is not None]
            row["n_closed"] = len(cl)
            row["delisted"] = [(c["pnl_usd"], c["waves_filled"]) for c in cl if c["reason"] == "delisted"]
        row["secs"] = round(time.time() - t0, 1)
        res[spec] = row
        out.write_text(json.dumps(res, indent=1))
        print(spec, row["cagr_own_pct"], row["max_drawdown_unit_nav_pct"], row["secs"], flush=True)


if __name__ == "__main__":
    main()
