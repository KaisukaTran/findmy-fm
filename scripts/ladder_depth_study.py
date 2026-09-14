"""Does a DEEP ladder (10 rungs, 50% stop) turn "many small wins" into "one large loss"?

WHY THIS EXISTS
    The product owner's thesis (2026-09-12): with 10 DCA rungs and a 50% hard stop, a single
    large loss is UNLIKELY compared with the stream of small take-profit wins — and the
    take-profit should climb 0.5% per rung filled so a deep ladder is paid for its depth. This
    measures that on the five-year, survivorship-free 1h panel with the production simulator
    (`app.backtest.simulate_kss`, the same code the scanner's gate calls), in DOLLARS at the
    owner's real first-wave size, so a $75 winner is not weighed like a $3,600 loser.

WHAT "NO TIMEOUT" MEANS HERE
    The owner chose "SL 50% only, no timeout". A trial with no exit cannot be scored, so every
    deep configuration runs with a 365-day horizon and a trial still open after a year is
    reported as its own bucket ("open after 365d", with the unrealised P&L it would carry) —
    never folded into the wins or the losses. A 7-day horizon is run alongside as the
    sensitivity: what the timeout the live app has today would have done.

TWO INTRA-BAR BOUNDS, ALWAYS (see scripts/ladder_panel_study.py)
    Both are printed. When they disagree in sign the honest answer is "unknown at 1h".

    python scripts/ladder_depth_study.py [--interval 1h|1d] [--symbols 120] [--every 168] [--seed 7]
        [--wave0 75] [--workers 8] [--out docs/ladder-depth-2026-09-12]
"""

from __future__ import annotations

import argparse
import json
import random
import statistics as st
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from multiprocessing import Pool
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import costengine  # noqa: E402
from app.backtest import simulate_kss  # noqa: E402
from scripts.ladder_panel_study import to_candles  # noqa: E402
from scripts.liquidity_tier_study import load  # noqa: E402

HOURS_PER_YEAR = 24 * 365

# name -> (distance, waves, tp, sl, deadline_days, tp_step)
CONFIGS: dict[str, tuple[float, int, float, float, float, float]] = {
    "A_current_3w_sl8_7d": (2.0, 3, 3.0, 8.0, 7.0, 0.0),
    "B_deep_flat_365d": (2.0, 10, 5.0, 50.0, 365.0, 0.0),
    "C_deep_step_365d": (2.0, 10, 5.0, 50.0, 365.0, 0.5),
    "D_deep_step_7d": (2.0, 10, 5.0, 50.0, 7.0, 0.5),
    "E_deep_step_d4_365d": (4.0, 10, 5.0, 50.0, 365.0, 0.5),
}


def _one_symbol(job: tuple) -> list[dict]:
    """All trials for one symbol × one config × one bound. Runs in a worker process."""
    sym, bars, name, cfg, pessimistic, every, wave0, cost, *rest = job
    # Optional 9th element: the set of bar indices this symbol may ENTER on (a point-in-time
    # liquidity rank, see ladder_grid_study --top). None = every bar on the schedule.
    allowed = rest[0] if rest else None
    distance, waves, tp, sl, deadline, step = cfg
    candles = to_candles(bars)
    out = []
    for i in range(24, len(candles) - 1, every):
        if allowed is not None and i not in allowed:
            continue
        r = simulate_kss(
            candles, i, distance_pct=distance, max_waves=waves, tp_pct=tp,
            deadline_days=deadline, sl_pct=sl, cost_pct=cost,
            pessimistic_intrabar=pessimistic, wave0_notional_usd=wave0, tp_step_pct=step,
        )
        # A trial that ran off the END of the data (no exit of any kind) cannot be scored.
        if not (r.tp_hit or r.stopped or r.hit_deadline):
            kind = "data_end"
        elif r.tp_hit:
            kind = "tp"
        elif r.stopped:
            kind = "sl"
        else:
            kind = "horizon"  # still open when the horizon ran out — sold at the last close
        year = datetime.fromtimestamp(candles[i]["ts"] / 1000, timezone.utc).year
        out.append({
            "symbol": sym, "year": year, "kind": kind, "pnl_pct": r.pnl_pct,
            "usd": round(r.pnl_pct / 100 * r.exit_capital, 4), "capital": r.exit_capital,
            "capital_days": r.capital_days, "waves": r.waves_filled, "mae": r.mae_pct,
            "days": r.days_to_tp,
        })
    return out


def _pct(vals: list[float], q: float) -> float:
    if not vals:
        return float("nan")
    s = sorted(vals)
    return s[min(len(s) - 1, int(q * len(s)))]


