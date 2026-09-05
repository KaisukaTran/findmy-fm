"""
Faithful, offline backtest harness for the KSS ``pyramid_up`` strategy mode (anti-martingale,
add-to-winners). Measurement only — no DB, no network, no imports from ``app/`` (deliberate: this
keeps the harness 100% reproducible regardless of the live worktree's ``.env``/runtime-config
state, and guarantees it can never touch ``data/live.db`` or the exchange). Every rule below is a
line-by-line port of the cited production code, with the exact file:line so a reviewer can diff
this against the source of truth.

======================================================================================
MECHANISM MIRRORED (file:line) — read this before changing anything below
======================================================================================

Pure ladder math — ``app/kss/pyramid_up.py``:
  - ``add_trigger_price(entry, n, step_pct) = entry * (1 + step_pct/100)**n``      (pyramid_up.py:62-69)
  - ``add_qty(base_qty, n, size_ratio) = base_qty * size_ratio**n``                (pyramid_up.py:72-81)
  - ``stop_after_add(avg, lock_pct, fee_floor) = max(fee_floor, avg*(1+lock_pct/100), avg)``
                                                                                     (pyramid_up.py:84-92)
  - ``MAX_ADDS_CAP = 3``                                                           (pyramid_up.py:28)
  Ported verbatim below as plain functions (the real module is pure/side-effect-free, but this
  harness never imports ``app.*`` at all — see "Why no app/ imports" below).

Service-layer wiring — ``app/kss/service.py``:
  - ``_clamped_pyramid_up_knobs`` (service.py:532-542): step_pct, size_ratio clamped to
    (0,1) exclusive, max_adds clamped to [0, MAX_ADDS_CAP], lock_pct passed through.
  - Defensive rung (service.py:660-703, ``DEFENSIVE_WAVE_NUM=-1`` at service.py:529): ONE
    conditional BUY at ``entry*(1 - kss_trail_arm_pct/100)``, sized by COST to match the base
    wave's own cost (service.py:669-682: "Size by COST relative to the ACTUAL base wave"),
    capped at remaining deploy headroom (ignored here — a single isolated backtest trial has no
    competing capital demand, so the defensive rung is always sized at exactly the base wave's
    notional, per this study's ``wave0_notional_usd`` knob).
  - ``_handle_pyramid_up_fill`` (service.py:1117-1170): recomputes avg/total_qty/total_cost from
    ALL filled waves (source of truth). If the fill is the DEFENSIVE wave -> flip
    (``_flip_to_dca_down``, service.py:1172-1172+). Else, for an ADD (wave_num>=1) that fills
    ABOVE its own break-even-plus floor (``fill_price > stop_after_add(avg, lock_pct, fee_floor)``),
    ARM trailing: ``trail_active=True``, ``peak=max(peak, fill, avg)``,
    ``trail_sl=max(new_sl, prev_trail_sl)``, ``trail_dist=trail_distance_pct(ATR%)``. The BASE
    wave (n=0) never arms by itself — only an in-profit ADD does.
  - ``_pyramid_up_hard_sl`` (service.py:1901-1929): before ``trail_active``, the ONLY protection is
    ``avg*(1 - sl_pct/100)`` — a MARKET exit allowed to realize a loss.
  - ``_evaluate_dynamic_exit`` (service.py:1654-1743, the "Ride & Trail" channel):
      * RIDE (in profit, not yet armed): suppresses the fixed TP; ALSO independently arms if
        price clears the standard ``avg*(1+kss_trail_arm_pct/100)`` threshold (service.py:1683-
        1702) — this is a SECOND, independent arming path from the add-fill arm above. Arming via
        THIS path calls ``_cancel_pending_waves`` (service.py:1584-1610) which cancels BOTH the
        remaining armed up-adds AND the still-armed defensive rung. Arming via the ADD-FILL path
        (``_handle_pyramid_up_fill``) does **not** cancel anything — the defensive rung and any
        remaining up-adds stay live and can still fire later, even while ``trail_active``. Given
        the default knobs (step_pct=2% << arm_pct=5%), the add-fill path almost always arms
        FIRST, so in practice the defensive rung typically stays a live flip-risk for the whole
        session (a genuine, faithfully-modelled mechanism quirk, not a simplification).
      * armed: checks the carried TP/SL, production order TP-then-SL (service.py:1716-1730,
        "armed — check the channel on the CARRIED sl/tp first (ordering)... if price>=carried_tp
        ... elif price<=carried_sl"), else ratchets peak/SL up (never down).
      * the K-2/K-trail "does this exit clear true cost basis" defer guard (``_tp_clears_cost``)
        is a cross-SESSION wallet-aggregate check — irrelevant to an isolated single-position
        backtest trial (no other session competes for the same symbol's cost basis), so it is
        always True here (a documented simplification, not a mirrored branch).
  - ``dynamic_exit.py`` pure formulas (dynamic_exit.py:34-104): ``fee_floor_price``,
    ``arm_threshold``, ``should_arm``, ``lock_floor_price``, ``trail_distance_pct``,
    ``compute_sl`` (grid snap-down on ``avg*(1+d)**k``, floored at ``lock_floor_price``, monotonic
    via ``prev_sl``), ``compute_tp = max(sl*(1+gap%/100), fee_floor)``. Ported verbatim below,
    taking every tunable as an explicit argument instead of reading the global ``settings``
    singleton (see "Why no app/ imports").
  - Router (``app/scanner.py:855-873`` ``_route_strategy_mode``, and ``app/kss/regime.py``
    ``classify_mode``, ports below): pyramid_up is selected only when ALL of:
      1. ``htf_trend=='up' OR supertrend=='up'``
      2. ``rel_strength > pyramid_up_min_rel_strength`` (STRICT >, regime.py's own comparison)
      3. ``macd_hist_pct > 0``
      4. ``adx >= pyramid_up_min_adx``
    ``rel_strength = coin_N_bar_return - BTC_N_bar_return`` (scanner.py:865-866,
    ``_nbar_return`` at scanner.py:826-829, N = ``rel_strength_lookback_bars`` = 7 daily bars,
    config.py:246), None-safe fallback to 0.0. ADX/MACD/HTF/Supertrend are Tier-1 TA
    (``app/ta/indicators.py`` — ``adx``:139-165, ``macd``:94-111 via ``_ema_series``:31-40,
    ``htf_trend``:244-251 via ``sma``:23-28, ``supertrend``:182-206 via
    ``_wilder_atr_series``:52-59/``_true_ranges``:42-49), computed on ``settings.backtest_
    timeframe`` candles (default ``"1d"`` — config.py:239), i.e. on DAILY bars in production. This
    study's source data is 4h bars, so every one of these signals is computed on a CAUSAL daily
    aggregation of the 4h series (see "Causal daily aggregation" below) — production's default
    config already runs the router on daily bars, so this is a faithful match, not a
    simplification, once the intraday-lookahead fix below is applied.
  - Knob defaults used for the "baseline" config in this study (config.py, cited per line, exact
    values given in the study brief): pyramid_up_step_pct=2.0 (:254), pyramid_up_size_ratio=0.7
    (:255), pyramid_up_max_adds=2 (:256), pyramid_up_lock_pct=1.0 (:257), sl_pct=8.0 (:345),
    kss_tp_gap_pct=5.0 (:350), kss_exit_fee_mult=3.0 (:351), kss_trail_atr_mult=1.0 (:352),
    kss_trail_min_pct=3.0 (:353), kss_trail_arm_pct=5.0 (:354), kss_trail_lock_pct=2.0 (:355),
    pyramid_up_min_adx=20.0 (:253), pyramid_up_min_rel_strength=0.0 (:252),
    rel_strength_lookback_bars=7 (:246). Deadline fixed at 7 days for this study (vs. config's
    general ``deadline_days`` knob) — 7 days = 42 4h-bars, which is also this study's entry
    spacing (coincidental but harmless: a trial's own deadline lands exactly when the NEXT trial
    for that symbol opens).
  - ATR for the trailing distance: the study brief specifies ``app/autotune.py::atr_pct``
    (autotune.py:126-144, ``_ATR_BARS=14``, ``_MIN_BARS=15`` at autotune.py:50-51) — a SIMPLE
    (non-Wilder) mean of true-range%, over the last 14 CLOSED daily bars. NOTE: the actual
    service-layer call site for a pyramid_up session's trailing distance
    (``_session_atr_pct``, service.py, calls ``app.ta.indicators.atr_pct`` — Wilder-smoothed) is a
    DIFFERENT function than the one the study brief names; we follow the brief's explicit
    instruction (autotune.py's simple-mean ATR) as the authoritative spec for this study, and flag
    the discrepancy here rather than silently picking one.
  - Cost: ``app/costengine.py::round_trip_cost_pct()`` = ``2*taker_fee_pct + 2*slippage_pct``
    = ``2*0.1 + 2*0.05`` = 0.30% (costengine.py:15-17, config.py:186-187 defaults). Subtracted once
    from every realized trial's percentage return (both legs of the round trip, one flat charge).

Causal daily aggregation (``to_daily``) — ported verbatim, including its causality contract, from
``scripts/live_mechanism.py:37-59`` (a sibling study in this same repo that found and fixed the
exact bug this harness must not reintroduce): grouping 4h bars into UTC-day buckets, each bucket
carrying ``end_ts`` = the timestamp of the LAST 4h bar folded into it so far. A daily bucket may be
used for a signal at entry timestamp ``entry_ts`` only when ``bucket['end_ts'] < entry_ts`` —
i.e. only fully-CLOSED days count; the still-forming day containing (or after) the entry bar itself
is always excluded. This is stricter than production's own literal behaviour (which runs on daily
candles directly, so ``candles[-1]`` IS the entry day's own just-closed candle, inclusive) — but
production's default config already operates one full timeframe grid coarser (daily) than this
study's 4h simulation grid, so re-deriving daily buckets from 4h data forces an explicit
closed/not-closed distinction production never has to make. Excluding the still-forming day is the
conservative, causal choice for our finer grid, and it is proven correct by ``--causality-check``
below (append random future data past the cutoff, recompute every signal, assert byte-identical
pre-cutoff values).

Fill modelling (study brief, not directly production behaviour — production posts real exchange
orders and the async approval queue governs actual fill timing; this harness fills synchronously
within a bar as a documented backtest simplification):
  - Base wave (n=0) fills at the ENTRY BAR's own CLOSE. The simulation loop starts at the NEXT bar
    (a lookahead bug from counting the entry bar itself was found and fixed in a sibling study
    before this one — this harness starts at ``entry_i + 1``, never ``entry_i``).
  - An up-add (n>=1) fills when ``bar.high >= trigger``, at ``max(trigger, bar.open)`` (a buy-stop
    through a gap fills at the worse, open, price).
  - The defensive rung fills when ``bar.low <= trigger``, at ``min(trigger, bar.open)``.
  - SL/trailing-SL/TP exits realize at the stop/TP price itself (no slippage modelled) — a known
    OPTIMISTIC simplification flagged in every findings table; a real MARKET stop can slip through
    its trigger in a fast market.
  - Within one 4h bar, several distinct trigger conditions can be geometrically compatible at once
    (e.g. a big-range bar clears both an up-add trigger and, on paper, the hard-SL floor) even
    though production evaluates on near-continuous live ticks where only one condition can be true
    at a time. This harness resolves that ambiguity with a FIXED, documented per-bar evaluation
    order that mirrors production's own per-cycle CALL order (service.py's ``manage_open_sessions``
    calls defensive-check, then add-check, then the exit/hard-SL check, every cycle — see
    ``_maybe_queue_pyramid_defensive`` then ``_maybe_queue_pyramid_add`` then
    ``_evaluate_dynamic_exit``/``_pyramid_up_hard_sl`` at service.py:2277-2289):
      1. defensive-rung fill check (flip — terminates the trial's pyramid_up measurement)
      2. up-add fill loop, lowest n first, each requiring the previous add already filled
         (mirrors ``_maybe_queue_pyramid_add``'s "find the lowest-n ARMED wave... AND the previous
         wave is filled" gate, service.py:1815-1836) — a wide-range bar can fill more than one add
         in sequence within the same bar.
      3. if not yet armed: the standalone RIDE-arm check (``bar.high >= avg*(1+arm_pct/100)``),
         else the pre-arm hard-SL check (``bar.low <= avg*(1-sl_pct/100)``).
      4. if armed (from step 2, step 3, or an earlier bar) and NOT freshly armed THIS bar: the
         trailing channel TP/SL check ("no exit on the arm tick" mirrors service.py:1702's own
         comment, applied identically to both arming paths for consistency), then ratchet.
    This resolution order is a disclosed assumption, not a mirrored line of code (OHLC bars have
    no true intrabar path) — the "both intrabar orderings" battery item (TP-first / SL-first)
    tests the ONE part of this ambiguity the study brief explicitly asks to bound; the
    defensive-before-add tie-break is fixed (not swept) since it structurally matches the code's
    own call order.

Why no ``app/`` imports: ``app/kss/pyramid_up.py`` is genuinely pure (no settings/DB import at
all) and would be safe to import directly, but ``app/kss/dynamic_exit.py`` and
``app/ta/indicators.py`` transitively import ``app.config.settings`` (via ``app/data/providers.py``
for the TA layer, and directly for dynamic_exit) — i.e. loading the live worktree's ``.env`` and
any persisted runtime-config mutations. This study needs the EXACT literal knob values given in
the brief, reproducibly, regardless of what live's ``.env``/DB currently holds — so every formula
below is transcribed with a file:line citation instead, and the harness never touches ``app/`` at
all (belt-and-suspenders with the "do not modify anything under app/" instruction — it also never
IMPORTS it).

======================================================================================
Usage
======================================================================================
    .venv/Scripts/python.exe scripts/pyramid_up_backtest.py --data <pkl> --out <dir> [options]

Options:
    --data PATH          Primary pickle: {symbol: [Candle,...]} 4h bars (required).
    --extended PATH       Optional secondary (longer-history) pickle, run as battery items 1-4
                          again if present. Default: <data's dir>/u30_4h_long.pkl if it exists.
    --out DIR             Output directory for battery_results.json (required).
    --n-boot INT           Bootstrap resamples for the gate-CI (default 2000, must be >=1000).
    --seed INT             RNG seed (default 42).
    --symbols a,b,c        Restrict to these symbols (debug/dev only).
    --skip-sweep           Skip the (expensive) battery item 5 knob sweep.
    --quiet                Less stdout progress chatter.
"""

