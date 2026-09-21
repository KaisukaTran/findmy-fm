"""What does the market-wide crash brake actually cost, and what does it save?

The owner proposed halting rung buys when 30% of sessions pass 75% of their rungs. Measurement
(2026-09-21) showed that threshold fires once in three years and only AFTER the money is spent,
because a real crash fills the deep rungs inside a single candle. This prices the alternatives:
each brake setting is run against the SAME seeds as the unbraked book, at three capital levels,
so the comparison is like-for-like rather than two different random draws.

Reported per setting: the median outcome, the share of paths that end below the money put in,
the worst path, and how often the brake fired -- the cost and the insurance premium side by side.
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
    _to_ts,
    run_portfolio,
)
from scripts.ladder_panel_study import to_candles  # noqa: E402
from scripts.liquidity_tier_study import load  # noqa: E402

# name -> (warn_depth_frac, warn_breadth_pct, halt_depth_frac, halt_breadth_pct)
# Depths are fractions of max_waves (30), so rung 4 = 0.1333, rung 5 = 0.1667, rung 22 = 0.7333.
BRAKES = {
    "off":            (0.0,    0.0,  0.0,    0.0),
    "A_rung4_5":      (4 / 30, 30.0, 5 / 30, 40.0),   # my recommendation
    "B_rung3_4":      (3 / 30, 30.0, 4 / 30, 50.0),   # earlier, blunter
    "C_rung6_8":      (6 / 30, 30.0, 8 / 30, 30.0),   # looser, fires less
    "D_owner_15_22":  (0.50,   80.0, 0.75,   30.0),   # the owner's original numbers
}


def pct(vals, q):
    s = sorted(vals)
    i = q * (len(s) - 1)
    lo, hi = int(i), min(int(i) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (i - lo)


def _job(args):
    cap, brake, seed = args
    w_d, w_b, h_d, h_b = BRAKES[brake]
    cfg = Config(capital=cap, gate="reserve", pessimistic=True, seed=seed,
                 brake_warn_depth_frac=w_d, brake_warn_breadth_pct=w_b,
                 brake_halt_depth_frac=h_d, brake_halt_breadth_pct=h_b,
                 brake_resume_days=3.0)
    r = run_portfolio(_S, cfg, _SINCE, _UNTIL)
    t = r["totals"]
    return (cap, brake, seed, t["final_equity"], t["max_drawdown_pct"], t["realized_usd"],
            t["sessions_opened"], t.get("brake_episodes",0), t.get("brake_rungs_blocked",0))


def _init(series, since, until):
    global _S, _SINCE, _UNTIL
    _init_worker(series, since, until)
    _S, _SINCE, _UNTIL = series, since, until


if __name__ == "__main__":
    interval = sys.argv[1] if len(sys.argv) > 1 else "1d"
    n_seeds = int(sys.argv[2]) if len(sys.argv) > 2 else 40
    since, until = ("2023-08-01", "2026-07-31") if interval == "1d" else ("2024-01-01", "2026-07-31")
    t0 = time.time()
    raw = load(ROOT / "data/research/market.db", interval)
    series = {s: to_candles(b) for s, b in raw.items() if len(b) >= 40}
    print(f"{interval}: {len(series)} symbols, {since}..{until}", file=sys.stderr)

    caps = [5000.0, 7000.0, 200000.0]
    jobs = [(c, b, sd) for c in caps for b in BRAKES for sd in range(1, n_seeds + 1)]
    print(f"{len(jobs)} runs", file=sys.stderr)
    out = []
    with Pool(8, initializer=_init, initargs=(series, _to_ts(since), _to_ts(until))) as pool:
        for i, res in enumerate(pool.imap_unordered(_job, jobs, chunksize=2)):
            out.append(res)
            if i % 100 == 0:
                print(f"  {i}/{len(jobs)} {time.time()-t0:.0f}s", file=sys.stderr)

    agg = {}
    for cap in caps:
        for brake in BRAKES:
            rows = [r for r in out if r[0] == cap and r[1] == brake]
            fin = [r[3] for r in rows]
            agg[f"{cap:g}_{brake}"] = {
                "capital": cap, "brake": brake, "n": len(rows),
                "median": round(st.median(fin)), "p10": round(pct(fin, .10)),
                "min": round(min(fin)), "max": round(max(fin)),
                "dd_med": round(st.median(r[4] for r in rows), 1),
                "below_start_pct": round(100 * sum(1 for f in fin if f < cap) / len(fin), 1),
                "opened_med": round(st.median(r[6] for r in rows)),
                "episodes_med": round(st.median(r[7] for r in rows), 1),
                "rungs_blocked_med": round(st.median(r[8] for r in rows), 1),
            }
    Path(ROOT / f"docs/brake-study-{interval}-2026-09-21.json").write_text(
        json.dumps({"agg": agg, "raw": [list(r) for r in out]}, indent=1), encoding="utf-8")

    print(f"\n=== {interval} | {n_seeds} seeds/cell | gate=reserve, pessimistic bound ===")
    for cap in caps:
        base = agg[f"{cap:g}_off"]
        print(f"\nVON ${cap:,.0f}   (khong phanh: trung vi ${base['median']:,}, "
              f"duoi von {base['below_start_pct']}%, DD {base['dd_med']}%)")
        print(f"  {'phanh':<16}{'trung vi':>11}{'vs off':>9}{'p10':>10}{'te nhat':>10}"
              f"{'duoi von':>10}{'DD':>7}{'phien mo':>10}{'lan phanh':>11}")
        for brake in BRAKES:
            a = agg[f"{cap:g}_{brake}"]
            delta = 100 * (a["median"] / base["median"] - 1) if base["median"] else 0
            print(f"  {brake:<16}{a['median']:>11,}{delta:>8.0f}%{a['p10']:>10,}"
                  f"{a['min']:>10,}{a['below_start_pct']:>9.1f}%{a['dd_med']:>6.0f}%"
                  f"{a['opened_med']:>10,}{a['episodes_med']:>11}")
    print(f"\nDONE {time.time()-t0:.0f}s", file=sys.stderr)
