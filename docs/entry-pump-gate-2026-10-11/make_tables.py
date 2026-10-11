"""Render results_<seed>.json into markdown table fragments (printed; report.md embeds them).

    python make_tables.py <seed> [<replicate_seed>]
"""
from __future__ import annotations

import common  # noqa: F401

import json
import sys

from common import STUDY

G_ALL = ["G1", "G2", "G3", "G4", "G5", "G6", "G7", "G8"]
B = {"o": "lạc quan", "p": "bi quan"}


def f(x, d=2, sign=False):
    if x is None:
        return "–"
    try:
        if x != x:
            return "–"
    except TypeError:
        return str(x)
    return f"{x:+.{d}f}" if sign else f"{x:.{d}f}"


def ci(c, d=2):
    return "–" if not c else f"[{c[0]:+.{d}f}; {c[1]:+.{d}f}]"


def pv(x):
    return "–" if x is None else (f"{x:.4f}" if x < 0.01 else f"{x:.3f}")


def load(seed):
    return json.load(open(STUDY / f"results_{seed}.json"))


def table_main(r):
    out = []
    for b in ("o", "p"):
        out.append(f"\n**Cận {B[b]}** — đơn vị: $ lãi ròng / $1000 vốn-ngày (ppdd). Baseline pool = {f(r['baseline'][b]['ppdd'],3)}.\n")
        out.append("| cổng | bị chặn (% pool; /ngày) | ppdd bị chặn | ppdd lọt | lift (lọt−nền) | CI khối-ngày 30d | CI theo coin | lọt−chặn |")
        out.append("|---|---|---|---|---|---|---|---|")
        for g in G_ALL:
            row = r["table"][b][g]
            if row.get("unjudgeable"):
                out.append(f"| {g} {row['desc']} | {row['n_vetoed']} | không đủ mẫu | | | | | |")
                continue
            tag = "*(sanity)*" if g == "G8" else ""
            out.append(f"| {g} {row['desc']} {tag} | {row['n_vetoed']:,} ({f(row['pct_vetoed'],1)}%; {f(row['removed_per_day'],1)}) | "
                       f"{f(row['vetoed']['ppdd'],3)} | {f(row['passed']['ppdd'],3)} | **{f(row['lift_pb'],3,True)}** | "
                       f"{ci(row['ci_day30']['pb'])} | {ci(row['ci_coin']['pb'])} | {f(row['lift_pv'],3,True)} |")
    return "\n".join(out)


def table_sig(r):
    out = ["| cổng | cận | p trong-ngày | FWER trong-ngày: đơn vị gốc G1–7 (công cụ 09-10) | FWER trong-ngày: studentised, bỏ G6 | p dịch-ngày | FWER dịch-ngày: đơn vị gốc | FWER dịch-ngày: studentised | lift tối thiểu phát hiện (trong-ngày / dịch-ngày, studentised) |",
           "|---|---|---|---|---|---|---|---|---|"]
    for g in G_ALL:
        for b in ("o", "p"):
            p = r["perms"].get(g, {})
            out.append(f"| {g}{' (sanity)' if g=='G8' else ''} | {B[b]} | {pv(p.get(f'p_within_{b}'))} | {pv(p.get(f'orig_fwer_within_{b}', p.get(f'fwer_within_raw_{b}')))} | "
                       f"{pv(p.get(f'fwer_within_stud_x6_{b}'))} | {pv(p.get(f'p_shift_{b}'))} | {pv(p.get(f'fwer_shift_raw_{b}'))} | {pv(p.get(f'fwer_shift_stud_{b}'))} | "
                       f"{f(p.get(f'minlift_within_stud_{b}'),2)} / {f(p.get(f'minlift_shift_stud_{b}'),2)} |")
    return "\n".join(out)


def table_verdict(r):
    out = ["| cổng | kết luận (quy tắc ghi trong analyze.verdict) |", "|---|---|"]
    for g in G_ALL:
        out.append(f"| {g} {r['gates'][g]} | {r['verdict'][g]} |")
    return "\n".join(out)