from __future__ import annotations

import argparse
import bisect
import json
import math
import pickle
import random
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

MS_DAY = 86_400_000.0
MS_4H = 14_400_000.0
BARS_PER_TRIAL_HORIZON = 42  # 7 days of 4h bars — this study's fixed deadline
ENTRY_SPACING_BARS = 42
ENTRY_START_BAR = 200
COST_PCT = 0.30  # app/costengine.py:15-17 round_trip_cost_pct(), default taker_fee/slippage
MAX_ADDS_CAP = 3  # app/kss/pyramid_up.py:28

# --- Baseline knob defaults (app/config.py, cited above) --------------------------------------
BASELINE_CFG = {
    "wave0_usd": 40.0,
    "step_pct": 2.0,            # config.py:254
    "size_ratio": 0.7,          # config.py:255
    "max_adds": 2,              # config.py:256
    "lock_pct": 1.0,            # config.py:257
    "arm_pct": 5.0,             # kss_trail_arm_pct, config.py:354
    "trail_lock_pct": 2.0,      # kss_trail_lock_pct, config.py:355
    "trail_atr_mult": 1.0,      # kss_trail_atr_mult, config.py:352
    "trail_min_pct": 3.0,       # kss_trail_min_pct, config.py:353
    "tp_gap_pct": 5.0,          # kss_tp_gap_pct, config.py:350
    "exit_fee_mult": 3.0,       # kss_exit_fee_mult, config.py:351
    "sl_pct": 8.0,              # config.py:345
    "cost_pct": COST_PCT,
    "deadline_bars": BARS_PER_TRIAL_HORIZON,
}
PU_MIN_ADX = 20.0                 # pyramid_up_min_adx, config.py:253
PU_MIN_REL_STRENGTH = 0.0         # pyramid_up_min_rel_strength, config.py:252
REL_STRENGTH_LOOKBACK = 7         # rel_strength_lookback_bars (DAILY bars), config.py:246


