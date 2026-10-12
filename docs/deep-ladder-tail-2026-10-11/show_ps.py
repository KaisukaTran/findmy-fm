"""Print per-session tables from analyze_ps json (markdown)."""
import json
import sys

r = json.load(open(sys.argv[1]))
L = []
for b, bn in (("pess", "pessimistic"), ("opt", "optimistic")):
    L.append(f"### {r['label']} -- {bn}")
    L.append("| variant | n | TP% | mean $ | median $ | days | timeout% | timeout $ | $/1000 capday | deep(>=5 fills)% | deep capday share% | loss share from deep% |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for v in ("A0", "D1", "D2", "D3", "D4"):
        s = r["stats"][f"{v}|{b}"]
        L.append(f"| {v} | {s['n']} | {s['tp_rate_pct']:.2f} | {s['mean_pnl']:.3f} | {s['median_pnl']:.3f} | {s['mean_days']:.2f} | "
                 f"{s['timeout_pct']:.2f} | {s['timeout_sum_pnl']:.0f} | {s['usd_per_1000capday']:.3f} | {s['deep_pct']:.2f} | "
                 f"{s['deep_capday_share_pct']:.1f} | {s['loss_share_deep_pct']:.1f} |")
    k = r["d5"][b]["keep"]
    L.append(f"| D5 (age>=180) | {k['n']} | {k['tp_rate_pct']:.2f} | {k['mean_pnl']:.3f} | {k['median_pnl']:.3f} | {k['mean_days']:.2f} | "
             f"{k['timeout_pct']:.2f} | {k['timeout_sum_pnl']:.0f} | {k['usd_per_1000capday']:.3f} | {k['deep_pct']:.2f} | "
             f"{k['deep_capday_share_pct']:.1f} | {k['loss_share_deep_pct']:.1f} |")
    dr = r["d5"][b]["drop"]
    L.append(f"| (excluded age<180) | {dr['n']} | {dr['tp_rate_pct']:.2f} | {dr['mean_pnl']:.3f} | {dr['median_pnl']:.3f} | {dr['mean_days']:.2f} | "
             f"{dr['timeout_pct']:.2f} | {dr['timeout_sum_pnl']:.0f} | {dr['usd_per_1000capday']:.3f} | {dr['deep_pct']:.2f} | "
             f"{dr['deep_capday_share_pct']:.1f} | {dr['loss_share_deep_pct']:.1f} |")
    L.append("")
    L.append(f"Paired V-A0 ({bn}); mean $/session [CI], $/1000 capday [CI], TP% [CI], days, better/worse/identical %")
    for v in ("D1", "D2", "D3", "D4"):
        d = r["diff"][f"{v}|{b}"]
        f = lambda k, p=3: f"{d[k]['diff']:+.{p}f} [{d[k]['ci'][0]:+.{p}f};{d[k]['ci'][1]:+.{p}f}]"  # noqa: E731
        L.append(f"- {v}: {f('mean_pnl')} | {f('usd_per_1000capday')} | {f('tp_rate_pct', 2)} | days {f('mean_days', 2)} | "
                 f"{d['share_better_pct']:.1f}/{d['share_worse_pct']:.1f}/{d['share_identical_pct']:.1f}")
    d = r["d5"][b]["diff"]
    f = lambda k, p=3: f"{d[k]['diff']:+.{p}f} [{d[k]['ci'][0]:+.{p}f};{d[k]['ci'][1]:+.{p}f}]"  # noqa: E731
    L.append(f"- D5 (drops {d['dropped_pct']:.1f}% of starts): {f('mean_pnl')} | {f('usd_per_1000capday')} | {f('tp_rate_pct', 2)} | capdays/sess {f('capday_per_session', 1)}")
    L.append("")
    L.append("By start year (mean $/session diff [CI]; $/1000 capday diff):")
    for v in ("D1", "D2", "D3", "D4", "D5"):
        by = r["by_year"][f"{v}|{b}"]
        L.append(f"- {v}: " + "; ".join(
            f"{y}: n={x['n']} {x['mean_pnl']['diff']:+.2f} [{x['mean_pnl']['ci'][0]:+.2f};{x['mean_pnl']['ci'][1]:+.2f}] / {x['usd_per_1000capday']['diff']:+.2f} [{x['usd_per_1000capday']['ci'][0]:+.2f};{x['usd_per_1000capday']['ci'][1]:+.2f}]"
            for y, x in sorted(by.items())))
    L.append("")
    L.append("D2/D3 deep-ladder accounting (A0 sessions with >=5 fills; paired on same start):")
    for v in ("D2", "D3"):
        g = r["d2_giveup"][f"{v}|{b}"]
        L.append(f"- {v}: deep n={g['deep_n']} (A0 TP {g['A0_tp_n']}, A0 not-TP {g['A0_not_tp_n']}); give-up on A0-TP ${g['giveup_on_A0_tp_usd']:,.0f}; "
                 f"gain on A0-not-TP ${g['gain_on_A0_not_tp_usd']:,.0f}; net ${g['net_usd']:,.0f}; A0 deep sum ${g['A0_deep_sum']:,.0f} -> {v} ${g['V_deep_sum']:,.0f}; "
                 f"capdays deep {g['A0_deep_capdays']:,.0f} saved {g['capday_saved_total']:,.0f}; V exits on A0-TP {g['V_exit_reason_on_A0_tp']}; on A0-notTP {g['V_exit_reason_on_A0_not_tp']}")
    L.append("")
    L.append("Distribution by fills at exit (A0): % sessions / mean $ / TP%:")
    s = r["stats"][f"A0|{b}"]
    L.append(" ".join(f"k{k}:{s['fills_dist_pct'][str(k)]:.2f}%/{(s['fills_mean_pnl'][str(k)] or 0):.1f}/{(s['fills_tp_rate'][str(k)] or 0):.0f}" for k in range(1, 11)))
    L.append("")
print("\n".join(L))
