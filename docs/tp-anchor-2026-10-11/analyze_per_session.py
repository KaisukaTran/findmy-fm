"""Analyse per_session_*.npz: per-variant stats, paired differences, cluster-by-coin bootstrap."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

REASONS = ["tp", "deadline", "data_end"]
VARS = ["avg", "entry", "max"]
RNG = np.random.default_rng(12345)
NBOOT = 4000


def load(path):
    z = np.load(path, allow_pickle=False)
    d = {k: z[k] for k in z.files}
    out = {}
    for v in VARS:
        for b in ("opt", "pess"):
            m = (d["variant"] == v) & (d["bound"] == b)
            out[(v, b)] = {k: d[k][m] for k in ("sym", "ts", "reason", "pnl_usd", "pnl_pct", "days", "waves", "deployed", "capdays")}
    # alignment assert: every combo has the identical (sym, ts) sequence
    ref = out[("avg", "opt")]
    for key, r in out.items():
        assert np.array_equal(r["sym"], ref["sym"]) and np.array_equal(r["ts"], ref["ts"]), key
    return out


def sanity(out):
    res = {}
    for b in ("opt", "pess"):
        A, B, C = out[("avg", b)], out[("entry", b)], out[("max", b)]
        one = A["waves"] == 1
        for f in ("reason", "pnl_usd", "pnl_pct", "days", "waves", "deployed", "capdays"):
            assert np.array_equal(A[f][one], B[f][one]), (b, "k=1 A vs B", f)
        le2 = A["waves"] <= 2
        for f in ("reason", "pnl_usd", "days", "waves"):
            assert np.array_equal(A[f][le2], C[f][le2]), (b, "k<=2 A vs C", f)
        res[b] = {"n_k1": int(one.sum()), "n_k_le2": int(le2.sum())}
    return res


def stats(r):
    n = len(r["pnl_usd"])
    reason = r["reason"]
    d = {"n": int(n)}
    d["tp_rate_pct"] = 100 * float((reason == 0).mean())
    d["mean_pnl_usd"] = float(r["pnl_usd"].mean())
    d["median_pnl_usd"] = float(np.median(r["pnl_usd"]))
    d["mean_days_all"] = float(r["days"].mean())
    d["mean_days_tp"] = float(r["days"][reason == 0].mean()) if (reason == 0).any() else None
    d["capdays_total"] = float(r["capdays"].sum())
    d["usd_per_1000_capday"] = 1000 * float(r["pnl_usd"].sum()) / d["capdays_total"]
    d["mean_deployed"] = float(r["deployed"].mean())
    for i, nm in ((1, "deadline"), (2, "data_end")):
        m = reason == i
        d[f"{nm}_share_pct"] = 100 * float(m.mean())
        d[f"{nm}_mean_pnl_usd"] = float(r["pnl_usd"][m].mean()) if m.any() else None
        d[f"{nm}_mean_pnl_pct"] = float(r["pnl_pct"][m].mean()) if m.any() else None
        d[f"{nm}_sum_pnl_usd"] = float(r["pnl_usd"][m].sum())
    d["tp_sum_pnl_usd"] = float(r["pnl_usd"][reason == 0].sum())
    d["waves_dist_pct"] = {int(k): 100 * float((r["waves"] == k).mean()) for k in range(1, 11)}
    d["waves_mean_pnl_usd"] = {int(k): (float(r["pnl_usd"][r["waves"] == k].mean()) if (r["waves"] == k).any() else None) for k in range(1, 11)}
    d["waves_tp_rate_pct"] = {int(k): (100 * float((reason[r["waves"] == k] == 0).mean()) if (r["waves"] == k).any() else None) for k in range(1, 11)}
    d["waves_mean_days"] = {int(k): (float(r["days"][r["waves"] == k].mean()) if (r["waves"] == k).any() else None) for k in range(1, 11)}
    return d


def cluster_index(sym):
    u, inv = np.unique(sym, return_inverse=True)
    return len(u), inv


def boot_ci(per_coin_num, per_coin_den=None, reps=NBOOT):
    """per-coin sums -> bootstrap CI of sum(num)/sum(den) (or sum(num)/n if den is counts)."""
    nc = len(per_coin_num)
    w = RNG.multinomial(nc, np.ones(nc) / nc, size=reps).astype(float)
    num = w @ per_coin_num
    den = w @ per_coin_den
    est = num / den
    return float(np.percentile(est, 2.5)), float(np.percentile(est, 97.5))


def per_coin_sum(inv, nc, x):
    return np.bincount(inv, weights=x, minlength=nc)


def diff_stats(A, V, inv, nc):
    """Paired V - A. Returns point estimates with cluster-bootstrap 95% CIs."""
    n_coin = per_coin_sum(inv, nc, np.ones(len(inv)))
    out = {}

    def ratio_diff(name, xa, xv, den_a=None, den_v=None, scale=1.0):
        na, nv = per_coin_sum(inv, nc, xa), per_coin_sum(inv, nc, xv)
        da = n_coin if den_a is None else per_coin_sum(inv, nc, den_a)
        dv = n_coin if den_v is None else per_coin_sum(inv, nc, den_v)
        est = scale * (nv.sum() / dv.sum() - na.sum() / da.sum())
        w = RNG.multinomial(nc, np.ones(nc) / nc, size=NBOOT).astype(float)
        bs = scale * ((w @ nv) / (w @ dv) - (w @ na) / (w @ da))
        out[name] = {"diff": float(est), "ci": [float(np.percentile(bs, 2.5)), float(np.percentile(bs, 97.5))]}

    ratio_diff("mean_pnl_usd", A["pnl_usd"], V["pnl_usd"])
    ratio_diff("usd_per_1000_capday", A["pnl_usd"], V["pnl_usd"], A["capdays"], V["capdays"], 1000.0)
    ratio_diff("tp_rate_pct", (A["reason"] == 0).astype(float), (V["reason"] == 0).astype(float), scale=100.0)
    ratio_diff("deadline_share_pct", (A["reason"] == 1).astype(float), (V["reason"] == 1).astype(float), scale=100.0)
    ratio_diff("data_end_share_pct", (A["reason"] == 2).astype(float), (V["reason"] == 2).astype(float), scale=100.0)
    ratio_diff("mean_days_held", A["days"], V["days"])
    ratio_diff("mean_capdays_per_session", A["capdays"], V["capdays"])
    # share of sessions where V is strictly better / worse
    out["share_V_better_pct"] = 100 * float((V["pnl_usd"] > A["pnl_usd"] + 1e-9).mean())
    out["share_V_worse_pct"] = 100 * float((V["pnl_usd"] < A["pnl_usd"] - 1e-9).mean())
    out["share_identical_pct"] = 100 * float((np.abs(V["pnl_usd"] - A["pnl_usd"]) <= 1e-9).mean())
    return out


def run(path, label):
    out = load(path)
    san = sanity(out)
    nc, inv = cluster_index(out[("avg", "opt")]["sym"])
    res = {"label": label, "n_coins": nc, "sanity": san, "stats": {}, "diff": {}, "by_year": {}}
    for b in ("opt", "pess"):
        for v in VARS:
            res["stats"][f"{v}|{b}"] = stats(out[(v, b)])
        for v in ("entry", "max"):
            res["diff"][f"{v}-avg|{b}"] = diff_stats(out[("avg", b)], out[(v, b)], inv, nc)
    # by start year
    ts = out[("avg", "opt")]["ts"]
    yr = np.array([np.datetime64(int(t), "ms").astype("datetime64[Y]").astype(int) + 1970 for t in ts])
    for y in sorted(set(yr.tolist())):
        m = yr == y
        sub = {}
        for b in ("opt", "pess"):
            A = {k: v[m] for k, v in out[("avg", b)].items()}
            for v in ("entry", "max"):
                V = {k: x[m] for k, x in out[(v, b)].items()}
                nc2, inv2 = cluster_index(A["sym"])
                dd = diff_stats(A, V, inv2, nc2)
                sub[f"{v}-avg|{b}"] = {k: dd[k] for k in ("mean_pnl_usd", "usd_per_1000_capday", "tp_rate_pct")}
                sub[f"n|{b}"] = int(m.sum())
        res["by_year"][int(y)] = sub
    return res


if __name__ == "__main__":
    path, label, outp = sys.argv[1], sys.argv[2], sys.argv[3]
    r = run(path, label)
    Path(outp).write_text(json.dumps(r, indent=1))
    print("ok", label, "n_coins", r["n_coins"], "sanity", r["sanity"])
    for b in ("opt", "pess"):
        for v in VARS:
            s = r["stats"][f"{v}|{b}"]
            print(b, v, {k: (round(x, 3) if isinstance(x, float) else x) for k, x in s.items() if not isinstance(x, dict)})
        for v in ("entry", "max"):
            print(b, v, "-avg", json.dumps(r["diff"][f"{v}-avg|{b}"]))
