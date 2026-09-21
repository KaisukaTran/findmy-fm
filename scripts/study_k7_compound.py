"""Fixed $28 wave vs wave = % of equity (what the app now does with capital_scale on)."""
import json
import statistics as st
import sys
from multiprocessing import Pool
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent; sys.path.insert(0, str(ROOT))
from scripts.capital_portfolio_study import Config, _init_worker, _to_ts, run_portfolio
from scripts.ladder_panel_study import to_candles
from scripts.liquidity_tier_study import load

# (label, step, fixed_wave, wave_pct, max_sessions)
GRID = [("co dinh $28, tran 80", 7.0, 28.0, 0.0, 80),
        ("co dinh $28, tran 20", 7.0, 28.0, 0.0, 20),
        ("0,4% von, tran 80",    7.0, 28.0, 0.4, 80),
        ("0,4% von, tran 40",    7.0, 28.0, 0.4, 40),
        ("0,4% von, tran 20",    7.0, 28.0, 0.4, 20),
        ("0,4% von, tran 15",    7.0, 28.0, 0.4, 15),
        ("0,3% von, tran 80",    7.0, 21.0, 0.3, 80),
        ("0,6% von, tran 80",    7.0, 42.0, 0.6, 80)]
def _job(a):
    (lab, d, w0, pct, n), seed = a
    cfg = Config(capital=7000.0, gate="reserve", pessimistic=True, seed=seed, max_waves=10,
                 distance_pct=d, wave0_usd=w0, wave0_pct=pct, max_sessions=n)
    t = run_portfolio(_S, cfg, _SI, _UN)["totals"]
    return lab, t["final_equity"], t["max_drawdown_pct"], t["sessions_opened"]
def _init(se, si, un):
    global _S, _SI, _UN; _init_worker(se, si, un); _S, _SI, _UN = se, si, un
if __name__ == "__main__":
    raw = load(ROOT / "data/research/market.db", "1d"); lo = _to_ts("2023-08-01")
    liquid = {s for s, b in raw.items() if len(b) >= 40 and st.median([r[5] for r in b if r[1] >= lo] or [0]) >= 1_000_000}
    series = {s: to_candles(b) for s, b in raw.items() if s in liquid}
    jobs = [(g, sd) for g in GRID for sd in range(1, 21)]
    with Pool(8, initializer=_init, initargs=(series, lo, _to_ts("2026-07-31"))) as pool:
        out = list(pool.imap_unordered(_job, jobs, chunksize=2))
    rows = []
    print(f"{'cau hinh (buoc 7%, 10 rung)':<26}{'trung vi':>10}{'p10':>9}{'te nhat':>9}{'mat von':>9}{'DD':>6}{'phien':>8}")
    for g in GRID:
        rs = [r for r in out if r[0] == g[0]]; fin = sorted(r[1] for r in rs)
        row = dict(label=g[0], median=round(st.median(fin)), p10=round(fin[2]), worst=round(fin[0]),
                   ruin=round(100*sum(1 for f in fin if f < 7000)/len(fin)),
                   dd=round(st.median(r[2] for r in rs), 1), opened=round(st.median(r[3] for r in rs)))
        rows.append(row)
        print(f"{g[0]:<26}{row['median']:>10,}{row['p10']:>9,}{row['worst']:>9,}{row['ruin']:>8}%{row['dd']:>5.0f}%{row['opened']:>8,}")
    Path(ROOT / "docs/k7-compound-2026-09-21.json").write_text(json.dumps(rows, indent=1), encoding="utf-8")
