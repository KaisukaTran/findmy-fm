"""Monte Carlo over the seeded coin-draw: turn a single lucky path into a distribution.

The audit found the engine's bookkeeping sound but every single-seed number noise-dominated
(seed spread 3-4x larger than the capital effect it was meant to measure). So: run N seeds and
report the distribution — median, p10/p90, and the share of paths that end below the money put in.
"""
import json
import statistics as st
import sys
import time
from multiprocessing import Pool
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from scripts.capital_portfolio_study import (  # noqa: E402
    Config,
    _init_worker,
    _run_job,
    _to_ts,
)
from scripts.ladder_panel_study import to_candles  # noqa: E402
from scripts.liquidity_tier_study import load  # noqa: E402

SEEDS = list(range(1, 41))
CAPITALS = [5000.0, 7000.0]
GATES = ["reserve", "cashflow"]
BOUNDS = [True, False]          # pessimistic, optimistic
SINCE, UNTIL = "2023-08-01", "2026-07-31"

def pct(vals, q):
    s = sorted(vals)
    if not s:
        return float("nan")
    i = q * (len(s) - 1)
    lo, hi = int(i), min(int(i) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (i - lo)

if __name__ == "__main__":
    t0 = time.time()
    raw = load(ROOT / "data/research/market.db", "1d")
    series = {s: to_candles(b) for s, b in raw.items() if len(b) >= 40}
    print(f"loaded {len(series)} symbols in {time.time()-t0:.0f}s", file=sys.stderr)
    jobs = [Config(capital=c, gate=g, pessimistic=p, seed=sd)
            for c in CAPITALS for g in GATES for p in BOUNDS for sd in SEEDS]
    print(f"{len(jobs)} runs...", file=sys.stderr)
    since_ts, until_ts = _to_ts(SINCE), _to_ts(UNTIL)
    out = {}
    t1 = time.time()
    with Pool(8, initializer=_init_worker, initargs=(series, since_ts, until_ts)) as pool:
        for i, (label, rep) in enumerate(pool.imap_unordered(_run_job, jobs, chunksize=2)):
            key = label.rsplit("_", 0)[0]
            out.setdefault(key, []).append(rep)
            if i % 40 == 0:
                print(f"  {i}/{len(jobs)}  {time.time()-t1:.0f}s", file=sys.stderr)
    # regroup: label already contains capital/gate/bound but not seed, so collect by that label
    agg = {}
    for label, reps in out.items():
        fin = [r["totals"]["final_equity"] for r in reps]
        real = [r["totals"]["realized_usd"] for r in reps]
        dd = [r["totals"]["max_drawdown_pct"] for r in reps]
        cap = reps[0]["config"]["capital"]
        years = sorted({y["year"] for r in reps for y in r["yearly"]})
        by_year = {}
        for y in years:
            v = [yy["realized_usd"] for r in reps for yy in r["yearly"] if yy["year"] == y]
            o = [yy["opened"] for r in reps for yy in r["yearly"] if yy["year"] == y]
            w = [yy["win_rate_pct"] for r in reps for yy in r["yearly"] if yy["year"] == y and yy["closed"]]
            by_year[y] = {"realized_p10": round(pct(v,.10),2), "realized_med": round(st.median(v),2),
                          "realized_p90": round(pct(v,.90),2), "opened_med": round(st.median(o),1),
                          "win_rate_med": round(st.median(w),2) if w else None}
        # Every month in the WINDOW, not only months that saw activity: a seed whose account
        # ran out of cash simply stops emitting rows, and dropping it from the median would
        # quietly delete the failures from the chart.
        months = []
        y, mo = 2023, 8
        while (y, mo) <= (2026, 7):
            months.append(f"{y:04d}-{mo:02d}")
            mo += 1
            if mo == 13:
                y, mo = y + 1, 1
        ZERO = {"opened": 0, "closed": 0, "realized_usd": 0.0, "equity_end": None,
                "win_rate_pct": None}
        by_month = {}
        for mth in months:
            rows = []
            for r in reps:
                hit = next((m for m in r["monthly"] if m["month"] == mth), None)
                rows.append(hit if hit else dict(ZERO, month=mth))
            wr = [m["win_rate_pct"] for m in rows if m["closed"]]  # only months that closed something
            by_month[mth] = {
                "opened_med": round(st.median([m["opened"] for m in rows]),1),
                "closed_med": round(st.median([m["closed"] for m in rows]),1),
                "seeds_idle": sum(1 for m in rows if not m["closed"]),
                "win_rate_med": round(st.median(wr),2) if wr else None,
                "win_rate_p10": round(pct(wr,.10),2) if wr else None,
                "realized_med": round(st.median([m["realized_usd"] for m in rows]),2),
                "realized_p10": round(pct([m["realized_usd"] for m in rows],.10),2),
                "realized_p90": round(pct([m["realized_usd"] for m in rows],.90),2),
                "equity_med": None,  # biased by construction — use `equity_band` (all seeds, daily)
            }
        # daily equity band across seeds
        dates = [p["date"] for p in reps[0]["equity_curve"]]
        band = []
        for i, d in enumerate(dates):
            es = [r["equity_curve"][i]["equity"] for r in reps]
            band.append({"date": d, "p10": round(pct(es,.10),1), "med": round(st.median(es),1),
                         "p90": round(pct(es,.90),1)})
        agg[label] = {
            "n_seeds": len(reps), "capital": cap,
            "final_equity": {"min": round(min(fin),0), "p10": round(pct(fin,.10),0),
                             "median": round(st.median(fin),0), "p90": round(pct(fin,.90),0),
                             "max": round(max(fin),0)},
            "realized": {"min": round(min(real),0), "median": round(st.median(real),0),
                         "max": round(max(real),0)},
            "max_drawdown_pct": {"median": round(st.median(dd),1), "worst": round(max(dd),1)},
            "paths_below_start_pct": round(100*sum(1 for f in fin if f < cap)/len(fin),1),
            "paths_halved_pct": round(100*sum(1 for f in fin if f < cap*0.5)/len(fin),1),
            "paths_10x_pct": round(100*sum(1 for f in fin if f >= cap*10)/len(fin),1),
            "final_equity_all": [round(f,0) for f in sorted(fin)],
            "yearly": by_year, "monthly": by_month, "equity_band": band,
        }
    out_dir = ROOT / "data/research/studies"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "capital-montecarlo-2026-09-20.json").write_text(
        json.dumps(agg, indent=1), encoding="utf-8")
    print(f"\nDONE in {time.time()-t0:.0f}s\n", file=sys.stderr)
    for k in sorted(agg):
        a = agg[k]
        print(f"{k:34s} n={a['n_seeds']}  final: min={a['final_equity']['min']:>9,.0f} "
              f"p10={a['final_equity']['p10']:>9,.0f} MED={a['final_equity']['median']:>9,.0f} "
              f"p90={a['final_equity']['p90']:>9,.0f} max={a['final_equity']['max']:>10,.0f}  "
              f"DD_med={a['max_drawdown_pct']['median']:5.1f}%  "
              f"lo_start={a['paths_below_start_pct']:5.1f}%  halved={a['paths_halved_pct']:5.1f}%")
