"""$7,000 account, owner's new shape: 10 rungs. Which spacing, which wave, how many sessions?"""
import json
import statistics as st
import sys
import time
from multiprocessing import Pool
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent; sys.path.insert(0, str(ROOT))
from scripts.capital_portfolio_study import (
    Config,
    _init_worker,
    _to_ts,
    full_ladder_cost,
    run_portfolio,
)
from scripts.ladder_panel_study import to_candles
from scripts.liquidity_tier_study import load

STAGE = sys.argv[1]
if STAGE == "shape":
    GRID = [(d, w0, 80) for d in (4.0, 5.0, 6.0, 7.0) for w0 in (10.0, 20.0, 28.0, 40.0)]
    GRID.append(("base", 28.0, 80))          # today's 30 rungs @4% as reference
else:
    d, w0 = float(sys.argv[2]), float(sys.argv[3])
    GRID = [(d, w0, n) for n in (3, 5, 8, 10, 15, 20, 80)]

def _job(a):
    (d, w0, n), seed = a
    waves, dist = (30, 4.0) if d == "base" else (10, d)
    cfg = Config(capital=7000.0, gate="reserve", pessimistic=True, seed=seed,
                 wave0_usd=w0, max_waves=waves, distance_pct=dist, max_sessions=n)
    t = run_portfolio(_S, cfg, _SI, _UN)["totals"]
    return (d, w0, n), t["final_equity"], t["max_drawdown_pct"], t["sessions_opened"], t["realized_usd"]

def _init(se, si, un):
    global _S, _SI, _UN; _init_worker(se, si, un); _S, _SI, _UN = se, si, un

if __name__ == "__main__":
    t0 = time.time()
    raw = load(ROOT / "data/research/market.db", "1d"); lo = _to_ts("2023-08-01")
    liquid = {s for s, b in raw.items() if len(b) >= 40 and st.median([r[5] for r in b if r[1] >= lo] or [0]) >= 1_000_000}
    series = {s: to_candles(b) for s, b in raw.items() if s in liquid}
    jobs = [(g, sd) for g in GRID for sd in range(1, 21)]
    with Pool(8, initializer=_init, initargs=(series, lo, _to_ts("2026-07-31"))) as pool:
        out = list(pool.imap_unordered(_job, jobs, chunksize=2))
    rows = []
    for g in GRID:
        rs = [r for r in out if r[0] == g]
        fin = [r[1] for r in rs]
        d, w0, n = g
        rows.append(dict(step=d, wave=w0, sessions=n, median=round(st.median(fin)), worst=round(min(fin)),
                         p10=round(sorted(fin)[2]), ruin=round(100*sum(1 for f in fin if f < 7000)/len(fin)),
                         dd=round(st.median(r[2] for r in rs), 1), opened=round(st.median(r[3] for r in rs))))
    Path(ROOT / f"docs/k7-{STAGE}-2026-09-21.json").write_text(json.dumps(rows, indent=1), encoding="utf-8")
    print(f"{'buoc':>6}{'wave':>6}{'tran phien':>11}{'thang':>9}{'trung vi':>10}{'p10':>9}{'te nhat':>9}{'mat von':>9}{'DD':>6}{'phien mo':>10}")
    for r in sorted(rows, key=lambda r: -r["median"]):
        cost = full_ladder_cost(4.0, 30, r["wave"]) if r["step"] == "base" else full_ladder_cost(r["step"], 10, r["wave"])
        lab = "30r@4%" if r["step"] == "base" else f"{r['step']:g}%"
        print(f"{lab:>6}{r['wave']:>6.0f}{r['sessions']:>11}{cost:>8,.0f}${r['median']:>9,}{r['p10']:>9,}{r['worst']:>9,}{r['ruin']:>8}%{r['dd']:>5.0f}%{r['opened']:>10,}")
    print(f"\n{time.time()-t0:.0f}s", file=sys.stderr)
