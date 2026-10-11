"""Paired per-session table bucketed by the rungs filled at A's exit (the mechanism table)."""
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from analyze_per_session import load  # noqa: E402

path, outp = sys.argv[1], sys.argv[2]
out = load(path)
res = {}
for b in ("opt", "pess"):
    A, B, C = out[("avg", b)], out[("entry", b)], out[("max", b)]
    rows = []
    # bucket by A's exit rung count; note sessions that A closes with k rungs may go deeper under B/C
    for lo, hi in ((1, 1), (2, 2), (3, 3), (4, 5), (6, 7), (8, 10)):
        m = (A["waves"] >= lo) & (A["waves"] <= hi)
        if not m.any():
            continue
        r = {"k": f"{lo}" if lo == hi else f"{lo}-{hi}", "n": int(m.sum()), "share_pct": 100 * float(m.mean())}
        for nm, V in (("A", A), ("B", B), ("C", C)):
            r[f"{nm}_mean_pnl"] = float(V["pnl_usd"][m].mean())
            r[f"{nm}_tp_pct"] = 100 * float((V["reason"][m] == 0).mean())
            r[f"{nm}_days"] = float(V["days"][m].mean())
            r[f"{nm}_deadline_pct"] = 100 * float((V["reason"][m] == 1).mean())
            r[f"{nm}_sum_pnl"] = float(V["pnl_usd"][m].sum())
            r[f"{nm}_capdays"] = float(V["capdays"][m].sum())
        rows.append(r)
    res[b] = rows
Path(outp).write_text(json.dumps(res, indent=1))
for b in res:
    print(b)
    for r in res[b]:
        print(f"k={r['k']:>4} n={r['n']:6d} ({r['share_pct']:.1f}%) "
              f"pnl A/B/C {r['A_mean_pnl']:+.1f}/{r['B_mean_pnl']:+.1f}/{r['C_mean_pnl']:+.1f}  "
              f"tp% {r['A_tp_pct']:.0f}/{r['B_tp_pct']:.0f}/{r['C_tp_pct']:.0f}  "
              f"days {r['A_days']:.1f}/{r['B_days']:.1f}/{r['C_days']:.1f}  "
              f"ddl% {r['A_deadline_pct']:.1f}/{r['B_deadline_pct']:.1f}/{r['C_deadline_pct']:.1f}")