def summarise(rows: list[dict], waves: int) -> dict:
    scored = [r for r in rows if r["kind"] != "data_end"]
    open_rows = [r for r in rows if r["kind"] == "data_end"]
    n = len(scored)
    if not n:
        return {"n": 0}
    wins = [r["usd"] for r in scored if r["usd"] > 0]
    losses = [r["usd"] for r in scored if r["usd"] <= 0]
    kinds = Counter(r["kind"] for r in scored)
    horizon_rows = [r for r in scored if r["kind"] == "horizon"]
    deep = [r for r in scored if r["waves"] >= 8 and r["usd"] < 0]
    total_loss = sum(losses)
    cap_days = sum(r["capital_days"] for r in scored)
    mean_win = st.mean(wins) if wins else float("nan")
    worst = min(losses) if losses else 0.0
    by_year = {}
    for y in sorted({r["year"] for r in scored}):
        ys = [r for r in scored if r["year"] == y]
        yl = [r["usd"] for r in ys if r["usd"] <= 0]
        by_year[y] = {
            "n": len(ys), "mean_usd": round(st.mean(r["usd"] for r in ys), 3),
            "sum_usd": round(sum(r["usd"] for r in ys), 2),
            "worst_usd": round(min(yl), 2) if yl else 0.0,
            "sl_pct": round(100 * sum(1 for r in ys if r["kind"] == "sl") / len(ys), 2),
            "horizon_pct": round(100 * sum(1 for r in ys if r["kind"] == "horizon") / len(ys), 2),
        }
    return {
        "n": n,
        "open_at_data_end": len(open_rows),
        # Still open when the data ends: the P&L they CARRY (unrealised at the last close) —
        # not a win, not a loss, but a deep ladder that has not come back is where the next
        # big loss lives, so it is reported, not hidden.
        "open_mean_pnl_pct": round(st.mean(r["pnl_pct"] for r in open_rows), 2) if open_rows else 0.0,
        "open_sum_usd": round(sum(r["usd"] for r in open_rows), 2),
        "open_worst_usd": round(min((r["usd"] for r in open_rows), default=0.0), 2),
        "open_10_rungs": sum(1 for r in open_rows if r["waves"] >= 10),
        "tp_pct": round(100 * kinds["tp"] / n, 2),
        "sl_pct": round(100 * kinds["sl"] / n, 2),
        "horizon_pct": round(100 * kinds["horizon"] / n, 2),
        "horizon_mean_pnl_pct": round(st.mean(r["pnl_pct"] for r in horizon_rows), 3) if horizon_rows else 0.0,
        "mean_pnl_pct": round(st.mean(r["pnl_pct"] for r in scored), 4),
        "mean_usd": round(st.mean(r["usd"] for r in scored), 4),
        "sum_usd": round(sum(r["usd"] for r in scored), 2),
        "sum_wins_usd": round(sum(wins), 2),
        "sum_losses_usd": round(total_loss, 2),
        "mean_win_usd": round(mean_win, 3),
        "mean_loss_usd": round(st.mean(losses), 3) if losses else 0.0,
        "loss_p50_usd": round(_pct(sorted(losses), 0.5), 2) if losses else 0.0,
        "loss_p95_usd": round(_pct(sorted(losses), 0.05), 2) if losses else 0.0,
        "loss_p99_usd": round(_pct(sorted(losses), 0.01), 2) if losses else 0.0,
        "worst_usd": round(worst, 2),
        "worst_over_mean_win": round(-worst / mean_win, 1) if wins and mean_win > 0 else float("nan"),
        "share_of_loss_from_8plus_rungs": round(100 * sum(r["usd"] for r in deep) / total_loss, 1) if total_loss < 0 else 0.0,
        "mean_capital_days": round(cap_days / n, 2),
        "pct_per_dollar_day": round(100 * sum(r["usd"] for r in scored) / cap_days, 5) if cap_days else float("nan"),
        "waves_hist": {w: sum(1 for r in scored if r["waves"] == w) for w in range(1, waves + 1)},
        "by_year": by_year,
    }


