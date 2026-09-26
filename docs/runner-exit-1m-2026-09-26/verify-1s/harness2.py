"""Harness v2: study-original walks + corrected walks.
 P / O   : exactly the study's conventions (reproduces report).
 Pf      : P, but a stop in the TOUCH minute fills at the stop (the minute's open precedes the
           touch, so min(stop, open) sells at a price from before the position was armed).
 Of      : O + same m0 fix + a later minute whose own high raised the stop fills at that stop
           (price path O->H->L passes the stop on the way down), open only if open<=stop_before.
 OPT-ladder touch fix: if the deepest rung filled INSIDE the touch hour, the touch minute must be
 at/after the first minute whose low reaches that rung ("tfix"); events with no such minute are
 counted as 'inconsistent' and dropped from the fixed set."""
import bisect, pickle, sys, time
from multiprocessing import Pool
from pathlib import Path
import numpy as np

sys.path.insert(0, "D:/FINDMY")
from scripts.ladder_panel_study import to_candles
from scripts.runner_exit_study import (DEADLINE, DISTANCE, MAX_WAVES, MAKER_FEE, TAKER_FEE,
                                       run_ladder, maker_sell, bt_targets)
from scripts.runner_exit_1m_study import choose_symbols, load_1m

OUT = Path(__file__).parent / "events2"
MS_DAY = 86_400_000
GAPS = (2.0, 3.0, 5.0); SLIPS = (0.1, 0.3)
CHUNKS = (64, 1024, 16384, 10**9)


def walk(m1, s, dl_idx, n, init_peak, floor, g, mode, m0_is_touch):
    end = min(dl_idx, n - 1)
    peak = init_peak; a = s; ci = 0
    H, L, O = m1["high"], m1["low"], m1["open"]
    gg = 1 - g / 100
    pmode = mode[0]
    while a <= end:
        b = min(end + 1, a + CHUNKS[min(ci, len(CHUNKS) - 1)]); ci += 1
        cm = np.maximum.accumulate(np.concatenate(([peak], H[a:b])))
        pb, pa = cm[:-1], cm[1:]
        st = np.maximum(floor, (pb if pmode == "P" else pa) * gg)
        br = L[a:b] <= st
        if br.any():
            j = int(np.argmax(br)); idx = a + j
            stop = float(st[j]); op = float(O[idx])
            if mode in ("P", "O"):
                base = min(stop, op)
            elif m0_is_touch and idx == s:
                base = stop
            elif mode == "Pf":
                base = min(stop, op)
            else:  # Of
                sb = max(floor, float(pb[j]) * gg)
                base = op if op <= sb else stop
            return idx, base, bool(pa[j] > pb[j]), float(pb[j] if pmode == "P" else pa[j])
        peak = float(cm[-1]); a = b
    return None, None, False, peak


def settle(m1, n, w, dl_idx, qty, slip):
    idx, base = w[0], w[1]
    if idx is not None:
        return "stop", qty * base * (1 - slip / 100) * (1 - TAKER_FEE / 100), idx
    if dl_idx < n:
        return "deadline", qty * float(m1["close"][dl_idx]) * (1 - TAKER_FEE / 100), dl_idx
    return "open", qty * float(m1["close"][n - 1]) * (1 - TAKER_FEE / 100), n - 1


def eval_touch(m1, n, ts, t, s, dl_idx, dl_ts, r):
    cost = t["deployed"] * (1 + MAKER_FEE / 100)
    v0_proc = maker_sell(t["qty"], t["tp_price"])
    v0 = v0_proc - cost
    r["v0"] = v0
    floor = t["avg"] * 1.03
    res = {}
    for g in GAPS:
        for mode in ("P", "O", "Pf", "Of"):
            w = walk(m1, s, dl_idx, n, t["tp_price"], floor, g, mode, True)
            for slip in SLIPS:
                kind, proc, ei = settle(m1, n, w, dl_idx, t["qty"], slip)
                res[("V4a", g, slip, mode)] = (proc - cost, kind, ei, w[2], w[3])
    for dlabel, delay in (("d2", 120_000), ("d3", 180_000)):
        d = bisect.bisect_left(ts, int(ts[s]) + delay, lo=s)
        if d < n and ts[d] < dl_ts and m1["open"][d] >= t["tp_price"] and v0 > 0:
            dp = float(m1["open"][d])
            for g in GAPS:
                usd = min(v0_proc, v0 / (g / 100 + 0.002 + 0.03))
                for mode in (("P", "O", "Of") if dlabel == "d2" else ("P",)):
                    w = walk(m1, d, dl_idx, n, dp, 0.0, g, mode, False)
                    for slip in SLIPS:
                        q = usd / (dp * (1 + slip / 100) * (1 + TAKER_FEE / 100))
                        kind, proc, ei = settle(m1, n, w, dl_idx, q, slip)
                        key = ("V5", g, slip, mode) if dlabel == "d2" else ("V5d3", g, slip, mode)
                        res[key] = (v0 + proc - usd, kind, ei, w[2], w[3], usd, d)
    r["res"] = res


