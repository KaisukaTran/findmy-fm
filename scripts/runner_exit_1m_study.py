"""Runner-exit study, 1-MINUTE resolution — does the owner's "let it float" rule (V4a) or a
post-TP re-buy runner (V5) beat selling 100% at the resting TP limit, once the exit decision is
modelled at the resolution the live app actually trades at?

WHY THIS EXISTS
    `scripts/runner_exit_study.py` answered this on 1h bars and found the trailing variants
    undetermined: the pessimistic/optimistic intra-BAR bounds straddle zero at a 2% gap. An
    hourly bar also has a real modelling flaw — the bar that TOUCHES the take-profit is never
    itself checked for a stop, so an in-hour reversal is only caught at the NEXT bar's open
    (`runner_exit_study.py` `run_ladder`/`v4a_variant`/`v4b_variant`, the "just_armed"
    convention). The live app exits on realtime WebSocket ticks every 2 seconds, so 1-minute
    bars are the right resolution to re-ask the question and to fix that flaw.

WHAT STAYS ON 1H, WHAT MOVES TO 1M
    The pre-TP ladder (which rungs fill, the touch bar, avg/qty/cost) is IDENTICAL to the 1h
    study for every variant — it is re-used verbatim (`run_ladder`/`verify_v0` imported from
    `runner_exit_study.py`, itself verified bar-for-bar against `app.backtest.simulate_kss`).
    Only what happens FROM the take-profit touch onward is re-modelled on 1-minute bars:
      1. The touch HOUR is located as before; the touch MINUTE is the first 1m bar in that hour
         whose high >= the TP price. If no such minute exists (a data mismatch between the 1h
         and 1m archives) the event is dropped and counted (`--report` prints N).
      2. V4a (owner's rule): do not sell at the touch; cancel remaining rungs; hold with a
         trailing stop = max(avg*1.03, peak*(1-g)), g in {2,3,5}%.
      3. V5 (new): sell 100% at the TP limit exactly like V0 (that leg's P&L is identical to
         V0's, by construction) — then, if the price is still at/above the TP price 2 minutes
         later (the app's own post-fill re-buy delay, ~90s rounded up to whole minutes), buy a
         SEPARATELY SIZED runner and trail it the same way. Session net = V0 net + runner net.

TWO INTRA-MINUTE BOUNDS, ALWAYS, AND THE TOUCH MINUTE IS NOW CHECKED
    A single bar's true high/low order is unknowable, so every stop is evaluated under both
    orderings, exactly as `runner_exit_study.py` does per-hour:
      pessimistic — the low is assumed to arrive BEFORE the high, so a stop is tested against
        the peak as it stood BEFORE this minute's high could raise it (worst case).
      optimistic  — the reverse: the high (and the peak it sets) lands before the low.
    THE FIX: the touch minute itself is now included in this walk, using the touch itself as the
    "prior peak". Concretely, for a walk starting at the touch minute m0 with initial peak p0 =
    the TP price (the exact level whose crossing DEFINED the touch, not m0's own high — using
    m0's own high here would already spend the optimistic bound's benefit of the doubt on the
    very bar the fix exists to police):
      pessimistic: stop_i is computed from the running max of {p0, high_0, ..., high_(i-1)} —
        i.e. the peak as it existed BEFORE minute i's own high can update it — and low_i is
        compared to that stop, for EVERY i including i=0 (m0 itself: stop_0 = f(p0)).
      optimistic:  stop_i is computed from the running max of {p0, high_0, ..., high_i} — i.e.
        minute i's own high is allowed to raise the peak before its own low is tested.
    This is a straight generalisation of `runner_exit_study.py`'s per-hour convention down to
    per-minute — the only change is that m0 is no longer exempt.

    Implemented with `numpy` running-max ("peak_before"/"peak_after" arrays below) rather than a
    per-minute Python loop: a naive loop over up to 86,400 minutes (the 60-day deadline) x
    ~110k touch events from the 1h study would be too slow to be a background job; the
    vectorised form is algebraically identical (verified against a slow per-event Python loop on
    a small sample — see `_slow_walk_reference` used only in `--selftest`).

FEES / SLIPPAGE — unchanged from `runner_exit_study.py`: maker 0.1% (TP legs, runner buy's
    "limit-like" assumption is NOT used — the runner is a market/taker buy per the spec), taker
    0.1% (stops, deadline closes, runner buy), stop slippage in {0.1%, 0.3%}. A stop fill is
    stop x (1 - slip) + taker fee, EXCEPT a true gap — the minute opened at/below the stop that
    was already in force when it began (never the arm minute, whose open precedes the touch) —
    which fills at the open (see the FILL RULE comment in `trail_walk`; fixed 2026-09-26). The deadline close (60 days from the session's entry) sells at that
    minute's CLOSE, taker fee, no slippage buffer (not a stop-hunt).

V5 RUNNER SIZING
    decision minute = the first 1m bar at/after touch_ts + 120,000 ms (2 minutes after the touch
    minute — the app's ~90s post-fill guard, rounded up to a whole minute boundary since we only
    have minute bars). decision_price = that minute's OPEN. A runner is bought only if
    decision_price >= the TP price (else the move already failed and V5 == V0 for that event —
    counted as "no runner").
    runner_usd = min(v0_proceeds, v0_net_profit / (g/100 + 0.002 + 0.03)) — bounded by the TP
    leg's own proceeds, and sized so a full loss of the runner's g%-gap cannot by itself put the
    WHOLE session net below zero with some margin (0.2% round-trip fee + a 3% gap-risk buffer).
    "full" sizing (runner_usd = v0_proceeds) is also computed FOR REFERENCE ONLY (`v5_full_ref`
    in results.json) — it is not a headline variant because it is not the owner's ask.
    The runner buy is a taker fill: fill = decision_price*(1+slip), cost/unit = fill*(1+taker
    fee); qty = runner_usd / (cost/unit), so the runner's OWN cost basis is exactly runner_usd.
    The runner has NO breakeven floor (V0 already locked in the session's profit) — just
    stop = peak*(1-g), peak starting at decision_price, walked with the same touch-inclusive
    convention as V4a (the decision minute is itself checked).

USAGE
    python scripts/research_dataset.py --out data/research/market_1m.db klines \
        --start 2024-01 --end 2026-08 --interval 1m --symbols <the 60 + XPLUSDT>
    python scripts/runner_exit_1m_study.py --symbols 60 --every 24 \
        [--workers 8] [--out docs/runner-exit-1m-2026-09-26]
    python scripts/runner_exit_1m_study.py --xpl-only     # real XPL session #36, needs network
"""

