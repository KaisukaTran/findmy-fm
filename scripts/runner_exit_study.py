"""Runner-exit study: does letting a KSS take-profit "float" beat selling 100% at the resting
TP limit — and can it be done WITHOUT ever realizing a session loss?

WHY THIS EXISTS
    The owner watched XPL (paper session 36: avg 0.0928, qty 1645.9, 3 rungs) sell its whole
    position at the resting TP limit (0.09859) and then keep running to 0.1154. He wants to
    "let it float to optimise profit but guarantee no loss." This script measures five exit
    variants against the production baseline (sell 100% at the TP limit) from the moment the
    TP price is first touched:

      V0  baseline      sell 100% at the TP limit (maker).
      V1  half+runner   sell 50% at TP; the other 50% trails a stop = max(breakeven, peak*(1-w)).
      V2  scale-out     1/3 at TP, 1/3 at TP*(1+s), 1/3 at TP*(1+2s); stop tightens as tranches fill.
      V3  sized re-entry  same exit as V0, but the NEXT session's wave0 scales with the capital
                          the exit just freed (k x the exited session's cost) instead of the
                          fixed $28 production uses. Does NOT guarantee no loss.
      V4  owner's rule  do not sell at all at the touch; arm a watch (continuous trail, or the
                          literal "lock rises 1% per 1% the peak clears the TP" step rule) and
                          cancel the remaining rungs.

REIMPLEMENTATION, VERIFIED
    `app.backtest.simulate_kss` does not expose the TP-touch bar/avg/qty/cost that every variant
    needs, and it always sells 100% the instant the target is touched. `run_ladder` below
    re-implements its SL=0 pre-TP phase bar-for-bar (reusing its own `_targets`/`_fill_price`
    helpers) and stops the instant TP would trigger instead of selling. V0 is checked against
    `simulate_kss(trail_after_tp_pct=0)` on EVERY trial the study runs (see `verify_v0`) — a
    mismatch raises immediately. See `docs/runner-exit-2026-09-25/report.md` for what that proves
    and does not prove.

TWO INTRA-BAR BOUNDS AND TWO SLIPPAGE LEVELS, ALWAYS (repo convention — see
    scripts/ladder_panel_study.py, scripts/tp_then_trail_study.py). A stop's fill uses
    min(stop, bar-open-if-it-gapped-below) x (1 - slip), + taker fee. The bar that arms a
    runner/watch never exits on itself (mirrors `simulate_kss`'s own `trail_after_tp_pct`
    "just_armed" convention, app/backtest.py ~L232-254) — the conservative, documented choice
    for the touch bar itself.

    python scripts/runner_exit_study.py --interval 1h --every 24 --symbols 60 \
        [--workers 8] [--out docs/runner-exit-2026-09-25]
    python scripts/runner_exit_study.py --real-only   # paper-DB sanity pass, needs network
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sqlite3
import statistics as st
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from multiprocessing import Pool
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import costengine  # noqa: E402
from app.backtest import _fill_price as bt_fill_price  # noqa: E402
from app.backtest import _targets as bt_targets  # noqa: E402
from app.backtest import simulate_kss  # noqa: E402
from scripts.ladder_panel_study import to_candles  # noqa: E402
from scripts.liquidity_tier_study import load  # noqa: E402

_MS_PER_DAY = 86_400_000

# --- production KSS config (see CLAUDE.md / kss-spec) ---
DISTANCE = 7.0
MAX_WAVES = 10
TP = 5.0
TP_STEP = 0.5
DEADLINE = 60.0
WAVE0 = 28.0
MAKER_FEE = 0.1     # %, resting-limit fills (buys, TP tranches)
TAKER_FEE = 0.1     # %, stop / forced-close fills
SLIPS = [0.1, 0.3]  # %, stop-fill slippage grid

W1_WIDTHS = [5.0, 8.0, 12.0]     # V1 runner trail width
V2_STEPS = [3.0, 5.0, 8.0]       # V2 scale-out spacing
V3_KS = [0.25, 0.5, 1.0]         # V3 re-entry sizing multiples
V4A_GAPS = [2.0, 3.0, 5.0]       # V4a continuous trail gap ("owner's number" = 2)

N_BOOT = 2000


# =====================================================================================
# Core ladder engine — mirrors app.backtest.simulate_kss's SL=0 pre-TP phase bar-for-bar,
# but stops (does not sell) the instant TP is touched, so a caller can take over the exit.
# =====================================================================================

def eff_tp(k: int) -> float:
    return TP + TP_STEP * max(0, k - 1)


def bar_len_days(candles: list[dict], idx: int) -> float:
    if idx + 1 < len(candles):
        return (candles[idx + 1]["ts"] - candles[idx]["ts"]) / _MS_PER_DAY
    if idx > 0:
        return (candles[idx]["ts"] - candles[idx - 1]["ts"]) / _MS_PER_DAY
    return 0.0


def run_ladder(candles: list[dict], start: int, pessimistic: bool, wave0_usd: float = WAVE0,
               entry_price: float | None = None) -> dict | None:
    """Walk the ladder from `start`. Returns None if `start` cannot even begin (too close to the
    end of the data). Otherwise a dict with outcome in {'tp', 'deadline', 'incomplete'}:
      tp:         touch_bar, avg, filled, qty, deployed, eff_tp, tp_price, entry_ts, days_to_tp
      deadline:   exit_bar, avg, filled, qty, deployed, exit_price, entry_ts, days_to_exit
      incomplete: exit_bar, avg, filled, qty, deployed, exit_price, entry_ts   (ran off data)
    `deployed`/`qty` are fee-free cost basis / coins — the caller applies fees.
    """
    if start >= len(candles) - 1:
        return None
    entry = entry_price if entry_price is not None else candles[start]["close"]
    if entry <= 0:
        return None
    entry_ts = candles[start]["ts"]
    targets = bt_targets(entry, DISTANCE, MAX_WAVES)
    weights = [n + 1 for n in range(MAX_WAVES)]
    fill_prices = [entry] + [targets[i] for i in range(1, MAX_WAVES)]
    filled = 1
    unit_qty = wave0_usd / entry

    def avg_price(k: int) -> float:
        num = sum(fill_prices[i] * weights[i] for i in range(k))
        den = sum(weights[i] for i in range(k))
        return num / den if den else entry

    def wave_cost(i: int) -> float:
        return weights[i] * unit_qty * fill_prices[i]

    def qty_of(k: int) -> float:
        return sum(weights[i] for i in range(k)) * unit_qty

    deployed = wave_cost(0)

    for j in range(start + 1, len(candles)):
        bar = candles[j]
        days = (bar["ts"] - entry_ts) / _MS_PER_DAY
        bar_open = bar.get("open", bar["close"])

        if pessimistic:
            pre_avg = avg_price(filled)
            tp_px = pre_avg * (1 + eff_tp(filled) / 100)
            if pre_avg > 0 and bar["high"] >= tp_px:
                return {"outcome": "tp", "touch_bar": j, "avg": pre_avg, "filled": filled,
                        "qty": qty_of(filled), "deployed": deployed, "eff_tp": eff_tp(filled),
                        "tp_price": tp_px, "entry_ts": entry_ts, "days_to_tp": days}

        while filled < MAX_WAVES and bar["low"] <= targets[filled]:
            fill_prices[filled] = bt_fill_price(targets[filled], bar_open)
            deployed += wave_cost(filled)
            filled += 1

        avg = avg_price(filled)

        if not pessimistic:
            tp_px = avg * (1 + eff_tp(filled) / 100)
            if bar["high"] >= tp_px:
                return {"outcome": "tp", "touch_bar": j, "avg": avg, "filled": filled,
                        "qty": qty_of(filled), "deployed": deployed, "eff_tp": eff_tp(filled),
                        "tp_price": tp_px, "entry_ts": entry_ts, "days_to_tp": days}

        if days >= DEADLINE:
            return {"outcome": "deadline", "exit_bar": j, "avg": avg, "filled": filled,
                    "qty": qty_of(filled), "deployed": deployed, "exit_price": bar["close"],
                    "entry_ts": entry_ts, "days_to_exit": days}

    avg = avg_price(filled)
    return {"outcome": "incomplete", "exit_bar": len(candles) - 1, "avg": avg, "filled": filled,
            "qty": qty_of(filled), "deployed": deployed, "exit_price": candles[-1]["close"],
            "entry_ts": entry_ts}


def verify_v0(candles: list[dict], start: int, touch: dict, pessimistic: bool, cost_pct: float) -> None:
    """Assert our re-implementation's TP touch reproduces simulate_kss(trail_after_tp_pct=0)
    exactly (its own flat round-trip cost_pct, not the per-leg maker/taker model the rest of
    this study uses — this call is a pure correctness check of `run_ladder`)."""
    sim = simulate_kss(candles, start, DISTANCE, MAX_WAVES, TP, DEADLINE, sl_pct=0.0,
                        cost_pct=cost_pct, pessimistic_intrabar=pessimistic,
                        wave0_notional_usd=WAVE0, tp_step_pct=TP_STEP, trail_after_tp_pct=0.0)
    if touch["outcome"] == "tp":
        assert sim.tp_hit, (start, pessimistic, "sim did not hit TP")
        assert sim.waves_filled == touch["filled"], (start, pessimistic, sim.waves_filled, touch["filled"])
        assert abs(sim.exit_capital - touch["deployed"]) < 1e-6, (start, pessimistic, sim.exit_capital, touch["deployed"])
        want = round(touch["eff_tp"] - cost_pct, 4)
        assert abs(sim.pnl_pct - want) < 1e-6, (start, pessimistic, sim.pnl_pct, want)
    elif touch["outcome"] == "deadline":
        assert sim.hit_deadline, (start, pessimistic, "sim did not hit deadline")
        assert sim.waves_filled == touch["filled"], (start, pessimistic)
        want = round((touch["exit_price"] / touch["avg"] - 1) * 100 - cost_pct, 4)
        assert abs(sim.pnl_pct - want) < 1e-6, (start, pessimistic, sim.pnl_pct, want)
    else:  # incomplete
        assert not (sim.tp_hit or sim.stopped or sim.hit_deadline), (start, pessimistic, "sim completed, we did not")


# =====================================================================================
# Execution model
# =====================================================================================

def maker_sell(qty: float, price: float) -> float:
    return qty * price * (1 - MAKER_FEE / 100)


def buy_cost_with_fee(deployed: float) -> float:
    """`deployed` is fee-free cost basis (sum of qty_i * fill_price_i); the maker fee applies
    proportionally to every buy leg, so scaling the sum is equivalent to summing fee-inclusive
    legs."""
    return deployed * (1 + MAKER_FEE / 100)


def taker_stop_fill(qty: float, stop_price: float, bar_open: float, slip_pct: float) -> tuple[float, float]:
    """fill = min(stop, bar_open) x (1 - slip); proceeds net of taker fee. Returns (fill_price, proceeds)."""
    base = min(stop_price, bar_open)
    fill_price = base * (1 - slip_pct / 100)
    return fill_price, qty * fill_price * (1 - TAKER_FEE / 100)


def taker_close(qty: float, price: float) -> float:
    """Deadline / data-end forced close: taker fee, no extra slippage buffer (not a stop-hunt)."""
    return qty * price * (1 - TAKER_FEE / 100)


def breakeven_price(total_cost_with_fee: float, realized_proceeds_so_far: float,
                     remaining_qty: float, slip_pct: float) -> float:
    """Price at which selling `remaining_qty` (taker fee + `slip_pct`) brings the WHOLE
    session's net P&L to exactly zero, given what has already been spent and already realized."""
    need = total_cost_with_fee - realized_proceeds_so_far
    denom = remaining_qty * (1 - TAKER_FEE / 100 - slip_pct / 100)
    if denom <= 0:
        return float("inf")
    return need / denom


