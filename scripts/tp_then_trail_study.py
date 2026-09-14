"""Does "take-profit, then trail" (Ride & Trail v2) beat the plain fixed take-profit?

WHY THIS EXISTS
    `app.backtest.simulate_kss` sells the whole ladder the instant a bar's high reaches the
    take-profit target. The owner approved an alternative: when the target is reached, ARM a
    trailing stop floored at that TP price instead of selling — the trade can then only end at
    the TP price or higher, never below it (see `trail_after_tp_pct` in simulate_kss's
    docstring, tests/app/test_backtest_tp_then_trail.py). This measures whether any trail width
    actually captures more $/trial than selling flat at the target, and what it costs in
    capital-days (money tied up longer waiting for the trail to give back its gains).

CONFIG (owner's numbers, matches recent ladder studies): distance 4%, 30 rungs, TP 5% + 0.5%
    per filled rung, no stop-loss, 60-day deadline, wave0 $17, round-trip cost from
    `app.costengine.round_trip_cost_pct()`. Trail widths compared: 0 (off, the baseline),
    1, 2, 3, 5, 8 percent.

TWO INTRA-BAR BOUNDS, ALWAYS (see scripts/ladder_panel_study.py) — both printed; when they
    disagree the honest answer is "unknown at this resolution".

    python scripts/tp_then_trail_study.py [--interval 1d] [--symbols 641] [--min-years 2]
        [--every 7] [--workers 8] [--out docs/tp-then-trail-2026-09-14-1d]
"""

from __future__ import annotations

import argparse
import json
import random
import statistics as st
import sys
import time
from datetime import datetime, timezone
from multiprocessing import Pool
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import costengine  # noqa: E402
from app.backtest import simulate_kss  # noqa: E402
from scripts.ladder_depth_study import HOURS_PER_YEAR, _pct  # noqa: E402
from scripts.ladder_grid_study import dollar_share, mtm_share  # noqa: E402
from scripts.ladder_panel_study import to_candles  # noqa: E402
from scripts.liquidity_tier_study import load  # noqa: E402

DISTANCE, MAX_WAVES, TP, TP_STEP, SL, DEADLINE = 4.0, 30, 5.0, 0.5, 0.0, 60.0
TRAILS = [0.0, 1.0, 2.0, 3.0, 5.0, 8.0]


def _one_symbol(job: tuple) -> list[dict]:
    """All trials for one symbol x one trail width x one intrabar bound. Runs in a worker
    process. Same output shape as scripts/ladder_depth_study._one_symbol, plus armed_exit_pct
    (SimResult.armed_exit_pct — None unless this trial armed and exited through the trail)."""
    sym, bars, trail, pessimistic, every, wave0, cost = job
    candles = to_candles(bars)
    out = []
    for i in range(24, len(candles) - 1, every):
        r = simulate_kss(
            candles, i, distance_pct=DISTANCE, max_waves=MAX_WAVES, tp_pct=TP,
            deadline_days=DEADLINE, sl_pct=SL, cost_pct=cost,
            pessimistic_intrabar=pessimistic, wave0_notional_usd=wave0, tp_step_pct=TP_STEP,
            trail_after_tp_pct=trail,
        )
        # A trial that ran off the END of the data (no exit of any kind) cannot be scored.
        if not (r.tp_hit or r.stopped or r.hit_deadline):
            kind = "data_end"
        elif r.tp_hit:
            kind = "tp"
        elif r.stopped:
            kind = "sl"
        else:
            kind = "horizon"  # deadline reached — sold at the last close
        year = datetime.fromtimestamp(candles[i]["ts"] / 1000, timezone.utc).year
        out.append({
            "symbol": sym, "year": year, "kind": kind, "pnl_pct": r.pnl_pct,
            "usd": round(r.pnl_pct / 100 * r.exit_capital, 4), "capital": r.exit_capital,
            "capital_days": r.capital_days, "waves": r.waves_filled, "mae": r.mae_pct,
            "days": r.days_to_tp, "armed_exit_pct": r.armed_exit_pct,
        })
    return out


def summarise(rows: list[dict]) -> dict:
    """Per-variant rollup: the ladder_depth_study-style headline numbers plus the
    trail-specific ones (armed-and-exited-above-floor share, and the extra % it captured)."""
    scored = [r for r in rows if r["kind"] != "data_end"]
    open_rows = [r for r in rows if r["kind"] == "data_end"]
    n = len(scored)
    if not n:
        return {"n": 0}
    wins = [r["usd"] for r in scored if r["usd"] > 0]
    losses = [r["usd"] for r in scored if r["usd"] <= 0]
    tp_rows = [r for r in scored if r["kind"] == "tp"]
    horizon_rows = [r for r in scored if r["kind"] == "horizon"]
    cap_days = sum(r["capital_days"] for r in scored)
    # Among TP exits, the ones that armed a trail AND closed above the floor (armed_exit_pct >
    # 0) — as opposed to arming and giving everything back down to the floor (== 0), or never
    # arming at all (None, only possible when trail_after_tp_pct == 0).
    above_floor = [r for r in tp_rows
                   if r["armed_exit_pct"] is not None and r["armed_exit_pct"] > 0]
    armed_vals = sorted(r["armed_exit_pct"] for r in above_floor)
    return {
        "n": n,
        "open_at_data_end": len(open_rows),
        "open_sum_usd": round(sum(r["usd"] for r in open_rows), 2),
        "tp_pct": round(100 * len(tp_rows) / n, 2),
        "horizon_pct": round(100 * len(horizon_rows) / n, 2),
        "mean_pnl_pct": round(st.mean(r["pnl_pct"] for r in scored), 4),
        "mean_usd": round(st.mean(r["usd"] for r in scored), 4),
        "sum_wins_usd": round(sum(wins), 2),
        "sum_losses_usd": round(sum(losses), 2),
        "worst_usd": round(min(losses), 2) if losses else 0.0,
        "horizon_mean_pnl_pct": round(st.mean(r["pnl_pct"] for r in horizon_rows), 3) if horizon_rows else 0.0,
        "mean_capital_days": round(cap_days / n, 2),
        "pct_per_dollar_day": round(100 * sum(r["usd"] for r in scored) / cap_days, 5) if cap_days else float("nan"),
        "armed_above_floor_share_of_tp_pct": round(100 * len(above_floor) / len(tp_rows), 2) if tp_rows else 0.0,
        "armed_exit_pct_mean": round(st.mean(armed_vals), 4) if armed_vals else 0.0,
        "armed_exit_pct_p90": round(_pct(armed_vals, 0.9), 4) if armed_vals else 0.0,
    }