def _fmt(s: dict) -> str:
    if not s.get("n"):
        return "  (no scored trials)"
    return (
        f"  n={s['n']:>6,}  + {s['open_at_data_end']} still OPEN at data end (mean {s['open_mean_pnl_pct']:+.1f}%, "
        f"carrying {s['open_sum_usd']:+,.0f}$, worst {s['open_worst_usd']:+,.0f}$, "
        f"{s['open_10_rungs']} with all 10 rungs)\n"
        f"  tp {s['tp_pct']:>5.1f}%  sl {s['sl_pct']:>5.1f}%  horizon {s['horizon_pct']:>5.1f}% "
        f"(mean {s['horizon_mean_pnl_pct']:+.1f}%)\n"
        f"  mean {s['mean_pnl_pct']:+.3f}%/trial = {s['mean_usd']:+.2f} $/trial   "
        f"sum wins {s['sum_wins_usd']:+,.0f}$  sum losses {s['sum_losses_usd']:+,.0f}$   net {s['sum_usd']:+,.0f}$\n"
        f"  mean win {s['mean_win_usd']:+.2f}$   mean loss {s['mean_loss_usd']:+.2f}$   "
        f"loss p50/p95/p99 {s['loss_p50_usd']:+.0f}/{s['loss_p95_usd']:+.0f}/{s['loss_p99_usd']:+.0f}$   "
        f"WORST {s['worst_usd']:+,.0f}$ = {s['worst_over_mean_win']:.0f} mean wins\n"
        f"  share of $loss from >=8 rungs {s['share_of_loss_from_8plus_rungs']:.0f}%   "
        f"capital-days/trial {s['mean_capital_days']:,.0f}   %/$-day {s['pct_per_dollar_day']:+.4f}\n"
        f"  waves at exit: {s['waves_hist']}\n"
        + "\n".join(
            f"    {y}: n={v['n']:>5} mean {v['mean_usd']:+.2f}$ sum {v['sum_usd']:+,.0f}$ "
            f"worst {v['worst_usd']:+,.0f}$ sl {v['sl_pct']:.1f}% horizon {v['horizon_pct']:.1f}%"
            for y, v in s["by_year"].items())
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--db", default="data/research/market.db")
    p.add_argument("--interval", default="1h", help="1h (2024-2026 in the research DB) or 1d (2021-2026)")
    p.add_argument("--symbols", type=int, default=120, help="seeded sample of coins with >= 2y of bars")
    p.add_argument("--every", type=int, default=168, help="entry spacing in BARS (168 = weekly on 1h, 7 on 1d)")
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--wave0", type=float, default=75.0, help="first-wave USD (owner's choice: $75)")
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--configs", default=",".join(CONFIGS))
    p.add_argument("--out", default="docs/ladder-depth-2026-09-12")
    args = p.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    cost = costengine.round_trip_cost_pct()
    series = load(Path(args.db), args.interval)
    bars_per_year = HOURS_PER_YEAR if args.interval == "1h" else 365
    eligible = sorted(s for s, b in series.items() if len(b) >= 2 * bars_per_year)
    rng = random.Random(args.seed)
    chosen = sorted(rng.sample(eligible, min(args.symbols, len(eligible))))
    print(f"{len(eligible)} coins with >=2y of 1h bars; sampled {len(chosen)} (seed {args.seed}); "
          f"entries every {args.every} bars; wave0 ${args.wave0:g}; cost {cost:.2f}%\n")

    results: dict[str, dict] = {}
    lines: list[str] = []
    t0 = time.time()
    with Pool(args.workers) as pool:
        for name in args.configs.split(","):
            cfg = CONFIGS[name]
            for pessimistic in (False, True):
                jobs = [(s, series[s], name, cfg, pessimistic, args.every, args.wave0, cost)
                        for s in chosen]
                rows = [r for chunk in pool.imap_unordered(_one_symbol, jobs, chunksize=2) for r in chunk]
                bound = "PESSIMISTIC" if pessimistic else "OPTIMISTIC"
                summ = summarise(rows, cfg[1])
                results[f"{name}/{bound}"] = summ
                head = (f"{name}  distance {cfg[0]}% waves {cfg[1]} tp {cfg[2]}% "
                        f"(+{cfg[5]}/rung) sl {cfg[3]}% horizon {cfg[4]:g}d   [{bound}]   "
                        f"({time.time() - t0:,.0f}s)")
                block = head + "\n" + _fmt(summ) + "\n"
                print(block)
                lines.append(block)

    out = Path(args.out)
    out.with_suffix(".json").write_text(json.dumps(results, indent=1, default=str), encoding="utf-8")
    out.with_suffix(".txt").write_text(
        f"ladder_depth_study  interval={args.interval} symbols={len(chosen)} every={args.every} bars wave0=${args.wave0:g} "
        f"cost={cost:.2f}% seed={args.seed}\n\n" + "\n".join(lines), encoding="utf-8")
    print(f"wrote {out.with_suffix('.txt')} and {out.with_suffix('.json')}  ({time.time() - t0:,.0f}s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