# =====================================================================================
# Post-TP variants. Each takes the touch dict + candles and returns a trial record.
# All monetary fields are in USD. `net_usd`/`net_pct` are for the WHOLE session (buy costs
# through every exit leg). `capital_days` covers the POST-TP phase only (touch bar through
# final exit) — pre-TP capital-days are identical across every variant sharing a touch trial,
# so excluding them isolates the exit choice being measured.
# =====================================================================================

def _base_row(touch: dict, kind: str) -> dict:
    total_cost = buy_cost_with_fee(touch["deployed"])
    return {"total_cost": total_cost, "kind": kind, "events": []}


def v0_variant(candles: list[dict], touch: dict) -> dict:
    total_cost = buy_cost_with_fee(touch["deployed"])
    proceeds = maker_sell(touch["qty"], touch["tp_price"])
    net = proceeds - total_cost
    cap_days = touch["deployed"] * bar_len_days(candles, touch["touch_bar"])
    return {"kind": "tp_full", "net_usd": net, "net_pct": net / total_cost * 100,
            "exit_bar": touch["touch_bar"], "capital_days": cap_days, "violation": net < 0}


def _walk_capital_days(candles: list[dict], from_bar: int, to_bar: int, qty_schedule: list[tuple]) -> float:
    """qty_schedule: sorted list of (bar_index_from_which_this_qty_applies, qty_usd_value)."""
    days = 0.0
    sched = sorted(qty_schedule)
    for j in range(from_bar, to_bar + 1):
        usd = 0.0
        for bar_from, val in sched:
            if bar_from <= j:
                usd = val
        days += usd * bar_len_days(candles, j)
    return days


