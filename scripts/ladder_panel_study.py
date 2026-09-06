"""Does the LADDER change the verdict? The same five years, but with the pyramid.

WHY THIS EXISTS
    Every selection measurement made on 2026-09-06 used a SINGLE entry with a fixed take-profit
    and stop. The live strategy is neither: it enters, then places laddered buys BELOW the
    entry, so a dip that would have been a loss on one entry can become an average-down and a
    win. Bounding the "entry premise" therefore does not bound KSS, and saying otherwise would
    be over-reading the result.

    This runs the production ladder simulator — `app.backtest.simulate_kss`, the same code the
    live scanner's win-rate gate calls — over the same survivorship-free panel, so the two
    numbers differ ONLY by the ladder.

WHAT IT STILL DOES NOT MODEL, stated up front
    The Ride&Trail dynamic exit. `simulate_kss` exits on take-profit, hard stop or deadline;
    the live instance runs `kss_dynamic_tp_enabled=True`, which lets a winner run and trails a
    stop behind it. Nothing in this codebase simulates that, so the result below is "ladder +
    fixed exits", not the full live strategy. That remains the last open gap.

TWO INTRA-BAR BOUNDS, ALWAYS
    On daily bars, when one bar contains both a rung's fill price and the take-profit, the
    order of events inside the bar is unknowable. The optimistic bound assumes the good order,
    the pessimistic the bad one. A previous study on this project found 3 of 5 configurations
    FLIP SIGN between the two, so a single-bound number is not a result. Both are printed; when
    they disagree in sign, the honest answer is "unknown at this resolution".

    python scripts/ladder_panel_study.py [--waves 3] [--distance 3.2] [--tp 5.5] [--tier-split]
"""

from __future__ import annotations

import argparse
import random
import statistics as st
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.backtest import simulate_kss  # noqa: E402
from scripts.liquidity_tier_study import (  # noqa: E402
    TIER_NAMES,
    load,
    simulate,
    tier_of,
    trailing_median_volume,
)

WAVE0_USD = 40.0        # the live `kss_first_wave_usd`


def reserved_capital(distance: float, waves: int, wave0_usd: float = WAVE0_USD) -> float:
    """What the session TIES UP, which is what a return should be measured against.

    `app/kss/service.py` reserves the whole ladder at session start, so the account cannot use
    that money elsewhere whether or not the rungs ever fill. Wave n carries weight n+1 and buys
    at (1-distance)^n of the entry, so the reserve is far larger than wave 0: at the live
    3.2%/3-wave shape it is $229.88, not $40 and not $120. Dividing profit by DEPLOYED capital
    instead overstates the return on committed money by about 2.6x.
    """
    f = 1 - distance / 100
    return sum((n + 1) * wave0_usd * (f ** n) for n in range(waves))


def to_candles(bars: list[tuple]) -> list[dict]:
    """Dataset rows -> the app's Candle shape (index 6 is the open, 7 the base volume)."""
    return [{"ts": b[1], "open": b[6], "high": b[2], "low": b[3], "close": b[4], "volume": b[7]}
            for b in bars]


def run_panel(series: dict, *, spacing: int, distance: float, tp: float, waves: int,
              sl: float, deadline: int, cost: float, min_bars: int, warmup: int,
              vol_window: int, pessimistic: bool) -> list[dict]:
    """One row per entry: the ladder result and the single-entry result on the SAME bar."""
    out: list[dict] = []
    reserve = reserved_capital(distance, waves)
    for sym, bars in series.items():
        if len(bars) < min_bars:
            continue
        candles = to_candles(bars)
        for i in range(warmup, len(bars), spacing):
            qv = trailing_median_volume([b[5] for b in bars[i - vol_window + 1:i + 1]])
            if qv <= 0:
                continue
            lad = simulate_kss(
                candles, i, distance_pct=distance, max_waves=waves, tp_pct=tp,
                deadline_days=float(deadline), sl_pct=sl, cost_pct=cost,
                pessimistic_intrabar=pessimistic, wave0_notional_usd=WAVE0_USD,
            )
            single = simulate(bars, i, tp, sl, deadline, cost)
            if single is None:
                continue
            out.append({
                "symbol": sym,
                "reserved": reserve,
                "day": datetime.fromtimestamp(bars[i][1] / 1000, timezone.utc).strftime("%Y-%m-%d"),
                "tier": tier_of(qv),
                "ladder_pnl": lad.pnl_pct,
                "ladder_days": lad.capital_days,
                "ladder_capital": lad.exit_capital,
                "waves_filled": lad.waves_filled,
                "stopped": lad.stopped,
                "tp_hit": lad.tp_hit,
                "single_pnl": single["pnl"],
            })
    return out


def boot_days(rows: list[dict], stat, n: int = 1500, seed: int = 23) -> tuple[float, float]:
    rng = random.Random(seed)
    by_day: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_day[r["day"]].append(r)
    days = list(by_day)
    if len(days) < 10:
        return float("nan"), float("nan")
    vals = []
    for _ in range(n):
        sample = [r for _ in days for r in by_day[rng.choice(days)]]
        v = stat(sample)
        if v == v:
            vals.append(v)
    vals.sort()
    return vals[int(0.025 * len(vals))], vals[int(0.975 * len(vals))]


