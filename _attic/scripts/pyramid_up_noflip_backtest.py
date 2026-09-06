"""
Adversarial follow-up to scripts/pyramid_up_backtest.py's baseline finding: ~36% of trials flip
(the defensive rung at entry*(1-arm_pct/100) fills and the session hands off to dca_down) and
that flip bucket alone accounts for MORE than 100% of the strategy's net loss (findings §6,
scratchpad/pyramid_up_findings.md). That exclusion was ex-post (you can't skip a trade for
flipping before it flips) — this script tests the actionable, causal alternative: what if the
defensive rung / reversal-flip mechanism did not exist at all?

Everything reusable (to_daily/ATR/TA/router, SymbolData, load_universe, entry_context,
collect_trials, collect_bh_trials, dollar_day_stat, summarize, cluster_bootstrap_diff,
half_year_split, date_label, BASELINE_CFG, ENTRY_START_BAR/ENTRY_SPACING_BARS/MAX_ADDS_CAP,
simulate_pyramid_up_trial [used unmodified for the "baseline" comparison arm],
simulate_buy_and_hold) is imported UNCHANGED from pyramid_up_backtest.py — none of it is
reimplemented or copy-pasted here.

WHAT THIS FILE ADDS (the only new code, both trivial deltas on the original trial loop):

  simulate_pyramid_up_trial_variant(sym, entry_i, cfg, ordering, variant) — a copy of
  ``pyramid_up_backtest.simulate_pyramid_up_trial`` (pyramid_up_backtest.py:562-702) with exactly
  two changes, gated on ``variant``:

  1. variant in {"nf", "ts"}: step 1 of the per-bar loop (the defensive-rung fill/flip check,
     original lines 608-616, and the ``defensive_trigger``/``defensive_qty`` setup at lines
     584-585, and the ``defensive_status`` state var and its "cancelled" transition at 591/651-652)
     is DELETED, not merely disabled by a flag check — the code path does not exist for these two
     variants. The position's only protection before trailing arms is the SAME pre-arm hard-SL
     branch that already existed in the original at lines 654-658
     (``if low <= avg*(1-sl_pct/100): exit "hard_sl"``) — unchanged. sl_pct is swept {5,8,12} for
     "nf". Once trailing arms (add-fill path or the standalone RIDE-threshold path, both
     unchanged from the original), protection is the normal trailing SL/TP channel, also
     unchanged.

  2. variant == "ts" ONLY: the pre-arm hard-SL trigger price is no longer avg*(1-sl_pct/100) but a
     FIXED price computed once at trial start from ``entry`` (never recomputed from ``avg`` as
     adds fill): ``entry * (1 - 5.0/100)`` — the exact price the deleted defensive rung used to
     sit at (arm_pct=5.0 in BASELINE_CFG). This isolates "stop out for real at the price the
     defensive rung used to buy at" from "just don't have a defensive rung" (variant "nf", whose
     hard-SL floor rises with avg once an add has filled, since pyramid_up only adds on the way
     UP). cfg["sl_pct"] is ignored for "ts".

  run_variant_battery(...) — mirrors run_battery's item1/item3 shape (trials, total $,
  $/dollar-day, win rate, stop rate, avg adds — flip rate is always 0 for nf/ts by construction),
  PLUS what this follow-up specifically asks for: buy-and-hold multiple, date-clustered bootstrap
  CI of (variant - buy_and_hold) $/dollar-day, half-year (primary)/calendar-year (extended) split
  with positive-period counts, and a concentration test (top-1%/top-5% share of total $, and
  total-$ with the single best calendar month removed).

Usage:
    .venv/Scripts/python.exe scripts/pyramid_up_noflip_backtest.py \\
        --data <u83_4h.pkl> --extended <u30_4h_long.pkl> --out <dir> [--n-boot 2000] [--seed 42]
"""

from __future__ import annotations

import argparse
import json
import math
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import pyramid_up_backtest as pub

