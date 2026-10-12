"""Per-session: D2/D3 vs A0 for starts in windows around the 2025-10-10 V-shape. Sessions that were deep (>=5 fills) at any point."""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from analyze_ps import load  # noqa: E402

out = load(str(Path(__file__).resolve().parent / "ps_1h_s168.npz"))
ts = out[("A0", "opt")]["ts"]
D = lambda s: np.datetime64(s).astype("datetime64[ms]").astype(np.int64)  # noqa: E731
wins = {"2025-08-01..2025-10-10 (pre-crash starts)": (D("2025-08-01"), D("2025-10-10T00:00")),
        "2025-10-10..2025-12-31 (post)": (D("2025-10-10T00:00"), D("2026-01-01")),
        "all": (0, 2 ** 62)}
for b in ("pess", "opt"):
    A = out[("A0", b)]
    print(f"== {b}")
    for name, (lo, hi) in wins.items():
        m = (ts >= lo) & (ts < hi)
        deep = m & (A["waves"] >= 5)
        tot = A["pnl_usd"][m].sum()
        print(f"{name}: n={m.sum()} total A0 ${tot:,.0f}; deep n={deep.sum()} deep sum ${A['pnl_usd'][deep].sum():,.0f} "
              f"({100*A['pnl_usd'][deep].sum()/tot:.1f}% of total); deep TP% {100*(A['reason'][deep]==0).mean():.1f}")
        for v in ("D1", "D2", "D3", "D4"):
            V = out[(v, b)]
            print(f"   {v}: total ${V['pnl_usd'][m].sum():,.0f} (delta {V['pnl_usd'][m].sum()-tot:+,.0f}); deep sum ${V['pnl_usd'][deep].sum():,.0f}; "
                  f"capdays {V['capdays'][m].sum()/A['capdays'][m].sum():.3f}x A0; $/1000cd {1000*V['pnl_usd'][m].sum()/V['capdays'][m].sum():.2f} vs A0 {1000*tot/A['capdays'][m].sum():.2f}")
    # 20-30 rung analog: ladders with >=7 fills share of profit
    deep7 = A["waves"] >= 7
    print(f"fills>=7: n={deep7.sum()} sum ${A['pnl_usd'][deep7].sum():,.0f} of total ${A['pnl_usd'].sum():,.0f} ({100*A['pnl_usd'][deep7].sum()/A['pnl_usd'].sum():.1f}%)")
    for k in (5, 6, 7, 8, 9, 10):
        m = A["waves"] == k
        print(f"  exit at {k} fills: n={m.sum()} sum ${A['pnl_usd'][m].sum():,.0f} ({100*A['pnl_usd'][m].sum()/A['pnl_usd'].sum():.1f}% of profit)")
