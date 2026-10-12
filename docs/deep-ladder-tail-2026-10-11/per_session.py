"""Per-session level (unlimited cash). One session per coin every --spacing-h hours.

    python per_session.py --db D:\\FINDMY\\data\\research\\market.db --spacing-h 168 --out ps_1h_s168.npz
    python per_session.py --db ...market_holdout.db --first-start 2026-08-01 --allow-open --spacing-h 24 ...

Primary: only starts with a full 60-day horizon before the global end (no censoring by the study end).
Holdout (--allow-open): every start; sessions unfinished at data end are marked to market at the last
close and flagged reason=4 (open_end). reason: 0 tp, 1 deadline(60d), 2 data_end (coin's own series ended),
3 deadline_deep (D3), 4 open_end.  D5 is a start-set filter (age >= 180d) -> done in analysis from `age_d`.
"""
from __future__ import annotations

import argparse
import hashlib
import sqlite3
import sys
import time
from multiprocessing import Pool
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import numpy as np  # noqa: E402

import deep_engine as eng  # noqa: E402
import scripts.capital_portfolio_study as cps  # noqa: E402
from app import costengine  # noqa: E402
from scripts.ladder_panel_study import to_candles  # noqa: E402

DAY = 86_400_000
WAVE0 = 28.0
VARS = ["A0", "D1", "D2", "D3", "D4"]
BOUNDS = ["opt", "pess"]
REASON = {"tp": 0, "deadline": 1, "delisted": 2, "deadline_deep": 3}
FIELDS = ("reason", "pnl_usd", "pnl_pct", "days", "waves", "deployed", "capdays")


def make_cfg(variant: str, pess: bool) -> eng.DeepConfig:
    kw = dict(
        capital=7000.0, gate="reserve", pessimistic=pess, distance_pct=7.0, max_waves=10,
        tp_pct=5.0, tp_step_pct=0.5, sl_pct=0.0, deadline_days=60.0, trail_after_tp_pct=0.0,
        cost_pct=costengine.round_trip_cost_pct(), tp_fee_buffer_pct=costengine.tp_fee_buffer_pct(),
        wave0_usd=WAVE0, wave0_pct=0.0, warmup=24, partial_last_rung=True, backstop=False,
    )
    kw.update(eng.VARIANTS[variant])
    return eng.DeepConfig(**kw)


CFGS = {(v, b): make_cfg(v, b == "pess") for v in VARS for b in BOUNDS}


def run_session(candles, start, cfg):
    state = cps.SessionState("X", candles, start, cfg, wave0=WAVE0, fund=0.0)
    ledger = cps.Ledger(1e15)
    for i in range(start + 1, len(candles)):
        r = eng.step_session(state, candles[i], i, cfg, ledger)
        if r["closed"] is not None:
            return r["closed"], False
    return cps._close_delisted(state, cfg), True


def work(args):
    db_path, sym, interval, spacing_h, global_end_ts, listing, first_start, allow_open = args
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    rows = db.execute("SELECT symbol, ts, high, low, close, quote_volume, open, volume FROM candles "
                      "WHERE symbol=? AND interval=? ORDER BY ts", (sym, interval)).fetchall()
    db.close()
    candles = to_candles([tuple(r) for r in rows])
    if not candles:
        return sym, []
    step_ms = spacing_h * 3_600_000
    phase = int(hashlib.md5(sym.encode()).hexdigest()[:8], 16) % spacing_h * 3_600_000
    first_ok = max(listing + 24 * DAY, candles[0]["ts"], first_start)
    last_start = global_end_ts if allow_open else global_end_ts - 60 * DAY
    out = []
    next_ts = first_ok + phase
    coin_ends_early = candles[-1]["ts"] < global_end_ts - 3_600_000 * 2
    for si, c in enumerate(candles):
        if c["ts"] < next_ts:
            continue
        if c["ts"] > last_start:
            break
        next_ts = c["ts"] + step_ms
        res = {}
        for (v, b), cfg in CFGS.items():
            cl, ended = run_session(candles, si, cfg)
            if ended:
                reason = 2 if coin_ends_early else 4
            else:
                reason = REASON[cl["reason"]]
            res[(v, b)] = (reason, cl["pnl_usd"], cl["pnl_pct"], cl["days"], cl["waves_filled"],
                           cl["deployed_usd"], cl["capital_days"])
        out.append((c["ts"], (c["ts"] - listing) / DAY, res))
    return sym, out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=r"D:\FINDMY\data\research\market.db")
    ap.add_argument("--interval", default="1h")
    ap.add_argument("--spacing-h", type=int, default=168)
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--first-start", default="")
    ap.add_argument("--allow-open", action="store_true")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    db = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    syms = [r[0] for r in db.execute("SELECT DISTINCT symbol FROM candles WHERE interval=? ORDER BY symbol", (a.interval,))]
    gend = db.execute("SELECT MAX(ts) FROM candles WHERE interval=?", (a.interval,)).fetchone()[0]
    listing = dict(db.execute("SELECT symbol, MIN(ts) FROM candles WHERE interval='1d' GROUP BY symbol").fetchall())
    db.close()
    fs = cps._to_ts(a.first_start) if a.first_start else 0
    if a.limit:
        syms = syms[:a.limit]
    jobs = [(a.db, s, a.interval, a.spacing_h, gend, listing.get(s, 0), fs, a.allow_open) for s in syms]
    t0 = time.time()
    rec = {k: [] for k in ("sym", "ts", "age_d", "variant", "bound", *FIELDS)}
    with Pool(a.workers) as pool:
        for n, (sym, out) in enumerate(pool.imap_unordered(work, jobs, chunksize=1), 1):
            for ts, age, res in out:
                for (v, b), r in res.items():
                    rec["sym"].append(sym); rec["ts"].append(ts); rec["age_d"].append(age)
                    rec["variant"].append(v); rec["bound"].append(b)
                    for k, x in zip(FIELDS, r):
                        rec[k].append(x)
            if n % 50 == 0:
                print(f"{n}/{len(jobs)} {time.time()-t0:.0f}s", flush=True)
    np.savez_compressed(a.out, **{k: np.array(v) for k, v in rec.items()})
    print("done", len(rec["sym"]), f"{time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