def table_secondary(r):
    out = []
    for b in ("o", "p"):
        out.append(f"\n**Cận {B[b]}** — chặn (V) vs lọt (L) vs nền (N).\n")
        out.append("| cổng | nhóm | n | P(rung2≤48h) | P(TP≤72h) | $/phiên | timeout % | $ TB phiên timeout | ngày giữ TB | MAE xấu nhất % |")
        out.append("|---|---|---|---|---|---|---|---|---|---|")
        base = r["baseline"][b]
        out.append(f"| nền | N | {base['n']:,} | {f(base['p_rung2_48']*100,1)}% | {f(base['p_tp_72']*100,1)}% | {f(base['mean_usd'],2)} | "
                   f"{f(base['timeout_share']*100,1)}% | {f(base['timeout_mean_usd'],0)} | {f(base['days_held'],1)} | {f(base['mae_worst'],0)} |")
        for g in G_ALL:
            row = r["table"][b][g]
            if row.get("unjudgeable"):
                continue
            for nm, k in (("V", "vetoed"), ("L", "passed")):
                m = row[k]
                out.append(f"| {g} | {nm} | {m['n']:,} | {f(m['p_rung2_48']*100,1)}% | {f(m['p_tp_72']*100,1)}% | {f(m['mean_usd'],2)} | "
                           f"{f(m['timeout_share']*100,1)}% | {f(m['timeout_mean_usd'],0)} | {f(m['days_held'],1)} | {f(m['mae_worst'],0)} |")
    return "\n".join(out)


def table_years(r):
    out = ["Lift (lọt − nền) theo năm; ô: lạc quan / bi quan.\n",
           "| cổng | 2024 | 2025 | 2026 (Jan–Jul) | cùng dấu mọi năm & cả hai cận? |", "|---|---|---|---|---|"]
    for g in G_ALL:
        ro, rp = r["table"]["o"][g], r["table"]["p"][g]
        if ro.get("unjudgeable"):
            continue
        cells, signs = [], []
        for y in ("2024", "2025", "2026Jan-Jul"):
            a, b_ = ro["by_year"][y], rp["by_year"][y]
            cells.append(f"{f(a['lift_pb'],2,True)} / {f(b_['lift_pb'],2,True)}" if a and b_ else "–")
            if a and b_:
                signs += [a["lift_pb"] > 0, b_["lift_pb"] > 0]
        out.append(f"| {g}{' (sanity)' if g=='G8' else ''} | " + " | ".join(cells) + f" | {'CÓ' if signs and (all(signs) or not any(signs)) else 'KHÔNG'} |")
    return "\n".join(out)


def table_buckets(r, key, title):
    out = [f"\n**{title}** (pool chính; ppdd lạc quan / bi quan; $/phiên lạc quan / bi quan)\n",
           "| nhóm | n (%) | P(rung2≤48h) | P(TP≤72h) | P(TP≤72h & không rung2) | ppdd (lq / bq) | $/phiên (lq / bq) | timeout % |", "|---|---|---|---|---|---|---|---|"]
    for x in r[key]:
        o, p = x["o"], x["p"]
        out.append(f"| {x['bucket']} | {x['n']:,} ({x['share']*100:.1f}%) | {f(o['p_rung2_48']*100,1)}% | {f(o['p_tp_72']*100,1)}% | {f(o.get('p_good', float('nan'))*100,1)}% | "
                   f"{f(o['ppdd'],2)} / {f(p['ppdd'],2)} | {f(o['mean_usd'],2)} / {f(p['mean_usd'],2)} | {f(o['timeout_share']*100,1)}% |")
    return "\n".join(out)