def mean_ladder(rows):
    return st.mean([r["ladder_pnl"] for r in rows]) if rows else float("nan")


def mean_single(rows):
    return st.mean([r["single_pnl"] for r in rows]) if rows else float("nan")


def profit_dollars(rows) -> float:
    """Mean realised DOLLARS per trial. The mean of `pnl_pct` is not this: it weights a $40
    winner exactly like a $230 loser, which is the whole reason the percentage looked good."""
    return st.mean([r["ladder_pnl"] / 100 * r["ladder_capital"] for r in rows]) if rows else float("nan")


def return_on_reserved(rows) -> float:
    """Total profit as a PERCENT of the capital the sessions reserved. Percent, not a fraction:
    the sister script prints %/dollar-day and printing a fraction next to it invited a 100x
    misreading."""
    profit = sum(r["ladder_pnl"] / 100 * r["ladder_capital"] for r in rows)
    reserved = sum(r["reserved"] for r in rows)
    return (profit / reserved * 100) if reserved else float("nan")


def dollar_day(rows):
    """Percent per dollar-DAY, against RESERVED capital-days (see `reserved_capital`)."""
    profit = sum(r["ladder_pnl"] / 100 * r["ladder_capital"] for r in rows)
    # capital_days is measured on deployed capital; scale it to the reserve the session held
    # for the same span, so the denominator is the money the account could not use.
    days = sum(r["ladder_days"] * (r["reserved"] / r["ladder_capital"])
               for r in rows if r["ladder_capital"] > 0)
    return (profit / days * 100) if days else float("nan")


def report(rows: list[dict], label: str) -> None:
    n = len(rows)
    if not n:
        print(f"{label}: no rows")
        return
    lad, sing = mean_ladder(rows), mean_single(rows)
    lo, hi = boot_days(rows, mean_ladder)
    dlo, dhi = boot_days(rows, lambda s: mean_ladder(s) - mean_single(s))
    stops = sum(1 for r in rows if r["stopped"]) / n
    tps = sum(1 for r in rows if r["tp_hit"]) / n
    waves = st.mean([r["waves_filled"] for r in rows])
    print(f"  {label:12} n={n:>7,}  ladder {lad:>+7.3f}% [{lo:>+7.3f},{hi:>+7.3f}]   "
          f"single {sing:>+7.3f}%   $/trial {profit_dollars(rows):>+7.2f}   "
          f"on reserved {return_on_reserved(rows):>+6.2f}%   %/reserved-$-day {dollar_day(rows):>+7.4f}   "
          f"tp {tps:>4.0%} sl {stops:>4.0%}  waves {waves:>4.2f}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--db", default="data/research/market.db")
    p.add_argument("--distance", type=float, default=3.2, help="rung spacing %% (live autotune ~3.2)")
    p.add_argument("--tp", type=float, default=5.5, help="take-profit %% (live autotune ~5.5)")
    p.add_argument("--waves", type=int, default=3)
    p.add_argument("--sl", type=float, default=8.0)
    p.add_argument("--deadline", type=int, default=7)
    p.add_argument("--cost", type=float, default=0.30)
    p.add_argument("--spacing", type=int, default=7)
    p.add_argument("--vol-window", type=int, default=30)
    p.add_argument("--warmup", type=int, default=30)
    p.add_argument("--min-bars", type=int, default=90)
    p.add_argument("--limit-coins", type=int, default=0)
    p.add_argument("--tier-split", action="store_true", help="also break the result down by liquidity tier")
    args = p.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    series = load(Path(args.db))
    if args.limit_coins:
        series = dict(list(series.items())[: args.limit_coins])

    print(f"ladder {args.waves} waves / {args.distance}% apart, tp {args.tp}%, sl {args.sl}%, "
          f"{args.deadline}d deadline, cost {args.cost}%, wave0 ${WAVE0_USD:g}")
    print("'single' is ONE entry on the same bar with the same exits — the difference is the ladder.\n")

    for pessimistic in (False, True):
        rows = run_panel(
            series, spacing=args.spacing, distance=args.distance, tp=args.tp, waves=args.waves,
            sl=args.sl, deadline=args.deadline, cost=args.cost, min_bars=args.min_bars,
            warmup=max(args.warmup, args.vol_window), vol_window=args.vol_window,
            pessimistic=pessimistic,
        )
        bound = "PESSIMISTIC" if pessimistic else "OPTIMISTIC"
        print(f"{bound} intra-bar bound   (reserve ${reserved_capital(args.distance, args.waves):.2f}/session)")
        report(rows, "all coins")
        # The decomposition that explains the headline: profit by how far the ladder got.
        for w in range(1, args.waves + 1):
            report([r for r in rows if r["waves_filled"] == w], f"  {w} wave(s)")
        if args.tier_split:
            for tier in TIER_NAMES:
                report([r for r in rows if r["tier"] == tier], tier)
        print()
    print("If the two bounds disagree in sign, the honest answer is 'unknown at daily resolution'.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
