"""Pre-registered HOLDOUT of G4 (daily RSI14 incl. forming bar >= 60) on data AFTER the original window.

Same pipeline as the main study (universe.py / entry_study.py / analyze.py), re-pointed at
data/research/market_holdout.db by patching module constants -- no committed script is edited.
July 2026 (1h from 2026-07-01) is warm-up only; entries are 2026-08-01 .. 2026-10-10.

    python holdout.py build <seed>      # universe + features + simulations -> cache_holdout_<seed>.pkl
    python holdout.py analyze           # both seeds -> results_holdout.json
Pre-registered rule (NOT negotiable): lift_pb(G4) >= +0.15 on BOTH bounds -> keep; <= 0 on EITHER -> drop;
otherwise inconclusive.
"""
from __future__ import annotations

import common  # noqa: F401  (throw-away DB env first)

import json
import multiprocessing as mp
import pickle
import sys
import time

import numpy as np
import pandas as pd

from common import D, H, ROOT, STUDY

HOLDOUT_DB = ROOT / "data" / "research" / "market_holdout.db"
H_START = 1_782_864_000_000          # 2026-07-01T00:00Z first 1h bar
H_END = 1_791_698_400_000            # 2026-10-11T06:00Z last 1h bar
ENTRY_FIRST = 1_785_542_400_000      # 2026-08-01T00:00Z
ENTRY_LAST_EXCL = 1_791_590_400_000 + D  # 2026-10-11T00:00Z (entries up to and incl. 2026-10-10 23:00)
SEEDS = (20261011, 20261012)
RULE_KEEP, RULE_DROP = 0.15, 0.0


def _patch():
    import entry_study as E
    import universe as U
    for m in (E, U):
        m.MARKET_DB = HOLDOUT_DB
        m.WINDOW_START_TS = H_START
        m.WINDOW_END_TS = H_END
    U.ENTRY_FIRST_DAY_TS = ENTRY_FIRST
    U.NH = (H_END - H_START) // H + 1
    return E, U


def _init_h(cfg):
    _patch()
    import entry_study as E
    E._init(cfg)


def build(seed: int, workers: int = 4):
    E, U = _patch()
    common.effective_config()
    cfg = E.make_cfg()
    print("cfg", cfg, flush=True)
    syms, hidx, sidx, hours, days = U.build(seed)
    keep = hidx < (ENTRY_LAST_EXCL - H_START) // H
    hidx, sidx = hidx[keep], sidx[keep]
    print(f"universe entries {len(hidx)} over {len(set((hidx // 24).tolist()))} days", flush=True)
    by_sym: dict[str, list[int]] = {}
    for h, s in zip(hidx.tolist(), sidx.tolist()):
        by_sym.setdefault(str(syms[s]), []).append(h)
    tasks = sorted(by_sym.items(), key=lambda kv: -len(kv[1]))
    out, viol, t0 = [], 0, time.time()
    with mp.get_context("spawn").Pool(workers, initializer=_init_h, initargs=(cfg,)) as pool:
        for i, (name, rows, v) in enumerate(pool.imap_unordered(E.process_symbol, tasks, chunksize=1), 1):
            out.extend(rows)
            viol += v
            if i % 50 == 0 or i == len(tasks):
                print(f"[{i}/{len(tasks)}] rows={len(out)} {time.time()-t0:.0f}s", flush=True)
    print("look-ahead violations:", viol)
    with open(STUDY / f"cache_holdout_{seed}.pkl", "wb") as fh:
        pickle.dump(dict(cfg=cfg, rows=out, violations=viol, seed=seed), fh)


