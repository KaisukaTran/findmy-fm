"""Where does buying the dip actually pay? A liquidity-tier study on five years.

THE QUESTION
    Four independent studies agree on one boundary: illiquid micro-caps mean-revert, liquid
    large-caps continue. This system filters to `min_quote_volume >= $1M`, which — if that
    boundary is real — keeps exactly the coins where a dip tends to keep falling and excludes
    the ones where the strategy's core assumption holds. Nobody has checked it on our own data.

    So: cut the whole survivorship-free universe into liquidity tiers, run the same KSS-shaped
    entry in each, and report what each tier paid.

WHAT IT DOES NOT DO
    It does not model the ladder. A single entry at the signal bar's close is the conservative
    shape: deeper rungs only improve the average price, so a tier that loses money on a single
    entry is not rescued by adding more of it. The point is the RANKING across tiers, measured
    identically, not an absolute P&L forecast.

DISCIPLINE (each of these has burned this project before)
    * No look-ahead: entry at the signal bar's CLOSE, outcome loop starts at the NEXT bar.
      Counting the entry bar's own high/low is what produced a 99.5% win rate in a bear market.
    * The liquidity tier is computed from a TRAILING window ending on the signal day — using
      a coin's average volume over the whole sample would rank it by information from its
      future.
    * Entries are spaced (default 7 days per coin) so one trend is not counted as many
      independent trials, and errors are bootstrapped over DAYS, because coins share a market.
    * Stop-first when a bar touches both stop and target: pessimistic on purpose.
    * Survivorship: the dataset keeps delisted pairs. A coin that dies mid-window is a real
      outcome, not a row to drop.

    python scripts/liquidity_tier_study.py [--tp 3.0] [--sl 8.0] [--horizon 7] [--spacing 7]
"""

from __future__ import annotations

import argparse
import random
import sqlite3
import statistics as st
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DB = Path("data/research/market.db")
DAY_MS = 86_400_000

# Tier edges in USD of daily quote volume. The live universe floor is $1M, so the tiers are
# chosen to straddle it: two below, two above.
TIER_EDGES = [0, 100_000, 1_000_000, 10_000_000, float("inf")]
TIER_NAMES = ["<$100k", "$100k-1M", "$1M-10M", ">$10M"]


def tier_of(quote_volume: float) -> str:
    """Which liquidity tier a coin was in ON the signal day."""
    for i in range(len(TIER_EDGES) - 1):
        if TIER_EDGES[i] <= quote_volume < TIER_EDGES[i + 1]:
            return TIER_NAMES[i]
    return TIER_NAMES[-1]


def trailing_median_volume(vols: list[float]) -> float:
    """Median of the trailing window. Median, not mean: one listing-day volume spike would
    otherwise promote a dead coin into the top tier for a month."""
    return st.median(vols) if vols else 0.0


def simulate(bars: list[tuple], i: int, tp_pct: float, sl_pct: float, horizon: int,
             cost_pct: float = 0.0) -> dict | None:
    """One entry at bars[i]'s close; exits scanned from bars[i+1] onward.

    Returns pnl_pct, the exit kind, and the days of capital consumed — the last one is what
    makes tiers comparable: a 3% win in 2 days is not the same trade as a 3% win in 7.
    """
    if i + 1 >= len(bars):
        return None
    entry = bars[i][4]
    if entry <= 0:
        return None
    tp, sl = entry * (1 + tp_pct / 100), entry * (1 - sl_pct / 100)
    for j in range(i + 1, min(i + 1 + horizon, len(bars))):
        # Index, never unpack: `load` carries more columns than this function needs, and an
        # unpack silently couples the two every time a column is added.
        high, low = bars[j][2], bars[j][3]
        if low <= sl:                       # stop first when both are touched: pessimistic
            return {"pnl": -sl_pct - cost_pct, "exit": "sl", "days": j - i}
        if high >= tp:
            return {"pnl": tp_pct - cost_pct, "exit": "tp", "days": j - i}
    last = bars[min(i + horizon, len(bars) - 1)][4]
    return {"pnl": (last / entry - 1) * 100 - cost_pct, "exit": "deadline",
            "days": min(horizon, len(bars) - 1 - i)}


def load(db_path: Path) -> dict[str, list[tuple]]:
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    rows = db.execute(
        "SELECT symbol, ts, high, low, close, quote_volume, open, volume FROM candles "
        "WHERE interval='1d' ORDER BY symbol, ts").fetchall()
    db.close()
    out: dict[str, list[tuple]] = defaultdict(list)
    for sym, ts, high, low, close, qv, op, vol in rows:
        # Index 5 is QUOTE volume (dollars) — that is what the tiers are cut on. Base volume
        # and the real open live at 6 and 7: anything handing bars to app/agents/* needs them,
        # and passing quote volume as `volume` made LiquidityAgent compute price x dollars.
        out[sym].append((sym, ts, high, low, close, qv or 0.0, op or close, vol or 0.0))
    return out


def build_entries(series: dict[str, list[tuple]], vol_window: int, spacing: int,
                  tp: float, sl: float, horizon: int, min_bars: int,
                  cost_pct: float = 0.0, since: str | None = None) -> list[dict]:
    entries = []
    for sym, bars in series.items():
        if len(bars) < min_bars:
            continue
        for i in range(vol_window, len(bars), spacing):
            # Exactly `vol_window` bars, ending on and including the signal bar. The old
            # slice took vol_window+1 — harmless for the result but not the window documented.
            vols = [b[5] for b in bars[i - vol_window + 1:i + 1]]
            qv = trailing_median_volume(vols)
            if qv <= 0:
                continue
            day = datetime.fromtimestamp(bars[i][1] / 1000, timezone.utc).strftime("%Y-%m-%d")
            if since and day < since:
                continue
            res = simulate(bars, i, tp, sl, horizon, cost_pct)
            if not res:
                continue
            entries.append({"symbol": sym, "day": day, "tier": tier_of(qv), "qv": qv, **res})
    return entries