TS_FIXED_SL_FROM_ENTRY_PCT = 5.0  # the old defensive rung's arm_pct — the price TS stops out at


# ======================================================================================
# The only new trial-simulation code — see module docstring for the exact diff vs the original.
# ======================================================================================

def simulate_pyramid_up_trial_variant(sym: "pub.SymbolData", entry_i: int, cfg: dict,
                                       ordering: str, variant: str) -> dict:
    """variant in {"baseline", "nf", "ts"}. "baseline" is byte-for-byte the original mechanism
    (defensive rung + flip) — kept only as an in-process cross-check hook; battery numbers for
    "baseline" are produced by calling the original ``pub.simulate_pyramid_up_trial`` directly,
    not this copy, so a bug in this function's baseline branch can never contaminate the reported
    baseline row. "nf" and "ts" delete the defensive rung/flip; "ts" additionally fixes the
    pre-arm hard-SL price at entry*(1-5%) instead of avg*(1-sl_pct/100)."""
    bars = sym.bars
    entry = bars[entry_i]["close"]
    wave0_usd = cfg["wave0_usd"]
    step_pct = cfg["step_pct"]
    size_ratio = cfg["size_ratio"]
    max_adds = min(cfg["max_adds"], pub.MAX_ADDS_CAP)
    lock_pct = cfg["lock_pct"]
    arm_pct = cfg["arm_pct"]
    trail_lock_pct = cfg["trail_lock_pct"]
    trail_atr_mult = cfg["trail_atr_mult"]
    trail_min_pct = cfg["trail_min_pct"]
    tp_gap_pct = cfg["tp_gap_pct"]
    exit_fee_mult = cfg["exit_fee_mult"]
    sl_pct = cfg["sl_pct"]
    cost_pct = cfg["cost_pct"]

    base_qty = wave0_usd / entry
    adds = [(n, pub.add_trigger_price(entry, n, step_pct), pub.add_qty(base_qty, n, size_ratio))
            for n in range(1, max_adds + 1)]

    if variant == "baseline":
        defensive_trigger = entry * (1 - arm_pct / 100.0)
        defensive_qty = (wave0_usd / defensive_trigger) if defensive_trigger > 0 else 0.0
        defensive_status = "armed"
    ts_fixed_sl_price = entry * (1 - TS_FIXED_SL_FROM_ENTRY_PCT / 100.0) if variant == "ts" else None

    total_qty = base_qty
    total_cost = base_qty * entry
    avg = entry
    next_add = 0
    trail_active = False
    peak = 0.0
    trail_sl = 0.0
    adds_filled = 0

    L = len(bars)
    end_j = min(entry_i + cfg["deadline_bars"], L - 1)
    exit_reason = None
    exit_price = None
    exit_j = None

    for j in range(entry_i + 1, end_j + 1):
        bar = bars[j]
        o, h, low, c = bar["open"], bar["high"], bar["low"], bar["close"]
        armed_this_bar = False

        # 1. defensive-rung fill (reversal-flip) — ONLY exists for variant == "baseline".
        if variant == "baseline":
            if defensive_status == "armed" and low <= defensive_trigger:
                fill_price = min(defensive_trigger, o)
                total_cost += defensive_qty * fill_price
                total_qty += defensive_qty
                avg = total_cost / total_qty
                defensive_status = "filled"
                exit_reason, exit_price, exit_j = "flip", fill_price, j
                break

        # 2. up-add fill loop (lowest n first, sequential) — unchanged from the original.
        while next_add < len(adds):
            n, trig, qty = adds[next_add]
            if h >= trig:
                fill_price = max(trig, o)
                total_cost += qty * fill_price
                total_qty += qty
                avg = total_cost / total_qty
                adds_filled += 1
                next_add += 1
                if not trail_active:
                    ff = pub.fee_floor_price(avg, exit_fee_mult, cost_pct)
                    new_sl = pub.stop_after_add(avg, lock_pct, ff)
                    if fill_price > new_sl:
                        trail_active = True
                        peak = max(fill_price, avg)
                        trail_sl = max(new_sl, trail_sl)
                        armed_this_bar = True
            else:
                break

        # 3. arm (standalone RIDE threshold) or pre-arm hard-SL — the hard-SL floor is the only
        #    line that changes for "ts" (fixed entry-relative price instead of avg-relative).
        if not trail_active:
            arm_threshold = avg * (1 + arm_pct / 100.0)
            if h >= arm_threshold:
                trail_active = True
                peak = arm_threshold
                atr = sym.atr_by_bar[j]
                td = pub.trail_distance_pct(atr, trail_atr_mult, trail_min_pct)
                trail_sl = pub.compute_sl(peak=peak, avg=avg, distance_pct=step_pct, trail_dist_pct=td,
                                           prev_sl=0.0, exit_fee_mult=exit_fee_mult, cost_pct=cost_pct,
                                           trail_lock_pct=trail_lock_pct)
                armed_this_bar = True
                if variant == "baseline" and defensive_status == "armed":
                    defensive_status = "cancelled"
                next_add = len(adds)  # cancel remaining armed up-adds
            else:
                if variant == "ts":
                    floor = ts_fixed_sl_price
                else:
                    floor = avg * (1 - sl_pct / 100.0)
                if low <= floor:
                    exit_reason, exit_price, exit_j = "hard_sl", floor, j
                    break

        # 4. armed channel — carried TP/SL check, then ratchet (no exit on the arm tick) —
        #    unchanged from the original.
        if trail_active and not armed_this_bar:
            carried_tp = pub.compute_tp(sl=trail_sl, avg=avg, tp_gap_pct=tp_gap_pct,
                                         exit_fee_mult=exit_fee_mult, cost_pct=cost_pct)
            hit_tp = h >= carried_tp
            hit_sl = low <= trail_sl
            if ordering == "tp_first":
                if hit_tp:
                    exit_reason, exit_price, exit_j = "tp", carried_tp, j
                    break
                if hit_sl:
                    exit_reason, exit_price, exit_j = "trail_sl", trail_sl, j
                    break
            else:
                if hit_sl:
                    exit_reason, exit_price, exit_j = "trail_sl", trail_sl, j
                    break
                if hit_tp:
                    exit_reason, exit_price, exit_j = "tp", carried_tp, j
                    break
            atr = sym.atr_by_bar[j]
            td = pub.trail_distance_pct(atr, trail_atr_mult, trail_min_pct)
            new_peak = max(peak, h)
            new_sl = pub.compute_sl(peak=new_peak, avg=avg, distance_pct=step_pct, trail_dist_pct=td,
                                     prev_sl=trail_sl, exit_fee_mult=exit_fee_mult, cost_pct=cost_pct,
                                     trail_lock_pct=trail_lock_pct)
            peak, trail_sl = new_peak, new_sl

    if exit_reason is None:
        exit_reason, exit_price, exit_j = "deadline", bars[end_j]["close"], end_j

    gross_pct = (exit_price / avg - 1.0) * 100.0
    net_pct = gross_pct - cost_pct
    pnl_dollars = total_cost * (net_pct / 100.0)
    days_held = (bars[exit_j]["ts"] - bars[entry_i]["ts"]) / pub.MS_DAY

    return {
        "symbol": sym.symbol, "entry_i": entry_i, "entry_ts": bars[entry_i]["ts"],
        "exit_j": exit_j, "exit_reason": exit_reason, "avg": avg, "exit_price": exit_price,
        "deployed": total_cost, "adds_filled": adds_filled, "gross_pct": gross_pct,
        "net_pct": net_pct, "pnl_dollars": pnl_dollars, "days_held": days_held,
    }