from __future__ import annotations

import argparse
import bisect
import json
import sqlite3
import statistics as st
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from multiprocessing import Pool
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import costengine  # noqa: E402
from scripts.ladder_panel_study import to_candles  # noqa: E402
from scripts.liquidity_tier_study import load  # noqa: E402
from scripts.runner_exit_study import (  # noqa: E402
    DEADLINE,
    DISTANCE,
    MAKER_FEE,
    MAX_WAVES,
    SLIPS,
    TAKER_FEE,
    TP,
    TP_STEP,
    WAVE0,
    bootstrap_mean_diff_ci,
    bt_targets,
    fetch_klines,
    klines_to_candles,
    maker_sell,
    run_ladder,
    taker_close,
    taker_stop_fill,
    top2_share,
    v0_variant,
    verify_v0,
)

_MS_MIN = 60_000
_MS_DAY = 86_400_000
DECISION_DELAY_MS = 2 * _MS_MIN  # V5: "90s post-fill guard" rounded up to 2 whole 1m bars
CRASH_DAY = "2025-10-10"


# =====================================================================================
# 1m data access — one symbol's whole series loaded as numpy arrays, once per worker job.
# =====================================================================================

def load_1m(db_path: Path, symbol: str) -> dict | None:
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = db.execute(
            "SELECT ts, open, high, low, close FROM candles WHERE symbol=? AND interval='1m' "
            "ORDER BY ts", (symbol,)).fetchall()
    finally:
        db.close()
    if not rows:
        return None
    a = np.array(rows, dtype=np.float64)
    return {"ts": a[:, 0].astype(np.int64), "open": a[:, 1], "high": a[:, 2],
            "low": a[:, 3], "close": a[:, 4]}


def find_touch_minute(m1: dict, hour_start_ms: int) -> int | None:
    """Index of the first 1m bar in [hour_start, hour_start+1h) whose high crosses the TP —
    caller filters by tp_price; this just bounds the search window. Returns None if the 1m
    archive has no bars at all in that hour (a data mismatch, not "TP wasn't touched")."""
    ts = m1["ts"]
    lo = bisect.bisect_left(ts, hour_start_ms)
    hi = bisect.bisect_left(ts, hour_start_ms + 3_600_000)
    if hi <= lo:
        return None
    return lo, hi


# =====================================================================================
# Vectorised trailing-stop walk (V4a and the V5 runner leg share this).
# =====================================================================================