# ======================================================================================
# Causal daily aggregation — verbatim port of scripts/live_mechanism.py:37-59
# ======================================================================================

def to_daily(bars: list[dict]) -> list[dict]:
    """Fold 4h bars into UTC-day buckets. ``end_ts`` = the timestamp of the last 4h bar folded
    into that bucket so far — a bucket may be used by a causal signal only once
    ``end_ts < entry_ts`` (see module docstring). Verbatim port of
    scripts/live_mechanism.py:37-59 (``to_daily``), which documents the exact lookahead bug this
    causal filter exists to prevent."""
    out: list[dict] = []
    cur = None
    day = None
    for b in bars:
        d = int(b["ts"] // MS_DAY)
        if d != day:
            if cur:
                out.append(cur)
            cur = {"ts": b["ts"], "end_ts": b["ts"], "open": b["open"], "high": b["high"],
                   "low": b["low"], "close": b["close"]}
            day = d
        else:
            cur["high"] = max(cur["high"], b["high"])
            cur["low"] = min(cur["low"], b["low"])
            cur["close"] = b["close"]
            cur["end_ts"] = b["ts"]
    if cur:
        out.append(cur)
    return out


def causal_slice(daily: list[dict], end_ts_list: list[float], entry_ts: float) -> list[dict]:
    """Daily buckets strictly closed before ``entry_ts`` (bisect_left => end_ts < entry_ts)."""
    idx = bisect.bisect_left(end_ts_list, entry_ts)
    return daily[:idx]


# ======================================================================================
# ATR — app/autotune.py:126-144 (simple mean TR%, 14 CLOSED daily bars) — per study brief
# ======================================================================================

_ATR_BARS = 14   # autotune.py:50
_MIN_BARS = 15   # autotune.py:51


def atr_pct_daily(daily_slice: list[dict]) -> float:
    """Port of app/autotune.py:126-144 ``atr_pct`` — simple mean of true-range%% over the last
    14 bars (needs a previous close per bar, so 15 candles minimum)."""
    if len(daily_slice) < _MIN_BARS:
        return 0.0
    ranges = []
    for prev, bar in zip(daily_slice[-_ATR_BARS - 1:-1], daily_slice[-_ATR_BARS:]):
        close = float(bar["close"]) or 1.0
        tr = max(
            float(bar["high"]) - float(bar["low"]),
            abs(float(bar["high"]) - float(prev["close"])),
            abs(float(bar["low"]) - float(prev["close"])),
        )
        ranges.append(tr / close * 100.0)
    return sum(ranges) / len(ranges) if ranges else 0.0


# ======================================================================================
# Tier-1 TA — verbatim port of app/ta/indicators.py (functions needed by the router gate)
# ======================================================================================

def _sma(values: list[float], n: int) -> float:
    """app/ta/indicators.py:23-28 ``sma``."""
    if not values:
        return 0.0
    window = values[-n:]
    return sum(window) / len(window)


def _ema_series(values: list[float], n: int) -> list[float]:
    """app/ta/indicators.py:31-40 ``_ema_series``."""
    if not values:
        return []
    k = 2.0 / (n + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def _true_ranges(candles: list[dict]) -> list[float]:
    """app/ta/indicators.py:42-49 ``_true_ranges``."""
    trs = [0.0]
    for i in range(1, len(candles)):
        h, low = candles[i]["high"], candles[i]["low"]
        pc = candles[i - 1]["close"]
        trs.append(max(h - low, abs(h - pc), abs(low - pc)))
    return trs


def _wilder_atr_series(candles: list[dict], n: int) -> list[float]:
    """app/ta/indicators.py:52-59 ``_wilder_atr_series``."""
    trs = _true_ranges(candles)
    atr = [0.0] * len(candles)
    if len(candles) <= n:
        return atr
    atr[n] = sum(trs[1:n + 1]) / n
    for i in range(n + 1, len(candles)):
        atr[i] = (atr[i - 1] * (n - 1) + trs[i]) / n
    return atr


def adx14(candles: list[dict], n: int = 14) -> dict:
    """app/ta/indicators.py:139-165 ``adx`` — Wilder ADX with +DI/-DI."""
    if len(candles) < 2 * n + 1:
        return {"adx": 0.0, "plus_di": 0.0, "minus_di": 0.0}
    trs = _true_ranges(candles)
    plus_dm, minus_dm = [0.0], [0.0]
    for i in range(1, len(candles)):
        up = candles[i]["high"] - candles[i - 1]["high"]
        down = candles[i - 1]["low"] - candles[i]["low"]
        plus_dm.append(up if (up > down and up > 0) else 0.0)
        minus_dm.append(down if (down > up and down > 0) else 0.0)

    def _wilder(series: list[float]) -> list[float]:
        sm = [0.0] * len(series)
        sm[n] = sum(series[1:n + 1])
        for i in range(n + 1, len(series)):
            sm[i] = sm[i - 1] - sm[i - 1] / n + series[i]
        return sm

    str_, pdm, mdm = _wilder(trs), _wilder(plus_dm), _wilder(minus_dm)
    dx: list[float] = []
    for i in range(n, len(candles)):
        if str_[i] == 0:
            dx.append(0.0)
            continue
        pdi = 100 * pdm[i] / str_[i]
        mdi = 100 * mdm[i] / str_[i]
        denom = pdi + mdi
        dx.append(100 * abs(pdi - mdi) / denom if denom else 0.0)
    if len(dx) < n:
        return {"adx": 0.0, "plus_di": 0.0, "minus_di": 0.0}
    adx_val = sum(dx[:n]) / n
    for d in dx[n:]:
        adx_val = (adx_val * (n - 1) + d) / n
    last_str = str_[-1] or 1.0
    return {"adx": adx_val, "plus_di": 100 * pdm[-1] / last_str, "minus_di": 100 * mdm[-1] / last_str}


def macd_hist_pct(candles: list[dict], fast: int = 12, slow: int = 26, signal: int = 9) -> float:
    """app/ta/indicators.py:94-111 ``macd`` — returns just ``hist_pct``."""
    cs = [c["close"] for c in candles]
    if len(cs) < slow + signal:
        return 0.0
    ef, es = _ema_series(cs, fast), _ema_series(cs, slow)
    line = [a - b for a, b in zip(ef, es, strict=True)]
    sig = _ema_series(line, signal)
    last_line, last_sig = line[-1], sig[-1]
    price = cs[-1] or 1.0
    return (last_line - last_sig) / price * 100


def supertrend_dir(candles: list[dict], n: int = 10, mult: float = 3.0) -> str:
    """app/ta/indicators.py:182-206 ``supertrend``."""
    length = len(candles)
    if length < n + 2:
        return "flat"
    atr = _wilder_atr_series(candles, n)
    f_upper = [0.0] * length
    f_lower = [0.0] * length
    direction = [1] * length
    for i in range(n, length):
        c = candles[i]
        hl2 = (c["high"] + c["low"]) / 2
        bu, bl = hl2 + mult * atr[i], hl2 - mult * atr[i]
        if i == n:
            f_upper[i], f_lower[i] = bu, bl
            direction[i] = 1 if c["close"] >= hl2 else -1
            continue
        prev_close = candles[i - 1]["close"]
        f_upper[i] = bu if (bu < f_upper[i - 1] or prev_close > f_upper[i - 1]) else f_upper[i - 1]
        f_lower[i] = bl if (bl > f_lower[i - 1] or prev_close < f_lower[i - 1]) else f_lower[i - 1]
        if direction[i - 1] == 1:
            direction[i] = -1 if c["close"] < f_lower[i] else 1
        else:
            direction[i] = 1 if c["close"] > f_upper[i] else -1
    return "up" if direction[-1] == 1 else "down"


def htf_trend_dir(candles: list[dict], factor: int = 7, n: int = 20) -> str:
    """app/ta/indicators.py:244-251 ``htf_trend``."""
    cs = [c["close"] for c in candles]
    grouped = [cs[i] for i in range(factor - 1, len(cs), factor)]
    if len(grouped) < 5:
        return "flat"
    ref = _sma(grouped, min(n, len(grouped)))
    return "up" if grouped[-1] >= ref else "down"


def nbar_return(candles: list[dict], n: int) -> float | None:
    """app/scanner.py:826-829 ``_nbar_return``."""
    if not candles or n <= 0 or len(candles) <= n:
        return None
    prev = candles[-1 - n]["close"]
    return (candles[-1]["close"] / prev - 1) * 100.0 if prev else None


def classify_mode(*, htf: str | None, st: str | None, adx: float, rel_strength: float,
                   macdh: float, min_rel_strength: float, min_adx: float) -> str:
    """Verbatim port of app/kss/regime.py ``classify_mode`` (``enabled`` always True here — the
    caller decides whether to apply the gate at all for the WITH/WITHOUT comparison)."""
    is_uptrend = htf == "up" or st == "up"
    if not is_uptrend:
        return "dca_down"
    if rel_strength <= min_rel_strength:
        return "dca_down"
    if macdh <= 0:
        return "dca_down"
    if adx < min_adx:
        return "dca_down"
    return "pyramid_up"


# ======================================================================================
# Pure ladder math — verbatim port of app/kss/pyramid_up.py:62-92
# ======================================================================================

def add_trigger_price(entry: float, n: int, step_pct: float) -> float:
    return entry * (1 + step_pct / 100.0) ** n


def add_qty(base_qty: float, n: int, size_ratio: float) -> float:
    return base_qty * (size_ratio ** n)


def stop_after_add(avg: float, lock_pct: float, fee_floor: float) -> float:
    floor = avg * (1 + max(lock_pct, 0.0) / 100.0)
    return max(fee_floor, floor, avg)


# ======================================================================================
# dynamic_exit.py formulas — verbatim port of app/kss/dynamic_exit.py:34-104, taking every
# tunable as an explicit argument (the real module reads them off ``settings``).
# ======================================================================================

def fee_floor_price(avg: float, exit_fee_mult: float, cost_pct: float) -> float:
    return avg * (1 + exit_fee_mult * cost_pct / 100.0)


def lock_floor_price(avg: float, exit_fee_mult: float, cost_pct: float, trail_lock_pct: float) -> float:
    return max(fee_floor_price(avg, exit_fee_mult, cost_pct), avg * (1 + trail_lock_pct / 100.0))


def trail_distance_pct(atr_pct_val: float, trail_atr_mult: float, trail_min_pct: float) -> float:
    atr = atr_pct_val if (atr_pct_val and atr_pct_val > 0) else 0.0
    return max(trail_atr_mult * atr, trail_min_pct)


def compute_sl(*, peak: float, avg: float, distance_pct: float, trail_dist_pct: float,
                prev_sl: float, exit_fee_mult: float, cost_pct: float, trail_lock_pct: float) -> float:
    d = distance_pct / 100.0
    floor = lock_floor_price(avg, exit_fee_mult, cost_pct, trail_lock_pct)
    target = peak * (1 - trail_dist_pct / 100.0)
    if target > avg and d > 0:
        k = max(math.floor(math.log(target / avg) / math.log(1 + d)), 0)
        grid_sl = avg * (1 + d) ** k
    else:
        grid_sl = avg
    return max(grid_sl, floor, prev_sl)


def compute_tp(*, sl: float, avg: float, tp_gap_pct: float, exit_fee_mult: float, cost_pct: float) -> float:
    return max(sl * (1 + tp_gap_pct / 100.0), fee_floor_price(avg, exit_fee_mult, cost_pct))


# ======================================================================================
# Data loading + per-symbol precompute
# ======================================================================================

class SymbolData:
    """Precomputed, knob-independent state for one symbol — built once, reused across every
    trial / knob combo / ordering for that symbol (this is the whole performance story: ~900k
    trial-simulations share this instead of re-deriving daily aggregation/ATR each time)."""

    __slots__ = ("symbol", "bars", "daily", "end_ts", "atr_by_bar")

    def __init__(self, symbol: str, bars: list[dict]):
        self.symbol = symbol
        self.bars = bars
        self.daily = to_daily(bars)
        self.end_ts = [d["end_ts"] for d in self.daily]
        self.atr_by_bar = self._precompute_atr_by_bar()

    def _precompute_atr_by_bar(self) -> list[float]:
        out = [0.0] * len(self.bars)
        ptr = 0
        n = len(self.end_ts)
        for j, b in enumerate(self.bars):
            ts = b["ts"]
            while ptr < n and self.end_ts[ptr] < ts:
                ptr += 1
            out[j] = atr_pct_daily(self.daily[:ptr])
        return out


def load_universe(path: Path) -> dict[str, list[dict]]:
    with open(path, "rb") as f:
        data = pickle.load(f)
    # ensure ascending ts order defensively
    for sym, bars in data.items():
        bars.sort(key=lambda c: c["ts"])
    return data


# ======================================================================================
# Router-gate entry context (independent of strategy knobs — computed once per (symbol, entry_i))
# ======================================================================================

def entry_context(sym: SymbolData, btc: SymbolData, entry_i: int) -> dict:
    entry_ts = sym.bars[entry_i]["ts"]
    coin_slice = causal_slice(sym.daily, sym.end_ts, entry_ts)
    btc_slice = causal_slice(btc.daily, btc.end_ts, entry_ts)
    adxd = adx14(coin_slice)
    macdh = macd_hist_pct(coin_slice)
    htf = htf_trend_dir(coin_slice)
    st = supertrend_dir(coin_slice)
    coin_ret = nbar_return(coin_slice, REL_STRENGTH_LOOKBACK)
    btc_ret = nbar_return(btc_slice, REL_STRENGTH_LOOKBACK)
    rel_strength = (coin_ret - btc_ret) if (coin_ret is not None and btc_ret is not None) else 0.0
    mode = classify_mode(htf=htf, st=st, adx=adxd["adx"], rel_strength=rel_strength, macdh=macdh,
                          min_rel_strength=PU_MIN_REL_STRENGTH, min_adx=PU_MIN_ADX)
    return {
        "entry_i": entry_i, "entry_ts": entry_ts, "adx": adxd["adx"], "rel_strength": rel_strength,
        "htf": htf, "st": st, "macdh": macdh, "gate_pass": mode == "pyramid_up",
    }


# ======================================================================================
# Trial simulators
# ======================================================================================

def simulate_pyramid_up_trial(sym: SymbolData, entry_i: int, cfg: dict, ordering: str) -> dict:
    """One pyramid_up trial. ``ordering`` in {"tp_first" (production), "sl_first" (pessimistic)}.
    See the module docstring's "Fill modelling" section for the full per-bar rule set."""
    bars = sym.bars
    entry = bars[entry_i]["close"]
    wave0_usd = cfg["wave0_usd"]
    step_pct = cfg["step_pct"]
    size_ratio = cfg["size_ratio"]
    max_adds = min(cfg["max_adds"], MAX_ADDS_CAP)
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
    adds = [(n, add_trigger_price(entry, n, step_pct), add_qty(base_qty, n, size_ratio))
            for n in range(1, max_adds + 1)]
    defensive_trigger = entry * (1 - arm_pct / 100.0)
    defensive_qty = (wave0_usd / defensive_trigger) if defensive_trigger > 0 else 0.0

    total_qty = base_qty
    total_cost = base_qty * entry
    avg = entry
    next_add = 0
    defensive_status = "armed"
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

        # 1. defensive-rung fill (reversal-flip)
        if defensive_status == "armed" and low <= defensive_trigger:
            fill_price = min(defensive_trigger, o)
            total_cost += defensive_qty * fill_price
            total_qty += defensive_qty
            avg = total_cost / total_qty
            defensive_status = "filled"
            exit_reason, exit_price, exit_j = "flip", fill_price, j
            break

        # 2. up-add fill loop (lowest n first, sequential)
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
                    ff = fee_floor_price(avg, exit_fee_mult, cost_pct)
                    new_sl = stop_after_add(avg, lock_pct, ff)
                    if fill_price > new_sl:
                        trail_active = True
                        peak = max(fill_price, avg)
                        trail_sl = max(new_sl, trail_sl)
                        armed_this_bar = True
            else:
                break

        # 3. arm (standalone RIDE threshold) or pre-arm hard-SL
        if not trail_active:
            arm_threshold = avg * (1 + arm_pct / 100.0)
            if h >= arm_threshold:
                trail_active = True
                peak = arm_threshold
                atr = sym.atr_by_bar[j]
                td = trail_distance_pct(atr, trail_atr_mult, trail_min_pct)
                trail_sl = compute_sl(peak=peak, avg=avg, distance_pct=step_pct, trail_dist_pct=td,
                                       prev_sl=0.0, exit_fee_mult=exit_fee_mult, cost_pct=cost_pct,
                                       trail_lock_pct=trail_lock_pct)
                armed_this_bar = True
                if defensive_status == "armed":
                    defensive_status = "cancelled"
                next_add = len(adds)  # cancel remaining armed up-adds
            else:
                floor = avg * (1 - sl_pct / 100.0)
                if low <= floor:
                    exit_reason, exit_price, exit_j = "hard_sl", floor, j
                    break

        # 4. armed channel — carried TP/SL check, then ratchet (no exit on the arm tick)
        if trail_active and not armed_this_bar:
            ff = fee_floor_price(avg, exit_fee_mult, cost_pct)
            carried_tp = compute_tp(sl=trail_sl, avg=avg, tp_gap_pct=tp_gap_pct,
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
            td = trail_distance_pct(atr, trail_atr_mult, trail_min_pct)
            new_peak = max(peak, h)
            new_sl = compute_sl(peak=new_peak, avg=avg, distance_pct=step_pct, trail_dist_pct=td,
                                 prev_sl=trail_sl, exit_fee_mult=exit_fee_mult, cost_pct=cost_pct,
                                 trail_lock_pct=trail_lock_pct)
            peak, trail_sl = new_peak, new_sl

    if exit_reason is None:
        exit_reason, exit_price, exit_j = "deadline", bars[end_j]["close"], end_j

    gross_pct = (exit_price / avg - 1.0) * 100.0
    net_pct = gross_pct - cost_pct
    pnl_dollars = total_cost * (net_pct / 100.0)
    days_held = (bars[exit_j]["ts"] - bars[entry_i]["ts"]) / MS_DAY

    return {
        "symbol": sym.symbol, "entry_i": entry_i, "entry_ts": bars[entry_i]["ts"],
        "exit_j": exit_j, "exit_reason": exit_reason, "avg": avg, "exit_price": exit_price,
        "deployed": total_cost, "adds_filled": adds_filled, "gross_pct": gross_pct,
        "net_pct": net_pct, "pnl_dollars": pnl_dollars, "days_held": days_held,
    }


def simulate_buy_and_hold(sym: SymbolData, entry_i: int, cfg: dict) -> dict:
    bars = sym.bars
    entry = bars[entry_i]["close"]
    L = len(bars)
    end_j = min(entry_i + cfg["deadline_bars"], L - 1)
    exit_price = bars[end_j]["close"]
    gross_pct = (exit_price / entry - 1.0) * 100.0
    net_pct = gross_pct - cfg["cost_pct"]
    deployed = cfg["wave0_usd"]
    pnl_dollars = deployed * (net_pct / 100.0)
    days_held = (bars[end_j]["ts"] - bars[entry_i]["ts"]) / MS_DAY
    return {
        "symbol": sym.symbol, "entry_i": entry_i, "entry_ts": bars[entry_i]["ts"],
        "exit_j": end_j, "exit_reason": "deadline", "avg": entry, "exit_price": exit_price,
        "deployed": deployed, "adds_filled": 0, "gross_pct": gross_pct, "net_pct": net_pct,
        "pnl_dollars": pnl_dollars, "days_held": days_held,
    }


# ======================================================================================
# Aggregation / stats
# ======================================================================================

def dollar_day_stat(trials: list[dict]) -> float:
    dep_days = sum(t["deployed"] * t["days_held"] for t in trials)
    if dep_days <= 0:
        return 0.0
    return sum(t["pnl_dollars"] for t in trials) / dep_days


def summarize(trials: list[dict]) -> dict:
    n = len(trials)
    if n == 0:
        return {"trials": 0}
    total_pnl = sum(t["pnl_dollars"] for t in trials)
    wins = sum(1 for t in trials if t["net_pct"] > 0)
    reasons = defaultdict(int)
    for t in trials:
        reasons[t["exit_reason"]] += 1
    stop_n = reasons.get("hard_sl", 0) + reasons.get("trail_sl", 0)
    flip_n = reasons.get("flip", 0)
    avg_adds = sum(t.get("adds_filled", 0) for t in trials) / n
    return {
        "trials": n,
        "total_pnl_usd": round(total_pnl, 2),
        "dollar_day_pct": round(dollar_day_stat(trials) * 100, 6),
        "win_rate_pct": round(100 * wins / n, 2),
        "stop_rate_pct": round(100 * stop_n / n, 2),
        "flip_rate_pct": round(100 * flip_n / n, 2),
        "avg_adds_filled": round(avg_adds, 3),
        "exit_reasons": dict(reasons),
    }


def half_year_label(ts_ms: float) -> str:
    dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    return f"{dt.year}H{1 if dt.month <= 6 else 2}"


def date_label(ts_ms: float) -> str:
    dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    return dt.strftime("%Y-%m-%d")


def cluster_bootstrap_diff(trials_a: list[dict], trials_b: list[dict], n_boot: int, seed: int) -> dict:
    """Date-clustered bootstrap 95%% CI on ``dollar_day(trials_a) - dollar_day(trials_b)``.
    Clusters = calendar UTC date of ``entry_ts``; each resample draws whole dates with
    replacement so trials sharing an entry date (across all 83/12 symbols) move together."""
    def cluster(trials: list[dict]) -> dict[str, list[dict]]:
        m: dict[str, list[dict]] = defaultdict(list)
        for t in trials:
            m[date_label(t["entry_ts"])].append(t)
        return m

    ma, mb = cluster(trials_a), cluster(trials_b)
    dates = sorted(set(ma) | set(mb))
    rng = random.Random(seed)
    n_dates = len(dates)
    diffs = []
    for _ in range(n_boot):
        sample = [dates[rng.randrange(n_dates)] for _ in range(n_dates)]
        pa = da = pb = db = 0.0
        for d in sample:
            for t in ma.get(d, ()):
                pa += t["pnl_dollars"]; da += t["deployed"] * t["days_held"]
            for t in mb.get(d, ()):
                pb += t["pnl_dollars"]; db += t["deployed"] * t["days_held"]
        stat_a = pa / da if da > 0 else 0.0
        stat_b = pb / db if db > 0 else 0.0
        diffs.append(stat_a - stat_b)
    diffs.sort()
    lo = diffs[max(0, int(0.025 * n_boot))]
    hi = diffs[min(n_boot - 1, int(0.975 * n_boot) - 1)]
    point = dollar_day_stat(trials_a) - dollar_day_stat(trials_b)
    return {
        "point_diff_dollar_day_pct": round(point * 100, 6),
        "ci_lo_pct": round(lo * 100, 6), "ci_hi_pct": round(hi * 100, 6),
        "excludes_zero": (lo > 0 or hi < 0), "n_boot": n_boot, "n_dates": n_dates,
    }


# ======================================================================================
# Causality proof
# ======================================================================================

def _random_future_bars(last_bar: dict, n: int, rng: random.Random) -> list[dict]:
    out = []
    price = last_bar["close"]
    ts = last_bar["ts"]
    for _ in range(n):
        ts += MS_4H
        o = price
        change = rng.uniform(-0.08, 0.08)
        price = max(o * (1 + change), 1e-6)
        h = max(o, price) * rng.uniform(1.0, 1.05)
        low = min(o, price) * rng.uniform(0.95, 1.0)
        vol = rng.uniform(100, 10_000)
        out.append({"ts": ts, "open": o, "high": h, "low": low, "close": price, "volume": vol})
    return out


def causality_check(coin_bars: list[dict], btc_bars: list[dict], symbol: str,
                     cutoffs: list[int], seed: int) -> list[dict]:
    """For each cutoff bar index, recompute every router-gate signal AND the ongoing ATR using
    (a) the real data, and (b) the real data truncated at the cutoff with random synthetic
    future appended to BOTH the coin and BTC series — then assert byte-identical results. This
    is the causality proof the study brief requires."""
    rng = random.Random(seed)
    results = []
    for cutoff in cutoffs:
        entry_ts = coin_bars[cutoff]["ts"]

        sym_real = SymbolData(symbol, coin_bars)
        btc_real = SymbolData("BTC", btc_bars)
        before = entry_context(sym_real, btc_real, cutoff)
        atr_before = sym_real.atr_by_bar[cutoff]

        coin_trunc = coin_bars[:cutoff + 1] + _random_future_bars(coin_bars[cutoff], 400, rng)
        btc_cut_idx = bisect.bisect_right([b["ts"] for b in btc_bars], entry_ts)
        btc_trunc = btc_bars[:btc_cut_idx] + _random_future_bars(
            btc_bars[btc_cut_idx - 1], 400, random.Random(seed + 1))

        sym_mod = SymbolData(symbol, coin_trunc)
        btc_mod = SymbolData("BTC", btc_trunc)
        after = entry_context(sym_mod, btc_mod, cutoff)
        atr_after = sym_mod.atr_by_bar[cutoff]

        match = (
            before["adx"] == after["adx"] and before["rel_strength"] == after["rel_strength"]
            and before["htf"] == after["htf"] and before["st"] == after["st"]
            and before["macdh"] == after["macdh"] and atr_before == atr_after
        )
        results.append({
            "symbol": symbol, "cutoff_bar": cutoff, "entry_ts": entry_ts, "byte_identical": match,
            "before": {**before, "atr": atr_before}, "after": {**after, "atr": atr_after},
        })
    return results


# ======================================================================================
# Battery runners
# ======================================================================================

def collect_trials(universe: dict[str, SymbolData], btc: SymbolData, cfg: dict, ordering: str,
                    contexts: dict[str, dict[int, dict]] | None = None) -> list[dict]:
    trials = []
    for sym_name, sym in universe.items():
        if sym_name == "BTC" and len(universe) > 1:
            pass  # BTC itself is still a valid trial symbol; only excluded from nothing here
        L = len(sym.bars)
        for i in range(ENTRY_START_BAR, L - ENTRY_SPACING_BARS, ENTRY_SPACING_BARS):
            t = simulate_pyramid_up_trial(sym, i, cfg, ordering)
            if contexts is not None:
                t["gate_pass"] = contexts[sym_name][i]["gate_pass"]
                t["adx"] = contexts[sym_name][i]["adx"]
                t["rel_strength"] = contexts[sym_name][i]["rel_strength"]
            trials.append(t)
    return trials


def collect_bh_trials(universe: dict[str, SymbolData], cfg: dict) -> list[dict]:
    trials = []
    for sym in universe.values():
        L = len(sym.bars)
        for i in range(ENTRY_START_BAR, L - ENTRY_SPACING_BARS, ENTRY_SPACING_BARS):
            trials.append(simulate_buy_and_hold(sym, i, cfg))
    return trials


def build_contexts(universe: dict[str, SymbolData], btc: SymbolData) -> dict[str, dict[int, dict]]:
    contexts: dict[str, dict[int, dict]] = {}
    for sym_name, sym in universe.items():
        L = len(sym.bars)
        contexts[sym_name] = {}
        for i in range(ENTRY_START_BAR, L - ENTRY_SPACING_BARS, ENTRY_SPACING_BARS):
            contexts[sym_name][i] = entry_context(sym, btc, i)
    return contexts


def half_year_split(trials: list[dict]) -> dict[str, float]:
    buckets: dict[str, list[dict]] = defaultdict(list)
    for t in trials:
        buckets[half_year_label(t["entry_ts"])].append(t)
    return {k: round(dollar_day_stat(v) * 100, 6) for k, v in sorted(buckets.items())}


def run_battery(data_path: Path, out_dir: Path, label: str, n_boot: int, seed: int,
                 symbols_filter: list[str] | None, skip_sweep: bool, quiet: bool) -> dict:
    def log(msg: str) -> None:
        if not quiet:
            print(f"[{label}] {msg}", flush=True)

    t0 = time.time()
    raw = load_universe(data_path)
    if symbols_filter:
        raw = {s: b for s, b in raw.items() if s in symbols_filter}
    if "BTC" not in raw:
        raise SystemExit(f"{data_path}: universe has no BTC series (needed for relative strength)")
    log(f"loaded {len(raw)} symbols from {data_path.name}")

    universe = {s: SymbolData(s, b) for s, b in raw.items()}
    btc = universe["BTC"]
    log(f"precomputed daily/ATR for {len(universe)} symbols in {time.time()-t0:.1f}s")

    t1 = time.time()
    contexts = build_contexts(universe, btc)
    n_entries = sum(len(v) for v in contexts.values())
    log(f"router-gate context for {n_entries} entries in {time.time()-t1:.1f}s")

    result: dict = {"label": label, "data_file": str(data_path), "n_symbols": len(universe),
                     "n_entries": n_entries}

    # --- Item 1: baseline, both orderings ---
    t1 = time.time()
    trials_tp = collect_trials(universe, btc, BASELINE_CFG, "tp_first", contexts)
    trials_sl = collect_trials(universe, btc, BASELINE_CFG, "sl_first", contexts)
    result["item1_baseline"] = {
        "tp_first": summarize(trials_tp),
        "sl_first": summarize(trials_sl),
    }
    log(f"item1 baseline: {len(trials_tp)} trials both orderings in {time.time()-t1:.1f}s")

    # --- Item 3: buy & hold control ---
    bh_trials = collect_bh_trials(universe, BASELINE_CFG)
    bh_summary = summarize(bh_trials)
    result["item3_buy_and_hold"] = bh_summary
    bh_dd = dollar_day_stat(bh_trials)
    tp_dd = dollar_day_stat(trials_tp)
    result["item3_multiple_of_bh"] = {
        "tp_first": (tp_dd / bh_dd) if bh_dd != 0 else None,
        "sl_first": (dollar_day_stat(trials_sl) / bh_dd) if bh_dd != 0 else None,
    }
    # Verdict-critical: date-clustered bootstrap CI on (pyramid_up - buy_and_hold) $/dollar-day,
    # both orderings — this is the comparison the study's final YES/NO/UNDERPOWERED verdict rests
    # on (item2's CI is about the router gate, a different question).
    result["item3_bootstrap_vs_bh"] = {
        "tp_first": cluster_bootstrap_diff(trials_tp, bh_trials, n_boot=n_boot, seed=seed + 2),
        "sl_first": cluster_bootstrap_diff(trials_sl, bh_trials, n_boot=n_boot, seed=seed + 3),
    }

    # --- Item 2: WITH vs WITHOUT router gate (use tp_first ordering as the representative series) ---
    kept = [t for t in trials_tp if t["gate_pass"]]
    rejected = [t for t in trials_tp if not t["gate_pass"]]
    boot = cluster_bootstrap_diff(kept, rejected, n_boot=n_boot, seed=seed)
    result["item2_gate"] = {
        "kept": summarize(kept), "rejected": summarize(rejected),
        "all_ungated": summarize(trials_tp),
        "bootstrap_kept_minus_rejected": boot,
        "bootstrap_kept_minus_all": cluster_bootstrap_diff(kept, trials_tp, n_boot=n_boot, seed=seed + 1),
    }
    log(f"item2 gate: kept={len(kept)} rejected={len(rejected)} "
        f"CI=[{boot['ci_lo_pct']:.4f},{boot['ci_hi_pct']:.4f}] excl0={boot['excludes_zero']}")

    # --- Item 4: half-year split ---
    hy_baseline = half_year_split(trials_tp)
    hy_gated = half_year_split(kept)
    hy_bh = half_year_split(bh_trials)
    result["item4_half_year"] = {
        "baseline": hy_baseline, "gated": hy_gated, "buy_and_hold": hy_bh,
        "n_positive": {
            "baseline": sum(1 for v in hy_baseline.values() if v > 0),
            "baseline_total": len(hy_baseline),
            "gated": sum(1 for v in hy_gated.values() if v > 0),
            "gated_total": len(hy_gated),
            "buy_and_hold": sum(1 for v in hy_bh.values() if v > 0),
            "buy_and_hold_total": len(hy_bh),
        },
    }

    # --- Item 5: knob sweep ---
    if not skip_sweep:
        t5 = time.time()
        step_vals = [1.0, 2.0, 3.0, 5.0]
        ratio_vals = [0.5, 0.7]
        adds_vals = [1, 2, 3]
        sl_vals = [5.0, 8.0, 12.0]
        combos = []
        for sp in step_vals:
            for sr in ratio_vals:
                for ma in adds_vals:
                    for sl in sl_vals:
                        combos.append({**BASELINE_CFG, "step_pct": sp, "size_ratio": sr,
                                       "max_adds": ma, "sl_pct": sl})
        sweep_results = []
        for combo in combos:
            trials = collect_trials(universe, btc, combo, "tp_first", None)
            s = summarize(trials)
            sweep_results.append({
                "step_pct": combo["step_pct"], "size_ratio": combo["size_ratio"],
                "max_adds": combo["max_adds"], "sl_pct": combo["sl_pct"],
                "dollar_day_pct": s.get("dollar_day_pct", 0.0), "trials": s.get("trials", 0),
                "win_rate_pct": s.get("win_rate_pct", 0.0),
            })
        sweep_results.sort(key=lambda r: r["dollar_day_pct"], reverse=True)
        result["item5_sweep"] = {
            "n_combinations": len(combos),
            "top5": sweep_results[:5],
            "bottom3": sweep_results[-3:],
            "all": sweep_results,
        }
        log(f"item5 sweep: {len(combos)} combos in {time.time()-t5:.1f}s")
    else:
        result["item5_sweep"] = {"skipped": True}

    result["wall_time_s"] = round(time.time() - t0, 1)
    return result


# ======================================================================================
# CLI
# ======================================================================================

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", required=True, type=Path, help="Primary pickle {symbol: [Candle]}")
    ap.add_argument("--extended", type=Path, default=None, help="Optional secondary (longer) pickle")
    ap.add_argument("--out", required=True, type=Path, help="Output directory")
    ap.add_argument("--n-boot", type=int, default=2000)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--symbols", type=str, default=None, help="Comma list to restrict universe (debug)")
    ap.add_argument("--skip-sweep", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--causality-only", action="store_true", help="Only run the causality proof and exit")
    args = ap.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    symbols_filter = args.symbols.split(",") if args.symbols else None

    # --- Causality proof (cheap, always run first) ---
    raw = load_universe(args.data)
    if symbols_filter:
        raw = {s: b for s, b in raw.items() if s in symbols_filter}
    btc_bars = raw["BTC"]
    check_symbols = [s for s in ("ETH", "SOL", "DOGE") if s in raw] or [s for s in raw if s != "BTC"][:1]
    causality_results = []
    for sym in check_symbols:
        bars = raw[sym]
        L = len(bars)
        cutoffs = [c for c in (250, L // 2, L - 100) if 30 <= c < L]
        causality_results.extend(causality_check(bars, btc_bars, sym, cutoffs, seed=args.seed))
    all_match = all(r["byte_identical"] for r in causality_results)
    print(f"[causality] {len(causality_results)} checks across {check_symbols}: "
          f"{'ALL BYTE-IDENTICAL (PASS)' if all_match else 'MISMATCH DETECTED (FAIL)'}")
    for r in causality_results:
        if not r["byte_identical"]:
            print(f"  MISMATCH symbol={r['symbol']} cutoff={r['cutoff_bar']}: "
                  f"before={r['before']} after={r['after']}")
    with open(args.out / "causality_check.json", "w") as f:
        json.dump({"all_byte_identical": all_match, "checks": causality_results}, f, indent=2, default=str)

    if args.causality_only:
        return

    # --- Primary battery ---
    primary = run_battery(args.data, args.out, "primary", args.n_boot, args.seed,
                           symbols_filter, args.skip_sweep, args.quiet)
    primary["causality_all_byte_identical"] = all_match

    out = {"primary": primary}

    # --- Extended (secondary) cache, if present ---
    ext_path = args.extended
    if ext_path is None:
        candidate = args.data.parent / "u30_4h_long.pkl"
        if candidate.exists():
            ext_path = candidate
    if ext_path and ext_path.exists():
        print(f"[extended] running battery on {ext_path}")
        extended = run_battery(ext_path, args.out, "extended", args.n_boot, args.seed,
                                symbols_filter, skip_sweep=True, quiet=args.quiet)
        out["extended"] = extended
    else:
        out["extended"] = None
        print("[extended] no extended cache found — skipped (per instructions)")

    with open(args.out / "battery_results.json", "w") as f:
        json.dump(out, f, indent=2, default=str)
    print(f"\nWrote {args.out / 'battery_results.json'}")


if __name__ == "__main__":
    main()