def one(job):
    sym, bars, every, warmup, db1m = job
    candles = to_candles(bars)
    m1 = load_1m(Path(db1m), sym)
    ts = m1["ts"]; n = len(ts)
    recs = []
    for start in range(warmup, len(candles) - 1, every):
        for pess in (False, True):
            t = run_ladder(candles, start, pess)
            if t is None or t["outcome"] != "tp":
                continue
            tb = t["touch_bar"]; hour = candles[tb]["ts"]
            lo = bisect.bisect_left(ts, hour); hi = bisect.bisect_left(ts, hour + 3_600_000)
            hits = np.nonzero(m1["high"][lo:hi] >= t["tp_price"])[0]
            if hits.size == 0:
                recs.append({"sym": sym, "mismatch": True}); continue
            s = lo + int(hits[0])
            dl_ts = t["entry_ts"] + int(DEADLINE * MS_DAY)
            dl_idx = bisect.bisect_left(ts, dl_ts, lo=s)
            base = {"sym": sym, "bound": "PESS" if pess else "OPT", "start": start,
                    "entry_ts": t["entry_ts"], "touch_hour": hour, "tp": t["tp_price"], "avg": t["avg"],
                    "qty": t["qty"], "deployed": t["deployed"], "filled": t["filled"], "dl_idx": dl_idx}
            r = dict(base, s=s, touch_ts=int(ts[s]), fixset="orig")
            eval_touch(m1, n, ts, t, s, dl_idx, dl_ts, r)
            recs.append(r)
            # OPT-ladder touch-minute fix
            if not pess:
                rung_in_bar = False
                if t["filled"] >= 2:
                    targets = bt_targets(candles[start]["close"], DISTANCE, MAX_WAVES)
                    deep = targets[t["filled"] - 1]
                    prev_low = min((candles[j]["low"] for j in range(start + 1, tb)), default=float("inf"))
                    rung_in_bar = prev_low > deep
                if rung_in_bar:
                    rl = np.nonzero(m1["low"][lo:hi] <= deep)[0]
                    rm = lo + int(rl[0]) if rl.size else lo
                    h2 = np.nonzero(m1["high"][rm:hi] >= t["tp_price"])[0]
                    if h2.size == 0:
                        recs.append(dict(base, fixset="tfix", inconsistent=True, s=None)); continue
                    s2 = rm + int(h2[0])
                    r2 = dict(base, s=s2, touch_ts=int(ts[s2]), fixset="tfix", moved=(s2 != s))
                    eval_touch(m1, n, ts, t, s2, dl_idx, dl_ts, r2)
                    recs.append(r2)
                else:
                    recs.append(dict(r, fixset="tfix", moved=False))
    with open(OUT / f"{sym}.pkl", "wb") as f:
        pickle.dump(recs, f)
    return sym, len(recs)


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    chosen, series = choose_symbols(Path("D:/FINDMY/data/research/market.db"), 60, 2.0, 7)
    syms = sorted(set(chosen) | {"XPLUSDT"})
    jobs = [(s, series[s], 24, 24, "D:/FINDMY/data/research/market_1m.db") for s in syms if s in series]
    del series
    t0 = time.time()
    with Pool(8) as p:
        for sym, k in p.imap_unordered(one, jobs):
            print(sym, k, f"{time.time()-t0:.0f}s", flush=True)