def trail_walk(m1: dict, start_idx: int, deadline_ts: int, init_peak: float, floor: float,
                gap_pct: float, qty: float, slip_pct: float, pessimistic: bool) -> dict:
    """Walk from `start_idx` (INCLUSIVE — the touch/decision minute is checked, fixing the 1h
    flaw) until a stop fires, the session deadline is reached, or the data runs out.

    Convention (see module docstring): pessimistic uses the peak BEFORE each minute's own high
    can raise it (`peak_before`); optimistic uses the peak AFTER (`peak_after`). Both are one
    running-max over [init_peak, high[start_idx], high[start_idx+1], ...].

    Returns kind in {'stop','deadline','open'} + net-relevant fields. `net_usd`/`violation` are
    NOT filled in here (this only knows the qty/fill side) — the caller subtracts its own cost
    basis, since V4a and the V5 runner have different cost bases.
    """
    ts = m1["ts"]
    n = len(ts)
    if start_idx >= n:
        return {"kind": "no_data"}

    # Mirrors runner_exit_study.py's per-bar order EXACTLY: on every bar, the stop is checked
    # FIRST; only if it does NOT breach does the SAME bar's deadline check run. So the deadline
    # bar itself still gets a stop check (a stop that would have fired take priority over
    # capping at the deadline) — the search window is INCLUSIVE of the deadline bar, not
    # exclusive of it.
    deadline_idx = bisect.bisect_left(ts, deadline_ts, lo=start_idx)  # first bar >= deadline, or n
    search_end = min(deadline_idx, n - 1)  # inclusive index of the last bar the stop is checked on

    hh = m1["high"][start_idx:search_end + 1]
    ll = m1["low"][start_idx:search_end + 1]
    oo = m1["open"][start_idx:search_end + 1]

    cummax = np.maximum.accumulate(np.concatenate(([init_peak], hh)))
    peak_before = cummax[:-1]
    peak_after = cummax[1:]
    peak_used = peak_before if pessimistic else peak_after
    stop_arr = np.maximum(floor, peak_used * (1 - gap_pct / 100))
    breach = ll <= stop_arr

    if breach.any():
        rel = int(np.argmax(breach))
        j = start_idx + rel
        stop_price = float(stop_arr[rel])
        # FILL RULE (verification fix 2026-09-26). The minute's OPEN is only a legitimate fill
        # when the price was ALREADY through the stop that was in force at the start of the
        # minute (a true gap). Two cases where min(stop, open) sold at a price the position
        # never saw while armed:
        #   (a) rel == 0, the arm minute (V4a's touch minute): its open PRECEDES the touch, so
        #       it is a pre-arm price. All 374 PESS no-loss "violations" were exactly this
        #       (e.g. WIF 2025-10-10: touch minute opened at 0.28x avg, sold there -> -$424).
        #   (b) optimistic only: the stop was raised by THIS minute's own high; on the assumed
        #       path open -> high -> low the price crosses the raised stop on the way down, so
        #       the fill is the stop, not the (lower) open it passed on the way up.
        stop_before = max(floor, float(peak_before[rel]) * (1 - gap_pct / 100))
        gapped = rel > 0 and float(oo[rel]) <= stop_before
        fill_px, proceeds = taker_stop_fill(qty, stop_price,
                                            float(oo[rel]) if gapped else stop_price, slip_pct)
        peak_reached = float(peak_used[rel])
        return {"kind": "stop", "exit_idx": j, "exit_ts": int(ts[j]), "proceeds": proceeds,
                "fill": fill_px, "peak": peak_reached,
                "exit_to_peak": fill_px / peak_reached - 1 if peak_reached else 0.0}

    peak_reached = float(cummax.max())
    if deadline_idx < n:
        # No breach through and including the deadline bar: close at ITS close (kind=deadline).
        proceeds = taker_close(qty, float(m1["close"][deadline_idx]))
        return {"kind": "deadline", "exit_idx": deadline_idx, "exit_ts": int(ts[deadline_idx]),
                "proceeds": proceeds, "peak": peak_reached,
                "exit_to_peak": float(m1["close"][deadline_idx]) / peak_reached - 1 if peak_reached else 0.0}

    # Ran off the end of the 1m archive before either firing or reaching the deadline.
    last = n - 1
    proceeds = taker_close(qty, float(m1["close"][last]))
    return {"kind": "open", "exit_idx": last, "exit_ts": int(ts[last]), "proceeds": proceeds,
            "peak": peak_reached,
            "exit_to_peak": float(m1["close"][last]) / peak_reached - 1 if peak_reached else 0.0}


def v4a_1m(m1: dict, touch: dict, gap_pct: float, slip_pct: float, pessimistic: bool,
           start_idx: int, deadline_ts: int) -> dict:
    total_cost = touch["deployed"] * (1 + MAKER_FEE / 100)
    floor = touch["avg"] * 1.03
    w = trail_walk(m1, start_idx, deadline_ts, touch["tp_price"], floor, gap_pct, touch["qty"],
                   slip_pct, pessimistic)
    if w["kind"] == "no_data":
        return w
    net = w["proceeds"] - total_cost
    cap_days = touch["deployed"] * max(w["exit_ts"] - int(touch["touch_ts"]), 0) / _MS_DAY
    out = {"kind": w["kind"], "net_usd": net, "net_pct": net / total_cost * 100,
           "exit_ts": w["exit_ts"], "capital_days": cap_days,
           "violation": net < 0 and w["kind"] != "open", "peak": w.get("peak"),
           "exit_to_peak": w.get("exit_to_peak")}
    return out


def v5_1m(m1: dict, touch: dict, gap_pct: float, slip_pct: float, pessimistic: bool,
          start_idx: int, deadline_ts: int, v0: dict) -> dict:
    """V0's TP leg (already computed, `v0`) + an optional runner leg. Session net = v0's net +
    runner net (0 if no runner)."""
    ts = m1["ts"]
    n = len(ts)
    decision_ts = int(touch["touch_ts"]) + DECISION_DELAY_MS
    d_idx = bisect.bisect_left(ts, decision_ts, lo=start_idx)
    if d_idx >= n or ts[d_idx] >= deadline_ts:
        return {"kind": "no_runner", "net_usd": v0["net_usd"], "net_pct": v0["net_pct"],
                "exit_ts": touch["touch_ts"], "capital_days": v0["capital_days"],
                "violation": v0["violation"], "runner_taken": False}
    decision_price = float(m1["open"][d_idx])
    if decision_price < touch["tp_price"]:
        return {"kind": "no_runner", "net_usd": v0["net_usd"], "net_pct": v0["net_pct"],
                "exit_ts": touch["touch_ts"], "capital_days": v0["capital_days"],
                "violation": v0["violation"], "runner_taken": False}

    v0_proceeds = maker_sell(touch["qty"], touch["tp_price"])
    v0_net = v0["net_usd"]
    denom = gap_pct / 100 + 0.002 + 0.03
    runner_usd = min(v0_proceeds, max(v0_net, 0.0) / denom) if v0_net > 0 else 0.0
    if runner_usd <= 0:
        return {"kind": "no_runner", "net_usd": v0["net_usd"], "net_pct": v0["net_pct"],
                "exit_ts": touch["touch_ts"], "capital_days": v0["capital_days"],
                "violation": v0["violation"], "runner_taken": False}

    fill_px = decision_price * (1 + slip_pct / 100)
    cost_per_unit = fill_px * (1 + TAKER_FEE / 100)
    qty_runner = runner_usd / cost_per_unit

    w = trail_walk(m1, d_idx, deadline_ts, decision_price, 0.0, gap_pct, qty_runner, slip_pct,
                    pessimistic)
    if w["kind"] == "no_data":
        return {"kind": "no_runner", "net_usd": v0["net_usd"], "net_pct": v0["net_pct"],
                "exit_ts": touch["touch_ts"], "capital_days": v0["capital_days"],
                "violation": v0["violation"], "runner_taken": False}
    runner_net = w["proceeds"] - runner_usd
    session_net = v0_net + runner_net
    runner_cap_days = runner_usd * max(w["exit_ts"] - decision_ts, 0) / _MS_DAY
    total_cost = touch["deployed"] * (1 + MAKER_FEE / 100)
    return {"kind": w["kind"], "net_usd": session_net, "net_pct": session_net / total_cost * 100,
            "exit_ts": w["exit_ts"], "capital_days": v0["capital_days"] + runner_cap_days,
            "violation": session_net < 0 and w["kind"] != "open", "runner_taken": True,
            "runner_usd": runner_usd, "runner_net": runner_net, "peak": w.get("peak")}