def v1_variant(candles: list[dict], touch: dict, width: float, slip_pct: float, pessimistic: bool) -> dict:
    total_cost = buy_cost_with_fee(touch["deployed"])
    half_qty = touch["qty"] * 0.5
    rest_qty = touch["qty"] - half_qty
    proceeds1 = maker_sell(half_qty, touch["tp_price"])
    cost_per_unit = touch["deployed"] / touch["qty"]
    remaining_cost_usd = cost_per_unit * rest_qty  # fee-free cost basis of the runner half
    j0 = touch["touch_bar"]
    peak = candles[j0]["high"]

    for j in range(j0 + 1, len(candles)):
        bar = candles[j]
        days = (bar["ts"] - touch["entry_ts"]) / _MS_PER_DAY
        bar_open = bar.get("open", bar["close"])
        be = breakeven_price(total_cost, proceeds1, rest_qty, slip_pct)

        if pessimistic:
            stop = max(be, peak * (1 - width / 100))
            if bar["low"] <= stop:
                fill_px, proceeds2 = taker_stop_fill(rest_qty, stop, bar_open, slip_pct)
                net = proceeds1 + proceeds2 - total_cost
                cd = _walk_capital_days(candles, j0, j, [(j0, touch["deployed"]), (j0 + 1, remaining_cost_usd)])
                return {"kind": "stop", "net_usd": net, "net_pct": net / total_cost * 100, "exit_bar": j,
                        "capital_days": cd, "violation": net < 0, "breakeven": be, "fill": fill_px,
                        "peak": peak}
            peak = max(peak, bar["high"])
        else:
            peak = max(peak, bar["high"])
            stop = max(be, peak * (1 - width / 100))
            if bar["low"] <= stop:
                fill_px, proceeds2 = taker_stop_fill(rest_qty, stop, bar_open, slip_pct)
                net = proceeds1 + proceeds2 - total_cost
                cd = _walk_capital_days(candles, j0, j, [(j0, touch["deployed"]), (j0 + 1, remaining_cost_usd)])
                return {"kind": "stop", "net_usd": net, "net_pct": net / total_cost * 100, "exit_bar": j,
                        "capital_days": cd, "violation": net < 0, "breakeven": be, "fill": fill_px,
                        "peak": peak}

        if days >= DEADLINE:
            proceeds2 = taker_close(rest_qty, bar["close"])
            net = proceeds1 + proceeds2 - total_cost
            cd = _walk_capital_days(candles, j0, j, [(j0, touch["deployed"]), (j0 + 1, remaining_cost_usd)])
            return {"kind": "deadline", "net_usd": net, "net_pct": net / total_cost * 100, "exit_bar": j,
                    "capital_days": cd, "violation": net < 0, "breakeven": be}

    last = len(candles) - 1
    proceeds2 = taker_close(rest_qty, candles[-1]["close"])  # mark-to-market, not realized
    net = proceeds1 + proceeds2 - total_cost
    cd = _walk_capital_days(candles, j0, last, [(j0, touch["deployed"]), (j0 + 1, remaining_cost_usd)])
    return {"kind": "open", "net_usd": net, "net_pct": net / total_cost * 100, "exit_bar": last,
            "capital_days": cd, "violation": False}


def v2_variant(candles: list[dict], touch: dict, s_pct: float, slip_pct: float, pessimistic: bool) -> dict:
    total_cost = buy_cost_with_fee(touch["deployed"])
    third = touch["qty"] / 3.0
    remaining_qty = touch["qty"] - third
    proceeds = maker_sell(third, touch["tp_price"])
    cost_per_unit = touch["deployed"] / touch["qty"]
    j0 = touch["touch_bar"]
    peak = candles[j0]["high"]
    tranche2_px = touch["tp_price"] * (1 + s_pct / 100)
    tranche3_px = touch["tp_price"] * (1 + 2 * s_pct / 100)
    tranches_left = [tranche2_px, tranche3_px]
    phase = 1  # 1 = stop is breakeven; 2 = stop is TP price (after tranche2 fills)
    qty_sched = [(j0, touch["deployed"]), (j0 + 1, cost_per_unit * remaining_qty)]

    for j in range(j0 + 1, len(candles)):
        bar = candles[j]
        days = (bar["ts"] - touch["entry_ts"]) / _MS_PER_DAY
        bar_open = bar.get("open", bar["close"])
        bh, bl = bar["high"], bar["low"]

        exited = False
        if pessimistic:
            # low-before-high: stop fires before a same-bar tranche limit could be credited.
            stop = touch["tp_price"] if phase == 2 else breakeven_price(
                total_cost, proceeds, remaining_qty, slip_pct)
            if bl <= stop:
                _fp, p2 = taker_stop_fill(remaining_qty, stop, bar_open, slip_pct)
                proceeds += p2
                remaining_qty = 0.0
                exited = True
            else:
                while tranches_left and bh >= tranches_left[0]:
                    px = tranches_left.pop(0)
                    qty = third if tranches_left else remaining_qty
                    proceeds += maker_sell(qty, px)
                    remaining_qty -= qty
                    if phase == 1:
                        phase = 2
                    qty_sched.append((j + 1, cost_per_unit * remaining_qty))
                    if not tranches_left and remaining_qty <= 1e-12:
                        exited = True
                        break
            peak = max(peak, bh)
        else:
            peak = max(peak, bh)
            while tranches_left and bh >= tranches_left[0]:
                px = tranches_left.pop(0)
                qty = third if tranches_left else remaining_qty
                proceeds += maker_sell(qty, px)
                remaining_qty -= qty
                if phase == 1:
                    phase = 2
                qty_sched.append((j + 1, cost_per_unit * remaining_qty))
                if not tranches_left and remaining_qty <= 1e-12:
                    exited = True
                    break
            if not exited:
                stop = touch["tp_price"] if phase == 2 else breakeven_price(
                    total_cost, proceeds, remaining_qty, slip_pct)
                if bl <= stop:
                    _fp, p2 = taker_stop_fill(remaining_qty, stop, bar_open, slip_pct)
                    proceeds += p2
                    remaining_qty = 0.0
                    exited = True

        if exited:
            net = proceeds - total_cost
            cd = _walk_capital_days(candles, j0, j, qty_sched)
            return {"kind": "scaleout", "net_usd": net, "net_pct": net / total_cost * 100, "exit_bar": j,
                    "capital_days": cd, "violation": net < 0}

        if days >= DEADLINE and remaining_qty > 1e-12:
            proceeds += taker_close(remaining_qty, bar["close"])
            net = proceeds - total_cost
            cd = _walk_capital_days(candles, j0, j, qty_sched)
            return {"kind": "deadline", "net_usd": net, "net_pct": net / total_cost * 100, "exit_bar": j,
                    "capital_days": cd, "violation": net < 0}

    last = len(candles) - 1
    if remaining_qty > 1e-12:
        proceeds += taker_close(remaining_qty, candles[-1]["close"])
    net = proceeds - total_cost
    cd = _walk_capital_days(candles, j0, last, qty_sched)
    return {"kind": "open", "net_usd": net, "net_pct": net / total_cost * 100, "exit_bar": last,
            "capital_days": cd, "violation": False}


def v4a_variant(candles: list[dict], touch: dict, gap_pct: float, slip_pct: float, pessimistic: bool) -> dict:
    total_cost = buy_cost_with_fee(touch["deployed"])
    qty = touch["qty"]
    floor = touch["avg"] * 1.03
    j0 = touch["touch_bar"]
    peak = candles[j0]["high"]

    for j in range(j0 + 1, len(candles)):
        bar = candles[j]
        days = (bar["ts"] - touch["entry_ts"]) / _MS_PER_DAY
        bar_open = bar.get("open", bar["close"])

        if pessimistic:
            stop = max(floor, peak * (1 - gap_pct / 100))
            if bar["low"] <= stop:
                fill_px, proceeds = taker_stop_fill(qty, stop, bar_open, slip_pct)
                net = proceeds - total_cost
                cd = touch["deployed"] * bar_len_days(candles, j0) + _walk_capital_days(candles, j0 + 1, j, [(j0, touch["deployed"])])
                gap_to_peak = fill_px / peak - 1
                return {"kind": "stop", "net_usd": net, "net_pct": net / total_cost * 100, "exit_bar": j,
                        "capital_days": cd, "violation": net < 0, "peak": peak, "exit_to_peak": gap_to_peak}
            peak = max(peak, bar["high"])
        else:
            peak = max(peak, bar["high"])
            stop = max(floor, peak * (1 - gap_pct / 100))
            if bar["low"] <= stop:
                fill_px, proceeds = taker_stop_fill(qty, stop, bar_open, slip_pct)
                net = proceeds - total_cost
                cd = touch["deployed"] * bar_len_days(candles, j0) + _walk_capital_days(candles, j0 + 1, j, [(j0, touch["deployed"])])
                gap_to_peak = fill_px / peak - 1
                return {"kind": "stop", "net_usd": net, "net_pct": net / total_cost * 100, "exit_bar": j,
                        "capital_days": cd, "violation": net < 0, "peak": peak, "exit_to_peak": gap_to_peak}

        if days >= DEADLINE:
            proceeds = taker_close(qty, bar["close"])
            net = proceeds - total_cost
            cd = touch["deployed"] * bar_len_days(candles, j0) + _walk_capital_days(candles, j0 + 1, j, [(j0, touch["deployed"])])
            return {"kind": "deadline", "net_usd": net, "net_pct": net / total_cost * 100, "exit_bar": j,
                    "capital_days": cd, "violation": net < 0, "peak": peak,
                    "exit_to_peak": bar["close"] / peak - 1}

    last = len(candles) - 1
    proceeds = taker_close(qty, candles[-1]["close"])
    net = proceeds - total_cost
    cd = touch["deployed"] * bar_len_days(candles, j0) + _walk_capital_days(candles, j0 + 1, last, [(j0, touch["deployed"])])
    return {"kind": "open", "net_usd": net, "net_pct": net / total_cost * 100, "exit_bar": last,
            "capital_days": cd, "violation": False, "peak": peak,
            "exit_to_peak": candles[-1]["close"] / peak - 1 if peak else 0.0}


