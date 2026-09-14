"""What stop, ladder depth and patience keep 80% of the money on the winning side?

The owner's thought experiment (2026-09-13): no stop-loss at all, unlimited capital, the
current entries, rungs 4% apart with no ceiling. First measure THAT — how deep the ladder
really goes, how long it waits, what it ties up — then find the smallest stop / rung count /
timeout for which the dollars won are at least 80% of all dollars that changed hands
(dollar win share = Σ$wins / (Σ$wins + Σ|$losses|)). Same simulator and panel as
scripts/ladder_depth_study.py; the trial generator and summariser are reused from it.

    python scripts/ladder_grid_study.py [--interval 1d] [--symbols 200] [--every 7] [--out docs/ladder-grid-2026-09-13]
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from multiprocessing import Pool
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import costengine  # noqa: E402
from app.kss.service import ladder_cost_for  # noqa: E402
from scripts.ladder_depth_study import HOURS_PER_YEAR, _one_symbol, _pct, summarise  # noqa: E402
from scripts.liquidity_tier_study import load  # noqa: E402

DISTANCE, TP, STEP = 4.0, 5.0, 0.5
WAVES = [5, 8, 10, 15, 20, 30, 60]
STOPS = [0.0, 20.0, 35.0, 50.0, 70.0]
DEADLINES = [7.0, 14.0, 30.0, 60.0, 90.0, 180.0, 365.0, 3650.0]


def run_cfg(pool, series, chosen, cfg, pessimistic, every, wave0, cost, allowed=None):
    jobs = [(s, series[s], "grid", cfg, pessimistic, every, wave0, cost,
             None if allowed is None else allowed.get(s, set())) for s in chosen]
    return [r for chunk in pool.imap_unordered(_one_symbol, jobs, chunksize=2) for r in chunk]


def top_n_membership(series: dict, n: int, bars_per_day: float) -> dict[str, set[int]]:
    """Point-in-time liquidity rank: for every bar, which symbols are in the top *n* by the
    TRAILING 30-day median quote volume (the window ends on the bar itself — nothing from the
    future). Returns symbol -> set of bar INDICES on which that symbol may enter. Ranking on a
    trailing figure rather than a whole-panel average keeps a coin that only became big in
    2025 out of the 2022 top list, and a coin that died in 2023 in the list while it was alive."""
    import pandas as pd

    win = int(round(30 * bars_per_day))
    frames = []
    for sym, bars in series.items():
        ts = [b[1] for b in bars]
        qv = pd.Series([b[5] or 0.0 for b in bars], index=ts, dtype="float64")
        med = qv.rolling(win, min_periods=win).median()
        frames.append(pd.DataFrame({"sym": sym, "ts": ts, "idx": range(len(bars)), "med": med.values}))
    df = pd.concat(frames, ignore_index=True).dropna(subset=["med"])
    df = df[df["med"] > 0]
    df["rank"] = df.groupby("ts")["med"].rank(ascending=False, method="first")
    top = df[df["rank"] <= n]
    return {sym: set(g["idx"].astype(int).tolist()) for sym, g in top.groupby("sym")}


def dollar_share(s: dict) -> float:
    w, loss = s.get("sum_wins_usd", 0.0), -s.get("sum_losses_usd", 0.0)
    return w / (w + loss) if (w + loss) > 0 else float("nan")


def mtm_share(s: dict) -> float:
    """Dollar win share with the ladders still OPEN at the end of the data counted as losses at
    their mark — without this a configuration with no stop and no clock scores 100% because its
    losses never realise; they just sit there."""
    w, loss = s.get("sum_wins_usd", 0.0), -s.get("sum_losses_usd", 0.0)
    o = max(0.0, -s.get("open_sum_usd", 0.0))
    return w / (w + loss + o) if (w + loss + o) > 0 else float("nan")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--db", default="data/research/market.db")
    p.add_argument("--interval", default="1d")
    p.add_argument("--symbols", type=int, default=200)
    p.add_argument("--every", type=int, default=7)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--wave0", type=float, default=75.0)
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--out", default="docs/ladder-grid-2026-09-13")
    p.add_argument("--only", default="", help="comma list of waves:sl:deadline to run instead of the full grid")
    p.add_argument("--skip-infinite", action="store_true")
    p.add_argument("--top", type=int, default=0, help="only enter a coin on bars where it ranks in the top N by trailing 30d median quote volume (point-in-time)")
    p.add_argument("--min-years", type=float, default=2.0, help="minimum history a coin needs to be in the panel")
    args = p.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    cost = costengine.round_trip_cost_pct()
    series = load(Path(args.db), args.interval)
    bars_per_year = HOURS_PER_YEAR if args.interval == "1h" else 365
    eligible = sorted(s for s, b in series.items() if len(b) >= args.min_years * bars_per_year)
    chosen = sorted(random.Random(args.seed).sample(eligible, min(args.symbols, len(eligible))))
    allowed = None
    if args.top:
        # Rank against EVERY coin in the dataset, not just the sampled panel, then enter only the
        # sampled coins on the bars where they made the cut.
        allowed = top_n_membership(series, args.top, bars_per_year / 365.0)
        chosen = [s for s in chosen if allowed.get(s)]
        n_entries = sum(len(v) for v in allowed.values())
        print(f"top-{args.top} by trailing 30d median quote volume, point-in-time: {len(allowed)} coins ever "
              f"qualify, {n_entries:,} symbol-bars; {len(chosen)} of the sampled coins qualify at some point")
    print(f"{len(chosen)} coins ({args.interval}), entries every {args.every} bars, wave0 ${args.wave0:g}, "
          f"distance {DISTANCE}%, tp {TP}% +{STEP}/rung, cost {cost:.2f}%\n")
    t0 = time.time()
    out: dict = {"meta": vars(args) | {"cost": cost, "coins": len(chosen)}, "infinite": {}, "grid": []}

    with Pool(args.workers) as pool:
        # --- 1. the thought experiment itself: no stop, no ceiling, no clock -------------------
        for pess in (() if args.skip_infinite else (False, True)):
            rows = run_cfg(pool, series, chosen, (DISTANCE, 60, TP, 0.0, 3650.0, STEP), pess, args.every, args.wave0, cost, allowed)
            scored = [r for r in rows if r["kind"] != "data_end"]
            openr = [r for r in rows if r["kind"] == "data_end"]
            waves = sorted(r["waves"] for r in rows)
            days = sorted(r["days"] for r in scored if r["days"] is not None)
            cap = sorted(r["capital"] for r in rows)
            mae = sorted(r["mae"] for r in rows)
            s = summarise(rows, 60)
            s.update({
                "waves_p50": _pct(waves, 0.5), "waves_p90": _pct(waves, 0.9), "waves_p99": _pct(waves, 0.99), "waves_max": max(waves),
                "days_to_tp_p50": _pct(days, 0.5), "days_to_tp_p90": _pct(days, 0.9), "days_to_tp_p99": _pct(days, 0.99), "days_max": max(days) if days else None,
                "capital_p50": _pct(cap, 0.5), "capital_p90": _pct(cap, 0.9), "capital_p99": _pct(cap, 0.99), "capital_max": max(cap),
                "mae_p50": _pct(mae, 0.5), "mae_p90": _pct(mae, 0.9), "mae_p99": _pct(mae, 0.99), "mae_min": min(mae),
                "open_share_pct": round(100 * len(openr) / len(rows), 2),
                "open_capital_sum": round(sum(r["capital"] for r in openr), 0),
            })
            bound = "PESSIMISTIC" if pess else "OPTIMISTIC"
            out["infinite"][bound] = s
            print(f"INFINITE ({bound})  n={s['n']:,} tp {s['tp_pct']}% | open at data end {len(openr)} ({s['open_share_pct']}%) "
                  f"mean {s['open_mean_pnl_pct']:+.1f}% tying up ${s['open_capital_sum']:,.0f}\n"
                  f"  rungs filled p50/p90/p99/max {s['waves_p50']}/{s['waves_p90']}/{s['waves_p99']}/{s['waves_max']}   "
                  f"days to TP p50/p90/p99/max {s['days_to_tp_p50']}/{s['days_to_tp_p90']}/{s['days_to_tp_p99']}/{s['days_max']}\n"
                  f"  $ deployed p50/p90/p99/max {s['capital_p50']:,.0f}/{s['capital_p90']:,.0f}/{s['capital_p99']:,.0f}/{s['capital_max']:,.0f}   "
                  f"MAE p50/p90/p99/min {s['mae_p50']:.1f}/{s['mae_p90']:.1f}/{s['mae_p99']:.1f}/{s['mae_min']:.1f}%\n"
                  f"  $/trial {s['mean_usd']:+.2f}  dollar win share {100*dollar_share(s):.1f}%  %/$-day {s['pct_per_dollar_day']:+.4f}  ({time.time()-t0:.0f}s)\n")

        # --- 2. the grid ------------------------------------------------------------------------
        combos = [(w, sl, dl) for w in WAVES for sl in STOPS for dl in DEADLINES]
        if args.only:
            combos = [tuple(float(x) for x in c.split(":")) for c in args.only.split(",")]
            combos = [(int(w), sl, dl) for w, sl, dl in combos]
        for waves, sl, dl in combos:
            ladder = ladder_cost_for(args.wave0, DISTANCE, waves)
            rec = {"waves": waves, "sl": sl, "deadline": dl, "ladder_usd": round(ladder)}
            for pess in (False, True):
                rows = run_cfg(pool, series, chosen, (DISTANCE, waves, TP, sl, dl, STEP), pess, args.every, args.wave0, cost, allowed)
                s = summarise(rows, waves)
                rec["PESS" if pess else "OPT"] = {
                    "n": s["n"], "share": round(100 * dollar_share(s), 2), "mtm": round(100 * mtm_share(s), 2),
                    "sum_wins_usd": s["sum_wins_usd"], "sum_losses_usd": s["sum_losses_usd"],
                    "tp_pct": s["tp_pct"], "sl_pct": s["sl_pct"],
                    "horizon_pct": s["horizon_pct"], "mean_usd": s["mean_usd"], "worst_usd": s["worst_usd"],
                    "pct_per_dollar_day": s["pct_per_dollar_day"], "open": s["open_at_data_end"],
                    "open_sum_usd": s["open_sum_usd"], "mean_capital_days": s["mean_capital_days"],
                }
            out["grid"].append(rec)
            o, q = rec["OPT"], rec["PESS"]
            print(f"waves {waves:>2} sl {sl:>4.0f} dl {dl:>5.0f}  ladder ${ladder:>7,.0f}  MTM share opt/pess {o['mtm']:5.1f}/{q['mtm']:5.1f}%  "
                  f"$/trial {o['mean_usd']:+7.2f}/{q['mean_usd']:+7.2f}  worst {q['worst_usd']:+8,.0f}  tp {q['tp_pct']:5.1f}% sl {q['sl_pct']:4.1f}% "
                  f"horizon {q['horizon_pct']:4.1f}%  open {q['open']:>4}  %/$-day {q['pct_per_dollar_day']:+.4f}  ({time.time()-t0:.0f}s)")
    Path(args.out).with_suffix(".json").write_text(json.dumps(out, indent=1, default=str), encoding="utf-8")
    print(f"wrote {Path(args.out).with_suffix('.json')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
