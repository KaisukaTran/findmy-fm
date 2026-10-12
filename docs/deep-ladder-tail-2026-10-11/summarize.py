"""Compact summary from results_*.json (output of analyze_portfolio.py).  python summarize.py results.json [metric]"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from analyze_portfolio import verdict  # noqa: E402

res = json.loads(Path(sys.argv[1]).read_text())
main = sys.argv[2] if len(sys.argv) > 2 else "cagr_own_pct"
V = ["A0", "D1", "D2", "D3", "D4", "D5"]


def f(d, p=1):
    return f"{d['mean']:+.{p}f} [{d['ci'][0]:+.{p}f};{d['ci'][1]:+.{p}f}] {d['neg']}/{d['n']}"


for cfg, r in res.items():
    if not r.get("seeds_pess"):
        continue
    print(f"##### {cfg}  seeds pess={len(r['seeds_pess'])} opt={len(r['seeds_opt'])}")
    for b in ("pess", "opt"):
        s = r["stats"]
        print(f"-- {b}: A0 median {main} {s[f'A0|{b}'][main]['median']:.1f}, DD {s[f'A0|{b}']['max_drawdown_unit_nav_pct']['median']:.1f}, "
              f"end$ {s[f'A0|{b}']['final_nav_own']['median']:,.0f}, closed {s[f'A0|{b}']['sessions_closed']['median']:.0f}, util {s[f'A0|{b}']['util_avg_pct']['median']:.1f}, "
              f"starved {s[f'A0|{b}']['starved_rungs_distinct']['median']:.0f}, stuck {s[f'A0|{b}']['sessions_still_open_at_end']['median']:.0f}/MTM {s[f'A0|{b}']['open_at_end_unrealized_usd']['median']:,.0f}, "
              f"60d-deadline {s[f'A0|{b}']['n_deadline']['median']:.0f}/${s[f'A0|{b}']['usd_deadline']['median']:,.0f}")
        for v in V[1:]:
            d = r["diff"][f"{v}|{b}"]
            sv = s[f"{v}|{b}"]
            extra = (f"closed {sv['sessions_closed']['median']:.0f} util {sv['util_avg_pct']['median']:.1f} starved {sv['starved_rungs_distinct']['median']:.0f} "
                     f"stuck {sv['sessions_still_open_at_end']['median']:.0f}/{sv['open_at_end_unrealized_usd']['median']:,.0f} "
                     f"dl {sv['n_deadline']['median']:.0f}/${sv['usd_deadline']['median']:,.0f} deep-exit {sv['n_deadline_deep']['median']:.0f}/${sv['usd_deadline_deep']['median']:,.0f}")
            yrs = " ".join(f"{y}:{d[f'y{y}.return_pct']['mean']:+.1f}({d[f'y{y}.return_pct']['neg']}/{d[f'y{y}.return_pct']['n']}<0)" for y in ("2024", "2025", "2026")
                           if f"y{y}.return_pct" in d)
            print(f"   {v}: d{main} {f(d[main])} | dDD {f(d['max_drawdown_unit_nav_pct'])} | dEnd$ {f(d['final_nav_own'], 0)} | yr {yrs}\n        {extra}")
    print("   VERDICT:", "; ".join(f"{v}: {verdict(r, v, main)}" for v in V[1:]))