def collect_trials_variant(universe: dict, cfg: dict, ordering: str, variant: str) -> list[dict]:
    trials = []
    for sym in universe.values():
        L = len(sym.bars)
        for i in range(pub.ENTRY_START_BAR, L - pub.ENTRY_SPACING_BARS, pub.ENTRY_SPACING_BARS):
            trials.append(simulate_pyramid_up_trial_variant(sym, i, cfg, ordering, variant))
    return trials


# ======================================================================================
# Reporting helpers new to this follow-up
# ======================================================================================

def month_label(ts_ms: float) -> str:
    dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    return f"{dt.year:04d}-{dt.month:02d}"


def calendar_year_split(trials: list[dict]) -> dict[str, float]:
    buckets: dict[str, list[dict]] = defaultdict(list)
    for t in trials:
        dt = datetime.fromtimestamp(t["entry_ts"] / 1000, tz=timezone.utc)
        buckets[str(dt.year)].append(t)
    return {k: round(pub.dollar_day_stat(v) * 100, 6) for k, v in sorted(buckets.items())}


def concentration(trials: list[dict]) -> dict:
    """Top-1%/top-5% share of total $ pnl, plus the same total with the single best calendar
    month (by summed pnl_dollars) removed."""
    n = len(trials)
    if n == 0:
        return {"n": 0}
    total = sum(t["pnl_dollars"] for t in trials)
    ordered = sorted(trials, key=lambda t: t["pnl_dollars"], reverse=True)
    top1_n = max(1, math.ceil(0.01 * n))
    top5_n = max(1, math.ceil(0.05 * n))
    top1_sum = sum(t["pnl_dollars"] for t in ordered[:top1_n])
    top5_sum = sum(t["pnl_dollars"] for t in ordered[:top5_n])

    months: dict[str, list[dict]] = defaultdict(list)
    for t in trials:
        months[month_label(t["entry_ts"])].append(t)
    best_month, best_month_pnl = None, None
    for m, ts in months.items():
        s = sum(t["pnl_dollars"] for t in ts)
        if best_month_pnl is None or s > best_month_pnl:
            best_month, best_month_pnl = m, s
    remainder = [t for t in trials if month_label(t["entry_ts"]) != best_month]
    dd_with_best_month = round(pub.dollar_day_stat(trials) * 100, 6)
    dd_without_best_month = round(pub.dollar_day_stat(remainder) * 100, 6) if remainder else None

    return {
        "n": n, "total_pnl_usd": round(total, 2),
        "top1pct_n": top1_n, "top1pct_pnl_usd": round(top1_sum, 2),
        "top1pct_share_of_total": (round(top1_sum / total, 4) if total != 0 else None),
        "top5pct_n": top5_n, "top5pct_pnl_usd": round(top5_sum, 2),
        "top5pct_share_of_total": (round(top5_sum / total, 4) if total != 0 else None),
        "best_month": best_month, "best_month_pnl_usd": round(best_month_pnl, 2) if best_month_pnl is not None else None,
        "dollar_day_pct_with_best_month": dd_with_best_month,
        "dollar_day_pct_without_best_month": dd_without_best_month,
        "n_without_best_month": len(remainder),
    }


