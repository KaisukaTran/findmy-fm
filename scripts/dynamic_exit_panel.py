"""The live strategy, whole, for the first time: ladder + Ride&Trail.

WHY THIS EXISTS
    Every measurement made on 2026-09-06 exited at a FIXED take-profit. The live instance does
    not: `kss_dynamic_tp_enabled` is on, so a session rides with NO ceiling until it clears
    `avg x (1 + trail_arm_pct)`, then trails a volatility-sized stop under the high-water mark
    with a spike-grab ceiling above it. Capping every winner at +5.5% removed the component the
    owner says produces the profit, and then the studies concluded the strategy did not produce
    profit. That is a circular result, and this script exists to break the circle.

    The owner's design, in his words: momentum picks a good coin; Ride&Trail maximises it when
    the coin runs; the DCA ladder is the cover for when a good coin turns down. The ladder is
    insurance, not the engine — so it must be judged as insurance, on the whole system, not on
    whether averaging down is profitable by itself.

THE EXIT MATH IS NOT REIMPLEMENTED
    `app/kss/dynamic_exit.py` is pure (no DB, no network, no PyramidSession), so this imports it
    and calls the same functions the live guard calls. A reimplementation would measure my
    reading of the strategy; this measures the strategy.

WHAT IS STILL APPROXIMATE, stated up front
    * Bars, not ticks: the live guard re-evaluates every 90 seconds against a live ticker. Here
      the channel is re-evaluated once per bar, so an intra-bar spike that would have hit the
      trailing stop and then recovered is missed. Hourly bars keep that error small; daily bars
      do not, which is why this defaults to 1h.
    * The rung fill model is `simulate_kss`'s: a rung fills when a later bar's low reaches its
      target, at that price.
    * Fees are a flat round trip; real slippage rises as liquidity falls.

    python scripts/dynamic_exit_panel.py --interval 1h --bars-per-day 24 [--selected]
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

from app.config import settings  # noqa: E402
from app.kss import dynamic_exit  # noqa: E402
from scripts.ladder_panel_study import reserved_capital  # noqa: E402
from scripts.liquidity_tier_study import load, trailing_median_volume  # noqa: E402

WAVE0_USD = 40.0


def atr_pct(bars: list[tuple], i: int, n: int = 14) -> float:
    """True-range average as a percent of price, causal (bars up to and including i)."""
    if i - n < 1:
        return 0.0
    trs = []
    for j in range(i - n + 1, i + 1):
        prev_close = bars[j - 1][4]
        hi, lo = bars[j][2], bars[j][3]
        trs.append(max(hi - lo, abs(hi - prev_close), abs(lo - prev_close)))
    px = bars[i][4]
    return (st.mean(trs) / px * 100.0) if px else 0.0


def simulate_ride_and_trail(
    bars: list[tuple], i: int, *, distance_pct: float, max_waves: int, sl_pct: float,
    horizon_bars: int, cost_pct: float, atr: float, pessimistic: bool = False,
) -> dict | None:
    """One session: ladder below, Ride&Trail above, hard stop and deadline behind.

    Returns realised percent on the average price, the dollars that implies, the capital
    actually deployed and the capital-days it consumed.
    """
    if i + 1 >= len(bars):
        return None
    entry = bars[i][4]
    if entry <= 0:
        return None

    f = 1 - distance_pct / 100.0
    targets = [entry * (f ** n) for n in range(max_waves)]
    weights = [n + 1 for n in range(max_waves)]
    unit_qty = WAVE0_USD / entry
    fills = [entry]

    def avg_of(k: int) -> float:
        num = sum(targets[n] * weights[n] if n else fills[0] * weights[0] for n in range(k))
        den = sum(weights[:k])
        return num / den if den else entry

    def deployed(k: int) -> float:
        return sum(weights[n] * unit_qty * (fills[0] if n == 0 else targets[n]) for n in range(k))

    filled = 1
    avg = entry
    peak = entry
    trail_active = False
    prev_sl = 0.0
    capital_days = 0.0
    bar_days = (bars[i + 1][1] - bars[i][1]) / 86_400_000 if i + 1 < len(bars) else 0.0

    def close(j: int, price: float, kind: str) -> dict:
        pnl = (price / avg - 1) * 100.0 - cost_pct
        cap = deployed(filled)
        return {"pnl": pnl, "exit": kind, "bars": j - i, "days": (j - i) * bar_days,
                "capital": cap, "capital_days": capital_days, "waves": filled,
                "dollars": pnl / 100.0 * cap}

    for j in range(i + 1, min(i + 1 + horizon_bars, len(bars))):
        hi, lo = bars[j][2], bars[j][3]
        capital_days += deployed(filled) * bar_days

        # PESSIMISTIC ordering: the adverse edge is tested against the average as it stood
        # BEFORE this bar's fills, so a rung cannot rescue the same candle that broke the stop.
        if pessimistic:
            if trail_active and lo <= prev_sl:
                return close(j, prev_sl, "trail_sl")
            if not trail_active and sl_pct > 0 and lo <= avg * (1 - sl_pct / 100.0):
                return close(j, avg * (1 - sl_pct / 100.0), "hard_sl")

        while filled < max_waves and lo <= targets[filled]:
            fills.append(targets[filled])
            filled += 1
            avg = avg_of(filled)

        if not pessimistic:
            if trail_active and lo <= prev_sl:
                return close(j, prev_sl, "trail_sl")
            if not trail_active and sl_pct > 0 and lo <= avg * (1 - sl_pct / 100.0):
                return close(j, avg * (1 - sl_pct / 100.0), "hard_sl")

        peak = max(peak, hi)
        # Arming and the channel use the PRODUCTION functions, not a local copy.
        if not trail_active and dynamic_exit.should_arm(
                market=hi, avg=avg, filled_qty=1.0, trail_active=trail_active):
            trail_active = True
        if trail_active:
            _td, sl, tp = dynamic_exit.dynamic_sl_tp(
                peak=peak, avg=avg, distance_pct=distance_pct, atr_pct=atr, prev_sl=prev_sl)
            prev_sl = sl
            if hi >= tp:
                return close(j, tp, "trail_tp")
            if lo <= sl:
                return close(j, sl, "trail_sl")

    j = min(i + horizon_bars, len(bars) - 1)
    return close(j, bars[j][4], "deadline")


def run(series: dict, *, spacing: int, distance: float, waves: int, sl: float,
        horizon_bars: int, cost: float, warmup: int, vol_window: int, min_bars: int,
        pessimistic: bool, since: str | None, until: str | None,
        selected_only: bool) -> list[dict]:
    from app.agents import SIGNAL_AGENTS, aggregate
    from app.agents.aggregator import DEFAULT_WEIGHTS

    out: list[dict] = []
    reserve = reserved_capital(distance, waves)
    for sym, bars in series.items():
        if len(bars) < min_bars:
            continue
        app_candles = [{"ts": b[1], "open": b[6], "high": b[2], "low": b[3],
                        "close": b[4], "volume": b[7]} for b in bars]
        for i in range(warmup, len(bars), spacing):
            qv = trailing_median_volume([b[5] for b in bars[i - vol_window + 1:i + 1]])
            if qv <= 0:
                continue
            day = datetime.fromtimestamp(bars[i][1] / 1000, timezone.utc).strftime("%Y-%m-%d")
            if (since and day < since) or (until and day > until):
                continue
            res = simulate_ride_and_trail(
                bars, i, distance_pct=distance, max_waves=waves, sl_pct=sl,
                horizon_bars=horizon_bars, cost_pct=cost, atr=atr_pct(bars, i),
                pessimistic=pessimistic)
            if not res:
                continue
            row = {"symbol": sym, "day": day, "qv": qv, "reserved": reserve, **res}
            if selected_only:
                window = app_candles[max(0, i - 364):i + 1]
                votes = [a.evaluate(sym, window, {}) for a in SIGNAL_AGENTS]
                row["consensus"] = aggregate(votes, DEFAULT_WEIGHTS)
            out.append(row)
    return out


def boot(rows: list[dict], stat, n: int = 1200, seed: int = 31) -> tuple[float, float]:
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


def report(rows: list[dict], label: str) -> None:
    if not rows:
        print(f"  {label:22} no rows")
        return
    n = len(rows)
    dollars = st.mean([r["dollars"] for r in rows])
    lo, hi = boot(rows, lambda s: st.mean([r["dollars"] for r in s]))
    reserved_days = sum(r["reserved"] * r["days"] for r in rows)
    profit = sum(r["dollars"] for r in rows)
    per_day = (profit / reserved_days * 100) if reserved_days else float("nan")
    kinds = defaultdict(int)
    for r in rows:
        kinds[r["exit"]] += 1
    worst = sorted(r["dollars"] for r in rows)[: max(1, n // 20)]
    print(f"  {label:22} n={n:>7,}  ${dollars:>+7.2f}/trial [{lo:>+6.2f},{hi:>+6.2f}]  "
          f"%/res-$-day {per_day:>+7.4f}  hold {st.mean([r['days'] for r in rows]):>5.1f}d  "
          f"waves {st.mean([r['waves'] for r in rows]):>4.2f}  worst5% ${st.mean(worst):>+8.2f}  "
          + " ".join(f"{k}={v / n:.0%}" for k, v in sorted(kinds.items())))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--db", default="data/research/market.db")
    p.add_argument("--interval", default="1h")
    p.add_argument("--bars-per-day", type=float, default=24.0)
    p.add_argument("--distance", type=float, default=3.2)
    p.add_argument("--waves", type=int, default=3)
    p.add_argument("--sl", type=float, default=8.0)
    p.add_argument("--deadline", type=float, default=7.0)
    p.add_argument("--cost", type=float, default=0.30)
    p.add_argument("--spacing", type=int, default=168)
    p.add_argument("--vol-window", type=int, default=720)
    p.add_argument("--warmup", type=int, default=720)
    p.add_argument("--min-bars", type=int, default=2160)
    p.add_argument("--since", default=None)
    p.add_argument("--until", default=None)
    p.add_argument("--limit-coins", type=int, default=0)
    args = p.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    # Mirror what LIVE actually runs. `kss_dynamic_tp_enabled` is False in `config.py` and True
    # in the live instance's `runtime_config` — the flag lives in the DB, not in the defaults.
    # Without this the harness silently measures the strategy with its exit switched OFF: every
    # session rides to the deadline or the hard stop and not one trailing exit ever fires. The
    # first run of this script did exactly that, and the table looked plausible.
    settings.kss_dynamic_tp_enabled = True

    series = load(Path(args.db), args.interval)
    if args.limit_coins:
        series = dict(list(series.items())[: args.limit_coins])
    horizon = max(1, round(args.deadline * args.bars_per_day))
    reserve = reserved_capital(args.distance, args.waves)

    print(f"Ride&Trail (production `app/kss/dynamic_exit.py`): arm at +{settings.kss_trail_arm_pct}%, "
          f"trail max({settings.kss_trail_atr_mult}xATR, {settings.kss_trail_min_pct}%), "
          f"lock {settings.kss_trail_lock_pct}%, spike-grab gap {settings.kss_tp_gap_pct}%")
    print(f"ladder {args.waves}x{args.distance}% (reserve ${reserve:.2f}), hard SL {args.sl}%, "
          f"deadline {args.deadline}d, cost {args.cost}%, {args.interval} bars\n")

    for pess in (False, True):
        rows = run(series, spacing=args.spacing, distance=args.distance, waves=args.waves,
                   sl=args.sl, horizon_bars=horizon, cost=args.cost, warmup=args.warmup,
                   vol_window=args.vol_window, min_bars=args.min_bars, pessimistic=pess,
                   since=args.since, until=args.until, selected_only=True)
        print("PESSIMISTIC ordering" if pess else "OPTIMISTIC ordering")
        report(rows, "all entries")
        if rows and "consensus" in rows[0]:
            by_day: dict[str, list[dict]] = defaultdict(list)
            for r in rows:
                by_day[r["day"]].append(r)
            top, bottom = [], []
            for _d, rs in by_day.items():
                if len(rs) < 6:
                    continue
                rs.sort(key=lambda r: r["consensus"], reverse=True)
                k = max(1, len(rs) // 3)
                top += rs[:k]
                bottom += rs[-k:]
            report(top, "  top-third consensus")
            report(bottom, "  bottom-third consensus")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
