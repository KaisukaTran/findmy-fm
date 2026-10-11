"""Level 1: per-session study (unlimited cash), paired across tp_anchor variants.

Same start set for every variant/bound: per coin, a start every `--spacing-h` hours (per-coin
phase from a stable hash), only after the coin has >= 24 days of listing age (1d table), and only
while a full 60-day horizon still fits before the global end of data (so a session is never
censored by the END OF THE STUDY; it can still end 'data_end' because the COIN's own series ended
- delisted / truncated download - and is then marked to market at the last close, like
cps._close_delisted).

    python per_session.py --interval 1h --spacing-h 168 --out per_session_1h.npz
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

import tp_anchor_engine as eng  # noqa: E402
import scripts.capital_portfolio_study as cps  # noqa: E402
from app import costengine  # noqa: E402
from scripts.ladder_panel_study import to_candles  # noqa: E402

DB = r"D:\FINDMY\data\research\market.db"
DAY = 86_400_000
VARIANTS = ["avg", "entry", "max"]
BOUNDS = ["opt", "pess"]
REASONS = ["tp", "deadline", "data_end"]
WAVE0 = 28.0


def make_cfg(anchor: str, pess: bool) -> eng.TPConfig:
    return eng.TPConfig(
        capital=7000.0, gate="reserve", pessimistic=pess, distance_pct=7.0, max_waves=10,
        tp_pct=5.0, tp_step_pct=0.5, sl_pct=0.0, deadline_days=60.0, trail_after_tp_pct=0.0,
        cost_pct=costengine.round_trip_cost_pct(), tp_fee_buffer_pct=costengine.tp_fee_buffer_pct(),
        wave0_usd=WAVE0, wave0_pct=0.0, warmup=24, tp_anchor=anchor,
        partial_last_rung=True, backstop=False,
    )


CFGS = {(a, b): make_cfg(a, b == "pess") for a in VARIANTS for b in BOUNDS}


def run_session(candles, start, cfg):
    state = cps.SessionState("X", candles, start, cfg, wave0=WAVE0, fund=0.0)
    ledger = cps.Ledger(1e15)
    for i in range(start + 1, len(candles)):
        r = eng.step_session(state, candles[i], i, cfg, ledger)
        c = r["closed"]
        if c is not None:
            return c, state
    return cps._close_delisted(state, cfg), state   # data ended: MTM at last close


def work(args):
    sym, interval, spacing_h, global_end_ts, listing = args
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    rows = db.execute("SELECT symbol, ts, high, low, close, quote_volume, open, volume FROM candles "
                      "WHERE symbol=? AND interval=? ORDER BY ts", (sym, interval)).fetchall()
    db.close()
    candles = to_candles([tuple(r) for r in rows])
    if not candles:
        return sym, []
    step_ms = spacing_h * 3_600_000
    phase = int(hashlib.md5(sym.encode()).hexdigest()[:8], 16) % spacing_h * 3_600_000
    first_ok = max(listing + 24 * DAY, candles[0]["ts"])
    last_start = global_end_ts - 60 * DAY
    out = []
    next_ts = first_ok + phase
    for si, c in enumerate(candles):
        if c["ts"] < next_ts:
            continue
        if c["ts"] > last_start:
            break
        next_ts = c["ts"] + step_ms
        res = {}
        for (a, b), cfg in CFGS.items():
            cl, st = run_session(candles, si, cfg)
            reason = cl["reason"] if cl["reason"] in ("tp", "deadline") else "data_end"
            res[(a, b)] = (REASONS.index(reason), cl["pnl_usd"], cl["pnl_pct"], cl["days"],
                           cl["waves_filled"], cl["deployed_usd"], cl["capital_days"])
        out.append((c["ts"], res))
    return sym, out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", default="1h")
    ap.add_argument("--spacing-h", type=int, default=168)
    ap.add_argument("--workers", type=int, default=14)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out", default=str(HERE / "per_session_1h.npz"))
    a = ap.parse_args()

    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    syms = [r[0] for r in db.execute("SELECT DISTINCT symbol FROM candles WHERE interval=? ORDER BY symbol", (a.interval,))]
    gend = db.execute("SELECT MAX(ts) FROM candles WHERE interval=?", (a.interval,)).fetchone()[0]
    listing = dict(db.execute("SELECT symbol, MIN(ts) FROM candles WHERE interval='1d' GROUP BY symbol").fetchall())
    db.close()
    if a.limit:
        syms = syms[:a.limit]
    jobs = [(s, a.interval, a.spacing_h, gend, listing.get(s, 0)) for s in syms]
    t0 = time.time()
    rec = {k: [] for k in ("sym", "ts", "variant", "bound", "reason", "pnl_usd", "pnl_pct", "days", "waves", "deployed", "capdays")}
    with Pool(a.workers) as pool:
        for n, (sym, out) in enumerate(pool.imap_unordered(work, jobs, chunksize=1), 1):
            for ts, res in out:
                for (v, b), r in res.items():
                    rec["sym"].append(sym); rec["ts"].append(ts); rec["variant"].append(v); rec["bound"].append(b)
                    for k, x in zip(("reason", "pnl_usd", "pnl_pct", "days", "waves", "deployed", "capdays"), r):
                        rec[k].append(x)
            if n % 50 == 0:
                print(f"{n}/{len(jobs)} {time.time()-t0:.0f}s", flush=True)
    np.savez_compressed(a.out, **{k: np.array(v) for k, v in rec.items()})
    print("done", len(rec["sym"]), f"{time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