# ----------------------------------------------------------------------------------------------
def _frame(seed: int):
    import analyze as A
    with open(STUDY / f"cache_holdout_{seed}.pkl", "rb") as fh:
        blob = pickle.load(fh)
    df = pd.DataFrame(blob["rows"])
    df["day"] = ((df.ts - ENTRY_FIRST) // D).astype(int)
    df["year"] = 2026
    df["coin"] = pd.factorize(df.sym)[0]
    for b in A.BOUNDS:
        for c in (f"tp_{b}", f"timed_out_{b}", f"trunc_{b}"):
            df[c] = df[c].fillna(False).astype(bool)
    df["rung2_48"] = df["rung2_48"].fillna(False).astype(bool)
    return A.add_gates(df), blob


def _gate_block(P: pd.DataFrame, g: str, ndays: int, rounds: int, seed: int) -> dict:
    import analyze as A
    import stats_ext as SX

    res: dict = {}
    for b in A.BOUNDS:
        row = A.gate_row(P, g, b, ndays, boot_rounds=4000)
        veto = P[g].to_numpy(bool)
        pnl, cap, day = P[f"pnl_usd_{b}"].to_numpy(), P[f"cap_days_{b}"].to_numpy(), P.day.to_numpy()
        if not row.get("unjudgeable"):
            for blk in (7, 14, 30):
                row[f"ci_day{blk}"] = SX.day_block_bootstrap(day, pnl, cap, veto, block=blk, rounds=4000, seed=11)
            lo, hi = row["ci_day14"]["pb"]
            se = (hi - lo) / (2 * 1.96)
            row["se_lift_pb_day14"] = se
            row["mde80_lift_pb"] = 2.8 * se       # 80% power, two-sided 5%
            obs, null = SX.within_day_null(day, pnl, cap, {g: veto}, rounds=rounds, seed=seed + 5)
            raw, _, _ = SX.westfall_young(obs, null)
            row["p_within_day"] = raw[g]
            row["perm_min_detectable_lift_pv"] = float(np.nanpercentile(null[g], 95))
            row["lift_pv_abs"] = obs[g]
        res[b] = row
    return res


def decide(lift_o: float, lift_p: float) -> str:
    if lift_o <= RULE_DROP or lift_p <= RULE_DROP:
        return "drop"
    if lift_o >= RULE_KEEP and lift_p >= RULE_KEEP:
        return "keep"
    return "inconclusive"


def analyze(rounds: int = 5000):
    import analyze as A
    import stats_ext as SX
    from common import MARKET_DB, ro

    out: dict = dict(rule=dict(keep=RULE_KEEP, drop=RULE_DROP), seeds={})
    for seed in SEEDS:
        df, blob = _frame(seed)
        P = df[df.pool_C].copy()
        end_ts = H_END
        P["full60"] = P.ts <= end_ts - 60 * D
        P["resolved"] = ~(P.trunc_o | P.trunc_p)
        subsets = {"a_all": P, "b_resolved": P[P.resolved], "c_full60_horizon": P[P.full60]}
        so: dict = dict(violations=blob["violations"], universe_entries=len(df), pool=len(P),
                        pool_entries_per_day=len(P) / P.day.nunique(), days=int(P.day.nunique()),
                        n_truncated=int(P.trunc_o.sum()), n_truncated_p=int(P.trunc_p.sum()),
                        n_resolved=int(P.resolved.sum()), n_full60=int(P.full60.sum()),
                        n_dead_end=int(P.dead_end.sum()))
        so["subsets"] = {}
        for name, S in subsets.items():
            d = dict(n=len(S), baseline={b: A.metrics(S, b) for b in A.BOUNDS} if len(S) else None)
            if len(S) >= 100:
                nd = max(1, S.day.nunique())
                d["G4"] = _gate_block(S, "G4", nd, rounds, seed)
                d["G2"] = _gate_block(S, "G2", nd, rounds, seed)
                for g in ("G4", "G2"):
                    lo_, lp_ = d[g]["o"].get("lift_pb"), d[g]["p"].get("lift_pb")
                    d[g]["decision_rule"] = decide(lo_, lp_) if lo_ is not None and lp_ is not None else "cannot judge"
            so["subsets"][name] = d
        # EXPLORATORY (outside the pre-registered rule): the pool's single worst coin by total $ (overlapping
        # sessions of one coin are one correlated loss, not many observations).
        worst = P.groupby("sym").pnl_usd_o.sum().idxmin()
        Px = P[P.sym != worst]
        so["exploratory_ex_worst_coin"] = dict(coin=worst, n=len(Px), total_usd_removed_o=float(P[P.sym == worst].pnl_usd_o.sum()),
                                               n_removed=int((P.sym == worst).sum()))
        for g in ("G4", "G2"):
            blk = _gate_block(Px, g, max(1, Px.day.nunique()), 2000, seed)
            so["exploratory_ex_worst_coin"][g] = {bb: {k: blk[bb].get(k) for k in ("lift_pb", "ci_day14", "p_within_day", "n_vetoed")} for bb in blk}
        so["buckets_rsiD"] = A.buckets(P, "rsiD_F", [-1, 40, 50, 60, 70, 101], ["<40", "40-50", "50-60", "60-70", ">=70"])
        so["buckets_pump24"] = A.buckets(P, "pump24", [-1e9, 0, 3, 6, 10, 1e9], ["<0", "0-3", "3-6", "6-10", ">=10"])
        Pr = P[P.resolved]
        so["buckets_rsiD_resolved"] = A.buckets(Pr, "rsiD_F", [-1, 40, 50, 60, 70, 101], ["<40", "40-50", "50-60", "60-70", ">=70"])
        so["median_at_entry"] = dict(rsiD_F=float(P.rsiD_F.median()), pump24=float(P.pump24.median()))
        so["share_G4"] = float(P.G4.mean())
        out["seeds"][str(seed)] = so
    # market context from the holdout DB (BTC)
    import sqlite3
    con = sqlite3.connect(f"file:{HOLDOUT_DB.as_posix()}?mode=ro", uri=True)
    rows = con.execute("select ts, close from candles where interval='1h' and symbol='BTCUSDT' and ts>=? order by ts", (ENTRY_FIRST,)).fetchall()
    px = np.array([r[1] for r in rows])
    peak = np.maximum.accumulate(px)
    out["btc"] = dict(start=float(px[0]), end=float(px[-1]), pct=float((px[-1] / px[0] - 1) * 100),
                      max_drawdown_pct=float(((px / peak) - 1).min() * 100), low=float(px.min()), high=float(px.max()))
    month = {}
    for t, c in rows:
        m = pd.Timestamp(t, unit="ms").strftime("%Y-%m")
        month.setdefault(m, [c, c])[1] = c
    out["btc"]["by_month_pct"] = {m: float((v[1] / v[0] - 1) * 100) for m, v in month.items()}
    con.close()
    with open(STUDY / "results_holdout.json", "w") as fh:
        json.dump(out, fh, indent=1, default=lambda o: None if (isinstance(o, float) and np.isnan(o)) else float(o) if isinstance(o, (np.floating, np.integer)) else str(o))
    print("saved results_holdout.json")


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "build":
        build(int(sys.argv[2]), 4)
    elif cmd == "analyze":
        analyze()
