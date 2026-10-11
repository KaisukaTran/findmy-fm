"""Stage 2: gate table, permutation tests, buckets, robustness -> results_<seed>.json.

    python analyze.py <seed> [perm_rounds=3000] [--no-perm]

Pre-registered gates (veto when TRUE; inputs strictly causal, see entry_study.py):
  G1 pump24h >= 10 %          G2 pump24h >= 6 %          G3 pump7d >= 15 %
  G4 daily RSI14 >= 60 (last closed daily + forming bar at the entry price)
  G5 1h RSI14 >= 70           G6 BTC 24h return <= -2 %
  G7 the shipped rel-strength veto as coded (7 daily bars vs BTC, margin 2 %), closed-bar variant
  G8 pump24h < 0 %  -- inverse SANITY row, outside the FWER family
Family for FWER = G1..G7.
"""
from __future__ import annotations

import common  # noqa: F401

import json
import pickle
import sys
import time

import numpy as np
import pandas as pd

import stats_ext as SX
from common import D, STUDY, WINDOW_START_TS

FAMILY = ["G1", "G2", "G3", "G4", "G5", "G6", "G7"]
SANITY = ["G8"]
GATE_DESC = {
    "G1": "pump24h >= 10%", "G2": "pump24h >= 6%", "G3": "pump7d >= 15%",
    "G4": "daily RSI14 >= 60 (forming bar)", "G5": "1h RSI14 >= 70", "G6": "BTC 24h ret <= -2%",
    "G7": "shipped rel-strength veto (daily, closed)", "G8": "SANITY: pump24h < 0%",
}
BOUNDS = {"o": "optimistic", "p": "pessimistic"}
YEARS = {"2024": (2024,), "2025": (2025,), "2026Jan-Jul": (2026,)}
BLOCK_MAIN, BLOCK_ALT = 30, 7