def run_variant(pool: Pool, series: dict, chosen: list[str], trail: float, pessimistic: bool,
                every: int, wave0: float, cost: float) -> list[dict]:
    jobs = [(s, series[s], trail, pessimistic, every, wave0, cost) for s in chosen]
    return [r for chunk in pool.imap_unordered(_one_symbol, jobs, chunksize=2) for r in chunk]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--db", default="data/research/market.db")
    p.add_argument("--interval", default="1d")
    p.add_argument("--symbols", type=int, default=641)
    p.add_argument("--min-years", type=float, default=2.0)
    p.add_argument("--every", type=int, default=7)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--wave0", type=float, default=17.0)
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--out", default="docs/tp-then-trail-2026-09-14")
    args = p.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    cost = costengine.round_trip_cost_pct()
    series = load(Path(args.db), args.interval)
    bars_per_year = HOURS_PER_YEAR if args.interval == "1h" else 365
    eligible = sorted(s for s, b in series.items() if len(b) >= args.min_years * bars_per_year)
    chosen = sorted(random.Random(args.seed).sample(eligible, min(args.symbols, len(eligible))))
    print(f"{len(chosen)} coins ({args.interval}, >= {args.min_years}y), entries every "
          f"{args.every} bars, wave0 ${args.wave0:g}, distance {DISTANCE}%, waves {MAX_WAVES}, "
          f"tp {TP}% +{TP_STEP}/rung, sl {SL}, deadline {DEADLINE:g}d, cost {cost:.2f}%\n"
          f"trail_after_tp_pct in {TRAILS}\n")

    t0 = time.time()
    out: dict = {"meta": vars(args) | {"cost": cost, "coins": len(chosen),
                                       "distance": DISTANCE, "waves": MAX_WAVES, "tp": TP,
                                       "tp_step": TP_STEP, "sl": SL, "deadline": DEADLINE,
                                       "trails": TRAILS},
                "variants": []}

    with Pool(args.workers) as pool:
        baseline_usd = {}  # bound -> mean_usd at trail=0, for the $ delta column
        for trail in TRAILS:
            rec: dict = {"trail": trail}
            for pessimistic in (False, True):
                rows = run_variant(pool, series, chosen, trail, pessimistic, args.every,
                                   args.wave0, cost)
                s = summarise(rows)
                bound = "PESS" if pessimistic else "OPT"
                if trail == 0.0:
                    baseline_usd[bound] = s.get("mean_usd", 0.0)
                s["mtm_share"] = round(100 * mtm_share(s), 2) if s.get("n") else 0.0
                s["dollar_share"] = round(100 * dollar_share(s), 2) if s.get("n") else 0.0
                s["delta_usd_vs_trail0"] = round(s.get("mean_usd", 0.0) - baseline_usd.get(bound, 0.0), 4)
                rec[bound] = s
            out["variants"].append(rec)
            o, q = rec["OPT"], rec["PESS"]
            print(f"trail {trail:>4.1f}%  MTM opt/pess {o['mtm_share']:5.1f}/{q['mtm_share']:5.1f}%  "
                  f"$/trial {o['mean_usd']:+7.2f}/{q['mean_usd']:+7.2f}  "
                  f"(delta {o['delta_usd_vs_trail0']:+6.2f}/{q['delta_usd_vs_trail0']:+6.2f})  "
                  f"tp {q['tp_pct']:5.1f}%  horizon {q['horizon_pct']:4.1f}%  "
                  f"cap-days {q['mean_capital_days']:>8,.0f}  %/$-day {q['pct_per_dollar_day']:+.4f}  "
                  f"armed>floor {q['armed_above_floor_share_of_tp_pct']:5.1f}% of tp  "
                  f"armed% mean/p90 {q['armed_exit_pct_mean']:+.2f}/{q['armed_exit_pct_p90']:+.2f}  "
                  f"({time.time()-t0:.0f}s)")

    Path(args.out).with_suffix(".json").write_text(json.dumps(out, indent=1, default=str), encoding="utf-8")
    print(f"\nwrote {Path(args.out).with_suffix('.json')}  ({time.time()-t0:.0f}s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
