"""Stage 1: per-entry causal features + forward outcomes (both intrabar bounds).

For every (day, coin) entry of the point-in-time top-100 universe (universe.py):
  * gate inputs, computed from data with ts <= entry-bar ts ONLY (asserted per entry);
  * the shipped daily vetoes + consensus via the REAL scanner/agent code
    (`scripts/measure_entry_gates.gate_snapshot`), on two daily-bar variants:
        C = last 365 daily bars CLOSED before the entry day            (pre-registered primary)
        F = C + the day's FORMING bar built from 1h bars up to the entry bar (what a live
            scanner fetching "the last N daily candles" sees)
  * the forward outcome with `app.backtest.simulate_kss` (optimistic AND pessimistic bound).

Decision-time convention: the entry bar `start` is a CLOSED 1h bar; we buy at its close
(simulate_kss does exactly that) -> every input has ts <= entry-bar ts, all of it closed by the
time the order is placed.

Run (<=4 processes):  python entry_study.py <seed> [workers=4] [--limit-symbols N]
"""
from __future__ import annotations

import common  # noqa: F401  (must be first)

import bisect
import multiprocessing as mp
import sys
import time

import numpy as np

from common import D, H, MARKET_DB, WINDOW_END_TS, WINDOW_START_TS, STUDY, ro

LOOKBACK_DAILY = 365          # settings.backtest_lookback_days on '1d'
MIN_DAILY_CLOSED = 30         # scanner._MIN_CANDLES
RSI1H_WINDOW = 200            # last 200 hourly closes feed indicators.rsi (Wilder, seeded on the window)
MAX_FFILL_H = 6               # tolerate <=6h hole when reading the close "24h / 7d ago"
SIM_FIELDS = ("pnl_usd", "cap_days", "pnl_pct", "tp", "days_to_tp", "waves", "timed_out",
              "trunc", "mae", "days_held")

# --------------------------------------------------------------------------------------
# Series access
# --------------------------------------------------------------------------------------


class Sym:
    """One symbol's 1h + 1d bars as parallel python lists (bisect-friendly)."""

    def __init__(self, con, name: str):
        self.name = name
        rows = con.execute("select ts,open,high,low,close,volume from candles where interval='1h' and symbol=? "
                           "and ts>=? and ts<=? order by ts", (name, WINDOW_START_TS, WINDOW_END_TS)).fetchall()
        self.ts = [r[0] for r in rows]
        self.o = [r[1] for r in rows]
        self.h = [r[2] for r in rows]
        self.l = [r[3] for r in rows]
        self.c = [r[4] for r in rows]
        self.v = [r[5] for r in rows]
        drows = con.execute("select ts,open,high,low,close,volume from candles where interval='1d' and symbol=? "
                            "and ts<=? order by ts", (name, WINDOW_END_TS)).fetchall()
        self.d_ts = [r[0] for r in drows]
        self.d = [dict(ts=r[0], open=r[1], high=r[2], low=r[3], close=r[4], volume=r[5]) for r in drows]
        self._candles = None

    def candles(self):
        """1h candle dicts for simulate_kss (built lazily, once per symbol)."""
        if self._candles is None:
            self._candles = [dict(ts=t, open=o, high=h, low=l, close=c, volume=v)
                             for t, o, h, l, c, v in zip(self.ts, self.o, self.h, self.l, self.c, self.v)]
        return self._candles

    def close_at_or_before(self, t: int, max_gap_h: int = MAX_FFILL_H):
        """(close, ts) of the last 1h bar with ts <= t, if within max_gap_h; else (nan, None)."""
        j = bisect.bisect_right(self.ts, t) - 1
        if j < 0 or t - self.ts[j] > max_gap_h * H:
            return float("nan"), None
        return self.c[j], self.ts[j]


def forming_bar(S: Sym, day_ts: int, start: int) -> dict | None:
    """Daily bar of the entry day as a live scanner sees it at the entry bar's close."""
    lo = bisect.bisect_left(S.ts, day_ts)
    if lo > start:
        return None
    return dict(ts=day_ts, open=S.o[lo], high=max(S.h[lo:start + 1]), low=min(S.l[lo:start + 1]),
                close=S.c[start], volume=float(sum(S.v[lo:start + 1])))


def daily_windows(S: Sym, day_ts: int, start: int):
    """(closed window C, forming window F, n_closed_total, max input ts) for symbol S."""
    k = bisect.bisect_left(S.d_ts, day_ts)           # daily bars with ts < day_ts are CLOSED
    closed = S.d[max(0, k - LOOKBACK_DAILY):k]
    max_ts = closed[-1]["ts"] if closed else -1
    fb = forming_bar(S, day_ts, start)
    win_f = (closed + [fb])[-LOOKBACK_DAILY:] if fb else list(closed)
    if fb:
        max_ts = max(max_ts, S.ts[start])
    return closed, win_f, k, max_ts


