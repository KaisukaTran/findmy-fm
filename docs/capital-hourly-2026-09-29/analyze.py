"""Turn scripts/capital_hourly_grid.py's results_*.json into the report tables.

    python docs/capital-hourly-2026-09-29/analyze.py docs/capital-hourly-2026-09-29/results_grid.json
"""
from __future__ import annotations

import json
import random
import sys
from collections import defaultdict
from pathlib import Path

METRICS = [
    "cagr_own_pct", "max_drawdown_unit_nav_pct", "y2024.return_pct", "y2025.return_pct",
    "y2026.return_pct", "crash2025.nav_return_pct", "util_normal_days_pct",
    "util_median_daily_pct", "starved_rungs_distinct", "starved_distinct_usd",
    "deadline_losses", "deadline_losses_usd", "ended_below_start_capital",
]


def _get(row: dict, key: str):
    return row.get(key)


def pctile(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    if not xs:
        return float("nan")
    k = max(0, min(len(xs) - 1, int(round(p * (len(xs) - 1)))))
    return xs[k]


def median(xs: list[float]) -> float:
    return pctile(xs, 0.5)


def summarize(rows: list[dict], key: str) -> str:
    xs = [r[key] for r in rows if r.get(key) is not None]
    if not xs:
        return "n/a"
    return f"{median(xs):.2f} [{pctile(xs,0.10):.2f}, {pctile(xs,0.90):.2f}]"


def bootstrap_ci(diffs: list[float], n: int = 10000, seed: int = 0) -> tuple[float, float, float]:
    if not diffs:
        return (float("nan"),) * 3
    rng = random.Random(seed)
    means = []
    for _ in range(n):
        sample = [diffs[rng.randrange(len(diffs))] for _ in range(len(diffs))]
        means.append(sum(sample) / len(sample))
    means.sort()
    mean_obs = sum(diffs) / len(diffs)
    return mean_obs, pctile(means, 0.025), pctile(means, 0.975)


def main() -> int:
    path = Path(sys.argv[1] if len(sys.argv) > 1 else "docs/capital-hourly-2026-09-29/results_grid.json")
    data = json.loads(path.read_text())
    rows_by_interval = data["rows"]

    for interval, rows in rows_by_interval.items():
        print(f"\n{'='*100}\ninterval = {interval}  ({len(rows)} runs)\n{'='*100}")
        groups: dict[tuple, list[dict]] = defaultdict(list)
        for label, row in rows.items():
            _, cell, n_tag, bound, seed_tag = label.split("|")
            groups[(cell, n_tag, bound)].append(row)

        print(f"\n{'cell':30s} {'n/day':8s} {'bound':5s} {'n':3s} {'cagr_own%':22s} "
              f"{'DD_unit_nav%':22s} {'below_$7k%':10s}")
        for (cell, n_tag, bound), grp in sorted(groups.items()):
            grp.sort(key=lambda r: r["seed"])
            below = 100.0 * sum(r["ended_below_start_capital"] for r in grp) / len(grp)
            print(f"{cell:30s} {n_tag:8s} {bound:5s} {len(grp):3d} "
                  f"{summarize(grp,'cagr_own_pct'):22s} "
                  f"{summarize(grp,'max_drawdown_unit_nav_pct'):22s} {below:9.1f}%")

        print("\nPer-year returns (median [p10,p90], pessimistic bound only):")
        for (cell, n_tag, bound), grp in sorted(groups.items()):
            if bound != "pess":
                continue
            print(f"  {cell:30s} {n_tag:6s}  2024: {summarize(grp,'y2024.return_pct')}"
                  f"   2025: {summarize(grp,'y2025.return_pct')}"
                  f"   2026H1: {summarize(grp,'y2026.return_pct')}"
                  f"   2025-10-10..: {summarize(grp,'crash2025.nav_return_pct')}")

        print("\nUtilization (pessimistic bound only):")
        for (cell, n_tag, bound), grp in sorted(groups.items()):
            if bound != "pess":
                continue
            print(f"  {cell:30s} {n_tag:6s}  normal-day: {summarize(grp,'util_normal_days_pct')}"
                  f"   median-daily: {summarize(grp,'util_median_daily_pct')}"
                  f"   starved-rungs: {summarize(grp,'starved_rungs_distinct')}"
                  f"   deadline-losses n/$: {summarize(grp,'deadline_losses')}"
                  f" / {summarize(grp,'deadline_losses_usd')}")

        print("\nBound gap (optimistic - pessimistic, cagr_own_pct, paired by seed):")
        for (cell, n_tag, bound), grp_pess in sorted(groups.items()):
            if bound != "pess":
                continue
            grp_opt = groups.get((cell, n_tag, "opt"), [])
            by_seed_opt = {r["seed"]: r for r in grp_opt}
            diffs = [r["cagr_own_pct"] - by_seed_opt[r["seed"]]["cagr_own_pct"]
                     for r in grp_pess if r["seed"] in by_seed_opt]
            diffs = [-d for d in diffs]  # opt - pess
            if diffs:
                mean_, lo, hi = bootstrap_ci(diffs)
                print(f"  {cell:30s} {n_tag:6s}  gap = {mean_:+.2f} [{lo:+.2f}, {hi:+.2f}] pts")

        print("\nPaired: coverage 1 vs 30 (same seed/bound/n), cagr_own_pct diff (1 minus 30):")
        for n_tag in sorted({g[1] for g in groups}):
            for bound in ("pess", "opt"):
                g1 = {r["seed"]: r for r in groups.get(("cov1_floor20", n_tag, bound), [])}
                g30 = {r["seed"]: r for r in groups.get(("cov30_floor20", n_tag, bound), [])}
                diffs = [g1[s]["cagr_own_pct"] - g30[s]["cagr_own_pct"]
                         for s in g1 if s in g30]
                if diffs:
                    mean_, lo, hi = bootstrap_ci(diffs)
                    print(f"  n={n_tag:6s} {bound:5s}  cov1-cov30 = {mean_:+.2f} [{lo:+.2f}, {hi:+.2f}] pts"
                          f"  (n={len(diffs)})")

        print("\nPaired: floor 0 vs 20 (coverage 1, same seed/bound/n), cagr_own_pct diff (0 minus 20):")
        for n_tag in sorted({g[1] for g in groups}):
            for bound in ("pess", "opt"):
                g0 = {r["seed"]: r for r in groups.get(("cov1_floor0", n_tag, bound), [])}
                g20 = {r["seed"]: r for r in groups.get(("cov1_floor20", n_tag, bound), [])}
                diffs = [g0[s]["cagr_own_pct"] - g20[s]["cagr_own_pct"] for s in g0 if s in g20]
                if diffs:
                    mean_, lo, hi = bootstrap_ci(diffs)
                    print(f"  n={n_tag:6s} {bound:5s}  floor0-floor20 = {mean_:+.2f} [{lo:+.2f}, {hi:+.2f}] pts"
                          f"  (n={len(diffs)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