def v4b_variant(candles: list[dict], touch: dict, slip_pct: float, pessimistic: bool) -> dict:
    """Literal step: L starts at avg*1.03; each time peak clears TP*1.01^n, L = avg*1.03*1.01^n."""
    total_cost = buy_cost_with_fee(touch["deployed"])
    qty = touch["qty"]
    base_l = touch["avg"] * 1.03
    tp_price = touch["tp_price"]
    j0 = touch["touch_bar"]
    peak = candles[j0]["high"]

    def lock_for(pk: float) -> float:
        if tp_price <= 0 or pk <= tp_price:
            return base_l
        n = math.floor(math.log(pk / tp_price) / math.log(1.01))
        return base_l * (1.01 ** max(0, n))

    for j in range(j0 + 1, len(candles)):
        bar = candles[j]
        days = (bar["ts"] - touch["entry_ts"]) / _MS_PER_DAY
        bar_open = bar.get("open", bar["close"])

        if pessimistic:
            L = lock_for(peak)
            if bar["low"] <= L:
                fill_px, proceeds = taker_stop_fill(qty, L, bar_open, slip_pct)
                net = proceeds - total_cost
                cd = touch["deployed"] * bar_len_days(candles, j0) + _walk_capital_days(candles, j0 + 1, j, [(j0, touch["deployed"])])
                return {"kind": "stop", "net_usd": net, "net_pct": net / total_cost * 100, "exit_bar": j,
                        "capital_days": cd, "violation": net < 0, "peak": peak, "lock": L,
                        "exit_to_peak": fill_px / peak - 1}
            peak = max(peak, bar["high"])
        else:
            peak = max(peak, bar["high"])
            L = lock_for(peak)
            if bar["low"] <= L:
                fill_px, proceeds = taker_stop_fill(qty, L, bar_open, slip_pct)
                net = proceeds - total_cost
                cd = touch["deployed"] * bar_len_days(candles, j0) + _walk_capital_days(candles, j0 + 1, j, [(j0, touch["deployed"])])
                return {"kind": "stop", "net_usd": net, "net_pct": net / total_cost * 100, "exit_bar": j,
                        "capital_days": cd, "violation": net < 0, "peak": peak, "lock": L,
                        "exit_to_peak": fill_px / peak - 1}

        if days >= DEADLINE:
            proceeds = taker_close(qty, bar["close"])
            net = proceeds - total_cost
            cd = touch["deployed"] * bar_len_days(candles, j0) + _walk_capital_days(candles, j0 + 1, j, [(j0, touch["deployed"])])
            return {"kind": "deadline", "net_usd": net, "net_pct": net / total_cost * 100, "exit_bar": j,
                    "capital_days": cd, "violation": net < 0, "peak": peak,
                    "exit_to_peak": bar["close"] / peak - 1}

    last = len(candles) - 1
    proceeds = taker_close(qty, candles[-1]["close"])
    net = proceeds - total_cost
    cd = touch["deployed"] * bar_len_days(candles, j0) + _walk_capital_days(candles, j0 + 1, last, [(j0, touch["deployed"])])
    return {"kind": "open", "net_usd": net, "net_pct": net / total_cost * 100, "exit_bar": last,
            "capital_days": cd, "violation": False, "peak": peak,
            "exit_to_peak": candles[-1]["close"] / peak - 1 if peak else 0.0}


def reserved_capital(wave0_usd: float) -> float:
    f = 1 - DISTANCE / 100
    return sum((n + 1) * wave0_usd * (f ** n) for n in range(MAX_WAVES))


def v3_variant(candles: list[dict], touch: dict, k: float, pessimistic: bool) -> dict | None:
    """Baseline V0 exit at TP frees `total_cost`; re-enter at the NEXT bar's OPEN with
    wave0 = k x total_cost. Runs the SAME ladder to its own TP/deadline/incomplete outcome
    (V0-style sell) and reports that new session's own $. Does NOT guarantee no loss."""
    j0 = touch["touch_bar"]
    if j0 + 1 >= len(candles):
        return None
    total_cost_exited = buy_cost_with_fee(touch["deployed"])
    new_wave0 = k * total_cost_exited
    entry_bar = j0 + 1
    entry_px = candles[entry_bar]["open"]
    new_touch = run_ladder(candles, entry_bar, pessimistic, wave0_usd=new_wave0, entry_price=entry_px)
    if new_touch is None:
        return None
    new_cost = buy_cost_with_fee(new_touch["deployed"])
    if new_touch["outcome"] == "tp":
        proceeds = maker_sell(new_touch["qty"], new_touch["tp_price"])
        exit_bar = new_touch["touch_bar"]
        cd = new_touch["deployed"] * bar_len_days(candles, exit_bar)
        kind = "tp"
    elif new_touch["outcome"] == "deadline":
        proceeds = taker_close(new_touch["qty"], new_touch["exit_price"])
        exit_bar = new_touch["exit_bar"]
        cd = new_touch["deployed"] * bar_len_days(candles, exit_bar)
        kind = "deadline"
    else:
        proceeds = taker_close(new_touch["qty"], new_touch["exit_price"])
        exit_bar = new_touch["exit_bar"]
        cd = new_touch["deployed"] * bar_len_days(candles, exit_bar)
        kind = "open"
    net = proceeds - new_cost
    return {"kind": kind, "net_usd": net, "net_pct": net / new_cost * 100 if new_cost else 0.0,
            "exit_bar": exit_bar, "capital_days": cd, "violation": net < 0 and kind != "open",
            "new_wave0": new_wave0, "peak_capital": reserved_capital(new_wave0),
            "waves_filled": new_touch["filled"], "deadline_loss": kind == "deadline" and net < 0}


# =====================================================================================
# Trial driver (runs in a worker process, one symbol at a time)
# =====================================================================================

