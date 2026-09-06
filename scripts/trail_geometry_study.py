"""
Does Ride & Trail earn more than not having it — and which (lock, trail_min) makes it move at all?

Measured on the live book 2026-09-07: the trailing stop has never once trailed. Its first ratchet
step needs the peak to reach `avg(1+d)/(1-trail_dist)` — +6.3% to +13.4% depending on the coin —
while the armed take-profit sits at `avg × (1+lock) × (1+gap)` = +7.1%. For three of six open
sessions that window is 0.01 to 0.77 percentage points wide; for the other three it is negative,
so the take-profit always fills first. The exit mix says the same thing out loud: 25 fixed
take-profits, 4 trail stops (all of them fired at the pinned lock floor, not from trailing), 1
hard stop. `kss_trail_lock_pct`, `kss_trail_min_pct` and `distance_pct` were each chosen sensibly
and never checked against each other.

So before changing any of them, measure. This script asks two questions, in order:

  1. **Does the stop ever move?** Pure arithmetic, no simulation, no data — printed first because
     a configuration that cannot ratchet is decided before any backtest runs.
  2. **Does it earn more?** For each configuration, replay real bars and compare against the same
     entries with the trail switched OFF (fixed take-profit + hard SL + ladder, the pre-Ride&Trail
     strategy). Same entries, paired — the last analysis in this repo compared two date ranges and
     called the difference the feature.

HONESTY RULES, learned the hard way in this codebase:
  * The real `app.kss.dynamic_exit` functions are called — never a reimplementation. A study that
    re-derives the formula measures the study's copy of it.
  * Nothing is decided on the entry bar (that look-ahead once produced a 99.5% win rate in a bear
    market), and the ladder is filled from bars strictly after entry.
  * When a bar touches the stop AND the take-profit, the STOP is taken and the case is counted
    separately, because a daily bar cannot say which came first. That count is printed; if it is
    large, the result is not trustworthy and the script says so.
  * Sample size is printed everywhere, and thin arms refuse to report a mean.

Usage:
    python scripts/trail_geometry_study.py                       # default sweep, 120 symbols
    python scripts/trail_geometry_study.py --distance 3.2 --tp 5.0
    python scripts/trail_geometry_study.py --symbols 300 --from 2024-01-01
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import statistics
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import settings  # noqa: E402
from app.kss import dynamic_exit as dx  # noqa: E402

DB = "data/research/market.db"


# --- part 1: can the stop move at all? (arithmetic, no data) ---------------------------------


def ratchet_threshold_pct(distance_pct: float, trail_dist_pct: float) -> float:
    """Peak, as % above avg, at which the trailing stop takes its FIRST step above the lock floor.

    `compute_sl` snaps the trail DOWN to a wave-grid level `avg(1+d)^k`, so the stop does not
    creep — it jumps. Moving off the floor needs k>=1, i.e. `peak(1-trail_dist) >= avg(1+d)`.
    That grid snap is the part everyone (including me) forgets when eyeballing this.
    """
    return ((1 + distance_pct / 100) / (1 - trail_dist_pct / 100) - 1) * 100


def armed_tp_pct(lock_pct: float, gap_pct: float, tp_pct: float) -> float:
    """Where the armed exit rests: the higher of the fixed take-profit and the trail ceiling."""
    return max(tp_pct, ((1 + lock_pct / 100) * (1 + gap_pct / 100) - 1) * 100)


# --- part 2: replay ---------------------------------------------------------------------------


def _load(con: sqlite3.Connection, symbol: str, start_ms: int, interval: str) -> list[tuple]:
    return con.execute(
        "select ts, open, high, low, close from candles "
        "where symbol=? and interval=? and ts>=? order by ts",
        (symbol, interval, start_ms)).fetchall()


def _simulate(bars: list[tuple], i0: int, *, distance_pct: float, tp_pct: float, sl_pct: float,
              max_waves: int, lock_pct: float, trail_min_pct: float, gap_pct: float,
              use_trail: bool, deadline_bars: int) -> dict | None:
    """One session opened at bars[i0]'s close. Returns its outcome, or None if it never resolves.

    The ladder is `entry*(1-d)^k` with weights 1..max_waves, matching `app/backtest.py:_targets`.
    Everything after entry is evaluated on LATER bars only.
    """
    entry = bars[i0][4]
    if entry <= 0:
        return None
    targets = [entry * ((1 - distance_pct / 100) ** k) for k in range(max_waves)]
    weights = [k + 1 for k in range(max_waves)]
    qty = weights[0]
    cost = weights[0] * entry
    filled = 1
    armed = False
    peak = 0.0
    sl = 0.0
    ratcheted = False
    ambiguous = 0

    settings.kss_trail_lock_pct = lock_pct
    settings.kss_trail_min_pct = trail_min_pct
    settings.kss_tp_gap_pct = gap_pct

    for j in range(i0 + 1, min(i0 + 1 + deadline_bars, len(bars))):
        _ts, _o, high, low, close = bars[j]
        avg = cost / qty

        # rungs fill on the way down (ladder is alive until the trail arms)
        while (not armed) and filled < max_waves and low <= targets[filled]:
            qty += weights[filled]
            cost += weights[filled] * targets[filled]
            filled += 1
            avg = cost / qty

        hard_sl = avg * (1 - sl_pct / 100)
        if not use_trail:
            fixed_tp = avg * (1 + tp_pct / 100)
            if low <= hard_sl and high >= fixed_tp:
                ambiguous += 1
            if low <= hard_sl:
                return {"ret": (hard_sl / avg - 1) * 100, "why": "hard_sl", "bars": j - i0,
                        "ratcheted": False, "ambiguous": ambiguous, "waves": filled}
            if high >= fixed_tp:
                return {"ret": (fixed_tp / avg - 1) * 100, "why": "fixed_tp", "bars": j - i0,
                        "ratcheted": False, "ambiguous": ambiguous, "waves": filled}
            continue

        if not armed:
            arm_px = dx.arm_threshold(avg, tp_pct)
            if low <= hard_sl:
                return {"ret": (hard_sl / avg - 1) * 100, "why": "hard_sl", "bars": j - i0,
                        "ratcheted": False, "ambiguous": ambiguous, "waves": filled}
            if high >= arm_px:
                armed, peak = True, arm_px          # arm at the threshold, not at the bar's high
                sl = dx.compute_sl(peak=peak, avg=avg, distance_pct=distance_pct,
                                   trail_dist_pct=trail_min_pct, prev_sl=0.0)
                if sl >= arm_px:                    # the guard added 2026-09-06: never arm under it
                    armed, peak, sl = False, 0.0, 0.0
                    continue
            else:
                continue                            # riding: no fixed TP, no trail — hard SL only

        peak = max(peak, high)
        new_sl = dx.compute_sl(peak=peak, avg=avg, distance_pct=distance_pct,
                               trail_dist_pct=trail_min_pct, prev_sl=sl)
        if new_sl > sl * 1.0000001:
            ratcheted = True
        sl = new_sl
        tp = max(avg * (1 + tp_pct / 100), dx.compute_tp(sl=sl, avg=avg))
        if low <= sl and high >= tp:
            ambiguous += 1
        if low <= sl:
            return {"ret": (sl / avg - 1) * 100, "why": "trail_sl", "bars": j - i0,
                    "ratcheted": ratcheted, "ambiguous": ambiguous, "waves": filled}
        if high >= tp:
            return {"ret": (tp / avg - 1) * 100, "why": "armed_tp", "bars": j - i0,
                    "ratcheted": ratcheted, "ambiguous": ambiguous, "waves": filled}

    j = min(i0 + deadline_bars, len(bars) - 1)
    avg = cost / qty
    return {"ret": (bars[j][4] / avg - 1) * 100, "why": "deadline", "bars": j - i0,
            "ratcheted": ratcheted, "ambiguous": ambiguous, "waves": filled}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--distance", type=float, default=3.2)
    ap.add_argument("--tp", type=float, default=5.0)
    ap.add_argument("--sl", type=float, default=8.0)
    ap.add_argument("--waves", type=int, default=3)
    ap.add_argument("--gap", type=float, default=5.0)
    ap.add_argument("--deadline", type=int, default=7, help="DAYS before giving up")
    ap.add_argument("--interval", default="1h", choices=("1h", "1d"),
                    help="1h (default) — daily bars leave 7% of cases where the stop and the "
                         "take-profit are both touched inside one bar, which no daily backtest "
                         "can resolve; hourly cuts that to a reportable number")
    ap.add_argument("--arm-frac", type=float, default=0.6,
                    help="kss_trail_arm_tp_frac. The live value is 0.6 and lives in the runtime "
                         "DB, not in config defaults — a study that forgets it arms at the flat "
                         "5%% and measures a rule the app does not use")
    ap.add_argument("--symbols", type=int, default=120)
    ap.add_argument("--spacing", type=int, default=7, help="days between entry events")
    ap.add_argument("--from", dest="start", default="2021-01-01")
    args = ap.parse_args()

    print("\n" + "=" * 78)
    print("PART 1 — can the stop move at all?  (arithmetic; no data can rescue a 'no')")
    print("=" * 78)
    print(f"distance={args.distance}%  tp={args.tp}%  gap={args.gap}%\n")
    print(f"{'lock':>6}{'trail_min':>11}{'stop nhich tu':>16}{'TP nam san':>13}   cua so")
    grid = [(lock, tmin) for lock in (0.5, 1.0, 2.0, 3.0) for tmin in (1.5, 2.0, 3.0, 5.0)]
    workable = []
    for lock, tmin in grid:
        ratchet = ratchet_threshold_pct(args.distance, tmin)
        tp_rest = armed_tp_pct(lock, args.gap, args.tp)
        window = tp_rest - ratchet
        if window > 0:
            workable.append((lock, tmin))
        print(f"{lock:>6.1f}{tmin:>11.1f}{ratchet:>15.2f}%{tp_rest:>12.2f}%   "
              f"{'+' if window > 0 else ''}{window:.2f} diem"
              f"{'' if window > 0 else '   <- KHONG BAO GIO nhich'}")
    print(f"\n{len(workable)}/{len(grid)} cau hinh cho phep trailing stop nhich duoc.")
    live = (settings.kss_trail_lock_pct, settings.kss_trail_min_pct)
    print(f"Cau hinh LIVE hien tai (lock={live[0]}, trail_min={live[1]}): "
          f"{'nhich duoc' if live in workable else 'KHONG nhich duoc'}")

    if not os.path.exists(DB):
        print(f"\n(bo qua PART 2: khong tim thay {DB})")
        return 0

    print("\n" + "=" * 78)
    print("PART 2 — does it earn more than having no trail at all?")
    print("=" * 78)
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    start_ms = int(datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc).timestamp() * 1000)
    settings.kss_trail_arm_tp_frac = args.arm_frac
    settings.kss_trail_arm_pct = 5.0
    bars_per_day = 24 if args.interval == "1h" else 1
    deadline_bars = args.deadline * bars_per_day
    spacing_bars = args.spacing * bars_per_day
    syms = [s for (s,) in con.execute(
        "select symbol from candles where interval=? group by symbol "
        "order by count(*) desc limit ?", (args.interval, args.symbols))]
    print(f"{len(syms)} coin, tu {args.start}, moi {args.spacing} ngay mot lenh vao\n")

    configs = [("KHONG trail (chi TP co dinh)", None)] + [
        (f"trail lock={lock} min={tmin}", (lock, tmin)) for lock, tmin in
        [(2.0, 3.0), (1.0, 1.5), (0.5, 1.5), (2.0, 1.5), (3.0, 5.0)]]
    results: dict[str, list[dict]] = {name: [] for name, _ in configs}

    for sym in syms:
        bars = _load(con, sym, start_ms, args.interval)
        if len(bars) < deadline_bars + 30:
            continue
        for i0 in range(20, len(bars) - deadline_bars - 1, spacing_bars):
            for name, cfg in configs:
                out = _simulate(
                    bars, i0, distance_pct=args.distance, tp_pct=args.tp, sl_pct=args.sl,
                    max_waves=args.waves, lock_pct=(cfg[0] if cfg else 2.0),
                    trail_min_pct=(cfg[1] if cfg else 3.0), gap_pct=args.gap,
                    use_trail=cfg is not None, deadline_bars=deadline_bars)
                if out:
                    results[name].append(out)

    base = results[configs[0][0]]
    n = len(base)
    print(f"{'cau hinh':<30}{'n':>7}{'loi TB':>9}{'thang%':>8}{'stop nhich':>12}{'vs khong trail':>16}")
    for name, _cfg in configs:
        rows = results[name]
        if len(rows) < 200:
            print(f"{name:<30}{len(rows):>7}   qua it de ket luan")
            continue
        mean = statistics.mean(r["ret"] for r in rows)
        win = sum(r["ret"] > 0 for r in rows) / len(rows) * 100
        armed_rows = [r for r in rows if r["why"] in ("trail_sl", "armed_tp")]
        ratch = (sum(r["ratcheted"] for r in armed_rows) / len(armed_rows) * 100) if armed_rows else 0.0
        delta = "" if name == configs[0][0] else f"{mean - statistics.mean(r['ret'] for r in base):+.3f}%"
        print(f"{name:<30}{len(rows):>7}{mean:>8.3f}%{win:>7.1f}%{ratch:>11.1f}%{delta:>16}")

    amb = sum(r["ambiguous"] for rows in results.values() for r in rows)
    tot = sum(len(rows) for rows in results.values())
    print(f"\nnen cham CA stop lan TP trong cung mot ngay: {amb} / {tot} "
          f"({amb/max(tot,1)*100:.1f}%) — da tinh la STOP (bi quan). "
          f"{'CANH BAO: ty le nay qua cao, ket qua khong dang tin.' if amb/max(tot,1) > 0.05 else ''}")
    print(f"\nSo lenh vao doc lap: {n}. Moi cau hinh chay tren CUNG bo lenh vao (ghep cap).\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