def table_buckets_year(r, key, title):
    out = [f"\n**{title}** — ppdd lạc quan / bi quan, P(rung2≤48h)\n", "| nhóm | 2024 | 2025 | 2026 Jan–Jul |", "|---|---|---|---|"]
    labs = [x["bucket"] for x in r[key]["2024"]]
    for i, lab in enumerate(labs):
        cells = []
        for y in ("2024", "2025", "2026Jan-Jul"):
            x = r[key][y][i]
            cells.append(f"n={x['n']:,}: {f(x['o']['ppdd'],2)} / {f(x['p']['ppdd'],2)}; {f((x['o']['p_rung2_48'] or 0)*100,0)}%" if x["n"] else "–")
        out.append(f"| {lab} | " + " | ".join(cells) + " |")
    return "\n".join(out)


def table_robust(r):
    out = []
    for name, d in r["robustness"].items():
        n = d.get("n", "")
        out.append(f"\n**{name}** (n={n})\n")
        out.append("| cổng | n chặn | lift lq | CI30d lq | lift bq | CI30d bq |")
        out.append("|---|---|---|---|---|---|")
        for g in (G_ALL if "G1" in d["o"] else list(d["o"])):
            a, b = d["o"][g], d["p"][g]
            out.append(f"| {g} | {a['n_vetoed']:,} | {f(a['lift_pb'],3,True)} | {ci(a['ci_day30']['pb'] if a['ci_day30'] else None)} | "
                       f"{f(b['lift_pb'],3,True)} | {ci(b['ci_day30']['pb'] if b['ci_day30'] else None)} |")
    return "\n".join(out)


def table_expl(r):
    out = ["| BTC 48h SAU vào lệnh | n | P(rung2≤48h) | pump24<0 | 0–6 | ≥6 |", "|---|---|---|---|---|---|"]
    for x in r["exploratory_btc_forward_terciles"]:
        cells = [f"{y['p_rung2_48']*100:.0f}% (n={y['n']:,})" if y["p_rung2_48"] is not None else "–" for y in x["by_pump24"]]
        out.append(f"| {x['btc_fwd_tercile']} ({x['btc_fwd_range'][0]:+.1f}…{x['btc_fwd_range'][1]:+.1f}%) | {x['n']:,} | {x['p_rung2_48']*100:.1f}% | " + " | ".join(cells) + " |")
    return "\n".join(out)


def table_replicate(r1, r2):
    out = ["Lift (lọt − nền) ppdd: hạt 1 (giờ vào lệnh A) | hạt 2 (giờ vào lệnh B); FWER = studentised tốt nhất (trong-ngày bỏ G6 / dịch-ngày).\n",
           "| cổng | lq hạt1 | lq hạt2 | bq hạt1 | bq hạt2 | FWER lq h1 / h2 | FWER bq h1 / h2 |", "|---|---|---|---|---|---|---|"]
    def best(r, g, b):
        p = r["perms"].get(g, {})
        v = [p.get(f"fwer_within_stud_x6_{b}"), p.get(f"fwer_shift_stud_{b}")]
        v = [x for x in v if x is not None]
        return min(v) if v else None
    for g in G_ALL:
        a = [r["table"][b][g].get("lift_pb") for r in (r1, r2) for b in ("o", "p")]
        out.append(f"| {g} | {f(a[0],3,True)} | {f(a[2],3,True)} | {f(a[1],3,True)} | {f(a[3],3,True)} | "
                   f"{pv(best(r1,g,'o'))} / {pv(best(r2,g,'o'))} | {pv(best(r1,g,'p'))} / {pv(best(r2,g,'p'))} |")
    return "\n".join(out)


if __name__ == "__main__":
    seed = int(sys.argv[1])
    r = load(seed)
    print(table_main(r)); print(); print(table_sig(r)); print(); print(table_verdict(r)); print(); print(table_secondary(r)); print(); print(table_years(r))
    print(table_buckets(r, "buckets_pump24", "pump24h")); print(table_buckets(r, "buckets_rsiD", "RSI ngày (nến đang hình thành)"))
    print(table_robust(r)); print(table_expl(r))