VARIANT_CONFIGS = [
    ("nf5", "nf", 5.0),
    ("nf8", "nf", 8.0),
    ("nf12", "nf", 12.0),
    ("ts", "ts", None),  # sl_pct unused for ts (fixed internally at TS_FIXED_SL_FROM_ENTRY_PCT)
]


def run_variant_battery(data_path: Path, out_dir: Path, label: str, n_boot: int, seed: int,
                         quiet: bool) -> dict:
    def log(msg: str) -> None:
        if not quiet:
            print(f"[{label}] {msg}", flush=True)

    t0 = time.time()
    raw = pub.load_universe(data_path)
    if "BTC" not in raw:
        raise SystemExit(f"{data_path}: universe has no BTC series")
    universe = {s: pub.SymbolData(s, b) for s, b in raw.items()}
    btc = universe["BTC"]
    log(f"loaded {len(universe)} symbols, precomputed ATR in {time.time()-t0:.1f}s")

    result: dict = {"label": label, "data_file": str(data_path), "n_symbols": len(universe)}
    orderings = ["tp_first", "sl_first"]

    # --- baseline (original, unmodified trial function) + buy-and-hold, both orderings ---
    baseline_trials = {o: pub.collect_trials(universe, btc, pub.BASELINE_CFG, o, None) for o in orderings}
    bh_trials = pub.collect_bh_trials(universe, pub.BASELINE_CFG)
    bh_dd = pub.dollar_day_stat(bh_trials)
    result["buy_and_hold"] = pub.summarize(bh_trials)
    result["baseline"] = {}
    for o in orderings:
        tr = baseline_trials[o]
        boot = pub.cluster_bootstrap_diff(tr, bh_trials, n_boot=n_boot, seed=seed)
        result["baseline"][o] = {
            **pub.summarize(tr),
            "multiple_of_bh": (pub.dollar_day_stat(tr) / bh_dd) if bh_dd != 0 else None,
            "bootstrap_vs_bh": boot,
            "half_or_calendar_year": (pub.half_year_split(tr) if label == "primary" else calendar_year_split(tr)),
            "concentration": concentration(tr),
        }
    log(f"baseline done in {time.time()-t0:.1f}s")

    # --- NF (5/8/12) and TS variants, both orderings ---
    result["variants"] = {}
    for vname, vkind, sl in VARIANT_CONFIGS:
        cfg = dict(pub.BASELINE_CFG)
        if sl is not None:
            cfg["sl_pct"] = sl
        result["variants"][vname] = {}
        for o in orderings:
            t1 = time.time()
            tr = collect_trials_variant(universe, cfg, o, vkind)
            dd = pub.dollar_day_stat(tr)
            boot = pub.cluster_bootstrap_diff(tr, bh_trials, n_boot=n_boot, seed=seed + hash((vname, o)) % 10_000)
            result["variants"][vname][o] = {
                **pub.summarize(tr),
                "multiple_of_bh": (dd / bh_dd) if bh_dd != 0 else None,
                "bootstrap_vs_bh": boot,
                "half_or_calendar_year": (pub.half_year_split(tr) if label == "primary" else calendar_year_split(tr)),
                "concentration": concentration(tr),
            }
            log(f"variant={vname} ordering={o}: trials={len(tr)} $/day={dd*100:.4f}% "
                f"CI=[{boot['ci_lo_pct']:.4f},{boot['ci_hi_pct']:.4f}] excl0={boot['excludes_zero']} "
                f"({time.time()-t1:.1f}s)")

    result["wall_time_s"] = round(time.time() - t0, 1)
    return result


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", required=True, type=Path, help="Primary pickle (u83_4h.pkl)")
    ap.add_argument("--extended", required=True, type=Path, help="Extended pickle (u30_4h_long.pkl)")
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--n-boot", type=int, default=2000)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)

    primary = run_variant_battery(args.data, args.out, "primary", args.n_boot, args.seed, args.quiet)
    extended = run_variant_battery(args.extended, args.out, "extended", args.n_boot, args.seed, args.quiet)

    out = {
        "hypothesis_count": {
            "variant_configs": len(VARIANT_CONFIGS),
            "datasets": 2, "orderings": 2,
            "total_point_estimates": len(VARIANT_CONFIGS) * 2 * 2,
            "note": "4 variant-configs (nf5, nf8, nf12, ts) x 2 datasets x 2 orderings = 16 "
                    "bootstrap CIs vs buy-and-hold, each with its own concentration test and "
                    "half/calendar-year split reported alongside (descriptive, not additional "
                    "inferential hypotheses).",
        },
        "primary": primary,
        "extended": extended,
    }
    with open(args.out / "noflip_results.json", "w") as f:
        json.dump(out, f, indent=2, default=str)
    print(f"\nWrote {args.out / 'noflip_results.json'}")


if __name__ == "__main__":
    main()