# --------------------------------------------------------------------------------------
# Pure feature function (look-ahead test targets this)
# --------------------------------------------------------------------------------------


def entry_features(S: Sym, B: Sym, start: int, gate_fn, rsi_fn):
    """All gate inputs/flags at 1h bar index `start` of S. Uses only bars with ts <= S.ts[start].

    `gate_fn(window, btc_window)` -> measure_entry_gates.GateSnapshot-like object (or None);
    `rsi_fn(values)` -> Wilder RSI(14) (app.ta.indicators.rsi).
    Returns (features dict, max_input_ts).
    """
    t = S.ts[start]
    day_ts = t - ((t - WINDOW_START_TS) % D)
    used: list[int] = [t]
    f: dict = {}

    def ret_pct(sym: Sym, hours: int):
        c_then, ts_then = sym.close_at_or_before(t - hours * H)
        c_now, ts_now = sym.close_at_or_before(t)
        if ts_then is None or ts_now is None or c_then <= 0:
            return float("nan")
        used.extend([ts_then, ts_now])
        return (c_now / c_then - 1.0) * 100.0

    f["pump24"] = ret_pct(S, 24)
    f["pump7d"] = ret_pct(S, 168)
    f["btc_ret24"] = ret_pct(B, 24)
    lo = max(0, start + 1 - RSI1H_WINDOW)
    f["rsi1h"] = rsi_fn(S.c[lo:start + 1]) if start + 1 - lo >= 15 else float("nan")

    # BTC daily windows at the same decision instant
    bstart = bisect.bisect_right(B.ts, t) - 1
    cC, cF, n_closed, mx = daily_windows(S, day_ts, start)
    used.append(mx)
    f["n_closed_daily"] = n_closed
    if bstart < 0 or B.ts[bstart] != t:
        # BTC bar missing at exactly this hour: use its last bar (still causal)
        pass
    bC, bF, _, mxb = daily_windows(B, day_ts, bstart) if bstart >= 0 else ([], [], 0, -1)
    used.append(mxb)

    f["rsiD_F"] = rsi_fn([x["close"] for x in cF]) if len(cF) > 14 else float("nan")
    f["rsiD_C"] = rsi_fn([x["close"] for x in cC]) if len(cC) > 14 else float("nan")

    for tag, wS, wB in (("C", cC, bC), ("F", cF, bF)):
        snap = gate_fn(wS, wB) if len(wS) >= MIN_DAILY_CLOSED and n_closed >= MIN_DAILY_CLOSED else None
        if snap is None:
            f[f"ok_{tag}"] = False
            for g in ("dt", "fk", "rs", "bev", "cons"):
                f[f"{g}_{tag}"] = False
        else:
            f[f"ok_{tag}"] = True
            f[f"dt_{tag}"] = bool(snap.downtrend)
            f[f"fk_{tag}"] = bool(snap.falling_knife)
            f[f"rs_{tag}"] = bool(snap.rel_strength)
            f[f"bev_{tag}"] = bool(snap.backtest_evidence)
            f[f"cons_{tag}"] = bool(snap.consensus)
    return f, max(used)


# --------------------------------------------------------------------------------------
# Outcomes
# --------------------------------------------------------------------------------------


def simulate_entry(S: Sym, start: int, cfg: dict, pessimistic: bool) -> dict:
    from app.backtest import simulate_kss

    res = simulate_kss(
        S.candles(), start, cfg["distance"], cfg["waves"], cfg["tp_eff"], cfg["deadline"],
        sl_pct=cfg["sl"], cost_pct=cfg["cost"], pessimistic_intrabar=pessimistic,
        wave0_notional_usd=cfg["wave0"], tp_step_pct=cfg["tp_step"],
    )
    complete = bool(res.tp_hit or res.hit_deadline or res.stopped)
    entry_ts = S.ts[start]
    if res.tp_hit:
        held = float(res.days_to_tp)
    elif res.hit_deadline:
        held = float(cfg["deadline"])
    else:
        held = (S.ts[-1] - entry_ts) / D
    return dict(
        pnl_usd=res.pnl_pct / 100.0 * res.exit_capital, cap_days=res.capital_days, pnl_pct=res.pnl_pct,
        tp=bool(res.tp_hit), days_to_tp=(float(res.days_to_tp) if res.tp_hit else float("nan")),
        waves=int(res.waves_filled), timed_out=bool(res.hit_deadline), trunc=not complete,
        mae=float(res.mae_pct), days_held=held,
    )


def rung2_within(S: Sym, start: int, entry: float, factor: float, hours: int = 48) -> bool:
    """Did a bar in (entry bar, entry bar + hours] trade down to the 2nd wave's target?
    Same trigger as simulate_kss (bar low <= target). Bars by timestamp, so holes do not shift it."""
    t0 = S.ts[start]
    tgt = entry * factor
    j = start + 1
    n = len(S.ts)
    while j < n and S.ts[j] - t0 <= hours * H:
        if S.l[j] <= tgt:
            return True
        j += 1
    return False