def v5_full_ref(m1: dict, touch: dict, gap_pct: float, slip_pct: float, pessimistic: bool,
                start_idx: int, deadline_ts: int, v0: dict) -> dict | None:
    """Same as v5_1m but runner_usd = v0_proceeds ('full' sizing) — reference only."""
    ts = m1["ts"]
    n = len(ts)
    decision_ts = int(touch["touch_ts"]) + DECISION_DELAY_MS
    d_idx = bisect.bisect_left(ts, decision_ts, lo=start_idx)
    if d_idx >= n or ts[d_idx] >= deadline_ts:
        return None
    decision_price = float(m1["open"][d_idx])
    if decision_price < touch["tp_price"]:
        return None
    v0_proceeds = maker_sell(touch["qty"], touch["tp_price"])
    runner_usd = v0_proceeds
    fill_px = decision_price * (1 + slip_pct / 100)
    cost_per_unit = fill_px * (1 + TAKER_FEE / 100)
    qty_runner = runner_usd / cost_per_unit
    w = trail_walk(m1, d_idx, deadline_ts, decision_price, 0.0, gap_pct, qty_runner, slip_pct,
                    pessimistic)
    if w["kind"] == "no_data":
        return None
    runner_net = w["proceeds"] - runner_usd
    session_net = v0["net_usd"] + runner_net
    return {"kind": w["kind"], "net_usd": session_net, "runner_usd": runner_usd,
            "runner_net": runner_net}


# =====================================================================================
# Per-symbol driver (runs in a worker process)
# =====================================================================================

def _one_symbol(job: tuple) -> dict:
    sym, bars_1h, every, cost_pct, warmup, db_1m_path, gaps, slips = job
    candles = to_candles(bars_1h)
    m1 = load_1m(Path(db_1m_path), sym)
    rows: list[dict] = []
    mismatches = 0
    order_conflicts = 0
    if m1 is None:
        # No 1m data for this symbol at all — every touch is a mismatch.
        for start in range(warmup, len(candles) - 1, every):
            for pessimistic in (False, True):
                touch = run_ladder(candles, start, pessimistic)
                if touch and touch["outcome"] == "tp":
                    mismatches += 1
        return {"rows": rows, "mismatches": mismatches, "symbol": sym}

    for start in range(warmup, len(candles) - 1, every):
        for pessimistic in (False, True):
            bound = "PESS" if pessimistic else "OPT"
            touch = run_ladder(candles, start, pessimistic)
            if touch is None:
                continue
            verify_v0(candles, start, touch, pessimistic, cost_pct)
            if touch["outcome"] != "tp":
                continue

            hour_ts = candles[touch["touch_bar"]]["ts"]
            window = find_touch_minute(m1, hour_ts)
            if window is None:
                mismatches += 1
                continue
            lo, hi = window
            tp_price = touch["tp_price"]
            # OPT-ladder time-travel fix (verification 2026-09-26): the optimistic 1h ladder lets
            # rungs fill at the touch HOUR's low and then touches TP at the new, lower average in
            # the same hour. The touch minute must then be at/after the first minute whose low
            # reaches the deepest rung — otherwise V4a/V5 walk from minutes BEFORE the coins were
            # bought (WBTC 2024-11-23 / 2025-10-10: sold at the pre-wick 98k/115k peak coins
            # "bought" at a 64k/80k wick average, +$400-500 per event). If no such minute exists
            # the 1m path contradicts the 1h optimistic ordering: counted, not used.
            first_ok = lo
            if not pessimistic and touch["filled"] >= 2:
                deep = bt_targets(candles[start]["close"], DISTANCE, MAX_WAVES)[touch["filled"] - 1]
                prev_low = min((candles[k]["low"] for k in range(start + 1, touch["touch_bar"])),
                               default=float("inf"))
                if prev_low > deep:  # the deepest rung filled INSIDE the touch hour
                    rl = np.nonzero(m1["low"][lo:hi] <= deep)[0]
                    first_ok = lo + int(rl[0]) if rl.size else lo
            seg_high = m1["high"][first_ok:hi]
            hits = np.nonzero(seg_high >= tp_price)[0]
            if hits.size == 0:
                if first_ok > lo:
                    order_conflicts += 1
                else:
                    mismatches += 1
                continue
            start_idx = first_ok + int(hits[0])
            touch["touch_ts"] = int(m1["ts"][start_idx])
            deadline_ts = touch["entry_ts"] + int(DEADLINE * _MS_DAY)

            year = datetime.fromtimestamp(candles[start]["ts"] / 1000, timezone.utc).year
            touch_date = datetime.fromtimestamp(touch["touch_ts"] / 1000, timezone.utc).strftime("%Y-%m-%d")
            common = {"symbol": sym, "bound": bound, "start": start, "year": year,
                      "waves": touch["filled"], "touch_hour_ts": hour_ts,
                      "touch_ts": touch["touch_ts"], "touch_date": touch_date}

            v0 = v0_variant(candles, touch)
            rows.append({**common, "variant": "V0", "param": None, "slip": None,
                         "v0_net_usd": v0["net_usd"], **v0})

            for g in gaps:
                for slip in slips:
                    r = v4a_1m(m1, touch, g, slip, pessimistic, start_idx, deadline_ts)
                    if r.get("kind") == "no_data":
                        continue
                    rows.append({**common, "variant": "V4a", "param": g, "slip": slip,
                                 "v0_net_usd": v0["net_usd"], **r})

            for g in gaps:
                for slip in slips:
                    r = v5_1m(m1, touch, g, slip, pessimistic, start_idx, deadline_ts, v0)
                    rows.append({**common, "variant": "V5", "param": g, "slip": slip,
                                 "v0_net_usd": v0["net_usd"], **r})
                    rf = v5_full_ref(m1, touch, g, slip, pessimistic, start_idx, deadline_ts, v0)
                    if rf is not None:
                        total_cost = touch["deployed"] * (1 + MAKER_FEE / 100)
                        rows.append({**common, "variant": "V5full", "param": g, "slip": slip,
                                     "v0_net_usd": v0["net_usd"], "net_usd": rf["net_usd"],
                                     "kind": rf["kind"],
                                     "net_pct": rf["net_usd"] / total_cost * 100,
                                     "capital_days": v0["capital_days"],
                                     "violation": rf["net_usd"] < 0 and rf["kind"] != "open"})
    return {"rows": rows, "mismatches": mismatches, "order_conflicts": order_conflicts,
            "symbol": sym}


