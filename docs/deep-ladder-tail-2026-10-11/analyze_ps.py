"""Analyse a per-session npz: per-variant stats, paired differences vs A0 with coin-cluster bootstrap CIs,
tail decomposition, D2 give-up accounting, D5 as a start-set filter (age_d >= 180).
    python analyze_ps.py ps_1h_s168.npz label out.json [--no-open-fix]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

RNG = np.random.default_rng(2026)
NBOOT = 3000
VARS = ["A0", "D1", "D2", "D3", "D4"]
REASON_NAMES = {0: "tp", 1: "deadline", 2: "data_end", 3: "deadline_deep", 4: "open_end"}


def load(path):
    z = np.load(path, allow_pickle=False)
    d = {k: z[k] for k in z.files}
    out = {}
    for v in VARS:
        for b in ("opt", "pess"):
            m = (d["variant"] == v) & (d["bound"] == b)
            out[(v, b)] = {k: d[k][m] for k in ("sym", "ts", "age_d", "reason", "pnl_usd", "pnl_pct", "days",
                                                "waves", "deployed", "capdays")}
    ref = out[("A0", "opt")]
    for key, r in out.items():
        assert np.array_equal(r["sym"], ref["sym"]) and np.array_equal(r["ts"], ref["ts"]), key
    return out


def sub(r, m):
    return {k: x[m] for k, x in r.items()}


def stats(r):
    n = len(r["pnl_usd"])
    rs = r["reason"]
    pnl = r["pnl_usd"]
    d = {"n": int(n)}
    d["tp_rate_pct"] = 100 * float((rs == 0).mean())
    d["mean_pnl"] = float(pnl.mean())
    d["median_pnl"] = float(np.median(pnl))
    d["sum_pnl"] = float(pnl.sum())
    d["mean_days"] = float(r["days"].mean())
    d["capdays"] = float(r["capdays"].sum())
    d["usd_per_1000capday"] = 1000 * float(pnl.sum()) / d["capdays"]
    to = (rs == 1) | (rs == 3)
    d["timeout_pct"] = 100 * float(to.mean())
    d["timeout_sum_pnl"] = float(pnl[to].sum())
    d["deadline60_pct"] = 100 * float((rs == 1).mean())
    d["deepexit_pct"] = 100 * float((rs == 3).mean())
    d["open_end_pct"] = 100 * float((rs == 4).mean())
    d["data_end_pct"] = 100 * float((rs == 2).mean())
    w = r["waves"]
    d["fills_dist_pct"] = {int(k): 100 * float((w == k).mean()) for k in range(1, 11)}
    d["fills_mean_pnl"] = {int(k): (float(pnl[w == k].mean()) if (w == k).any() else None) for k in range(1, 11)}
    d["fills_tp_rate"] = {int(k): (100 * float((rs[w == k] == 0).mean()) if (w == k).any() else None) for k in range(1, 11)}
    neg = pnl < 0
    tot_loss = float(pnl[neg].sum())
    deep = w >= 5
    d["total_loss"] = tot_loss
    d["loss_from_deep"] = float(pnl[neg & deep].sum())
    d["loss_share_deep_pct"] = 100 * d["loss_from_deep"] / tot_loss if tot_loss else 0.0
    d["deep_n"] = int(deep.sum())
    d["deep_pct"] = 100 * float(deep.mean())
    d["deep_capday_share_pct"] = 100 * float(r["capdays"][deep].sum()) / d["capdays"]
    d["deep_sum_pnl"] = float(pnl[deep].sum())
    d["deep_mean_pnl"] = float(pnl[deep].mean()) if deep.any() else None
    d["net_ex_deep"] = float(pnl[~deep].sum())
    return d


def cluster(sym):
    u, inv = np.unique(sym, return_inverse=True)
    return len(u), inv


def ratio_diff(inv, nc, xa, xv, da=None, dv=None, scale=1.0):
    ones = np.ones(len(inv))
    s = lambda x: np.bincount(inv, weights=x, minlength=nc)  # noqa: E731
    na, nv = s(xa), s(xv)
    da_ = s(ones if da is None else da)
    dv_ = s(ones if dv is None else dv)
    est = scale * (nv.sum() / dv_.sum() - na.sum() / da_.sum())
    w = RNG.multinomial(nc, np.ones(nc) / nc, size=NBOOT).astype(float)
    bs = scale * ((w @ nv) / (w @ dv_) - (w @ na) / (w @ da_))
    return {"diff": float(est), "ci": [float(np.percentile(bs, 2.5)), float(np.percentile(bs, 97.5))]}


def sum_diff(inv, nc, xa, xv):
    """difference of totals (V - A) per ALL sessions, i.e. $ per start-set, bootstrap over coins."""
    s = lambda x: np.bincount(inv, weights=x, minlength=nc)  # noqa: E731
    d = s(xv) - s(xa)
    w = RNG.multinomial(nc, np.ones(nc) / nc, size=NBOOT).astype(float)
    bs = w @ d
    return {"diff": float(d.sum()), "ci": [float(np.percentile(bs, 2.5)), float(np.percentile(bs, 97.5))]}


def paired(A, V, mask=None):
    """V - A on the same start set (rows aligned). mask optionally restricts rows (D5 uses its own path)."""
    if mask is not None:
        A, V = sub(A, mask), sub(V, mask)
    nc, inv = cluster(A["sym"])
    o = {}
    o["mean_pnl"] = ratio_diff(inv, nc, A["pnl_usd"], V["pnl_usd"])
    o["usd_per_1000capday"] = ratio_diff(inv, nc, A["pnl_usd"], V["pnl_usd"], A["capdays"], V["capdays"], 1000.0)
    o["tp_rate_pct"] = ratio_diff(inv, nc, (A["reason"] == 0) * 1.0, (V["reason"] == 0) * 1.0, scale=100.0)
    o["mean_days"] = ratio_diff(inv, nc, A["days"], V["days"])
    o["capday_per_session"] = ratio_diff(inv, nc, A["capdays"], V["capdays"])
    o["total_pnl"] = sum_diff(inv, nc, A["pnl_usd"], V["pnl_usd"])
    o["share_better_pct"] = 100 * float((V["pnl_usd"] > A["pnl_usd"] + 1e-9).mean())
    o["share_worse_pct"] = 100 * float((V["pnl_usd"] < A["pnl_usd"] - 1e-9).mean())
    o["share_identical_pct"] = 100 * float((np.abs(V["pnl_usd"] - A["pnl_usd"]) <= 1e-9).mean())
    return o


def d5_vs_a0(A, age_min=180.0):
    """D5 = the A0 sessions with coin age >= age_min (same sessions, identical outcomes) vs all A0 sessions."""
    keep = A["age_d"] >= age_min
    nc, inv = cluster(A["sym"])
    k = keep.astype(float)
    o = {"n_keep": int(keep.sum()), "n_all": int(len(keep)), "dropped_pct": 100 * float(1 - keep.mean())}
    o["mean_pnl"] = ratio_diff(inv, nc, A["pnl_usd"], A["pnl_usd"] * k, None, k)
    o["usd_per_1000capday"] = ratio_diff(inv, nc, A["pnl_usd"], A["pnl_usd"] * k, A["capdays"], A["capdays"] * k, 1000.0)
    o["tp_rate_pct"] = ratio_diff(inv, nc, (A["reason"] == 0) * 1.0, (A["reason"] == 0) * k, None, k, 100.0)
    o["capday_per_session"] = ratio_diff(inv, nc, A["capdays"], A["capdays"] * k, None, k)
    return o, stats(sub(A, keep)), stats(sub(A, ~keep))


def run(path, label):
    out = load(path)
    res = {"label": label, "stats": {}, "diff": {}, "d5": {}, "d2_giveup": {}, "by_year": {}}
    ts = out[("A0", "opt")]["ts"]
    yr = np.array([np.datetime64(int(t), "ms").astype("datetime64[Y]").astype(int) + 1970 for t in ts])
    res["n_coins"] = int(len(np.unique(out[("A0", "opt")]["sym"])))
    for b in ("opt", "pess"):
        A = out[("A0", b)]
        for v in VARS:
            res["stats"][f"{v}|{b}"] = stats(out[(v, b)])
        for v in VARS[1:]:
            res["diff"][f"{v}|{b}"] = paired(A, out[(v, b)])
            res["by_year"][f"{v}|{b}"] = {}
            for y in sorted(set(yr.tolist())):
                m = yr == y
                if m.sum() > 50:
                    dd = paired(A, out[(v, b)], m)
                    res["by_year"][f"{v}|{b}"][int(y)] = {"n": int(m.sum()), "mean_pnl": dd["mean_pnl"],
                                                          "usd_per_1000capday": dd["usd_per_1000capday"]}
        d5, keep_s, drop_s = d5_vs_a0(A)
        res["d5"][b] = {"diff": d5, "keep": keep_s, "drop": drop_s}
        res["by_year"][f"D5|{b}"] = {}
        for y in sorted(set(yr.tolist())):
            m = yr == y
            if m.sum() > 50:
                dd, _, _ = d5_vs_a0(sub(A, m))
                res["by_year"][f"D5|{b}"][int(y)] = {"n": int(m.sum()), "mean_pnl": dd["mean_pnl"],
                                                     "usd_per_1000capday": dd["usd_per_1000capday"]}
        # D2 / D3 shallow-identity assertion + give-up accounting
        deep = A["waves"] >= 5
        for v in ("D2", "D3"):
            V = out[(v, b)]
            for f in ("reason", "pnl_usd", "days", "waves", "capdays", "deployed"):
                assert np.array_equal(A[f][~deep], V[f][~deep]), (v, b, f, "shallow sessions differ from A0")
        # deep A0 sessions: split by A0's outcome
        for v in ("D2", "D3"):
            V = out[(v, b)]
            a_tp = deep & (A["reason"] == 0)
            a_bad = deep & (A["reason"] != 0)
            g = {"deep_n": int(deep.sum()),
                 "A0_tp_n": int(a_tp.sum()),
                 "A0_not_tp_n": int(a_bad.sum()),
                 "giveup_on_A0_tp_usd": float((A["pnl_usd"][a_tp] - V["pnl_usd"][a_tp]).sum()),
                 "gain_on_A0_not_tp_usd": float((V["pnl_usd"][a_bad] - A["pnl_usd"][a_bad]).sum()),
                 "net_usd": float((V["pnl_usd"][deep] - A["pnl_usd"][deep]).sum()),
                 "A0_deep_sum": float(A["pnl_usd"][deep].sum()),
                 "V_deep_sum": float(V["pnl_usd"][deep].sum()),
                 "V_exit_reason_on_A0_tp": {REASON_NAMES[k]: int((V["reason"][a_tp] == k).sum()) for k in (0, 1, 2, 3, 4)},
                 "V_exit_reason_on_A0_not_tp": {REASON_NAMES[k]: int((V["reason"][a_bad] == k).sum()) for k in (0, 1, 2, 3, 4)},
                 "A0_tp_extra_days_mean": float((A["days"][a_tp] - V["days"][a_tp]).mean()) if a_tp.any() else None,
                 "capday_saved_total": float((A["capdays"][deep] - V["capdays"][deep]).sum()),
                 "A0_deep_capdays": float(A["capdays"][deep].sum())}
            # of A0's deep TP sessions how many hit TP earlier under D2 (just the same, at lower TP)
            res["d2_giveup"][f"{v}|{b}"] = g
    return res


if __name__ == "__main__":
    path, label, outp = sys.argv[1], sys.argv[2], sys.argv[3]
    r = run(path, label)
    Path(outp).write_text(json.dumps(r, indent=1))
    print("ok", label, "coins", r["n_coins"])
