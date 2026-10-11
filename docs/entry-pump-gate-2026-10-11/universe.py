"""Stage 0: point-in-time universe. Top-100 coins by trailing-24h quote volume at each entry hour.

Reads only `quote_volume` of 1h bars (read-only DB). Output: entries_universe_<seed>.npz with, for
every (day, member coin): entry ts. The entry HOUR of a day is drawn once per day (seeded) and is
SHARED by all coins that day (so a within-day permutation cluster = one scan, like the live bot).
"""
from __future__ import annotations

import common  # noqa: F401  (must be first)
import sys
import time
import numpy as np
from common import MARKET_DB, WINDOW_START_TS, WINDOW_END_TS, ENTRY_FIRST_DAY_TS, H, D, ro

# Not tradable coins in the sense of the scanner (stable / pegged / wrapped / tokenised equities /
# commodity tokens / the live asset-guard denylist LUNA, LUNC). Detected by price stdev + names.
EXCLUDED = {
    "BFUSDUSDT", "EURIUSDT", "KGSTUSDT", "RLUSDUSDT", "USDEUSDT", "UUSDT", "FDUSDUSDT", "USDCUSDT",
    "XAUTUSDT", "PAXGUSDT", "WBTCUSDT", "WBETHUSDT", "BNSOLUSDT", "LUNAUSDT", "LUNCUSDT",
    "TSLABUSDT", "SPCXBUSDT", "SNDKBUSDT", "CRCLBUSDT", "MSTRBUSDT", "DRAMBUSDT", "SOXLBUSDT",
    "NVDABUSDT", "EWYBUSDT", "QQQBUSDT", "SKHYBUSDT", "INTCBUSDT", "KORUBUSDT", "GOOGLBUSDT",
    "INTWBUSDT", "SNXXBUSDT", "MUUBUSDT", "NBISBUSDT", "AMDBUSDT", "AAPLBUSDT", "SOXSBUSDT",
    "AXTIBUSDT", "PLTRBUSDT",
}

NH = (WINDOW_END_TS - WINDOW_START_TS) // H + 1   # hourly grid length


def load_qvol_grid():
    """dense [NH, S] float32 of 1h quote volume (NaN -> 0 = absent), symbols list."""
    con = ro(MARKET_DB)
    syms = [r[0] for r in con.execute("select distinct symbol from candles where interval='1h' order by 1")]
    q = np.zeros((NH, len(syms)), dtype=np.float32)
    t0 = time.time()
    for j, s in enumerate(syms):
        rows = con.execute("select ts, quote_volume from candles where interval='1h' and symbol=? and ts>=? and ts<=?",
                           (s, WINDOW_START_TS, WINDOW_END_TS)).fetchall()
        if rows:
            a = np.array(rows, dtype=np.float64)
            idx = ((a[:, 0] - WINDOW_START_TS) // H).astype(np.int64)
            q[idx, j] = np.nan_to_num(a[:, 1]).astype(np.float32)
    con.close()
    for j, s in enumerate(syms):
        if s in EXCLUDED:
            q[:, j] = 0.0
    print(f"loaded qvol grid {q.shape} in {time.time()-t0:.0f}s", flush=True)
    return q, syms


def trailing24(q: np.ndarray) -> np.ndarray:
    """trailing 24-bar quote volume INCLUDING the bar at t (bar t is closed at the entry decision
    time t+1h). float64 cumsum for exactness."""
    c = np.cumsum(q.astype(np.float64), axis=0)
    out = c.copy()
    out[24:] -= c[:-24]
    return out


def day_hours(seed: int, n_days: int) -> np.ndarray:
    return np.random.default_rng(seed).integers(0, 24, size=n_days)


def members(q24: np.ndarray, hour_idx: int, top_n: int, min_qv: float) -> np.ndarray:
    row = q24[hour_idx]
    order = np.argsort(-row, kind="stable")[:top_n]
    return order[row[order] >= min_qv]


def build(seed: int, top_n: int = 100, min_qv: float = 1e6):
    q, syms = load_qvol_grid()
    q24 = trailing24(q)
    first_day = (ENTRY_FIRST_DAY_TS - WINDOW_START_TS) // D
    last_day = (WINDOW_END_TS - WINDOW_START_TS) // D        # inclusive, 2026-07-31
    days = np.arange(first_day, last_day + 1)
    hours = day_hours(seed, len(days))
    entry_h_idx, ent_sym = [], []
    for d, h in zip(days, hours):
        hi = int(d * 24 + h)
        if hi >= NH:
            continue
        m = members(q24, hi, top_n, min_qv)
        entry_h_idx.extend([hi] * len(m))
        ent_sym.extend(m.tolist())
    entry_h_idx = np.array(entry_h_idx, dtype=np.int64)
    ent_sym = np.array(ent_sym, dtype=np.int64)
    return syms, entry_h_idx, ent_sym, hours, days


if __name__ == "__main__":
    seed = int(sys.argv[1]) if len(sys.argv) > 1 else 20261011
    syms, hi, es, hours, days = build(seed)
    out = common.STUDY / f"cache_universe_{seed}.npz"
    np.savez(out, syms=np.array(syms), hour_idx=hi, sym_idx=es, hours=hours, days=days)
    print(f"entries (coin-hours in top-100): {len(hi)} over {len(days)} days; distinct coins {len(set(es.tolist()))}")