def choose_symbols(db: Path, n_symbols: int, min_years: float, seed: int) -> tuple[list[str], dict]:
    import random
    series = load(db, "1h")
    bars_per_year = 24 * 365
    eligible = sorted(s for s, b in series.items() if len(b) >= min_years * bars_per_year)
    chosen = sorted(random.Random(seed).sample(eligible, min(n_symbols, len(eligible))))
    return chosen, series


def run_population(db_1h: Path, db_1m: Path, n_symbols: int, min_years: float, every: int,
                    seed: int, workers: int, extra_symbols: list[str], gaps: list[float],
                    slips: list[float]) -> tuple[list[dict], dict]:
    cost_pct = costengine.round_trip_cost_pct()
    chosen, series = choose_symbols(db_1h, n_symbols, min_years, seed)
    all_syms = sorted(set(chosen) | set(extra_symbols))
    print(f"population: {len(chosen)} sampled (seed {seed}) + {len(extra_symbols)} extra "
          f"= {len(all_syms)} symbols, entries every {every} bars")
    warmup = 24
    jobs = []
    for s in all_syms:
        bars = series.get(s)
        if bars is None:
            print(f"  {s}: not in 1h db, skipped")
            continue
        jobs.append((s, bars, every, cost_pct, warmup, str(db_1m), gaps, slips))

    t0 = time.time()
    rows: list[dict] = []
    total_mismatches = 0
    total_conflicts = 0
    with Pool(workers) as pool:
        for i, res in enumerate(pool.imap_unordered(_one_symbol, jobs, chunksize=1)):
            rows.extend(res["rows"])
            total_mismatches += res["mismatches"]
            total_conflicts += res.get("order_conflicts", 0)
            if (i + 1) % 10 == 0 or i + 1 == len(jobs):
                print(f"  {i+1}/{len(jobs)} symbols, {len(rows):,} rows, "
                      f"{total_mismatches} mismatches so far ({time.time()-t0:.0f}s)")
    print(f"population done: {len(rows):,} rows, {total_mismatches} touch-minute mismatches, "
          f"{total_conflicts} OPT-ladder order conflicts ({time.time()-t0:.0f}s)")
    return rows, {"mismatches": total_mismatches, "opt_order_conflicts": total_conflicts,
                  "symbols": all_syms}


# =====================================================================================
# Aggregation (mirrors runner_exit_study.py's shape so the two reports read the same way)
# =====================================================================================

def summarise(rows: list[dict]) -> dict:
    """`kind == 'open'` means the position was still open at data-end/deadline-cap-exceeded —
    excluded from realised stats, reported separately as mark-to-market. `no_runner` (V5 only)
    IS realised: it is V0's own already-realised outcome, just relabelled."""
    opens = [r for r in rows if r["kind"] == "open"]
    realised = [r for r in rows if r["kind"] != "open"]
    n = len(realised)
    if not n:
        return {"n": 0, "open_count": len(opens)}
    diffs = [r["net_usd"] - r["v0_net_usd"] for r in realised]
    cap_days = sum(r["capital_days"] for r in realised)
    net_sum = sum(r["net_usd"] for r in realised)
    worst = min(realised, key=lambda r: r["net_usd"])
    lo, hi = bootstrap_mean_diff_ci(realised)
    viol = [r for r in realised if r.get("violation")]
    viol_episodes = {(r["symbol"], r["touch_ts"]) for r in viol}
    return {
        "n": n,
        "mean_usd": round(st.mean(r["net_usd"] for r in realised), 4),
        "mean_diff_vs_v0_usd": round(st.mean(diffs), 4),
        "diff_ci95": [round(lo, 4), round(hi, 4)],
        "median_diff_vs_v0_usd": round(st.median(diffs), 4),
        "pct_worse_than_v0": round(100 * sum(1 for d in diffs if d < 0) / n, 2),
        "worst_event_usd": round(worst["net_usd"], 4),
        "worst_event_symbol": worst["symbol"],
        "top2_symbol_share_pct": round(top2_share(realised), 2) if net_sum else 0.0,
        "capital_days": round(cap_days, 2),
        "usd_per_capital_day": round(net_sum / cap_days, 6) if cap_days else float("nan"),
        "no_loss_violations": len(viol),
        "no_loss_violation_usd": round(sum(r["net_usd"] for r in viol), 4),
        "no_loss_episodes": len(viol_episodes),
        "open_count": len(opens),
        "open_mtm_usd": round(sum(r["net_usd"] for r in opens), 2) if opens else 0.0,
    }


