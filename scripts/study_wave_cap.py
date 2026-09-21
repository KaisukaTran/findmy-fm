"""0.4%-of-equity first wave with a dollar cap: past the cap, profit opens new sessions instead
of growing old ones. $7,000 start, 10 rungs @7%, reserve gate, pessimistic bound, 20 seeds."""
import statistics as st
import sys
from multiprocessing import Pool
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from scripts.capital_portfolio_study import Config, _init_worker, _to_ts, run_portfolio  # noqa: E402
from scripts.ladder_panel_study import to_candles  # noqa: E402
from scripts.liquidity_tier_study import load  # noqa: E402

GRID = [("co dinh $28", 0.0, 0.0), ("0,4% khong tran", 0.4, 0.0), ("0,4% tran $35", 0.4, 35.0),
        ("0,4% tran $40", 0.4, 40.0), ("0,4% tran $56", 0.4, 56.0), ("0,4% tran $80", 0.4, 80.0)]


def _job(a):
    (lab, pct, cap), seed = a
    cfg = Config(capital=7000.0, gate="reserve", pessimistic=True, seed=seed, max_waves=10,
                 distance_pct=7.0, wave0_usd=28.0, wave0_pct=pct, wave0_cap=cap)
    r = run_portfolio(_S, cfg, _SI, _UN)
    t = r["totals"]
    peak_open = max(p["open_n"] for p in r["equity_curve"])
    return lab, t["final_equity"], t["max_drawdown_pct"], t["sessions_opened"], peak_open


def _init(se, si, un):
    global _S, _SI, _UN
    _init_worker(se, si, un)
    _S, _SI, _UN = se, si, un


if __name__ == "__main__":
    raw = load(ROOT / "data/research/market.db", "1d")
    lo = _to_ts("2023-08-01")
    liquid = {s for s, b in raw.items()
              if len(b) >= 40 and st.median([r[5] for r in b if r[1] >= lo] or [0]) >= 1_000_000}
    series = {s: to_candles(b) for s, b in raw.items() if s in liquid}
    with Pool(8, initializer=_init, initargs=(series, lo, _to_ts("2026-07-31"))) as pool:
        out = list(pool.imap_unordered(_job, [(g, sd) for g in GRID for sd in range(1, 21)], chunksize=2))
    print(f"{'cau hinh':<18}{'trung vi':>10}{'p10':>9}{'te nhat':>9}{'mat von':>9}{'DD':>6}{'phien':>8}{'dong thoi max':>15}")
    for lab, *_ in GRID:
        rs = [r for r in out if r[0] == lab]
        fin = sorted(r[1] for r in rs)
        print(f"{lab:<18}{st.median(fin):>10,.0f}{fin[2]:>9,.0f}{fin[0]:>9,.0f}"
              f"{100*sum(1 for f in fin if f < 7000)/len(fin):>8.0f}%{st.median(r[2] for r in rs):>5.0f}%"
              f"{st.median(r[3] for r in rs):>8,.0f}{st.median(r[4] for r in rs):>15,.0f}")
