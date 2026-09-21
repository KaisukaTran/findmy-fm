import statistics as st
import sys
from multiprocessing import Pool
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent; sys.path.insert(0, str(ROOT))
from scripts.capital_portfolio_study import Config, _init_worker, _to_ts, run_portfolio
from scripts.ladder_panel_study import to_candles
from scripts.liquidity_tier_study import load

GRID = [("30 rung @4% (hien tai)", 30, 4.0), ("10 rung @4%", 10, 4.0), ("10 rung @6%", 10, 6.0), ("10 rung @7%", 10, 7.0)]
def _job(a):
    (lab, w, d), seed = a
    r = run_portfolio(_S, Config(capital=7000.0, gate="reserve", pessimistic=True, seed=seed,
                                 max_waves=w, distance_pct=d, wave0_usd=28.0), _SI, _UN)
    return lab, {y["year"]: y["realized_usd"] for y in r["yearly"]}, {y["year"]: y["losses"] for y in r["yearly"]}
def _init(se, si, un):
    global _S, _SI, _UN; _init_worker(se, si, un); _S, _SI, _UN = se, si, un
if __name__ == "__main__":
    raw = load(ROOT / "data/research/market.db", "1d"); lo = _to_ts("2023-08-01")
    liquid = {s for s, b in raw.items() if len(b) >= 40 and st.median([r[5] for r in b if r[1] >= lo] or [0]) >= 1_000_000}
    series = {s: to_candles(b) for s, b in raw.items() if s in liquid}
    with Pool(8, initializer=_init, initargs=(series, lo, _to_ts("2026-07-31"))) as pool:
        out = list(pool.imap_unordered(_job, [(g, sd) for g in GRID for sd in range(1, 21)], chunksize=2))
    print(f"{'lai thuc hien trung vi / nam':<26}" + "".join(f"{y:>10}" for y in ("2023", "2024", "2025", "2026")))
    for g in GRID:
        rs = [r for r in out if r[0] == g[0]]
        print(f"{g[0]:<26}" + "".join(f"{st.median(r[1].get(y, 0) for r in rs):>10,.0f}" for y in ("2023", "2024", "2025", "2026")))