def build_grid(rows: list[dict], variants: list[str]) -> dict:
    grid: dict[str, dict] = {}
    keyed: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        if r["variant"] in variants:
            keyed[(r["variant"], r["param"], r["bound"], r["slip"])].append(r)
    for key, rs in keyed.items():
        variant, param, bound, slip = key
        label = f"{variant}|g={param}|{bound}|slip={slip}"
        grid[label] = {"variant": variant, "param": param, "bound": bound, "slip": slip,
                       **summarise(rs)}
    return grid


def dedup_one_per_symbol_touch_hour(rows: list[dict]) -> list[dict]:
    """One event per (symbol, touch_hour, bound) — keeps the FIRST by `start` (earliest entry)
    when several overlapping sessions independently touch TP in the same hour."""
    seen: dict[tuple, dict] = {}
    for r in rows:
        key = (r["symbol"], r["touch_hour_ts"], r["bound"], r["variant"], r["param"], r["slip"])
        # keep the earliest 'start' per key
        prior = seen.get(key)
        if prior is None or r["start"] < prior["start"]:
            seen[key] = r
    # rebuild list preserving one row per (symbol,touch_hour,bound) per (variant,param,slip)
    return list(seen.values())


def exclude_crash_day(rows: list[dict]) -> list[dict]:
    return [r for r in rows if r.get("touch_date") != CRASH_DAY]


def grid_table(grid: dict, title: str) -> str:
    lines = [f"### {title}", "",
             "| variant | gap | bound | slip | N | mean $/event | mean diff vs V0 | "
             "95% CI | median diff | % worse | worst event | top-2 sym | $/cap-day | "
             "no-loss viol (episodes) |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for key in sorted(grid, key=lambda k: (grid[k]["variant"], str(grid[k]["param"]),
                                            grid[k]["bound"], str(grid[k]["slip"]))):
        h = grid[key]
        if not h.get("n"):
            lines.append(f"| {h['variant']} | {h['param']} | {h['bound']} | {h['slip']} | 0 | "
                         "- | - | - | - | - | - | - | - |")
            continue
        ci = h.get("diff_ci95", [float("nan"), float("nan")])
        lines.append(
            f"| {h['variant']} | {h['param']} | {h['bound']} | {h['slip']} | {h['n']} | "
            f"{h['mean_usd']:+.3f} | {h['mean_diff_vs_v0_usd']:+.3f} | "
            f"[{ci[0]:+.3f}, {ci[1]:+.3f}] | {h['median_diff_vs_v0_usd']:+.3f} | "
            f"{h['pct_worse_than_v0']:.1f}% | {h['worst_event_usd']:+.3f} "
            f"({h['worst_event_symbol']}) | {h['top2_symbol_share_pct']:.1f}% | "
            f"{h['usd_per_capital_day']:+.6f} | {h['no_loss_violations']} "
            f"({h['no_loss_episodes']}) |")
    return "\n".join(lines)


def runner_taken_rate(rows: list[dict]) -> dict:
    v5 = [r for r in rows if r["variant"] == "V5"]
    by_param_bound: dict[tuple, list[bool]] = defaultdict(list)
    for r in v5:
        by_param_bound[(r["param"], r["bound"], r["slip"])].append(r.get("runner_taken", False))
    out = {}
    for k, vals in by_param_bound.items():
        out[f"g={k[0]}|{k[1]}|slip={k[2]}"] = {
            "n": len(vals), "runner_taken_pct": round(100 * sum(vals) / len(vals), 2) if vals else 0.0}
    return out


# =====================================================================================
# XPL session #36 — real 1m Binance data
# =====================================================================================

XPL_AVG = 0.09280176777477368
XPL_QTY = 1645.9
XPL_WAVES = 3
XPL_TP_PRICE = 0.09859259808391956
# fills.executed_at for pyramid:36:tp is stored as naive UTC (utcnow): 2026-09-24 16:04:01. The
# earlier "09:04:01 UTC" assumed it was UTC+7 local — wrong. The market first reached the TP at
# 15:55 UTC (the paper fill lagged ~9 min); the walk starts at that market touch minute.
XPL_TP_EXEC_UTC = "2026-09-24 16:04:01"