def _one_symbol(job: tuple) -> list[dict]:
    sym, bars, every, cost_pct, warmup = job
    candles = to_candles(bars)
    out: list[dict] = []
    for start in range(warmup, len(candles) - 1, every):
        for pessimistic in (False, True):
            bound = "PESS" if pessimistic else "OPT"
            touch = run_ladder(candles, start, pessimistic)
            if touch is None:
                continue
            verify_v0(candles, start, touch, pessimistic, cost_pct)
            if touch["outcome"] != "tp":
                continue  # this study only measures what happens FROM the TP touch onward

            v0 = v0_variant(candles, touch)
            year = datetime.fromtimestamp(candles[start]["ts"] / 1000, timezone.utc).year
            common = {"symbol": sym, "bound": bound, "start": start, "year": year,
                     "days_to_tp": touch["days_to_tp"], "waves": touch["filled"]}

            out.append({**common, "variant": "V0", "param": None, "slip": None,
                        "v0_net_usd": v0["net_usd"], **v0})

            for w in W1_WIDTHS:
                for slip in SLIPS:
                    r = v1_variant(candles, touch, w, slip, pessimistic)
                    out.append({**common, "variant": "V1", "param": w, "slip": slip,
                                "v0_net_usd": v0["net_usd"], **r})

            for s in V2_STEPS:
                for slip in SLIPS:
                    r = v2_variant(candles, touch, s, slip, pessimistic)
                    out.append({**common, "variant": "V2", "param": s, "slip": slip,
                                "v0_net_usd": v0["net_usd"], **r})

            for k in V3_KS:
                r = v3_variant(candles, touch, k, pessimistic)
                if r is None:
                    continue
                out.append({**common, "variant": "V3", "param": k, "slip": None,
                            "v0_net_usd": v0["net_usd"], **r})

            for g in V4A_GAPS:
                for slip in SLIPS:
                    r = v4a_variant(candles, touch, g, slip, pessimistic)
                    out.append({**common, "variant": "V4a", "param": g, "slip": slip,
                                "v0_net_usd": v0["net_usd"], **r})

            for slip in SLIPS:
                r = v4b_variant(candles, touch, slip, pessimistic)
                out.append({**common, "variant": "V4b", "param": None, "slip": slip,
                            "v0_net_usd": v0["net_usd"], **r})
    return out


def run_backtest_population(db: Path, interval: str, n_symbols: int, min_years: float,
                            every: int, seed: int, workers: int) -> list[dict]:
    cost_pct = costengine.round_trip_cost_pct()
    series = load(db, interval)
    bars_per_year = 24 * 365 if interval == "1h" else 365
    eligible = sorted(s for s, b in series.items() if len(b) >= min_years * bars_per_year)
    chosen = sorted(random.Random(seed).sample(eligible, min(n_symbols, len(eligible))))
    print(f"backtest population: {len(chosen)}/{len(eligible)} eligible coins ({interval}, "
          f">= {min_years}y), entries every {every} bars, wave0 ${WAVE0:g}, distance {DISTANCE}%, "
          f"waves {MAX_WAVES}, tp {TP}%+{TP_STEP}/rung, deadline {DEADLINE:g}d, "
          f"round-trip verification cost {cost_pct:.2f}%")
    warmup = 24
    jobs = [(s, series[s], every, cost_pct, warmup) for s in chosen]
    t0 = time.time()
    rows: list[dict] = []
    with Pool(workers) as pool:
        for i, chunk in enumerate(pool.imap_unordered(_one_symbol, jobs, chunksize=1)):
            rows.extend(chunk)
            if (i + 1) % 10 == 0 or i + 1 == len(jobs):
                print(f"  {i+1}/{len(jobs)} symbols done, {len(rows):,} rows ({time.time()-t0:.0f}s)")
    print(f"backtest population done: {len(rows):,} rows over {len({r['symbol'] for r in rows})} "
          f"symbols, all V0 rows verified against simulate_kss ({time.time()-t0:.0f}s)")
    return rows


# =====================================================================================
# Aggregation / bootstrap
# =====================================================================================

def bootstrap_mean_diff_ci(rows: list[dict], n: int = N_BOOT, seed: int = 11) -> tuple[float, float]:
    """95% CI for the mean of (net_usd - v0_net_usd) from resampling SYMBOLS with replacement
    (the repo convention for this kind of study is to resample the cluster that shares a market
    — here symbols, see scripts/ladder_panel_study.py's `boot_days`).

    Each iteration needs only per-symbol (sum, count) — O(#symbols) — never a rebuilt row list,
    which is what made the naive version (rebuild + re-mean thousands of rows x 2000 draws x
    dozens of cells) the dominant cost of a full run."""
    per_symbol: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        per_symbol[r["symbol"]].append(r["net_usd"] - r["v0_net_usd"])
    symbols = list(per_symbol)
    if len(symbols) < 5:
        return float("nan"), float("nan")
    sums = {s: math.fsum(v) for s, v in per_symbol.items()}
    counts = {s: len(v) for s, v in per_symbol.items()}
    rng = random.Random(seed)
    vals = []
    for _ in range(n):
        draw = rng.choices(symbols, k=len(symbols))
        total = sum(sums[s] for s in draw)
        cnt = sum(counts[s] for s in draw)
        if cnt:
            vals.append(total / cnt)
    if not vals:
        return float("nan"), float("nan")
    vals.sort()
    lo = vals[max(0, int(0.025 * len(vals)) - 1)]
    hi = vals[min(len(vals) - 1, int(0.975 * len(vals)))]
    return lo, hi


def top2_share(rows: list[dict]) -> float:
    by_sym: dict[str, float] = defaultdict(float)
    for r in rows:
        by_sym[r["symbol"]] += r["net_usd"]
    total = sum(by_sym.values())
    if total == 0:
        return float("nan")
    top2 = sum(sorted(by_sym.values(), reverse=True)[:2])
    return top2 / total * 100


def summarise_variant(rows: list[dict]) -> dict:
    """Realised events only (kind != 'open') feed every headline stat — an event still open at
    data-end/now is reported separately (count + mark-to-market $), never mixed into a mean."""
    opens = [r for r in rows if r["kind"] == "open"]
    realised = [r for r in rows if r["kind"] != "open"]
    n = len(realised)
    if not n:
        return {"n": 0, "open_count": len(opens),
                "open_mtm_usd": round(sum(r["net_usd"] for r in opens), 2) if opens else 0.0}
    diffs = [r["net_usd"] - r["v0_net_usd"] for r in realised]
    cap_days = sum(r["capital_days"] for r in realised)
    net_sum = sum(r["net_usd"] for r in realised)
    worst = min(realised, key=lambda r: r["net_usd"])
    lo, hi = bootstrap_mean_diff_ci(realised)
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
        "no_loss_violations": sum(1 for r in realised if r.get("violation")),
        "open_count": len(opens),
        "open_mtm_usd": round(sum(r["net_usd"] for r in opens), 2) if opens else 0.0,
    }


def summarise_v3(rows: list[dict]) -> dict:
    opens = [r for r in rows if r["kind"] == "open"]
    realised = [r for r in rows if r["kind"] != "open"]
    n = len(realised)
    if not n:
        return {"n": 0, "open_count": len(opens)}
    dl = [r for r in realised if r.get("kind") == "deadline"]
    worst = min(realised, key=lambda r: r["net_usd"])
    return {
        "n": n,
        "mean_usd": round(st.mean(r["net_usd"] for r in realised), 4),
        "median_usd": round(st.median(r["net_usd"] for r in realised), 4),
        "deadline_loss_count": sum(1 for r in dl if r.get("deadline_loss")),
        "deadline_share_pct": round(100 * len(dl) / n, 2),
        "worst_event_usd": round(worst["net_usd"], 4),
        "worst_event_symbol": worst["symbol"],
        "mean_peak_capital_usd": round(st.mean(r["peak_capital"] for r in realised), 2),
        "capital_days": round(sum(r["capital_days"] for r in realised), 2),
        "open_count": len(opens),
    }