def boot_by_day(entries: list[dict], stat, n: int = 2000) -> tuple[float, float]:
    """Bootstrap over calendar days — the cluster — not over rows."""
    by_day: dict[str, list[dict]] = defaultdict(list)
    for e in entries:
        by_day[e["day"]].append(e)
    days = list(by_day)
    if len(days) < 5:
        return float("nan"), float("nan")
    vals = []
    for _ in range(n):
        sample = [e for _ in days for e in by_day[random.choice(days)]]
        v = stat(sample)
        if v == v:
            vals.append(v)
    vals.sort()
    return vals[int(0.025 * len(vals))], vals[int(0.975 * len(vals))]


def mean_pnl(rows: list[dict]) -> float:
    return st.mean([r["pnl"] for r in rows]) if rows else float("nan")


def per_dollar_day(rows: list[dict]) -> float:
    """Profit per dollar-DAY of capital held: the metric that does not reward slow winners."""
    days = sum(r["days"] for r in rows)
    return (sum(r["pnl"] for r in rows) / days) if days else float("nan")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--db", default=str(DEFAULT_DB))
    p.add_argument("--tp", type=float, default=3.0)
    p.add_argument("--sl", type=float, default=8.0)
    p.add_argument("--horizon", type=int, default=7, help="deadline in days")
    p.add_argument("--spacing", type=int, default=7, help="days between entries per coin")
    p.add_argument("--vol-window", type=int, default=30, help="trailing bars for the tier")
    p.add_argument("--min-bars", type=int, default=90)
    p.add_argument("--cost", type=float, default=0.30,
                   help="round-trip cost %% charged to every entry (production is 0.30). WARNING: "
                        "a single number understates the thin tiers — real slippage rises as "
                        "liquidity falls, so the low tiers flatter themselves here.")
    p.add_argument("--since", default=None, help="only entries on/after this YYYY-MM-DD")
    p.add_argument("--seed", type=int, default=13)
    args = p.parse_args(argv)
    random.seed(args.seed)
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    series = load(Path(args.db))
    entries = build_entries(series, args.vol_window, args.spacing, args.tp, args.sl,
                            args.horizon, args.min_bars, args.cost, args.since)
    if not entries:
        print("no entries — is the dataset built?")
        return 1

    days = {e["day"] for e in entries}
    print(f"TP {args.tp}% / SL {args.sl}% / {args.horizon}d deadline, cost {args.cost}%/round trip, "
          f"entries every {args.spacing}d per coin, tier from the trailing "
          f"{args.vol_window}-day median volume" + (f", from {args.since}" if args.since else ""))
    print(f"{len(entries):,} entries · {len({e['symbol'] for e in entries})} coins · "
          f"{len(days)} distinct days\n")

    hdr = f"{'tier':10} {'n':>7} {'coins':>6} {'mean pnl':>10} {'95% CI':>18} " \
          f"{'%/dollar-day':>13} {'tp':>6} {'sl':>6} {'dl':>6}"
    print(hdr)
    print("-" * len(hdr))
    for tier in TIER_NAMES:
        rows = [e for e in entries if e["tier"] == tier]
        if not rows:
            continue
        lo, hi = boot_by_day(rows, mean_pnl)
        exits = defaultdict(int)
        for r in rows:
            exits[r["exit"]] += 1
        n = len(rows)
        print(f"{tier:10} {n:>7,} {len({r['symbol'] for r in rows}):>6} "
              f"{mean_pnl(rows):>+9.3f}% [{lo:>+7.3f},{hi:>+7.3f}] "
              f"{per_dollar_day(rows):>+12.4f} "
              f"{exits['tp']/n:>5.0%} {exits['sl']/n:>5.0%} {exits['deadline']/n:>5.0%}")

    # The decision the study exists to inform.
    below = [e for e in entries if e["tier"] in TIER_NAMES[:2]]
    above = [e for e in entries if e["tier"] in TIER_NAMES[2:]]
    if below and above:
        gap = mean_pnl(below) - mean_pnl(above)
        lo, hi = boot_by_day(entries, lambda s: (
            mean_pnl([e for e in s if e["tier"] in TIER_NAMES[:2]])
            - mean_pnl([e for e in s if e["tier"] in TIER_NAMES[2:]])))
        print(f"\nbelow the $1M floor minus above it: {gap:+.3f} pct-points  CI [{lo:+.3f}, {hi:+.3f}]")
        # "Less bad" is not "profitable", and conflating the two is how a study talks someone
        # into trading a losing tier. Report the direction and the level separately.
        below_lo, below_hi = boot_by_day(below, mean_pnl)
        if lo > 0:
            direction = "the floor is keeping the WORSE half: the excluded tiers lose less"
        elif hi < 0:
            direction = "the floor is keeping the better half"
        else:
            direction = "no evidence either way - the floor is not what decides this"
        print(f"  -> {direction}")
        level = ("and the excluded tiers are PROFITABLE net of cost" if below_lo > 0 else
                 "but the excluded tiers are still LOSING net of cost" if below_hi < 0 else
                 "and the excluded tiers are indistinguishable from zero net of cost")
        print(f"     {level}: below-floor mean {mean_pnl(below):+.3f}% "
              f"CI [{below_lo:+.3f}, {below_hi:+.3f}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