# --------------------------------------------------------------------------------------
# Worker
# --------------------------------------------------------------------------------------

_W: dict = {}


def _init(cfg: dict):
    import common as _c  # noqa: F401
    _c.effective_config()
    from app.config import settings
    import measure_entry_gates as m
    from app.ta import indicators
    _W["cfg"] = cfg
    _W["con"] = ro(MARKET_DB)
    _W["B"] = Sym(_W["con"], "BTCUSDT")
    _W["m"] = m
    _W["indicators"] = indicators
    _W["force"] = m.gates_forced_on()
    _W["force"].__enter__()           # rel_strength / mae gates ON in this process (no DB write)
    _W["settings"] = settings


def _gate_fn(wS, wB):
    m = _W["m"]
    c = _W["cfg"]
    return m.gate_snapshot(wS, wB, distance_pct=c["distance"], tp_pct=c["tp_nominal"],
                           max_waves=c["waves"], pessimistic_intrabar=False)


def process_symbol(task):
    name, hour_idx = task
    cfg = _W["cfg"]
    S = Sym(_W["con"], name)
    B = _W["B"]
    rows = []
    viol = 0
    for hi in hour_idx:
        t = WINDOW_START_TS + int(hi) * H
        start = bisect.bisect_left(S.ts, t)
        if start >= len(S.ts) or S.ts[start] != t:
            continue                                   # no bar at the decision hour -> not tradable
        f, max_in = entry_features(S, B, start, _gate_fn, _W["indicators"].rsi)
        if max_in > t:
            viol += 1
            continue
        f.update(sym=name, ts=t, hour_idx=int(hi), start=start)
        base_ok = {k: f[f"ok_{k}"] and not (f[f"dt_{k}"] or f[f"fk_{k}"] or f[f"cons_{k}"]) for k in ("C", "F")}
        f["pool_C"], f["pool_F"] = base_ok["C"], base_ok["F"]
        if base_ok["C"] or base_ok["F"]:
            entry = S.c[start]
            f["rung2_48"] = rung2_within(S, start, entry, 1 - cfg["distance"] / 100.0)
            last_ts = S.ts[-1]
            f["dead_end"] = last_ts < WINDOW_END_TS - 3 * D
            for b, pess in (("o", False), ("p", True)):
                r = simulate_entry(S, start, cfg, pess)
                for k, v in r.items():
                    f[f"{k}_{b}"] = v
        rows.append(f)
    return name, rows, viol


def make_cfg():
    from app import costengine
    from app.config import settings
    return dict(
        distance=float(settings.scan_distance_pct), waves=int(settings.scan_max_waves),
        tp_nominal=float(settings.scan_tp_pct),
        tp_eff=float(settings.scan_tp_pct) + costengine.tp_fee_buffer_pct(),
        deadline=float(settings.deadline_days), sl=float(settings.sl_pct),
        cost=costengine.round_trip_cost_pct(), tp_step=float(settings.kss_tp_step_per_rung_pct),
        wave0=float(settings.kss_first_wave_usd),
    )


def main():
    import pickle

    seed = int(sys.argv[1]) if len(sys.argv) > 1 else 20261011
    workers = min(4, int(sys.argv[2])) if len(sys.argv) > 2 else 4
    limit = None
    if "--limit-symbols" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit-symbols") + 1])
    common.effective_config()
    cfg = make_cfg()
    print("cfg", cfg, flush=True)
    z = np.load(STUDY / f"cache_universe_{seed}.npz")
    syms, hidx, sidx = z["syms"], z["hour_idx"], z["sym_idx"]
    by_sym: dict[str, list[int]] = {}
    for h, s in zip(hidx.tolist(), sidx.tolist()):
        by_sym.setdefault(str(syms[s]), []).append(h)
    tasks = sorted(by_sym.items(), key=lambda kv: -len(kv[1]))
    if limit:
        tasks = tasks[:limit]
    t0 = time.time()
    out, viol = [], 0
    ctx = mp.get_context("spawn")
    with ctx.Pool(workers, initializer=_init, initargs=(cfg,)) as pool:
        for i, (name, rows, v) in enumerate(pool.imap_unordered(process_symbol, tasks, chunksize=1), 1):
            out.extend(rows)
            viol += v
            if i % 25 == 0 or i == len(tasks):
                print(f"[{i}/{len(tasks)}] rows={len(out)} elapsed={time.time()-t0:.0f}s", flush=True)
    print(f"look-ahead violations (max input ts > entry ts): {viol}")
    path = STUDY / f"cache_entries_{seed}{'_lim' if limit else ''}.pkl"
    with open(path, "wb") as fh:
        pickle.dump(dict(cfg=cfg, rows=out, violations=viol, seed=seed), fh)
    print("saved", path, len(out))


if __name__ == "__main__":
    main()
