"""Final pricing of the crash brake, on the universe the live scanner actually trades.

Three earlier attempts and why they were discarded:
  1. The owner's thresholds (30% of sessions past 75% of rungs) -- fires once in three years,
     and one hour AFTER the deep rungs already filled, because a crash fills them in one candle.
  2. Session-breadth at a shallower depth -- structurally impossible at small capital: at $5,000
     the book holds a MEDIAN OF 2 open sessions, so any "% of sessions" rule is triggered by one
     unlucky coin. Measured: 14 sessions opened in three years against 394 unbraked.
  3. Universe breadth against a 24-bar high -- most coins sit below their 24-day high most of the
     time, so the brake was engaged 61-98% of the time.

This version measures a FAST fall (one bar, versus the previous bar's high) across the LIQUID
universe only (median quote volume >= $1M/day, the scanner's own floor), which is what the live
`min_quote_volume` gate already selects. Calibration: -20%/70% breadth fires on 16 days in three
years, about five alarms a year.
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
    universe_breadth,
)
from scripts.ladder_panel_study import to_candles  # noqa: E402
from scripts.liquidity_tier_study import load  # noqa: E402

SINCE, UNTIL = "2023-08-01", "2026-07-31"
# (drop_pct, breadth_pct) — 0,0 is the unbraked baseline
SETTINGS = [(0.0, 0.0), (20.0, 70.0), (20.0, 50.0), (15.0, 70.0), (12.0, 70.0)]
CAPS = [5000.0, 7000.0, 200000.0]
SEEDS = range(1, 21)


def _job(a):
    cap, drop, wide, seed = a
    kw = {} if drop == 0 else dict(brake_universe_drop_pct=drop, brake_universe_breadth_pct=wide,
                                   brake_universe_lookback=1, brake_resume_days=3.0,
                                   brake_suspend_deadline=True)
    cfg = Config(capital=cap, gate="reserve", pessimistic=True, seed=seed, **kw)
    r = run_portfolio(_S, cfg, _SI, _UN, breadth=_B.get(drop))
    t = r["totals"]
    return (cap, drop, wide, t["final_equity"], t["sessions_opened"], t["max_drawdown_pct"],
            t.get("brake_episodes", 0), t.get("brake_bars_halted", 0), t["realized_usd"])


def _init(se, si, un, b):
    global _S, _SI, _UN, _B
    _init_worker(se, si, un)
    _S, _SI, _UN, _B = se, si, un, b


if __name__ == "__main__":
    t0 = time.time()
    raw = load(ROOT / "data/research/market.db", "1d")
    lo_ts = _to_ts(SINCE)
    liquid = {s for s, b in raw.items()
              if len(b) >= 40 and st.median([r[5] for r in b if r[1] >= lo_ts] or [0]) >= 1_000_000}
    series = {s: to_candles(b) for s, b in raw.items() if s in liquid}
    print(f"universe: {len(series)} liquid symbols", file=sys.stderr)

    B = {0.0: None}
    for d in {s[0] for s in SETTINGS if s[0]}:
        B[d] = universe_breadth(series, d, 1)
    jobs = [(c, d, w, sd) for c in CAPS for d, w in SETTINGS for sd in SEEDS]
    with Pool(8, initializer=_init, initargs=(series, lo_ts, _to_ts(UNTIL), B)) as pool:
        out = list(pool.imap_unordered(_job, jobs, chunksize=2))

    res = {}
    for cap in CAPS:
        base = [r for r in out if r[0] == cap and r[1] == 0.0]
        bmed = st.median(r[3] for r in base)
        bbelow = 100 * sum(1 for r in base if r[3] < cap) / len(base)
        print(f"\n=== VON ${cap:,.0f} | khong phanh: trung vi ${bmed:,.0f}, "
              f"duoi von {bbelow:.0f}%, {st.median(r[4] for r in base):,.0f} phien, "
              f"DD {st.median(r[5] for r in base):.0f}% ===")
        print(f"  {'phanh':<16}{'trung vi':>11}{'vs off':>8}{'te nhat':>10}{'duoi von':>10}"
              f"{'phien mo':>10}{'DD':>6}{'lan':>6}{'%tg dung':>10}")
        for d, w in SETTINGS:
            rs = [r for r in out if r[0] == cap and r[1] == d and r[2] == w]
            med = st.median(r[3] for r in rs)
            below = 100 * sum(1 for r in rs if r[3] < cap) / len(rs)
            name = "off" if d == 0 else f"-{d:g}% / {w:g}%"
            res[f"{cap:g}_{name}"] = {"median": med, "below": below,
                                      "worst": min(r[3] for r in rs),
                                      "opened": st.median(r[4] for r in rs),
                                      "dd": st.median(r[5] for r in rs)}
            print(f"  {name:<16}{med:>11,.0f}{100*(med/bmed-1):>7.0f}%"
                  f"{min(r[3] for r in rs):>10,.0f}{below:>9.0f}%"
                  f"{st.median(r[4] for r in rs):>10,.0f}{st.median(r[5] for r in rs):>5.0f}%"
                  f"{st.median(r[6] for r in rs):>6.0f}{100*st.median(r[7] for r in rs)/1095:>9.1f}%")
    Path(ROOT / "docs/brake-universe-2026-09-21.json").write_text(
        json.dumps({"settings": [list(s) for s in SETTINGS], "result": res,
                    "raw": [list(r) for r in out]}, indent=1), encoding="utf-8")
    print(f"\n{time.time()-t0:.0f}s", file=sys.stderr)