def load(seed: int) -> tuple[pd.DataFrame, dict]:
    with open(STUDY / f"cache_entries_{seed}.pkl", "rb") as fh:
        blob = pickle.load(fh)
    df = pd.DataFrame(blob["rows"])
    df["day"] = ((df.ts - WINDOW_START_TS) // D).astype(int)
    df["year"] = pd.to_datetime(df.ts, unit="ms").dt.year
    df["coin"] = pd.factorize(df.sym)[0]
    for b in BOUNDS:
        for c in (f"tp_{b}", f"timed_out_{b}", f"trunc_{b}"):
            df[c] = df[c].fillna(False).astype(bool)
    df["rung2_48"] = df["rung2_48"].fillna(False).astype(bool)
    return df, blob


def add_gates(df: pd.DataFrame) -> pd.DataFrame:
    def ge(col, x):
        return (df[col] >= x).fillna(False)

    df["G1"] = ge("pump24", 10.0)
    df["G2"] = ge("pump24", 6.0)
    df["G3"] = ge("pump7d", 15.0)
    df["G4"] = ge("rsiD_F", 60.0)
    df["G5"] = ge("rsi1h", 70.0)
    df["G6"] = (df["btc_ret24"] <= -2.0).fillna(False)
    df["G7"] = df["rs_C"].astype(bool)
    df["G7F"] = df["rs_F"].astype(bool)          # variant: forming-bar daily context
    df["G8"] = (df["pump24"] < 0.0).fillna(False)
    return df


# ------------------------------------------------------------------------------------------
def metrics(d: pd.DataFrame, b: str) -> dict:
    n = len(d)
    if n == 0:
        return {"n": 0}
    pnl, cap = d[f"pnl_usd_{b}"].to_numpy(), d[f"cap_days_{b}"].to_numpy()
    to = d[f"timed_out_{b}"].to_numpy()
    tp72 = (d[f"tp_{b}"] & (d[f"days_to_tp_{b}"] <= 3.0)).to_numpy()
    return dict(
        n=n, ppdd=SX.ppdd(pnl, cap), mean_usd=float(pnl.mean()), p_rung2_48=float(d.rung2_48.mean()),
        p_tp_72=float(tp72.mean()), p_good=float((tp72 & ~d.rung2_48.to_numpy()).mean()),
        timeout_share=float(to.mean()),
        timeout_mean_usd=float(pnl[to].mean()) if to.any() else None,
        timeout_total_usd=float(pnl[to].sum()), trunc_share=float(d[f"trunc_{b}"].mean()),
        days_held=float(d[f"days_held_{b}"].mean()), mae_mean=float(d[f"mae_{b}"].mean()),
        mae_worst=float(d[f"mae_{b}"].min()), tp_share=float(d[f"tp_{b}"].mean()),
        total_usd=float(pnl.sum()), cap_days=float(cap.sum()),
    )


def gate_row(d: pd.DataFrame, g: str, b: str, ndays: int, *, boot_rounds: int = 2000) -> dict:
    veto = d[g].to_numpy(bool)
    pnl, cap = d[f"pnl_usd_{b}"].to_numpy(), d[f"cap_days_{b}"].to_numpy()
    day, coin = d.day.to_numpy(), d.coin.to_numpy()
    row = dict(gate=g, desc=GATE_DESC.get(g, g), bound=BOUNDS[b], n_pool=len(d),
               n_vetoed=int(veto.sum()), n_passed=int((~veto).sum()),
               pct_vetoed=float(veto.mean() * 100), removed_per_day=float(veto.sum() / ndays),
               baseline=metrics(d, b), vetoed=metrics(d[veto], b), passed=metrics(d[~veto], b))
    if veto.sum() >= 30 and (~veto).sum() >= 30:
        pb, pv = SX.lifts(pnl, cap, veto)
        row.update(lift_pb=pb, lift_pv=pv,
                   ci_coin=SX.coin_bootstrap(coin, pnl, cap, veto, rounds=boot_rounds),
                   ci_day30=SX.day_block_bootstrap(day, pnl, cap, veto, block=BLOCK_MAIN, rounds=boot_rounds),
                   ci_day7=SX.day_block_bootstrap(day, pnl, cap, veto, block=BLOCK_ALT, rounds=boot_rounds))
        yr = {}
        for name, ys in YEARS.items():
            m = d.year.isin(ys).to_numpy()
            v = veto[m]
            if v.sum() >= 30 and (~v).sum() >= 30:
                a, c = SX.lifts(pnl[m], cap[m], v)
                yr[name] = dict(lift_pb=a, lift_pv=c, n_vetoed=int(v.sum()), n_passed=int((~v).sum()))
            else:
                yr[name] = None
        row["by_year"] = yr
    else:
        row["unjudgeable"] = True
    return row


# ------------------------------------------------------------------------------------------
def within_day_perm(d: pd.DataFrame, b: str, gates: list[str], rounds: int, seed: int):
    """Original tool's cluster_permutation_pvalues (labels shuffled WITHIN each entry day)."""
    import measure_entry_gates as M

    keys, outs, vet = [], {}, {}
    for r in d.itertuples(index=False):
        k = (r.sym, int(r.hour_idx))
        keys.append(k)
        outs[k] = M.EntryOutcome(symbol=r.sym, index=int(r.hour_idx), ts=int(r.ts),
                                 pnl_usd=getattr(r, f"pnl_usd_{b}"), capital_days=getattr(r, f"cap_days_{b}"),
                                 pnl_pct=getattr(r, f"pnl_pct_{b}"), stopped=False, tp_hit=bool(getattr(r, f"tp_{b}")))
        vet[k] = {g: bool(getattr(r, g)) for g in gates}
    raw, adj, det = M.cluster_permutation_pvalues(keys, outs, vet, gates, rounds=rounds, seed=seed)
    # original reports |lift| in $/$-day; convert to $ per $1000-capital-day
    return raw, adj, {g: v * 1000 for g, v in det.items()}


def shift_perm(d: pd.DataFrame, b: str, gates: list[str], seed: int):
    vet = {g: d[g].to_numpy(bool) for g in gates}
    raw, adj, det, n = SX.day_shift_permutation(d.coin.to_numpy(), d.day.to_numpy(),
                                                d[f"pnl_usd_{b}"].to_numpy(), d[f"cap_days_{b}"].to_numpy(),
                                                vet, min_shift=30, seed=seed)
    return raw, adj, det, n


# ------------------------------------------------------------------------------------------
def buckets(d: pd.DataFrame, col: str, edges: list[float], labels: list[str]) -> list[dict]:
    cut = pd.cut(d[col], bins=edges, labels=labels, right=False)
    out = []
    for lab in labels:
        s = d[cut == lab]
        row = dict(bucket=lab, n=len(s), share=len(s) / len(d))
        for b in BOUNDS:
            m = metrics(s, b)
            row[b] = {k: m.get(k) for k in ("ppdd", "mean_usd", "p_rung2_48", "p_tp_72", "p_good", "timeout_share")}
        out.append(row)
    return out


def btc_forward(d: pd.DataFrame) -> np.ndarray:
    """BTC return over the NEXT 48h after entry (EXPLORATORY diagnostic only -- uses the future on
    purpose, never feeds a gate)."""
    from entry_study import Sym
    from common import MARKET_DB, ro, H

    con = ro(MARKET_DB)
    B = Sym(con, "BTCUSDT")
    con.close()
    import bisect
    out = np.full(len(d), np.nan)
    for i, t in enumerate(d.ts.to_numpy()):
        a = bisect.bisect_right(B.ts, t) - 1
        z = bisect.bisect_right(B.ts, t + 48 * H) - 1
        if a >= 0 and z > a and B.ts[z] == t + 48 * H:
            out[i] = (B.c[z] / B.c[a] - 1) * 100
    return out


# ------------------------------------------------------------------------------------------
def verdict(row_o: dict, row_p: dict, pw: dict) -> str:
    """Evidence tiers. 'FWER' = best of the studentised max-T schemes that have power for the gate
    (within-day, family G1..G7 minus zero-power gates; day-shift, family G1..G7)."""
    if row_o.get("unjudgeable") or row_p.get("unjudgeable"):
        return "cannot judge"
    so, sp = np.sign(row_o["lift_pb"]), np.sign(row_p["lift_pb"])
    if so != sp:
        return "undecided (bounds disagree in sign)"
    direction = "helps" if so > 0 else "hurts"
    day_ex = all((r["ci_day30"]["pb"][0] > 0) if so > 0 else (r["ci_day30"]["pb"][1] < 0) for r in (row_o, row_p))
    yrs = [np.sign(y["lift_pb"]) for r in (row_o, row_p) for y in r["by_year"].values() if y is not None]
    same_years = len(yrs) > 0 and all(y == so for y in yrs)

    def best(b):
        vals = [pw.get(f"fwer_within_stud_x6_{b}"), pw.get(f"fwer_shift_stud_{b}")]
        vals = [v for v in vals if v is not None]
        return min(vals) if vals else 1.0
    fw_o, fw_p = best("o"), best("p")
    fw_both = fw_o < 0.05 and fw_p < 0.05
    if fw_both and day_ex and same_years:
        return f"{direction}: FWER<0.05 both bounds + CI + all years"
    if fw_both and day_ex:
        return f"{direction}: FWER<0.05 + CI but year signs differ"
    if fw_both:
        return f"{direction}: FWER<0.05 but day-block CI includes 0"
    if day_ex:
        return f"suggestive {direction}: day-block CI excludes 0, FWER (o/p) = {fw_o:.2f}/{fw_p:.2f}"
    return "no evidence (CI includes 0)"


def main():
    seed = int(sys.argv[1]) if len(sys.argv) > 1 else 20261011
    rounds = int(sys.argv[2]) if len(sys.argv) > 2 and not sys.argv[2].startswith("--") else 3000
    do_perm = "--no-perm" not in sys.argv
    t0 = time.time()
    df, blob = load(seed)
    df = add_gates(df)
    ndays = df.day.nunique()
    res: dict = dict(seed=seed, cfg=blob["cfg"], violations=blob["violations"], n_universe_entries=len(df),
                     n_days=int(ndays), gates=GATE_DESC, perm_rounds=rounds)
    res["nan_counts"] = {c: int(df[c].isna().sum()) for c in ("pump24", "pump7d", "rsi1h", "rsiD_F", "btc_ret24")}
    res["pool_sizes"] = dict(universe=len(df), ok_C=int(df.ok_C.sum()),
                             pool_C=int(df.pool_C.sum()), pool_F=int(df.pool_F.sum()),
                             both=int((df.pool_C & df.pool_F).sum()),
                             dt_C=int((df.ok_C & df.dt_C).sum()), fk_C=int((df.ok_C & df.fk_C).sum()),
                             cons_C=int((df.ok_C & df.cons_C).sum()), bev_C=int((df.ok_C & df.bev_C).sum()))
    P = df[df.pool_C].copy()
    res["pool_days"] = int(P.day.nunique())
    print(f"pool_C={len(P)} of {len(df)}; days={res['pool_days']}", flush=True)

    # --- baseline & gate table, both bounds
    tab: dict = {b: {} for b in BOUNDS}
    for b in BOUNDS:
        res.setdefault("baseline", {})[b] = metrics(P, b)
        for g in FAMILY + SANITY + ["G7F"]:
            tab[b][g] = gate_row(P, g, b, res["pool_days"])
    # --- permutation tests (primary pool only)
    perms: dict = {g: {} for g in FAMILY + SANITY}
    if do_perm:
        for b in BOUNDS:
            judge_f = [g for g in FAMILY if not tab[b][g].get("unjudgeable")]
            judge_s = [g for g in SANITY if not tab[b][g].get("unjudgeable")]
            judge = judge_f + judge_s
            # (1) the original tool, untouched (continuity with 2026-09-10): raw-unit max-T, family G1..G7
            raw, adj, det = within_day_perm(P, b, judge_f, rounds, seed)
            for g in judge_f:
                perms[g].update({f"orig_p_within_{b}": raw.get(g), f"orig_fwer_within_{b}": adj.get(g),
                                 f"orig_minlift_within_{b}": det.get(g)})
            # (2) within-day null, own implementation (returns the draws -> studentised variants)
            vet = {g: P[g].to_numpy(bool) for g in judge}
            obs_w, null_w = SX.within_day_null(P.day.to_numpy(), P[f"pnl_usd_{b}"].to_numpy(),
                                               P[f"cap_days_{b}"].to_numpy(), vet, rounds=rounds, seed=seed + 1)
            # (3) coin-wise circular day-shift null (timing)
            obs_s, null_s, nshift = SX.day_shift_null(P.coin.to_numpy(), P.day.to_numpy(),
                                                      P[f"pnl_usd_{b}"].to_numpy(), P[f"cap_days_{b}"].to_numpy(),
                                                      vet, min_shift=30, seed=seed + 2)
            res["n_shifts"] = nshift
            for tag, obs, null in (("within", obs_w, null_w), ("shift", obs_s, null_s)):
                rawf, adjf, detf = SX.westfall_young({g: obs[g] for g in judge_f}, {g: null[g] for g in judge_f})
                rawS, adjS, detS, dropS = SX.studentized_wy(obs, null, judge_f)
                rawX, adjX, detX, dropX = SX.studentized_wy(obs, null, [g for g in judge_f if g != "G6"])
                san = SX.studentized_wy(obs, null, judge_s)
                for g in judge_f:
                    perms[g].update({
                        f"p_{tag}_{b}": rawf.get(g),                       # raw-unit raw p
                        f"fwer_{tag}_raw_{b}": adjf.get(g),                # raw-unit max-T, G1..G7 (pre-registered family)
                        f"minlift_{tag}_raw_{b}": detf.get(g),
                        f"fwer_{tag}_stud_{b}": adjS.get(g),               # studentised max-T, G1..G7 (zero-power gates dropped)
                        f"fwer_{tag}_stud_x6_{b}": adjX.get(g),            # studentised, G6 excluded
                        f"minlift_{tag}_stud_{b}": detS.get(g),
                    })
                    perms[g][f"dropped_{tag}_{b}"] = dropS
                for g in judge_s:
                    perms[g].update({f"p_{tag}_{b}": san[0].get(g), f"fwer_{tag}_raw_{b}": san[0].get(g),
                                     f"fwer_{tag}_stud_{b}": san[1].get(g), f"fwer_{tag}_stud_x6_{b}": san[1].get(g),
                                     f"minlift_{tag}_stud_{b}": san[2].get(g)})
            print(f"perm {b} done {time.time()-t0:.0f}s", flush=True)
    res["table"] = tab
    res["perms"] = perms
    res["verdict"] = {g: verdict(tab["o"][g], tab["p"][g], perms[g]) for g in FAMILY + SANITY}

    # --- buckets (pool_C)
    res["buckets_pump24"] = buckets(P, "pump24", [-1e9, 0, 3, 6, 10, 1e9], ["<0", "0-3", "3-6", "6-10", ">=10"])
    res["buckets_rsiD"] = buckets(P, "rsiD_F", [-1, 40, 50, 60, 70, 101], ["<40", "40-50", "50-60", "60-70", ">=70"])
    res["buckets_rsiD_closed"] = buckets(P, "rsiD_C", [-1, 40, 50, 60, 70, 101], ["<40", "40-50", "50-60", "60-70", ">=70"])
    res["buckets_pump24_by_year"] = {
        y: buckets(P[P.year.isin(ys)], "pump24", [-1e9, 0, 3, 6, 10, 1e9], ["<0", "0-3", "3-6", "6-10", ">=10"])
        for y, ys in YEARS.items()}
    res["buckets_rsiD_by_year"] = {
        y: buckets(P[P.year.isin(ys)], "rsiD_F", [-1, 40, 50, 60, 70, 101], ["<40", "40-50", "50-60", "60-70", ">=70"])
        for y, ys in YEARS.items()}
    # distribution at open (paper comparison)
    res["median_at_entry"] = dict(rsiD_F=float(P.rsiD_F.median()), pump24=float(P.pump24.median()),
                                  pump7d=float(P.pump7d.median()))

    # --- robustness: lifts + CI only (no permutation), several pools
    def lifts_only(sub: pd.DataFrame, gates=FAMILY + SANITY):
        out = {}
        nd = max(1, sub.day.nunique())
        for b in BOUNDS:
            out[b] = {}
            for g in gates:
                r = gate_row(sub, g, b, nd, boot_rounds=1000)
                out[b][g] = {k: r.get(k) for k in ("n_vetoed", "n_passed", "lift_pb", "lift_pv", "ci_day30")}
                out[b][g]["baseline_ppdd"] = r["baseline"].get("ppdd")
        return out
    rob = {}
    PF = df[df.pool_F].copy()
    PF["G7"] = PF["G7F"]
    rob["pool_F_forming_bar"] = lifts_only(PF)
    rob["pool_F_forming_bar"]["n"] = len(PF)
    P7 = P[((P.day + P.coin) % 7) == 0]
    rob["sparse_1in7_per_coin"] = lifts_only(P7)
    rob["sparse_1in7_per_coin"]["n"] = len(P7)
    cut = pd.Timestamp("2026-06-01").value // 10**6
    PT = P[P.ts <= cut]
    rob["horizon_complete_ts_le_2026-06-01"] = lifts_only(PT)
    rob["horizon_complete_ts_le_2026-06-01"]["n"] = len(PT)
    PG7F = P.copy()
    PG7F["G7"] = PG7F["G7F"]
    rob["G7_with_forming_daily_context"] = lifts_only(PG7F, ["G7"])
    rob["G7_with_forming_daily_context"]["n"] = len(PG7F)
    ex = P[~P.sym.isin(["BTCUSDT", "ETHUSDT"])]
    rob["ex_BTC_ETH"] = lifts_only(ex)
    rob["ex_BTC_ETH"]["n"] = len(ex)
    res["robustness"] = rob

    # --- EXPLORATORY: how much of "fell" is the market? (uses BTC's FUTURE on purpose)
    P["btc_fwd48"] = btc_forward(P)
    terc = pd.qcut(P["btc_fwd48"], 3, labels=["BTC48h down", "flat", "up"])
    expl = []
    for lab in ["BTC48h down", "flat", "up"]:
        s = P[terc == lab]
        row = dict(btc_fwd_tercile=lab, n=len(s), p_rung2_48=float(s.rung2_48.mean()),
                   btc_fwd_range=[float(s.btc_fwd48.min()), float(s.btc_fwd48.max())], by_pump24=[])
        for blab, (lo, hi) in {"<0": (-1e9, 0), "0-6": (0, 6), ">=6": (6, 1e9)}.items():
            q = s[(s.pump24 >= lo) & (s.pump24 < hi)]
            row["by_pump24"].append(dict(bucket=blab, n=len(q), p_rung2_48=float(q.rung2_48.mean()) if len(q) else None))
        expl.append(row)
    res["exploratory_btc_forward_terciles"] = expl
    out = STUDY / f"results_{seed}.json"
    with open(out, "w") as fh:
        json.dump(res, fh, indent=1, default=lambda o: None if (isinstance(o, float) and np.isnan(o)) else float(o) if isinstance(o, (np.floating, np.integer)) else str(o))
    print("saved", out, f"{time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
