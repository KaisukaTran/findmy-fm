"""Re-walk a stratified sample of PESS-ladder events on Binance 1-SECOND klines (REST
/api/v3/klines interval=1s, 1000 bars/request, cached). 1m bars are used only to skip minutes in
which NO ordering could fire the stop; every candidate minute is walked second by second.
Models:  1sP  (per-second low-before-high),  1sO (per-second high-before-low, fill at stop),
         T2   (a 2-second poller: every 2nd 1s CLOSE is the observed price; peak and trigger use
               observed prices; fill = observed price) — closest to the app's 2s tick loop."""
import bisect, json, os, pickle, random, sys, time, urllib.request, glob
from collections import defaultdict
from pathlib import Path
import numpy as np

sys.path.insert(0, "D:/FINDMY")
sys.path.insert(0, os.path.dirname(__file__))
from scripts.runner_exit_1m_study import load_1m
from common import SP

CACHE = Path(SP) / "cache1s"; CACHE.mkdir(exist_ok=True)
FEE = 0.001
_last = [0.0]
NREQ = [0]
MINS = [0]
MISM = []


def fetch_block(sym, b0):
    f = CACHE / f"{sym}_{b0}.json"
    if f.exists():
        return json.loads(f.read_text())
    wait = 0.34 - (time.time() - _last[0])
    if wait > 0:
        time.sleep(wait)
    url = f"https://api.binance.com/api/v3/klines?symbol={sym}&interval=1s&startTime={b0}&endTime={b0+999_999}&limit=1000"
    for attempt in range(6):
        try:
            with urllib.request.urlopen(url, timeout=30) as resp:
                used = int(resp.headers.get("X-MBX-USED-WEIGHT-1M", "0"))
                data = json.loads(resp.read())
            break
        except Exception as e:  # 429/418/network: back off hard
            print("  fetch error", sym, b0, e, flush=True); time.sleep(10 * (attempt + 1))
    else:
        raise RuntimeError("fetch failed")
    _last[0] = time.time(); NREQ[0] += 1
    if used > 1500:
        time.sleep(20)
    rows = [[int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4])] for k in data]
    f.write_text(json.dumps(rows))
    return rows


def seconds_of_minute(sym, mts):
    b0 = mts - (mts % 1_000_000)
    rows = fetch_block(sym, b0)
    if mts + 60_000 > b0 + 1_000_000:  # minute straddles two blocks
        rows = rows + fetch_block(sym, b0 + 1_000_000)
    return [r for r in rows if mts <= r[0] < mts + 60_000]


def walk_1s(sym, m1, s, end_idx, n, dl_idx, init_peak, floor, g, model, touch_px=None):
    """Returns (kind, fill_base_or_close, exit_ts). touch_px: if set, arm at the first SECOND of
    minute s whose high >= touch_px (V4a); else armed from second 0 (V5 runner)."""
    gg = 1 - g / 100
    H, L = m1["high"], m1["low"]
    peak = init_peak
    a = s
    first_second = True
    while a <= end_idx:
        # vectorised skip: first minute j>=a where the MAX possible stop could be hit
        if a == s:
            j = s
        else:
            b = min(end_idx + 1, a + 200_000)
            cm = np.maximum.accumulate(np.concatenate(([peak], H[a:b])))[1:]
            br = L[a:b] <= np.maximum(floor, cm * gg)
            if not br.any():
                peak = float(cm[-1]); a = b; continue
            jj = int(np.argmax(br)); j = a + jj
            if jj > 0:
                peak = max(peak, float(cm[jj - 1]))
        secs = seconds_of_minute(sym, int(m1["ts"][j]))
        MINS[0] += 1
        if not secs or abs(max(r[2] for r in secs) - H[j]) > 1e-12 or abs(min(r[3] for r in secs) - L[j]) > 1e-12:
            MISM.append((sym, int(m1["ts"][j]), len(secs)))
        if j == s and touch_px is not None:
            k0 = next((i for i, r in enumerate(secs) if r[2] >= touch_px), None)
            secs = secs[k0:] if k0 is not None else secs
        if model == "T2":
            # observed price every 2s; phase anchored on the arm second
            if j == s:
                anchor = secs[0][0] if secs else int(m1["ts"][j])
                walk_1s.anchor = anchor
            obs = [r for r in secs if (r[0] - walk_1s.anchor) % 2000 == 0]
            for r in obs:
                px = r[4]
                peak = max(peak, px)
                if px <= max(floor, peak * gg):
                    return "stop", px, r[0]
            # peak over the unobserved seconds is not used by a poller
        else:
            for r in secs:
                t, o, h, l, c = r
                sb = max(floor, peak * gg)
                if model == "1sP":
                    if l <= sb:
                        return "stop", (sb if first_second else min(sb, o)), t
                    peak = max(peak, h)
                else:
                    pa = max(peak, h); sa = max(floor, pa * gg)
                    if l <= sa:
                        base = o if (o <= sb and not first_second) else sa
                        return "stop", base, t
                    peak = pa
                first_second = False
        first_second = False
        a = j + 1
    if dl_idx < n:
        return "deadline", float(m1["close"][dl_idx]), int(m1["ts"][dl_idx])
    return "open", float(m1["close"][n - 1]), int(m1["ts"][n - 1])


