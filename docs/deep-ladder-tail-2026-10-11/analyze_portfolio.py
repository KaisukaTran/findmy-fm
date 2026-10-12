"""Aggregate port_*.json: per-variant medians/means across seeds, paired seed differences vs A0 with bootstrap 95% CI
(and a Bonferroni 99% CI over the D1..D5 family), count of seeds negative, per-year signs, verdict logic.
    python analyze_portfolio.py out_prefix glob1 [glob2 ...]       e.g.  results "port_w*.json"
"""
from __future__ import annotations

import glob
import json
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
RNG = np.random.default_rng(4242)
VARS = ["A0", "D1", "D2", "D3", "D4", "D5"]
METRICS = [
    ("cagr_own_pct", "CAGR %"), ("total_return_pct", "total return %"), ("final_nav_own", "end equity $"),
    ("realized_usd", "realized $"), ("max_drawdown_unit_nav_pct", "max DD %"),
    ("sessions_closed", "sessions closed"), ("sessions_opened", "sessions opened"),
    ("util_avg_pct", "avg util %"), ("rungs_starved", "starved rung-retries"),
    ("starved_rungs_distinct", "starved rungs (distinct)"),
    ("sessions_still_open_at_end", "stuck at end"), ("open_at_end_unrealized_usd", "stuck MTM $"),
    ("n_tp", "TP exits"), ("n_deadline", "60d deadline exits"), ("usd_deadline", "60d deadline $"),
    ("n_deadline_deep", "D3 deep exits"), ("usd_deadline_deep", "D3 deep exit $"),
    ("usd_tp", "TP $"), ("mean_days_all", "mean days held"), ("mean_waves_closed", "mean fills at exit"),
    ("closed_loss_usd", "closed losses $"), ("closed_loss_deep_usd", "of which >=5 fills $"),
    ("n_deep_closed", "closed >=5 fills (n)"), ("pnl_deep_closed", "pnl of >=5-fill closed $"),
    ("open_end_deep_n", "stuck >=5 fills (n)"), ("open_end_deep_mtm", "stuck >=5 fills MTM $"),
    ("y2024.return_pct", "2024 return %"), ("y2025.return_pct", "2025 return %"),
    ("y2026.return_pct", "2026 return %"),
]


def load(globs):
    rows = {}
    for g in globs:
        for f in sorted(glob.glob(str(HERE / g))):
            rows.update(json.loads(Path(f).read_text()))
    return rows


def boot(d, lo=2.5, hi=97.5, reps=20000):
    d = np.asarray(d, float)
    idx = RNG.integers(0, len(d), size=(reps, len(d)))
    m = d[idx].mean(axis=1)
    return float(np.percentile(m, lo)), float(np.percentile(m, hi))


def analyse(rows, cfgname):
    out = {"stats": {}, "diff": {}}
    for bound in ("pess", "opt"):
        seeds = sorted({int(k.split("|")[3]) for k in rows if k.startswith(f"{cfgname}|") and k.split("|")[2] == bound})
        full = [s for s in seeds if all(f"{cfgname}|{v}|{bound}|{s}" in rows for v in VARS)]
        out["seeds_" + bound] = full
        if not full:
            continue
        for v in VARS:
            r = [rows[f"{cfgname}|{v}|{bound}|{s}"] for s in full]
            st = {}
            for m, _ in METRICS:
                vals = [x.get(m) for x in r]
                if all(x is not None for x in vals):
                    st[m] = {"median": float(np.median(vals)), "mean": float(np.mean(vals))}
            st["ended_below_start_pct"] = 100 * float(np.mean([x["ended_below_start_capital"] for x in r]))
            out["stats"][f"{v}|{bound}"] = st
        for v in VARS[1:]:
            dd = {}
            for m, _ in METRICS:
                try:
                    d = [rows[f"{cfgname}|{v}|{bound}|{s}"][m] - rows[f"{cfgname}|A0|{bound}|{s}"][m] for s in full]
                except (TypeError, KeyError):
                    continue
                lo, hi = boot(d)
                flo, fhi = boot(d, 0.5, 99.5)
                dd[m] = {"mean": float(np.mean(d)), "median": float(np.median(d)), "ci": [lo, hi], "ci_fw": [flo, fhi],
                         "neg": int(sum(1 for x in d if x < 0)), "pos": int(sum(1 for x in d if x > 0)), "n": len(d)}
            out["diff"][f"{v}|{bound}"] = dd
    return out