def xpl_session(out_dir: Path) -> dict:
    exec_dt = datetime.strptime(XPL_TP_EXEC_UTC, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    exec_ms = int(exec_dt.timestamp() * 1000)
    now_ms = int(time.time() * 1000)
    cache_dir = out_dir / "cache"
    start_fetch = exec_ms - 15 * _MS_MIN
    raw = fetch_klines("XPLUSDT", "1m", start_fetch, now_ms, cache_dir)
    candles = klines_to_candles(raw)
    if len(candles) < 2:
        return {"error": "no real XPL 1m data available", "n_candles": len(candles)}
    m1 = {"ts": np.array([c["ts"] for c in candles], dtype=np.int64),
          "open": np.array([c["open"] for c in candles]),
          "high": np.array([c["high"] for c in candles]),
          "low": np.array([c["low"] for c in candles]),
          "close": np.array([c["close"] for c in candles])}
    hits = np.nonzero(m1["high"] >= XPL_TP_PRICE)[0]
    if hits.size == 0:
        return {"error": "real 1m data never reaches the recorded TP price — cannot locate the "
                          "touch minute", "n_candles": len(candles)}
    start_idx = int(hits[0])
    touch_ts = int(m1["ts"][start_idx])
    deployed = XPL_AVG * XPL_QTY
    touch = {"avg": XPL_AVG, "qty": XPL_QTY, "deployed": deployed, "filled": XPL_WAVES,
             "tp_price": XPL_TP_PRICE, "touch_ts": touch_ts,
             "entry_ts": touch_ts - 3 * _MS_DAY,  # unknown true entry; only used for a 60d cap
             "touch_bar": start_idx}
    # bar_len_days-free v0 (avoid importing candles-shape coupling): compute directly.
    total_cost = deployed * (1 + MAKER_FEE / 100)
    v0_proceeds = maker_sell(XPL_QTY, XPL_TP_PRICE)
    v0_net = v0_proceeds - total_cost
    v0 = {"net_usd": v0_net, "net_pct": v0_net / total_cost * 100, "capital_days": 0.0,
          "violation": v0_net < 0, "kind": "tp_full"}

    real_deadline_ts = touch["entry_ts"] + int(DEADLINE * _MS_DAY)
    peak_idx = int(np.argmax(m1["high"]))
    real_peak = float(m1["high"][peak_idx])
    real_peak_ts = int(m1["ts"][peak_idx])

    out = {"avg": XPL_AVG, "qty": XPL_QTY, "tp_price": XPL_TP_PRICE, "touch_ts_utc": XPL_TP_EXEC_UTC,
           "v0_net_usd": round(v0_net, 4), "v0_net_pct": round(v0["net_pct"], 4),
           "real_peak": real_peak, "real_peak_ts_utc": datetime.fromtimestamp(
               real_peak_ts / 1000, timezone.utc).isoformat(),
           "real_peak_vs_tp_pct": round((real_peak / XPL_TP_PRICE - 1) * 100, 3),
           "data_through_utc": datetime.fromtimestamp(int(m1["ts"][-1]) / 1000, timezone.utc).isoformat(),
           "variants": {}}
    for g in (2.0, 3.0, 5.0):
        for slip in SLIPS:
            for pessimistic in (False, True):
                bound = "PESS" if pessimistic else "OPT"
                r4 = v4a_1m(m1, touch, g, slip, pessimistic, start_idx, real_deadline_ts)
                out["variants"][f"V4a|g={g}|{bound}|slip={slip}"] = {
                    k: (round(v, 4) if isinstance(v, float) else v) for k, v in r4.items()}
                r5 = v5_1m(m1, touch, g, slip, pessimistic, start_idx, real_deadline_ts, v0)
                out["variants"][f"V5|g={g}|{bound}|slip={slip}"] = {
                    k: (round(v, 4) if isinstance(v, float) else v) for k, v in r5.items()}
    return out


# =====================================================================================
# CLI / reporting
# =====================================================================================

def write_outputs(out_dir: Path, meta: dict, grid_all: dict, grid_dedup: dict,
                   grid_no_crash: dict, mismatch_info: dict, runner_rate: dict,
                   xpl: dict, v5full_grid: dict) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {"meta": meta, "grid_all": grid_all, "grid_dedup_per_symbol_touch_hour": grid_dedup,
               "grid_excl_2025_10_10": grid_no_crash, "mismatch_info": mismatch_info,
               "v5_runner_taken_rate": runner_rate, "xpl_session_36": xpl,
               "v5_full_sizing_reference": v5full_grid}
    (out_dir / "results.json").write_text(json.dumps(payload, indent=1, default=str), encoding="utf-8")

    md = ["# Runner-exit study, 1-minute resolution", "",
          f"Generated {datetime.now(timezone.utc).isoformat()}Z. Same production config as the "
          f"1h study: distance {DISTANCE}%, {MAX_WAVES} rungs max, TP {TP}%+{TP_STEP}%/rung, no "
          f"stop-loss, {DEADLINE:g}-day deadline, wave0 ${WAVE0:g}, maker {MAKER_FEE}% / taker "
          f"{TAKER_FEE}%, stop slippage {SLIPS}%. Pre-TP ladder unchanged (1h, verified against "
          "`app.backtest.simulate_kss`); everything from the TP touch onward now runs on 1m bars, "
          "with the touch/decision minute itself checked for a stop (the 1h study's flaw).", "",
          "## Plain-language summary", "",
          "- **V0** = production behaviour (sell 100% at the TP limit) — this is the reference "
          "every other row is measured against.",
          "- **V4a** = the owner's rule: don't sell at the touch, trail a stop instead. See the "
          "table; PESS/OPT are two intra-minute ORDERINGS, not bounds — for a trailing stop "
          "the high-first ordering fires earlier (higher stop, less ride), so it is not "
          "uniformly better. Headline = PESS ladder (the OPT 1h ladder is not time-consistent "
          "with 1m data in the touch hour; see order conflicts).",
          "- **V5** = sell at the TP exactly like V0, THEN optionally buy a separately-sized "
          "runner ~2 minutes later if price is still at/above the TP. See the runner-taken rate "
          "and the V5 table for whether this adds value net of the runner's own risk.",
          "- Headline numbers below are picked by the **pessimistic** bound per repo "
          "convention; both bounds and both slippage levels are shown in full.", "",
          f"**Touch-minute mismatches (1h said TP was touched, no 1m bar in that hour actually "
          f"reaches it, or the 1m archive has no data for that hour at all): "
          f"{mismatch_info['mismatches']}** out of the touches attempted — see meta for the "
          f"denominator. **OPT-ladder order conflicts (deepest rung filled inside the touch "
          f"hour and no minute at/after that fill reaches the TP — dropped): "
          f"{mismatch_info.get('opt_order_conflicts', 0)}**.", ""]

    md.append(grid_table(grid_all, "All events (variant x gap x bound x slip)"))
    md.append("")
    md.append(grid_table(grid_dedup, "De-duplicated: one event per (symbol, touch hour, bound)"))
    md.append("")
    md.append(grid_table(grid_no_crash, f"Excluding the {CRASH_DAY} crash day"))
    md.append("")

    md.append("### V5 runner-taken rate")
    md.append("")
    md.append("| gap | bound | slip | N touches | runner taken % |")
    md.append("|---|---|---|---|---|")
    for k, v in sorted(runner_rate.items()):
        parts = k.split("|")
        md.append(f"| {parts[0][2:]} | {parts[1]} | {parts[2][5:]} | {v['n']} | "
                  f"{v['runner_taken_pct']:.1f}% |")
    md.append("")

    if v5full_grid:
        md.append(grid_table(v5full_grid, "V5 'full' sizing (runner_usd = V0 proceeds) — REFERENCE ONLY"))
        md.append("")

    if xpl and "error" not in xpl:
        md.append("### XPL — paper session #36 (real 1m Binance data)")
        md.append("")
        md.append(f"avg {xpl['avg']}, qty {xpl['qty']}, 3 rungs, TP fill {xpl['tp_price']} at "
                  f"{xpl['touch_ts_utc']} UTC. V0 net **${xpl['v0_net_usd']:+.4f}** "
                  f"({xpl['v0_net_pct']:+.3f}%). Real peak reached after the TP: "
                  f"**{xpl['real_peak']:.8f}** ({xpl['real_peak_vs_tp_pct']:+.3f}% above the TP "
                  f"price) at {xpl['real_peak_ts_utc']}. Real 1m data available through "
                  f"{xpl['data_through_utc']} (i.e. this position's post-TP fate beyond that "
                  f"instant is not yet knowable — the variants below use only data up to there).")
        md.append("")
        md.append("| variant | gap | bound | slip | kind | net $ |")
        md.append("|---|---|---|---|---|---|")
        for label, v in sorted(xpl["variants"].items()):
            variant, g, bound, slip = label.split("|")
            md.append(f"| {variant} | {g[2:]} | {bound} | {slip[5:]} | {v.get('kind')} | "
                      f"{v.get('net_usd', float('nan')):+.4f} |")
        md.append("")
    elif xpl:
        md.append(f"### XPL — {xpl.get('error')}")
        md.append("")

    out_dir.joinpath("report.md").write_text("\n".join(md), encoding="utf-8")
    print(f"\nwrote {out_dir/'results.json'} and {out_dir/'report.md'}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--db-1h", default="data/research/market.db")
    p.add_argument("--db-1m", default="data/research/market_1m.db")
    p.add_argument("--symbols", type=int, default=60)
    p.add_argument("--min-years", type=float, default=2.0)
    p.add_argument("--every", type=int, default=24)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--out", default="docs/runner-exit-1m-2026-09-26")
    p.add_argument("--extra-symbols", default="XPLUSDT")
    p.add_argument("--gaps", default="2,3,5")
    p.add_argument("--xpl-only", action="store_true")
    p.add_argument("--no-bulk", action="store_true")
    args = p.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    out_dir = Path(args.out)
    gaps = [float(x) for x in args.gaps.split(",") if x.strip()]
    extra = [s.strip().upper() for s in args.extra_symbols.split(",") if s.strip()]

    xpl = None
    if args.xpl_only:
        xpl = xpl_session(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "xpl_only.json").write_text(json.dumps(xpl, indent=1, default=str), encoding="utf-8")
        print(json.dumps(xpl, indent=1, default=str))
        return 0

    t0 = time.time()
    meta = vars(args) | {"distance": DISTANCE, "max_waves": MAX_WAVES, "tp": TP,
                          "tp_step": TP_STEP, "deadline": DEADLINE, "wave0": WAVE0,
                          "maker_fee": MAKER_FEE, "taker_fee": TAKER_FEE, "slips": SLIPS,
                          "gaps": gaps, "decision_delay_ms": DECISION_DELAY_MS}

    grid_all = grid_dedup = grid_no_crash = v5full_grid = {}
    mismatch_info = {"mismatches": 0}
    runner_rate: dict = {}
    if not args.no_bulk:
        rows, mismatch_info = run_population(Path(args.db_1h), Path(args.db_1m), args.symbols,
                                             args.min_years, args.every, args.seed, args.workers,
                                             extra, gaps, SLIPS)
        headline_vars = ["V0", "V4a", "V5"]
        grid_all = build_grid(rows, headline_vars)
        grid_dedup = build_grid(dedup_one_per_symbol_touch_hour(rows), headline_vars)
        grid_no_crash = build_grid(exclude_crash_day(rows), headline_vars)
        v5full_grid = build_grid(rows, ["V5full"])
        runner_rate = runner_taken_rate(rows)
        print(f"grids built ({time.time()-t0:.0f}s)")

    xpl = xpl_session(out_dir)

    write_outputs(out_dir, meta, grid_all, grid_dedup, grid_no_crash, mismatch_info, runner_rate,
                  xpl, v5full_grid)
    print(f"\ndone in {time.time()-t0:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