def build_grid(rows: list[dict]) -> dict:
    """One entry per (variant, param, bound, slip)."""
    grid: dict[str, dict] = {}
    keyed: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        keyed[(r["variant"], r["param"], r["bound"], r["slip"])].append(r)
    for key, rs in keyed.items():
        variant, param, bound, slip = key
        label = f"{variant}|param={param}|{bound}|slip={slip}"
        if variant == "V3":
            grid[label] = {"variant": variant, "param": param, "bound": bound, "slip": slip,
                          **summarise_v3(rs)}
        else:
            grid[label] = {"variant": variant, "param": param, "bound": bound, "slip": slip,
                          **summarise_variant(rs)}
    return grid


def pick_headline(grid: dict, variant: str) -> dict | None:
    """Best setting of a variant, judged by the PESSIMISTIC bound's mean_diff_vs_v0_usd (V3:
    by mean_usd — it has no V0 comparison)."""
    cands = [v for v in grid.values() if v["variant"] == variant and v["bound"] == "PESS" and v.get("n")]
    if not cands:
        return None
    key = (lambda v: v.get("mean_usd", float("-inf"))) if variant == "V3" else \
          (lambda v: v.get("mean_diff_vs_v0_usd", float("-inf")))
    return max(cands, key=key)


# =====================================================================================
# Real paper-event sanity pass — data/findmy.db (+ the pre-"7k reset" backup for older
# events). Read-only: `sqlite3.connect('file:...?mode=ro', uri=True)` ONLY. Never imports
# app.main / builds a TestClient (see memory topic adhoc-script-hits-prod-db-2026-09-21.md —
# an ad-hoc script that did that once wrote straight into the running paper DB).
# =====================================================================================

BACKUP_DB = ROOT / "data" / "backups" / "findmy.db.bak-before-reset7k-20260921_163148"
BINANCE_BASE = "https://api.binance.com/api/v3/klines"
PHANTOM_WINDOW_CAP_MS = 3 * 86_400_000  # see docstring in phantom_ok()


def binance_symbol(coin: str) -> str:
    return f"{coin}USDT"


def parse_dt_ms(s: str) -> int:
    """Paper's DB timestamps are naive UTC (app.clock.utcnow-style)."""
    s = (s or "").split(".")[0]
    dt = datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def parse_tp_events(db_path: Path, label: str) -> list[dict]:
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        fills = db.execute(
            "SELECT id, symbol, source_ref, quantity, price, fee, executed_at FROM fills "
            "WHERE source_ref LIKE 'pyramid:%:tp' ORDER BY executed_at").fetchall()
        out = []
        for fid, sym, sref, qty, price, fee, ts in fills:
            parts = sref.split(":")
            if len(parts) < 2:
                continue
            try:
                sid = int(parts[1])
            except ValueError:
                continue
            srow = db.execute(
                "SELECT avg_price, total_filled_qty, total_cost, current_wave, last_fill_at, "
                "created_at FROM kss_sessions WHERE id=?", (sid,)).fetchone()
            if not srow:
                continue
            avg, tot_qty, tot_cost, waves, last_fill_at, created_at = srow
            out.append({"fill_id": fid, "session_id": sid, "symbol": sym, "tp_qty": qty,
                        "tp_price": price, "tp_fee": fee, "executed_at": ts, "avg": avg,
                        "sess_qty": tot_qty, "sess_cost": tot_cost, "waves": waves or 1,
                        "last_fill_at": last_fill_at or created_at, "created_at": created_at,
                        "source": label})
        return out
    finally:
        db.close()


def _http_get_json(url: str, tries: int = 5):
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "findmy-research/1.0"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            if e.code in (429, 418) and attempt < tries - 1:
                time.sleep(2 ** attempt)
                continue
            raise
        except urllib.error.URLError:
            if attempt < tries - 1:
                time.sleep(1.5)
                continue
            raise
    raise RuntimeError(f"exhausted retries for {url}")


def fetch_klines(symbol: str, interval: str, start_ms: int, end_ms: int, cache_dir: Path) -> list:
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = cache_dir / f"{symbol}_{interval}_{start_ms}_{end_ms}.json"
    if cache_file.exists():
        return json.loads(cache_file.read_text())
    out: list = []
    cur = start_ms
    while cur < end_ms:
        url = (f"{BINANCE_BASE}?symbol={symbol}&interval={interval}&startTime={cur}"
              f"&endTime={end_ms}&limit=1000")
        data = _http_get_json(url)
        if not data:
            break
        out.extend(data)
        last_ts = data[-1][0]
        got_full_page = len(data) >= 1000
        cur = last_ts + 1
        time.sleep(0.2)
        if not got_full_page:
            break
    cache_file.write_text(json.dumps(out))
    return out


def klines_to_candles(raw: list) -> list[dict]:
    return [{"ts": int(k[0]), "open": float(k[1]), "high": float(k[2]), "low": float(k[3]),
            "close": float(k[4])} for k in raw]


def phantom_ok(event: dict, cache_dir: Path) -> tuple[bool | None, str]:
    """A TP fill counts only if a real 1m candle high >= the TP price exists at/after the
    minute FOLLOWING the session's last BUY fill (paper once "filled" TPs on stale candles
    from before the drop). The 1m lookback is capped at PHANTOM_WINDOW_CAP_MS (3 days) before
    the TP fill to bound Binance call volume — if the true gap between the last buy and the TP
    exceeds that, only the 3 days immediately before the TP are checked and the event is
    flagged 'capped' rather than silently trusted."""
    sym = binance_symbol(event["symbol"])
    try:
        last_fill_ms = parse_dt_ms(event["last_fill_at"])
        exec_ms = parse_dt_ms(event["executed_at"])
    except ValueError:
        return None, "bad_timestamp"
    window_start = last_fill_ms + 60_000
    capped = False
    if exec_ms - window_start > PHANTOM_WINDOW_CAP_MS:
        window_start = exec_ms - PHANTOM_WINDOW_CAP_MS
        capped = True
    end = exec_ms + 5 * 60_000
    if window_start >= end:
        window_start = end - 60_000
    try:
        raw = fetch_klines(sym, "1m", window_start, end, cache_dir)
    except Exception as e:  # noqa: BLE001 - network/exchange errors are all "unknown", not "phantom"
        return None, f"fetch_failed:{type(e).__name__}:{e}"
    if not raw:
        return False, "no_data" + ("_capped" if capped else "")
    ok = any(float(k[2]) >= event["tp_price"] for k in raw)
    return ok, ("ok_capped" if capped else "ok")


