"""Aggregate port_*w*.json: per-variant medians/means across seeds, paired seed differences (B-A, C-A)
with bootstrap 95% CI over seeds, and a t-interval for reference."""
from __future__ import annotations

import glob
import json
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
RNG = np.random.default_rng(777)
VARS = ["avg", "entry", "max"]
METRICS = [
    ("cagr_own_pct", "CAGR %"), ("final_nav_own", "end equity $"), ("realized_usd", "realized $"),
    ("max_drawdown_unit_nav_pct", "max DD %"), ("sessions_closed", "sessions closed"),
    ("util_avg_pct", "avg util %"), ("rungs_starved", "starved rung-retries"),
    ("starved_rungs_distinct", "starved rungs (distinct)"),
    ("sessions_still_open_at_end", "stuck at end"), ("open_at_end_unrealized_usd", "stuck: MTM $"),
    ("n_tp", "TP exits"), ("n_deadline", "deadline exits"), ("usd_deadline", "deadline $"),
    ("usd_tp", "TP $"), ("mean_days_all", "mean days held"), ("mean_waves_closed", "mean rungs at exit"),
    ("y2024.return_pct", "2024 return %"), ("y2025.return_pct", "2025 return %"),
    ("y2026.return_pct", "2026H1 return %"), ("sessions_opened", "sessions opened"),
]


def load():
    rows = {}
    for f in sorted(glob.glob(str(HERE / "port_*w*.json"))):
        rows.update(json.loads(Path(f).read_text()))
    return rows


def boot(d, reps=10000):
    d = np.asarray(d, float)
    n = len(d)
    idx = RNG.integers(0, n, size=(reps, n))
    m = d[idx].mean(axis=1)
    return float(np.percentile(m, 2.5)), float(np.percentile(m, 97.5))


def tci(d):
    d = np.asarray(d, float)
    n = len(d)
    if n < 2:
        return (float("nan"), float("nan"))
    from math import sqrt
    t = {5: 2.776, 8: 2.306, 9: 2.262, 10: 2.228, 14: 2.145, 19: 2.093, 20: 2.086}.get(n - 1, 2.0)
    se = d.std(ddof=1) / sqrt(n)
    return float(d.mean() - t * se), float(d.mean() + t * se)


def analyse(rows, cfgname):
    out = {"stats": {}, "diff": {}}
    for bound in ("pess", "opt"):
        seeds = sorted({int(k.split("|")[3]) for k in rows if k.startswith(f"{cfgname}|") and k.split("|")[2] == bound})
        full = [s for s in seeds if all(f"{cfgname}|{v}|{bound}|{s}" in rows for v in VARS)]
        out["seeds_" + bound] = full
        for v in VARS:
            r = [rows[f"{cfgname}|{v}|{bound}|{s}"] for s in full]
            out["stats"][f"{v}|{bound}"] = {
                m: {"median": float(np.median([x[m] for x in r])), "mean": float(np.mean([x[m] for x in r]))}
                for m, _ in METRICS if all(x.get(m) is not None for x in r)}
            out["stats"][f"{v}|{bound}"]["ended_below_start_pct"] = 100 * float(np.mean([x["ended_below_start_capital"] for x in r]))
        for v in ("entry", "max"):
            dd = {}
            for m, _ in METRICS:
                try:
                    d = [rows[f"{cfgname}|{v}|{bound}|{s}"][m] - rows[f"{cfgname}|avg|{bound}|{s}"][m] for s in full]
                except TypeError:
                    continue
                lo, hi = boot(d)
                tlo, thi = tci(d)
                dd[m] = {"mean": float(np.mean(d)), "ci_boot": [lo, hi], "ci_t": [tlo, thi],
                         "neg": int(sum(1 for x in d if x < 0)), "n": len(d)}
            out["diff"][f"{v}-avg|{bound}"] = dd
    return out


def md_tables(res, title):
    L = [f"### {title}", ""]
    for bound, bn in (("pess", "bi quan"), ("opt", "lạc quan")):
        n = len(res["seeds_" + bound])
        L.append(f"**{bn}** (N={n} seed). Trung vị [trung bình] qua seed:")
        L.append("")
        L.append("| chỉ số | A avg | B entry | C max(avg,entry) |")
        L.append("|---|---|---|---|")
        for m, name in METRICS:
            cells = []
            for v in VARS:
                s = res["stats"][f"{v}|{bound}"].get(m)
                cells.append("-" if s is None else f"{s['median']:,.1f} [{s['mean']:,.1f}]")
            L.append(f"| {name} | " + " | ".join(cells) + " |")
        cells = [f"{res['stats'][f'{v}|{bound}']['ended_below_start_pct']:.0f}%" for v in VARS]
        L.append("| % seed kết thúc dưới $7k | " + " | ".join(cells) + " |")
        L.append("")
        L.append(f"Chênh lệch cặp theo seed ({bn}): trung bình [KTC bootstrap 95%] (số seed âm/N)")
        L.append("")
        L.append("| chỉ số | B−A | C−A |")
        L.append("|---|---|---|")
        for m, name in METRICS:
            cells = []
            for v in ("entry", "max"):
                d = res["diff"][f"{v}-avg|{bound}"].get(m)
                if d is None:
                    cells.append("-")
                else:
                    cells.append(f"{d['mean']:+,.1f} [{d['ci_boot'][0]:+,.1f}; {d['ci_boot'][1]:+,.1f}] ({d['neg']}/{d['n']})")
            L.append(f"| {name} | " + " | ".join(cells) + " |")
        L.append("")
    return "\n".join(L)


if __name__ == "__main__":
    rows = load()
    cfgs = sorted({k.split("|")[0] for k in rows})
    allres = {c: analyse(rows, c) for c in cfgs}
    (HERE / "results_portfolio.json").write_text(json.dumps(allres, indent=1))
    txt = "\n\n".join(md_tables(allres[c], c) for c in cfgs if "avg|pess" in " ".join(allres[c]["stats"]) and len(allres[c]["seeds_pess"]) > 0)
    (HERE / "portfolio_tables.md").write_text(txt, encoding="utf-8")
    print(txt)
