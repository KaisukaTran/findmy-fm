import json
import math
import random
import statistics as st

S = r"D:/FINDMY/docs/capital-hourly-2026-09-29/"
big = json.load(open(S + "verify_1h.json"))
main = json.load(open(r"D:/FINDMY/docs/capital-hourly-2026-09-29/results_main.json"))["rows"]["1h"]

# unify: key (cell, n, bound, seed, mode)
rows = {}
for k, v in big.items():
    c, n, b, s, m = k.split("|")
    rows[(c, int(n), b, int(s), m)] = v
for k, v in main.items():
    _, c, n, b, s = k.split("|")
    rows.setdefault((c, int(n[1:]), b, int(s[1:]), "old"), v)


def boot(d, n=10000, seed=0):
    rng = random.Random(seed)
    ms = sorted(sum(d[rng.randrange(len(d))] for _ in d) / len(d) for _ in range(n))
    return ms[int(0.025 * (n - 1))], ms[int(0.975 * (n - 1))]


TT = {9: 2.262, 19: 2.093, 29: 2.045}


def fmt_diff(d):
    m = st.mean(d)
    lo, hi = boot(d)
    h = TT.get(len(d) - 1, 2.0) * st.stdev(d) / math.sqrt(len(d))
    return f"{m:+.1f} boot[{lo:+.1f},{hi:+.1f}] t[{m-h:+.1f},{m+h:+.1f}] neg {sum(x<0 for x in d)}/{len(d)}"


def med(xs):
    xs = sorted(xs)
    k = len(xs)
    return xs[k // 2] if k % 2 else (xs[k // 2 - 1] + xs[k // 2]) / 2


def cellsum(c, n, b, m, seeds):
    g = [rows[(c, n, b, s, m)] for s in seeds if (c, n, b, s, m) in rows]
    if not g:
        return None
    return (len(g), med([r["cagr_own_pct"] for r in g]), med([r["max_drawdown_unit_nav_pct"] for r in g]),
            100 * sum(r["ended_below_start_capital"] for r in g) / len(g),
            med([r["starved_rungs_distinct"] for r in g]), med([r["deadline_losses"] for r in g]),
            med([r["deadline_losses_usd"] for r in g]), med([r["y2025.return_pct"] for r in g]))


def paired(c1, c2, n, b, m, seeds, key="cagr_own_pct"):
    d = [rows[(c1, n, b, s, m)][key] - rows[(c2, n, b, s, m)][key] for s in seeds
         if (c1, n, b, s, m) in rows and (c2, n, b, s, m) in rows]
    return (len(d), fmt_diff(d)) if len(d) > 2 else None


for m in ("old", "fix"):
    print(f"\n######## mode={m}")
    for n in (40, 5):
        for c in ("cov1_floor20", "cov1_floor0", "cov30_floor20", "cov30_floor0"):
            for b in ("pess", "opt"):
                for seeds in (range(10), range(20)):
                    r = cellsum(c, n, b, m, seeds)
                    if r and (seeds == range(10) or r[0] == 20):
                        print(f"{c:14s} n{n:<3d}{b:5s} N={r[0]:2d} CAGR {r[1]:+7.2f} DD {r[2]:6.2f} below {r[3]:5.1f}% "
                              f"starved {r[4]:6.0f} dl-loss {r[5]:4.0f} / {r[6]:9.0f}  y2025 {r[7]:+.1f}")
        for b in ("pess", "opt"):
            for seeds in (range(10), range(10, 20), range(20)):
                p = paired("cov1_floor20", "cov30_floor20", n, b, m, seeds)
                if p:
                    print(f"  cov1-cov30 floor20 n{n} {b} seeds {seeds.start}-{seeds.stop-1}: N={p[0]} {p[1]}")
                    p2 = paired("cov1_floor20", "cov30_floor20", n, b, m, seeds, "max_drawdown_unit_nav_pct")
                    print(f"      DD diff: {p2[1]}")
                    for y in ("y2024.return_pct", "y2025.return_pct", "y2026.return_pct"):
                        py = paired("cov1_floor20", "cov30_floor20", n, b, m, seeds, y)
                        print(f"      {y}: {py[1]}")
            for seeds in (range(10),):
                p = paired("cov1_floor0", "cov1_floor20", n, b, m, seeds)
                if p:
                    print(f"  floor0-floor20 @cov1 n{n} {b}: N={p[0]} {p[1]}")
                p = paired("cov30_floor0", "cov30_floor20", n, b, m, seeds)
                if p:
                    print(f"  floor0-floor20 @cov30 n{n} {b}: N={p[0]} {p[1]}")
                p = paired("cov1_floor0", "cov30_floor0", n, b, m, seeds)
                if p:
                    print(f"  cov1-cov30 floor0 n{n} {b}: N={p[0]} {p[1]}")
    # bound gap
    for n in (40, 5):
        for c in ("cov1_floor20", "cov30_floor20"):
            p = [rows[(c, n, "opt", s, m)]["cagr_own_pct"] - rows[(c, n, "pess", s, m)]["cagr_own_pct"]
                 for s in range(20) if (c, n, "opt", s, m) in rows and (c, n, "pess", s, m) in rows]
            if len(p) > 2:
                print(f"  bound gap opt-pess {c} n{n}: N={len(p)} {fmt_diff(p)}")
# old vs fix, same seed
print("\n#### fix - old (warmup) same seed")
for c in ("cov1_floor20", "cov30_floor20"):
    for b in ("pess", "opt"):
        d = [rows[(c, 40, b, s, "fix")]["cagr_own_pct"] - rows[(c, 40, b, s, "old")]["cagr_own_pct"]
             for s in range(20) if (c, 40, b, s, "fix") in rows and (c, 40, b, s, "old") in rows]
        if len(d) > 2:
            print(f"  {c} n40 {b}: N={len(d)} {fmt_diff(d)}")
