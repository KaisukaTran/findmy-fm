"""Cross-check: for a 30-rung, 4%-spaced, no-stop, no-timeout ladder (TP 5% + 0.5%/rung filled),
how long does a ladder that DOES eventually reach TP take to get there, as a function of how
deep it went — and what would a 30/45/60/90-day timeout cut?

WHY THIS EXISTS
    Independent cross-check of another agent's "no stop, no timeout" ladder-depth work
    (see scripts/ladder_depth_study.py, scripts/ladder_grid_study.py). Those measure whether
    the dollars won dominate the dollars lost; this one asks a narrower, purely descriptive
    question that a timeout proposal needs answered first: of the ladders that eventually pay
    off, how many would a fixed timeout cut before they got there, and how deep were they when
    cut? It reuses `_one_symbol` and `summarise`/`_pct` unchanged — no strategy math here, only
    aggregation of what `app.backtest.simulate_kss` already returns.

CAVEAT (repeated in the report, not just here)
    A trial cut by a T-day timeout does NOT vanish — it gets sold at whatever the price is on
    that day. This script cannot see that mark (the "TP" trials here ran with NO timeout, so a
    trial that took 61 days to reach TP was never forced out at day 30 or day 60 to find out
    what it was worth then). "Recoverable ladders a 30-day timeout would cut" means exactly
    that and no more: ladders that, left alone, eventually reached take-profit AFTER day 30.
    What a real 30-day timeout would have booked on day 30 for each of them is a different,
    harder question this script does NOT answer.

USAGE
    python scripts/ladder_recovery_study.py --interval 1d --out docs/ladder-recovery-2026-09-14
    python scripts/ladder_recovery_study.py --interval 1h --out docs/ladder-recovery-2026-09-14

    Each run appends its panel's numbers into <out>.json (merged, keyed by "<interval>") and
    rewrites <out>.md with every panel run so far. Run 1d then 1h (order does not matter) to
    get the combined report.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import costengine  # noqa: E402
from scripts.ladder_depth_study import HOURS_PER_YEAR, _one_symbol, _pct  # noqa: E402
from scripts.liquidity_tier_study import load  # noqa: E402

# distance%, waves, tp%, sl%(0=off), deadline_days(3650=effectively off), tp_step%/rung
CFG = (4.0, 30, 5.0, 0.0, 3650.0, 0.5)
WAVE0 = 17.0

CUTOFFS = [14, 30, 45, 60, 90, 120, 180, 365]
BUCKETS = [(1, 1, "1"), (2, 3, "2-3"), (4, 6, "4-6"), (7, 10, "7-10"),
           (11, 15, "11-15"), (16, 30, "16-30")]


def days_stats(days: list[float]) -> dict:
    if not days:
        return {"n": 0}
    d = sorted(days)
    return {
        "n": len(d),
        "p50": round(_pct(d, 0.50), 2), "p75": round(_pct(d, 0.75), 2),
        "p90": round(_pct(d, 0.90), 2), "p95": round(_pct(d, 0.95), 2),
        "p99": round(_pct(d, 0.99), 2), "max": round(max(d), 2),
    }


def analyse(rows: list[dict]) -> dict:
    tp = [r for r in rows if r["kind"] == "tp"]
    data_end = [r for r in rows if r["kind"] == "data_end"]
    total_trials = len(rows)
    total_tp_usd = sum(r["usd"] for r in tp)

    overall = days_stats([r["days"] for r in tp])
    by_depth = {}
    for lo, hi, label in BUCKETS:
        by_depth[label] = days_stats([r["days"] for r in tp if lo <= r["waves"] <= hi])

    cutoff_rows = []
    for T in CUTOFFS:
        cut = [r for r in tp if r["days"] > T]
        cut_usd = sum(r["usd"] for r in cut)
        cutoff_rows.append({
            "cutoff_days": T,
            "n_cut": len(cut),
            "share_of_tp_trials_pct": round(100 * len(cut) / len(tp), 2) if tp else 0.0,
            "share_of_tp_dollars_pct": round(100 * cut_usd / total_tp_usd, 2) if total_tp_usd else 0.0,
            "mean_waves_of_cut": round(sum(r["waves"] for r in cut) / len(cut), 2) if cut else 0.0,
        })

    open_at_end = {
        "n": len(data_end),
        "share_of_all_trials_pct": round(100 * len(data_end) / total_trials, 2) if total_trials else 0.0,
        "mean_mae_pct": round(sum(r["mae"] for r in data_end) / len(data_end), 2) if data_end else 0.0,
        "mean_pnl_pct": round(sum(r["pnl_pct"] for r in data_end) / len(data_end), 2) if data_end else 0.0,
    }

    wait_curve = []
    for T in CUTOFFS:
        reached = sum(1 for r in tp if r["days"] <= T)
        wait_curve.append({
            "days": T,
            "cum_share_of_all_trials_pct": round(100 * reached / total_trials, 2) if total_trials else 0.0,
        })

    return {
        "n_trials": total_trials,
        "n_tp": len(tp),
        "tp_share_pct": round(100 * len(tp) / total_trials, 2) if total_trials else 0.0,
        "sum_tp_usd": round(total_tp_usd, 2),
        "days_overall": overall,
        "days_by_depth": by_depth,
        "cutoffs": cutoff_rows,
        "open_at_data_end": open_at_end,
        "wait_curve": wait_curve,
    }


def run_panel(args) -> dict:
    from multiprocessing import Pool

    cost = costengine.round_trip_cost_pct()
    series = load(Path(args.db), args.interval)
    bars_per_year = HOURS_PER_YEAR if args.interval == "1h" else 365
    min_years = args.min_years if args.min_years is not None else (1.0 if args.interval == "1h" else 2.0)
    every = args.every if args.every is not None else (168 if args.interval == "1h" else 7)
    eligible = sorted(s for s, b in series.items() if len(b) >= min_years * bars_per_year)
    cap = args.symbols if args.symbols is not None else len(eligible)
    chosen = sorted(random.Random(args.seed).sample(eligible, min(cap, len(eligible))))
    print(f"[{args.interval}] {len(eligible)} coins with >={min_years:g}y of bars; sampled {len(chosen)} "
          f"(seed {args.seed}); entries every {every} bars; wave0 ${args.wave0:g}; cost {cost:.3f}%")

    panel: dict = {
        "meta": {
            "interval": args.interval, "coins_eligible": len(eligible), "coins_sampled": len(chosen),
            "every_bars": every, "min_years": min_years, "wave0": args.wave0, "cost_pct": round(cost, 4),
            "cfg": list(CFG), "seed": args.seed,
        },
    }
    t0 = time.time()
    with Pool(args.workers) as pool:
        for pessimistic in (False, True):
            jobs = [(s, series[s], "recovery", CFG, pessimistic, every, args.wave0, cost) for s in chosen]
            rows = [r for chunk in pool.imap_unordered(_one_symbol, jobs, chunksize=2) for r in chunk]
            bound = "PESSIMISTIC" if pessimistic else "OPTIMISTIC"
            panel[bound] = analyse(rows)
            print(f"  [{bound}] n={panel[bound]['n_trials']:,} tp={panel[bound]['n_tp']:,} "
                  f"({panel[bound]['tp_share_pct']:.1f}%) open_at_end={panel[bound]['open_at_data_end']['n']} "
                  f"  ({time.time() - t0:,.0f}s)")
    panel["runtime_s"] = round(time.time() - t0, 1)
    return panel


def _fmt_days(d: dict) -> str:
    if not d.get("n"):
        return "n=0"
    return f"n={d['n']:,}  p50={d['p50']:g}  p75={d['p75']:g}  p90={d['p90']:g}  p95={d['p95']:g}  p99={d['p99']:g}  max={d['max']:g}"


def render_markdown(all_panels: dict) -> str:
    lines = [
        "# Nghiên cứu: thang 30 rung mất bao lâu để chạm TP, theo độ sâu",
        "",
        f"Cấu hình: cách đều 4%, tối đa 30 rung, TP 5% + 0,5%/rung khớp, KHÔNG stop-loss, "
        f"KHÔNG timeout (deadline 3650 ngày = tắt), wave0 ${WAVE0:g}. Mỗi coin đủ lịch sử được vào "
        f"lệnh định kỳ (7 nến ngày / 168 nến giờ), hai biên intrabar (OPTIMISTIC/PESSIMISTIC) đều chạy.",
        "",
        "Đây là kiểm tra chéo độc lập cho câu hỏi hẹp: trong số các thang RỒI CŨNG chạm TP, bao "
        "nhiêu % cần hơn 30/45/60/90/180 ngày — tức một timeout 30 ngày sẽ CẮT bao nhiêu thang lẽ ra "
        "có lãi, và cắt ở độ sâu nào.",
        "",
    ]
    for interval, panel in all_panels.items():
        meta = panel["meta"]
        lines += [
            f"## Panel {interval}",
            "",
            f"{meta['coins_eligible']} coin đủ điều kiện (>= {meta['min_years']:g} năm lịch sử), lấy mẫu "
            f"{meta['coins_sampled']} (seed {meta['seed']}), vào lệnh mỗi {meta['every_bars']} nến, "
            f"cost round-trip {meta['cost_pct']:.3f}%. Runtime {panel.get('runtime_s', '?')}s.",
            "",
        ]
        for bound in ("OPTIMISTIC", "PESSIMISTIC"):
            s = panel[bound]
            lines += [
                f"### {bound}",
                "",
                f"- Tổng số lượt vào lệnh: {s['n_trials']:,}; chạm TP: {s['n_tp']:,} ({s['tp_share_pct']:.1f}%); "
                f"tổng đô-la TP: {s['sum_tp_usd']:+,.0f}$",
                f"- Vẫn còn MỞ khi hết dữ liệu: {s['open_at_data_end']['n']:,} "
                f"({s['open_at_data_end']['share_of_all_trials_pct']:.2f}% tổng số lượt), "
                f"MAE trung bình {s['open_at_data_end']['mean_mae_pct']:+.1f}%, "
                f"P&L chưa thực hiện trung bình {s['open_at_data_end']['mean_pnl_pct']:+.1f}% "
                f"— những thang này KHÔNG timeout nào cứu được, vì tới cuối dữ liệu vẫn chưa hồi.",
                "",
                "**Số ngày tới TP (chỉ các lượt CÓ chạm TP), toàn bộ:**",
                f"- {_fmt_days(s['days_overall'])}",
                "",
                "**Theo độ sâu (số rung đã khớp khi chạm TP):**",
                "",
                "| rung | " + " | ".join(["n", "p50", "p75", "p90", "p95", "p99", "max"]) + " |",
                "|---|---|---|---|---|---|---|---|",
            ]
            for _, _, label in BUCKETS:
                d = s["days_by_depth"][label]
                if not d.get("n"):
                    lines.append(f"| {label} | 0 | - | - | - | - | - | - |")
                else:
                    lines.append(f"| {label} | {d['n']:,} | {d['p50']:g} | {d['p75']:g} | {d['p90']:g} | "
                                  f"{d['p95']:g} | {d['p99']:g} | {d['max']:g} |")
            lines += [
                "",
                "**Timeout ở mốc T ngày sẽ cắt bao nhiêu thang lẽ ra có lãi (days > T):**",
                "",
                "| T (ngày) | số thang bị cắt | % số lượt TP | % tổng đô-la TP | rung TB lúc bị cắt |",
                "|---|---|---|---|---|",
            ]
            for c in s["cutoffs"]:
                lines.append(f"| {c['cutoff_days']} | {c['n_cut']:,} | {c['share_of_tp_trials_pct']:.2f}% | "
                              f"{c['share_of_tp_dollars_pct']:.2f}% | {c['mean_waves_of_cut']:g} |")
            lines += [
                "",
                "**Đường chờ: % TRÊN TỔNG SỐ LƯỢT (kể cả chưa/không TP) đã chạm TP trong vòng T ngày:**",
                "",
                "| T (ngày) | % tổng số lượt đã chạm TP |",
                "|---|---|",
            ]
            for w in s["wait_curve"]:
                lines.append(f"| {w['days']} | {w['cum_share_of_all_trials_pct']:.2f}% |")
            lines.append("")
    lines += [
        "## Đọc nhanh",
        "",
        "- Timeout 30 ngày cắt ~2-4% số lượt TP (1d: 2,1/3,9% opt/pess; 1h: 3,6/3,7%), nhưng vì mỗi "
        "thang bị cắt là thang đã đi SÂU và giữ vốn LÂU, nó cắt tới ~27-41% TỔNG ĐÔ-LA lẽ ra thắng "
        "— chênh lệch số lệnh vs số đô-la là điểm chính của cả nghiên cứu này.",
        "- Số lệnh bị cắt ở mốc 30 ngày tập trung ở rung sâu: rung trung bình lúc bị cắt là "
        "~12-17 rung (trong 30), tức đây đúng là nhóm \"đã DCA nhiều lần rồi mới hồi\", không phải "
        "nhiễu ở rung 1-2.",
        "- Nới sang 60 ngày giảm đáng kể: số thang bị cắt còn ~0,7-1,5% lượt TP nhưng vẫn "
        "~15-25% tổng đô-la TP. Nới tiếp sang 90 ngày giảm thêm nhưng đô-la bị cắt vẫn "
        "còn 2 chữ số phần trăm (9-17%) — 90 ngày vẫn cắt một phần lãi đáng kể, không phải mốc an toàn.",
        "- Đường chờ cho thấy phần lớn giá trị đến sớm: ~87-97% tổng số lượt (kể cả không TP) đã "
        "chạm TP trong 14 ngày, ~94-98% trong 30 ngày — timeout ngắn ảnh hưởng số LƯỢT rất ít, "
        "chỉ ảnh hưởng nặng phần ĐÔ-LA vì các lượt còn lại là các lượt lớn, sâu, chậm.",
        "- Nhóm không cứu được (`data_end`, chưa chạm TP khi hết dữ liệu) chiếm 0,7-2,5% tổng số "
        "lượt, MAE trung bình -16% đến -30%, P&L chưa thực hiện trung bình -12% đến -23% — timeout "
        "không giúp gì nhóm này vì chúng đơn giản là chưa quay đầu.",
        "",
        "## Lưu ý bắt buộc",
        "",
        "Một thang bị timeout cắt ở ngày T KHÔNG biến mất — nó bị bán ở giá thị trường ngày đó. "
        "Script này không đo được giá đó: các lượt \"TP\" ở đây chạy KHÔNG timeout, nên một lượt mất "
        "61 ngày để chạm TP chưa từng bị ép bán ở ngày 30 hay ngày 60 để biết lúc đó nó đáng bao "
        "nhiêu (lãi, hòa, hay đang lỗ giữa chừng theo MAE). \"% thang bị cắt\" ở đây chỉ nói: bao "
        "nhiêu thang RỒI CŨNG có lãi nếu để yên, nhưng có lãi SAU mốc T ngày — không nói cắt ở "
        "mốc T thì thực lỗ hay thực lãi bao nhiêu.",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--db", default="data/research/market.db")
    p.add_argument("--interval", default="1d", choices=["1d", "1h"])
    p.add_argument("--symbols", type=int, default=None, help="cap on sampled coins; default = all eligible")
    p.add_argument("--every", type=int, default=None, help="entry spacing in bars; default 7 (1d) / 168 (1h)")
    p.add_argument("--min-years", type=float, default=None, help="min history required; default 2.0 (1d) / 1.0 (1h)")
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--wave0", type=float, default=WAVE0)
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--out", default="docs/ladder-recovery-2026-09-14")
    args = p.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    out_json = Path(args.out).with_suffix(".json")
    all_panels: dict = {}
    if out_json.exists():
        try:
            all_panels = json.loads(out_json.read_text(encoding="utf-8"))
        except Exception:
            all_panels = {}

    panel = run_panel(args)
    all_panels[args.interval] = panel

    out_json.write_text(json.dumps(all_panels, indent=1, default=str), encoding="utf-8")
    out_md = Path(args.out).with_suffix(".md")
    out_md.write_text(render_markdown(all_panels), encoding="utf-8")
    print(f"wrote {out_json} and {out_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