def main():
    R = []
    for f in glob.glob(SP + "/events2/*.pkl"):
        R += pickle.load(open(f, "rb"))
    P = [r for r in R if not r.get("mismatch") and r["bound"] == "PESS" and r["fixset"] == "orig"]
    key = lambda r, m: r["res"][("V4a", 2.0, 0.1, m)][0]
    A = [r for r in P if abs(key(r, "Pf") - key(r, "Of")) > 1e-9]
    B = [r for r in P if abs(key(r, "Pf") - key(r, "Of")) <= 1e-9]
    A.sort(key=lambda r: -abs(key(r, "Of") - key(r, "Pf")))
    rng = random.Random(2026)
    A1 = A[:60]; A2 = rng.sample(A[60:], 100); Bs = rng.sample(B, 50)
    runners = [r for r in P if ("V5", 2.0, 0.1, "P") in r["res"]]
    k5 = lambda r, m: r["res"][("V5", 2.0, 0.1, m)][0]
    C = [r for r in runners if abs(k5(r, "P") - k5(r, "Of")) > 1e-9]
    D = [r for r in runners if abs(k5(r, "P") - k5(r, "Of")) <= 1e-9]
    C.sort(key=lambda r: -abs(k5(r, "Of") - k5(r, "P")))
    C1 = C[:30]; C2 = rng.sample(C[30:], 60); Ds = rng.sample(D, 60)
    strata = {"A1": (A1, len(A1)), "A2": (A2, len(A) - 60), "B": (Bs, len(B)),
              "C1": (C1, len(C1)), "C2": (C2, len(C) - 30), "D": (Ds, len(D))}
    meta = {k: v[1] for k, v in strata.items()}
    meta["N_PESS"] = len(P); meta["N_runner_events"] = len(runners)
    jobs = defaultdict(list)
    for name, (lst, _) in strata.items():
        for r in lst:
            jobs[r["sym"]].append((name, r))
    out = []
    t0 = time.time()
    for sym in sorted(jobs):
        m1 = load_1m(Path("D:/FINDMY/data/research/market_1m.db"), sym)
        n = len(m1["ts"])
        for name, r in jobs[sym]:
            s = r["s"]; dl = r["dl_idx"]; end = min(dl, n - 1)
            rec = {"stratum": name, "sym": sym, "start": r["start"], "touch_ts": r["touch_ts"], "v0": r["v0"],
                   "m1": {}, "s1": {}}
            if name[0] in "AB":
                cost = r["deployed"] * (1 + FEE)
                for m in ("P", "Pf", "O", "Of"):
                    rec["m1"][("V4a", m)] = r["res"][("V4a", 2.0, 0.1, m)][0]
                for model in ("1sP", "1sO", "T2"):
                    kind, base, ets = walk_1s(sym, m1, s, end, n, dl, r["tp"], r["avg"] * 1.03, 2.0, model, touch_px=r["tp"])
                    slip = 0.001 if kind == "stop" else 0.0
                    rec["s1"][("V4a", model)] = (r["qty"] * base * (1 - slip) * (1 - FEE) - cost, kind, ets)
            else:
                x = r["res"][("V5", 2.0, 0.1, "P")]
                usd, d = x[5], x[6]
                dp = float(m1["open"][d])
                for m in ("P", "O", "Of"):
                    rec["m1"][("V5", m)] = r["res"][("V5", 2.0, 0.1, m)][0]
                q = usd / (dp * 1.001 * (1 + FEE))
                for model in ("1sP", "1sO", "T2"):
                    kind, base, ets = walk_1s(sym, m1, d, end, n, dl, dp, 0.0, 2.0, model, touch_px=None)
                    slip = 0.001 if kind == "stop" else 0.0
                    rec["s1"][("V5", model)] = (r["v0"] + q * base * (1 - slip) * (1 - FEE) - usd, kind, ets)
            out.append(rec)
        print(sym, len(jobs[sym]), "events; requests so far", NREQ[0], f"{time.time()-t0:.0f}s", flush=True)
        pickle.dump({"meta": meta, "mism": MISM, "rows": out}, open(Path(SP) / "walk1s.pkl", "wb"))
    print("done", NREQ[0], "requests", MINS[0], "minutes walked on 1s;", len(MISM), "1s/1m H-L mismatches", MISM[:10])


if __name__ == "__main__":
    main()
