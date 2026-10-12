import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import analyze_ps as ap  # noqa: E402

ap.VARS = ["A0", "E1", "E2", "E3", "E4", "E5"]
out = ap.load(str(Path(__file__).resolve().parent / "ps_explore_s168.npz"))
for b in ("pess", "opt"):
    A = out[("A0", b)]
    print(f"== {b} (EXPLORATORY)")
    for v in ap.VARS:
        s = ap.stats(out[(v, b)])
        line = (f"{v}: TP% {s['tp_rate_pct']:.2f} mean$ {s['mean_pnl']:.3f} days {s['mean_days']:.2f} timeout% {s['timeout_pct']:.2f} "
                f"timeout$ {s['timeout_sum_pnl']:.0f} $/1000cd {s['usd_per_1000capday']:.3f} capdays {s['capdays']:.3e}")
        if v != "A0":
            d = ap.paired(A, out[(v, b)])
            line += (f" | d mean$ {d['mean_pnl']['diff']:+.3f} [{d['mean_pnl']['ci'][0]:+.3f};{d['mean_pnl']['ci'][1]:+.3f}]"
                     f" d$/1000cd {d['usd_per_1000capday']['diff']:+.3f} [{d['usd_per_1000capday']['ci'][0]:+.3f};{d['usd_per_1000capday']['ci'][1]:+.3f}]")
        print(line)
