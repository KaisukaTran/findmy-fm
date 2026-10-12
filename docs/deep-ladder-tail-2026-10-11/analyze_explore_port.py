"""EXPLORATORY portfolio: E1 (D1+D3), E5 (D3@20d) vs A0 (and vs D3) on seeds 0-9. Outside the family; no multiplicity correction."""
import glob
import json
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
rng = np.random.default_rng(99)
rows = {}
for g in ("port_w*.json", "explore_x*.json"):
    for f in glob.glob(str(HERE / g)):
        rows.update(json.loads(Path(f).read_text()))


def boot(d):
    d = np.asarray(d, float)
    m = d[rng.integers(0, len(d), size=(20000, len(d)))].mean(axis=1)
    return np.percentile(m, 2.5), np.percentile(m, 97.5)


for cfg in ("cov1", "cov30"):
    for b in ("pess", "opt"):
        seeds = [s for s in range(10) if all(f"{cfg}|{v}|{b}|{s}" in rows for v in ("A0", "D3", "E1", "E5"))]
        for v in ("D3", "E1", "E5"):
            for m in ("cagr_own_pct", "max_drawdown_unit_nav_pct"):
                d = [rows[f"{cfg}|{v}|{b}|{s}"][m] - rows[f"{cfg}|A0|{b}|{s}"][m] for s in seeds]
                lo, hi = boot(d)
                print(f"{cfg} {b} N={len(seeds)} {v}-A0 {m}: {np.mean(d):+.1f} [{lo:+.1f};{hi:+.1f}] neg {sum(x<0 for x in d)}/{len(d)}")
            for y in ("2024", "2025", "2026"):
                d = [rows[f"{cfg}|{v}|{b}|{s}"][f"y{y}.return_pct"] - rows[f"{cfg}|A0|{b}|{s}"][f"y{y}.return_pct"] for s in seeds]
                print(f"      {y}: {np.mean(d):+.1f} (neg {sum(x<0 for x in d)}/{len(d)})", end="")
            print()