def verdict(res, v, metric="cagr_own_pct", good_sign=1):
    """per (cfg, variant): combine the two bounds."""
    s = {}
    for b in ("pess", "opt"):
        d = res["diff"].get(f"{v}|{b}", {}).get(metric)
        if d is None:
            return "n/a"
        lo, hi = d["ci"]
        s[b] = (1 if lo > 0 else (-1 if hi < 0 else 0), d["mean"])
    if s["pess"][0] == 0 and s["opt"][0] == 0:
        return "null (CI contains 0, both bounds)"
    if s["pess"][0] * s["opt"][0] < 0 or (np.sign(s["pess"][1]) != np.sign(s["opt"][1])):
        return "UNDECIDED (bounds disagree in sign)"
    if s["pess"][0] == s["opt"][0] != 0:
        return "BETTER (both CIs > 0)" if s["pess"][0] > 0 else "WORSE (both CIs < 0)"
    k = s["pess"][0] or s["opt"][0]
    return ("leans better (one bound CI > 0)" if k > 0 else "leans worse (one bound CI < 0)")


def fmt(d, p=1):
    return f"{d['mean']:+,.{p}f} [{d['ci'][0]:+,.{p}f}; {d['ci'][1]:+,.{p}f}] ({d['neg']}/{d['n']}<0)"


def md_tables(res, title, main_metric="cagr_own_pct"):
    L = [f"### {title}", ""]
    for bound, bn in (("pess", "pessimistic"), ("opt", "optimistic")):
        if not res.get("seeds_" + bound):
            continue
        n = len(res["seeds_" + bound])
        L.append(f"**{bn}** (N={n} seeds). Median [mean] across seeds:")
        L.append("")
        L.append("| metric | " + " | ".join(VARS) + " |")
        L.append("|---|" + "---|" * len(VARS))
        for m, name in METRICS:
            cells = []
            for v in VARS:
                s = res["stats"][f"{v}|{bound}"].get(m)
                cells.append("-" if s is None else f"{s['median']:,.1f} [{s['mean']:,.1f}]")
            L.append(f"| {name} | " + " | ".join(cells) + " |")
        cells = [f"{res['stats'][f'{v}|{bound}']['ended_below_start_pct']:.0f}%" for v in VARS]
        L.append("| % seeds ended < start | " + " | ".join(cells) + " |")
        L.append("")
        L.append(f"Paired seed differences vs A0 ({bn}): mean [95% bootstrap CI] (seeds negative/N)")
        L.append("")
        L.append("| metric | " + " | ".join(VARS[1:]) + " |")
        L.append("|---|" + "---|" * (len(VARS) - 1))
        for m, name in METRICS:
            cells = []
            for v in VARS[1:]:
                d = res["diff"][f"{v}|{bound}"].get(m)
                cells.append("-" if d is None else fmt(d))
            L.append(f"| {name} | " + " | ".join(cells) + " |")
        L.append("")
    L.append("Verdict (decisive = paired dCAGR / dTotalReturn, both bounds; family D1..D5 note: 99% FW CI shown below):")
    for v in VARS[1:]:
        fw = []
        for b in ("pess", "opt"):
            d = res["diff"].get(f"{v}|{b}", {}).get(main_metric)
            if d:
                fw.append(f"{b}: [{d['ci_fw'][0]:+.1f}; {d['ci_fw'][1]:+.1f}]")
        L.append(f"- {v}: {verdict(res, v, main_metric)}   (FW99 CI {'; '.join(fw)})")
    L.append("")
    return "\n".join(L)


if __name__ == "__main__":
    prefix = sys.argv[1]
    rows = load(sys.argv[2:])
    cfgs = sorted({k.split("|")[0] for k in rows})
    allres = {c: analyse(rows, c) for c in cfgs}
    (HERE / f"{prefix}.json").write_text(json.dumps(allres, indent=1))
    main_metric = "total_return_pct" if "holdout" in prefix else "cagr_own_pct"
    txt = "\n\n".join(md_tables(allres[c], c, main_metric) for c in cfgs if allres[c].get("seeds_pess"))
    (HERE / f"{prefix}_tables.md").write_text(txt, encoding="utf-8")
    print(txt)