def run_real_events(out_dir: Path, max_events: int, seed: int) -> dict:
    cur = parse_tp_events(ROOT / "data" / "findmy.db", "current")
    backup_events: list[dict] = []
    backup_note = "not found"
    if BACKUP_DB.exists():
        size = BACKUP_DB.stat().st_size
        if size > 10_000_000:
            backup_events = parse_tp_events(BACKUP_DB, "backup-pre-reset7k")
            backup_note = f"used, {size:,} bytes"
        else:
            backup_note = f"too small ({size:,} bytes) — looks incomplete, skipped"
    else:
        backup_note = "file not found, skipped"
    print(f"backup DB: {backup_note}")

    combined = backup_events + cur
    seen: set[tuple] = set()
    deduped = []
    for e in combined:
        key = (e["symbol"], e["executed_at"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(e)
    print(f"{len(deduped)} raw TP fill events ({len(backup_events)} backup + {len(cur)} current, "
          f"{len(combined) - len(deduped)} exact-timestamp duplicates dropped)")

    xpl = [e for e in deduped if e["symbol"] == "XPL"]
    rest = [e for e in deduped if e["symbol"] != "XPL"]
    rng = random.Random(seed)
    if len(rest) > max_events:
        rest = rng.sample(rest, max_events)
    sample = xpl + rest
    print(f"sampling {len(sample)} events for the network pass (all {len(xpl)} XPL forced-in, "
          f"cap {max_events} on the rest, seed {seed})")

    cache_dir = out_dir / "cache"
    kept, dropped = [], []
    for e in sample:
        ok, note = phantom_ok(e, cache_dir)
        e["phantom_ok"], e["phantom_note"] = ok, note
        (kept if ok else dropped).append(e)
    n_unknown = sum(1 for e in dropped if e["phantom_ok"] is None)
    print(f"phantom filter: {len(kept)} kept / {len(dropped)} dropped "
          f"({n_unknown} of those are fetch failures, i.e. unknown rather than confirmed-phantom)")

    now_ms = int(time.time() * 1000)
    rows: list[dict] = []
    open_events: list[dict] = []
    for e in kept:
        try:
            exec_ms = parse_dt_ms(e["executed_at"])
        except ValueError:
            continue
        end_ms = min(exec_ms + 60 * 86_400_000, now_ms)
        try:
            raw = fetch_klines(binance_symbol(e["symbol"]), "5m", exec_ms - 5 * 60_000, end_ms, cache_dir)
        except Exception as ex:  # noqa: BLE001
            e["fetch_error"] = f"{type(ex).__name__}:{ex}"
            continue
        candles = klines_to_candles(raw)
        if len(candles) < 2:
            continue
        touch_bar = 0
        for i, c in enumerate(candles):
            if c["ts"] <= exec_ms:
                touch_bar = i
            else:
                break
        deployed = e["sess_cost"] if e["sess_cost"] else e["avg"] * e["sess_qty"]
        touch = {"touch_bar": touch_bar, "avg": e["avg"], "filled": e["waves"],
                "qty": e["sess_qty"], "deployed": deployed, "eff_tp": TP,
                "tp_price": e["tp_price"], "entry_ts": candles[0]["ts"], "days_to_tp": None}
        common = {"symbol": e["symbol"], "year": datetime.fromtimestamp(exec_ms / 1000, timezone.utc).year,
                 "days_to_tp": None, "waves": e["waves"], "fill_id": e["fill_id"],
                 "session_id": e["session_id"], "source": e["source"]}
        v0 = v0_variant(candles, touch)
        for pessimistic in (False, True):
            bound = "PESS" if pessimistic else "OPT"
            rows.append({**common, "bound": bound, "variant": "V0", "param": None, "slip": None,
                        "v0_net_usd": v0["net_usd"], **v0})
            for w in W1_WIDTHS:
                for slip in SLIPS:
                    r = v1_variant(candles, touch, w, slip, pessimistic)
                    rows.append({**common, "bound": bound, "variant": "V1", "param": w, "slip": slip,
                                "v0_net_usd": v0["net_usd"], **r})
            for s in V2_STEPS:
                for slip in SLIPS:
                    r = v2_variant(candles, touch, s, slip, pessimistic)
                    rows.append({**common, "bound": bound, "variant": "V2", "param": s, "slip": slip,
                                "v0_net_usd": v0["net_usd"], **r})
            for g in V4A_GAPS:
                for slip in SLIPS:
                    r = v4a_variant(candles, touch, g, slip, pessimistic)
                    rows.append({**common, "bound": bound, "variant": "V4a", "param": g, "slip": slip,
                                "v0_net_usd": v0["net_usd"], **r})
            for slip in SLIPS:
                r = v4b_variant(candles, touch, slip, pessimistic)
                rows.append({**common, "bound": bound, "variant": "V4b", "param": None, "slip": slip,
                            "v0_net_usd": v0["net_usd"], **r})
        if end_ms >= now_ms - 6 * 3_600_000:  # this event's window reaches "now" — still open
            open_events.append({"symbol": e["symbol"], "session_id": e["session_id"],
                                "executed_at": e["executed_at"]})
        time.sleep(0.15)

    xpl_rows = [r for r in rows if r["symbol"] == "XPL" and r["session_id"] == 36]
    return {
        "raw_events": len(deduped), "sampled": len(sample), "kept": len(kept),
        "dropped": len(dropped), "dropped_unknown": n_unknown,
        "dropped_events": [{"symbol": e["symbol"], "session_id": e["session_id"],
                            "executed_at": e["executed_at"], "note": e["phantom_note"]}
                           for e in dropped],
        "backup_note": backup_note, "rows": rows, "xpl_session_36_rows": xpl_rows,
        "still_open_events": open_events,
    }


# =====================================================================================
# Reporting
# =====================================================================================

VARIANT_ORDER = ["V0", "V1", "V2", "V3", "V4a", "V4b"]


def headline_table(grid: dict, label: str) -> str:
    lines = [f"### {label} — best setting per variant (chosen by the PESSIMISTIC bound)", "",
            "| variant | param | slip | N | mean $/event | mean diff vs V0 | 95% CI (symbol-boot) | "
            "median diff | % worse than V0 | worst event | top-2 sym share | $/capital-day | "
            "no-loss violations |",
            "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for v in VARIANT_ORDER:
        h = pick_headline(grid, v)
        if not h or not h.get("n"):
            lines.append(f"| {v} | - | - | 0 | - | - | - | - | - | - | - | - | - |")
            continue
        if v == "V3":
            lines.append(f"| {v} | k={h['param']} | n/a | {h['n']} | {h['mean_usd']:+.3f} | n/a | n/a | "
                         f"{h['median_usd']:+.3f} | n/a | {h['worst_event_usd']:+.3f} "
                         f"({h['worst_event_symbol']}) | n/a | dl-loss {h['deadline_loss_count']} | "
                         f"NOT no-loss-guaranteed")
            continue
        ci = h.get("diff_ci95", [float("nan"), float("nan")])
        lines.append(
            f"| {v} | {h['param']} | {h['slip']} | {h['n']} | {h['mean_usd']:+.3f} | "
            f"{h['mean_diff_vs_v0_usd']:+.3f} | [{ci[0]:+.3f}, {ci[1]:+.3f}] | "
            f"{h['median_diff_vs_v0_usd']:+.3f} | {h['pct_worse_than_v0']:.1f}% | "
            f"{h['worst_event_usd']:+.3f} ({h['worst_event_symbol']}) | "
            f"{h['top2_symbol_share_pct']:.1f}% | {h['usd_per_capital_day']:+.6f} | "
            f"{h['no_loss_violations']} |")
    return "\n".join(lines)


def audit_violations(rows: list[dict], top_n: int = 20) -> dict:
    """Every event where the WHOLE session's net P&L < 0, and (a subset of those) where a stop
    filled below the true cost+fees breakeven — the two "no-loss" failure modes the brief asks
    to count and list. Sorted worst-first, capped at `top_n` for the report (the full count is
    in the grid's `no_loss_violations` per cell)."""
    viol = [r for r in rows if r.get("violation")]
    viol.sort(key=lambda r: r["net_usd"])
    listed = [{"symbol": r["symbol"], "variant": r["variant"], "param": r["param"],
              "bound": r["bound"], "slip": r["slip"], "kind": r["kind"],
              "net_usd": round(r["net_usd"], 4), "net_pct": round(r["net_pct"], 4),
              "breakeven": r.get("breakeven"), "fill": r.get("fill"), "start": r.get("start"),
              "year": r.get("year")}
             for r in viol[:top_n]]
    return {"total_violations": len(viol), "listed_worst": listed}


def write_outputs(out_dir: Path, meta: dict, bt_grid: dict, real_grid: dict | None,
                  real_extra: dict | None, bt_audit: dict | None = None,
                  real_audit: dict | None = None) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {"meta": meta, "backtest_grid": bt_grid, "backtest_no_loss_audit": bt_audit,
              "real_grid": real_grid, "real_no_loss_audit": real_audit,
              "real_extra": {k: v for k, v in (real_extra or {}).items() if k != "rows"}}
    (out_dir / "results.json").write_text(json.dumps(payload, indent=1, default=str), encoding="utf-8")

    md = ["# Runner-exit study — does letting the KSS take-profit float beat selling at the limit?",
        "", f"Generated {datetime.now(timezone.utc).isoformat()}Z. Config: distance "
        f"{DISTANCE}%, {MAX_WAVES} rungs max, TP {TP}%+{TP_STEP}%/rung, no stop-loss, "
        f"{DEADLINE:g}-day deadline, wave0 ${WAVE0:g}, maker {MAKER_FEE}% / taker {TAKER_FEE}%, "
        f"stop slippage {SLIPS}%.", "",
        "## What this means",
        "",
        "V0 is the production behaviour: sell the whole position the instant the resting "
        "take-profit limit is touched. Every other variant answers 'what if we didn't sell "
        "there' under a specific rule, each checked under BOTH intra-bar orderings (optimistic "
        "and pessimistic — a single bar's true high/low order is unknowable) and, for every "
        "stop-based exit, both a 0.1% and 0.3% slippage assumption. The headline tables below "
        "pick each variant's best-performing SETTING by its PESSIMISTIC-bound result, not its "
        "best case, per the brief. A variant only 'guarantees no loss' if its no-loss-violation "
        "count is 0 across every setting and bound checked — see the audit table.", "",
    ]
    md.append(headline_table(bt_grid, "Backtest population (primary)"))
    md.append("")
    if real_grid:
        md.append(headline_table(real_grid, "Real paper events (secondary, sanity)"))
        md.append("")

    for label, audit in (("Backtest", bt_audit), ("Real events", real_audit)):
        if not audit or not audit.get("total_violations"):
            continue
        md.append(f"### {label} — no-loss audit ({audit['total_violations']} total violations "
                  f"across every setting/bound/slip; worst {len(audit['listed_worst'])} listed)")
        md.append("")
        md.append("| symbol | variant | param | bound | slip | kind | net $ | net % | breakeven | fill |")
        md.append("|---|---|---|---|---|---|---|---|---|---|")
        for v in audit["listed_worst"]:
            be = f"{v['breakeven']:.6g}" if v.get("breakeven") is not None else "-"
            fl = f"{v['fill']:.6g}" if v.get("fill") is not None else "-"
            md.append(f"| {v['symbol']} | {v['variant']} | {v['param']} | {v['bound']} | "
                      f"{v['slip']} | {v['kind']} | {v['net_usd']:+.3f} | {v['net_pct']:+.3f}% | "
                      f"{be} | {fl} |")
        md.append("")
    if real_extra:
        md.append("## Real-event dataset detail")
        md.append("")
        md.append(f"- raw TP fills found: {real_extra['raw_events']}")
        md.append(f"- backup DB: {real_extra['backup_note']}")
        md.append(f"- sampled for the network pass: {real_extra['sampled']}")
        md.append(f"- kept after the phantom filter: {real_extra['kept']} / dropped: "
                  f"{real_extra['dropped']} (of which {real_extra['dropped_unknown']} were fetch "
                  f"failures — unknown, not confirmed phantom)")
        md.append(f"- events whose 60-day window reaches 'now' (still open, MTM only): "
                  f"{len(real_extra['still_open_events'])}")
        xr = real_extra.get("xpl_session_36_rows", [])
        v0xr = [r for r in xr if r["variant"] == "V0"]
        if v0xr:
            md.append("")
            md.append("### XPL session 36 (the owner's example)")
            md.append(f"avg 0.0928, qty 1645.9, 3 rungs, TP fill 0.09859259808391956 — V0 net "
                      f"${v0xr[0]['net_usd']:+.3f} ({v0xr[0]['net_pct']:+.3f}%).")
    out_dir.joinpath("report.md").write_text("\n".join(md), encoding="utf-8")
    print(f"\nwrote {out_dir/'results.json'} and {out_dir/'report.md'}")


# =====================================================================================
# CLI
# =====================================================================================

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--db", default="data/research/market.db")
    p.add_argument("--interval", default="1h")
    p.add_argument("--symbols", type=int, default=60)
    p.add_argument("--min-years", type=float, default=2.0)
    p.add_argument("--every", type=int, default=24)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--out", default="docs/runner-exit-2026-09-25")
    p.add_argument("--real-only", action="store_true", help="skip the backtest population, run only the paper-DB sanity pass")
    p.add_argument("--no-real", action="store_true", help="skip the paper-DB / Binance network pass")
    p.add_argument("--real-max-events", type=int, default=45)
    p.add_argument("--real-seed", type=int, default=13)
    args = p.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    out_dir = Path(args.out)
    t0 = time.time()
    meta = vars(args) | {"distance": DISTANCE, "max_waves": MAX_WAVES, "tp": TP, "tp_step": TP_STEP,
                        "deadline": DEADLINE, "wave0": WAVE0, "maker_fee": MAKER_FEE,
                        "taker_fee": TAKER_FEE, "slips": SLIPS, "v1_widths": W1_WIDTHS,
                        "v2_steps": V2_STEPS, "v3_ks": V3_KS, "v4a_gaps": V4A_GAPS}

    bt_grid: dict = {}
    bt_audit: dict = {}
    if not args.real_only:
        rows = run_backtest_population(Path(args.db), args.interval, args.symbols, args.min_years,
                                       args.every, args.seed, args.workers)
        bt_grid = build_grid(rows)
        bt_audit = audit_violations(rows)
        print(f"backtest grid: {len(bt_grid)} (variant,param,bound,slip) cells, "
              f"{bt_audit['total_violations']} no-loss violations ({time.time()-t0:.0f}s)")
        del rows

    real_grid = None
    real_extra = None
    real_audit = None
    if not args.no_real:
        real_extra = run_real_events(out_dir, args.real_max_events, args.real_seed)
        real_grid = build_grid(real_extra["rows"])
        real_audit = audit_violations(real_extra["rows"])
        print(f"real-event grid: {len(real_grid)} cells, {real_audit['total_violations']} "
              f"no-loss violations ({time.time()-t0:.0f}s)")

    write_outputs(out_dir, meta, bt_grid, real_grid, real_extra, bt_audit, real_audit)
    print(f"\ndone in {time.time()-t0:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
