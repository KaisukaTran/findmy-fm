"""Rank the coins, then check whether the ranking knew anything. Five years, one ruler.

WHAT THIS ANSWERS
    The system rates ~23 coins tradeable a day and can open ~5, so what decides results is the
    RANKING, not the filtering. Measured on the live book the current key does not beat picking
    at random — but that measurement had 11 independent days, and the arithmetic says a
    genuinely good signal (IC 0.05) needs years of trading before its interval clears zero. So
    the ranking question cannot be settled on the live book at all. It can be settled here.

    Every feature below is scored the same way on the same panel: the four agents this system
    actually ranks with, and the candidates the published evidence points to.

THE METRIC
    Daily cross-sectional rank IC: within each day, Spearman-correlate the feature against what
    the trade went on to do; then look at the series of daily ICs. Errors come from the spread
    of that series, because coins on one day share a market and are not independent draws.

    Pre-registered thresholds, written before looking:
        IC < 0.019   cannot pay a 0.30% round trip at this horizon -> discard
        0.019-0.05   may be real, but cannot be proven on our own book in any useful time
        IC >= 0.05   usable

CAUSALITY
    Features see bars up to and including the signal day. The outcome starts at the NEXT bar
    (`liquidity_tier_study.simulate`, reused deliberately — one exit model, one look-ahead
    discipline, one place to get it wrong). The correlation is computed WITHIN each day and
    on RANKS, so a day where everything rose cannot make a feature look clever, and no
    z-scoring or outlier trimming is needed for it to be comparable across days.

    python scripts/cross_section_ic.py [--spacing 7] [--tp 3] [--horizon 7] [--limit-coins N]
"""

from __future__ import annotations

import argparse
import math
import random
import statistics as st
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.agents.dip import DipAgent  # noqa: E402
from app.agents.liquidity import LiquidityAgent  # noqa: E402
from app.agents.trend import TrendAgent  # noqa: E402
from app.agents.volatility import VolatilityAgent  # noqa: E402
from scripts.liquidity_tier_study import load, simulate  # noqa: E402

COST_HURDLE = 0.019     # break-even IC against a 0.30% round trip at a ~5-day hold
USABLE = 0.05           # "good" by the Grinold-Kahn convention

LIVE_AGENTS = [TrendAgent(), DipAgent(), VolatilityAgent(), LiquidityAgent()]


# ---------------------------------------------------------------------------
# Features (all causal: they see bars[:i+1] and nothing after)
# ---------------------------------------------------------------------------

def _ret(bars: list[tuple], i: int, n: int) -> float | None:
    if i - n < 0:
        return None
    prev, now = bars[i - n][4], bars[i][4]
    return (now / prev - 1) * 100 if prev else None


def _sma_ratio(bars: list[tuple], i: int, n: int) -> float | None:
    if i - n < 0:
        return None
    window = [b[4] for b in bars[i - n + 1:i + 1]]
    avg = st.mean(window) if window else 0.0
    return (bars[i][4] / avg) if avg else None


def trend_composite(bars: list[tuple], i: int) -> float | None:
    """A CTREND-shaped blend: price against its own moving averages over several horizons.

    The published trend factor (Fieberg et al., JFQA) is an elastic-net blend of ~28 technical
    signals; this is the cheap, honest core of it — the part that needs no fitting, so it
    cannot be overfitted here before it is even tested.
    """
    ratios = [r for n in (5, 10, 20, 50) if (r := _sma_ratio(bars, i, n)) is not None]
    return st.mean(ratios) if len(ratios) == 4 else None


def turnover_volatility(bars: list[tuple], i: int, n: int = 30) -> float | None:
    """Volatility of turnover — one of the two or three factors that survived the crypto
    factor-zoo replication (Mercik, Zaremba & Demir, IRFA 2026). Sign is an open question,
    which is exactly why it is measured rather than assumed."""
    if i - n < 0:
        return None
    vols = [b[5] for b in bars[i - n + 1:i + 1]]
    mean = st.mean(vols)
    return (st.pstdev(vols) / mean) if mean else None


def amihud_illiquidity(bars: list[tuple], i: int, n: int = 30) -> float | None:
    """|return| per dollar of volume: the classic illiquidity measure."""
    if i - n < 0:
        return None
    vals = []
    for j in range(i - n + 1, i + 1):
        prev = bars[j - 1][4] if j > 0 else 0.0
        if prev and bars[j][5]:
            vals.append(abs(bars[j][4] / prev - 1) / bars[j][5])
    return st.mean(vals) if vals else None


def realized_vol(bars: list[tuple], i: int, n: int = 30) -> float | None:
    if i - n < 0:
        return None
    rets = []
    for j in range(i - n + 1, i + 1):
        prev = bars[j - 1][4] if j > 0 else 0.0
        if prev:
            rets.append(bars[j][4] / prev - 1)
    return st.pstdev(rets) * 100 if len(rets) > 2 else None


def _as_app_candles(bars: list[tuple], i: int, lookback: int = 200) -> list[dict]:
    """The candle shape app.agents expects, ending at the signal bar."""
    start = max(0, i - lookback + 1)
    # b[6] is the real open and b[7] the BASE volume. Passing b[5] (quote volume) as `volume`
    # made LiquidityAgent score price x dollar-volume — dimensionally wrong, and it saturated
    # that agent's clamp on 80% of rows, which is what made its tie-inflated IC so large.
    return [{"ts": b[1], "open": b[6], "high": b[2], "low": b[3], "close": b[4], "volume": b[7]}
            for b in bars[start:i + 1]]


def live_agent_scores(bars: list[tuple], i: int, symbol: str) -> dict[str, float]:
    """The four agents whose weighted mean IS the consensus that leads `_open_rank_key`.

    Scored here on five years of history — the same question the live book could only ask of
    11 independent days.
    """
    candles = _as_app_candles(bars, i)
    out: dict[str, float] = {}
    for agent in LIVE_AGENTS:
        try:
            vote = agent.evaluate(symbol, candles, {})
        except Exception:
            continue
        if vote and vote.confidence > 0:
            out[f"agent:{vote.name}"] = vote.score
    return out


FEATURES = {
    "trend_composite": trend_composite,
    "mom_7d": lambda b, i: _ret(b, i, 7),
    "mom_30d": lambda b, i: _ret(b, i, 30),
    "reversal_3d": lambda b, i: (-r if (r := _ret(b, i, 3)) is not None else None),
    "turnover_vol": turnover_volatility,
    "amihud_illiq": amihud_illiquidity,
    "realized_vol": realized_vol,
}


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def _midranks(v: list[float]) -> list[float]:
    """Ranks with TIES AVERAGED. Ordinal ranks are not a bug you can shrug at here.

    Both variables in this panel are dominated by ties: `triangular()` saturates to exactly 0.0
    on about a third of rows, and the outcome is essentially three values (take-profit, stop,
    deadline). Ordinal ranking breaks every tie by list position, both variables inherit the
    same positional order, and the correlation is manufactured out of the panel's row order.
    Measured 2026-09-06 by an independent reimplementation: with outcomes PERMUTED at random
    inside each day — zero signal by construction — the ordinal version still reported
    IC +0.1445 with t = 19.3. The midrank version reports +0.0043, t = 0.93.
    """
    order = sorted(range(len(v)), key=lambda k: v[k])
    r = [0.0] * len(v)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
            j += 1
        avg = (i + j) / 2.0
        for k in range(i, j + 1):
            r[order[k]] = avg
        i = j + 1
    return r


def spearman(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 5 or len(set(xs)) < 3:
        return None
    rx, ry = _midranks(xs), _midranks(ys)
    mx, my = st.mean(rx), st.mean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry, strict=True))
    den = math.sqrt(sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry))
    return (num / den) if den else None


def build_panel(series: dict, spacing: int, tp: float, sl: float, horizon: int,
                cost: float, min_bars: int, warmup: int) -> dict[str, list[dict]]:
    """{day: [{feature: value, ..., 'outcome': pnl}]} — one row per (coin, day)."""
    panel: dict[str, list[dict]] = defaultdict(list)
    for sym, bars in series.items():
        if len(bars) < min_bars:
            continue
        for i in range(warmup, len(bars), spacing):
            res = simulate(bars, i, tp, sl, horizon, cost)
            if not res:
                continue
            row: dict = {"symbol": sym, "outcome": res["pnl"]}
            for name, fn in FEATURES.items():
                v = fn(bars, i)
                if v is not None:
                    row[name] = v
            row.update(live_agent_scores(bars, i, sym))
            day = datetime.fromtimestamp(bars[i][1] / 1000, timezone.utc).strftime("%Y-%m-%d")
            panel[day].append(row)
    return panel


def daily_ic(panel: dict[str, list[dict]], feature: str, min_names: int) -> list[float]:
    ics = []
    for rows in panel.values():
        pairs = [(r[feature], r["outcome"]) for r in rows if feature in r]
        if len(pairs) < min_names:
            continue
        ic = spearman([p[0] for p in pairs], [p[1] for p in pairs])
        if ic is not None:
            ics.append(ic)
    return ics


def summarize(ics: list[float], seed: int = 5) -> dict:
    if len(ics) < 20:
        return {"n_days": len(ics)}
    rng = random.Random(seed)
    mean = st.mean(ics)
    se = st.pstdev(ics) / math.sqrt(len(ics))
    boots = sorted(st.mean([rng.choice(ics) for _ in ics]) for _ in range(2000))
    return {
        "n_days": len(ics), "ic": mean, "t": (mean / se) if se else float("nan"),
        "lo": boots[50], "hi": boots[-50],
    }


def portfolio_test(panel: dict[str, list[dict]], feature: str, k: int, min_names: int,
                   seed: int = 17) -> dict | None:
    """Rank IS NOT profit. This is the question the system actually asks: take the top *k* by
    the feature each day and compare against *k* drawn at random from the same day's pool.

    A feature can carry a real rank correlation and still pick worse trades — the outcome here
    is dominated by which coins hit an 8% stop, so a signal that merely ranks survivors above
    casualties can post a large IC while adding nothing to the mean the account actually feels.
    """
    rng = random.Random(seed)
    pnl_key = "outcome"
    days = [rows for rows in panel.values()
            if len([r for r in rows if feature in r]) >= min_names]
    if len(days) < 20:
        return None

    def pick(rows_by_day, chooser):
        got = []
        for rows in rows_by_day:
            pool = [r for r in rows if feature in r]
            got += [r["outcome"] for r in chooser(pool)[:k]]
        return st.mean(got) if got else float("nan")

    def top(pool):
        return sorted(pool, key=lambda r: r[feature], reverse=True)

    def bot(pool):
        return sorted(pool, key=lambda r: r[feature])

    def rnd(pool):
        return rng.sample(pool, len(pool))

    def base_of(rows_by_day) -> float:
        """The random baseline EXACTLY, not by simulation.

        The expected outcome of picking k at random from a day's pool IS that pool's mean —
        zero variance and free. Drawing it by Monte Carlo adds the draw's own variance to the
        difference and pushes every verdict toward "no evidence"; a single draw per bootstrap
        replicate (the first version here) hid a real effect that survives Bonferroni.
        """
        got = []
        for rows in rows_by_day:
            pool = [r[pnl_key] for r in rows if feature in r]
            if pool:
                got += pool
        return st.mean(got) if got else float("nan")

    t, b = pick(days, top), pick(days, bot)
    base = base_of(days)
    diffs, diffs_bot = [], []
    for _ in range(600):
        sample = [rng.choice(days) for _ in days]
        base_s = base_of(sample)
        diffs.append(pick(sample, top) - base_s)
        diffs_bot.append(pick(sample, bot) - base_s)
    diffs.sort()
    diffs_bot.sort()
    return {"top": t, "bottom": b, "random": base,
            "lo": diffs[15], "hi": diffs[-15],
            "blo": diffs_bot[15], "bhi": diffs_bot[-15], "n_days": len(days)}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--db", default="data/research/market.db")
    p.add_argument("--spacing", type=int, default=7)
    p.add_argument("--tp", type=float, default=3.0)
    p.add_argument("--sl", type=float, default=8.0)
    p.add_argument("--horizon", type=int, default=7)
    p.add_argument("--cost", type=float, default=0.30)
    p.add_argument("--min-bars", type=int, default=250)
    p.add_argument("--warmup", type=int, default=60, help="bars a feature needs before it counts")
    p.add_argument("--min-names", type=int, default=20, help="coins needed for a day to count")
    p.add_argument("--top-k", type=int, default=5, help="picks per day in the portfolio test")
    p.add_argument("--limit-coins", type=int, default=0)
    args = p.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    series = load(Path(args.db))
    if args.limit_coins:
        series = dict(list(series.items())[: args.limit_coins])
    panel = build_panel(series, args.spacing, args.tp, args.sl, args.horizon,
                        args.cost, args.min_bars, args.warmup)
    rows = sum(len(v) for v in panel.values())
    print(f"panel: {rows:,} (coin, day) rows across {len(panel)} days, "
          f"tp {args.tp}% / sl {args.sl}% / {args.horizon}d, cost {args.cost}%")
    print(f"pre-registered: IC < {COST_HURDLE} discard · >= {USABLE} usable\n")

    names = sorted({k for v in panel.values() for r in v for k in r
                    if k not in ("symbol", "outcome")})
    hdr = f"{'feature':22} {'days':>6} {'IC':>8} {'t':>7} {'95% CI':>18}   verdict"
    print(hdr)
    print("-" * len(hdr))
    for name in names:
        s = summarize(daily_ic(panel, name, args.min_names))
        if "ic" not in s:
            print(f"{name:22} {s['n_days']:>6}   too few days")
            continue
        if s["lo"] > 0 or s["hi"] < 0:
            verdict = "usable" if abs(s["ic"]) >= USABLE else (
                "real but unprovable live" if abs(s["ic"]) >= COST_HURDLE else "real but too small")
        else:
            verdict = "no evidence"
        print(f"{name:22} {s['n_days']:>6} {s['ic']:>+8.4f} {s['t']:>+7.2f} "
              f"[{s['lo']:>+7.4f},{s['hi']:>+7.4f}]   {verdict}")
    print(f"\nPICKING {args.top_k} A DAY — what the system actually does with a ranking")
    hdr2 = (f"{'feature':22} {'top':>9} {'bottom':>9} {'random':>9} "
            f"{'top-random 95% CI':>22}   verdict")
    print(hdr2)
    print("-" * len(hdr2))
    for name in names:
        r = portfolio_test(panel, name, args.top_k, args.min_names)
        if not r:
            print(f"{name:22}   too few days")
            continue
        beats = ("BEATS random" if r["lo"] > 0 else
                 "LOSES to random" if r["hi"] < 0 else "no evidence")
        # The BOTTOM arm gets its own inference. Printing it without one hid the most robust
        # result in the table: picking the LOWEST realised volatility beat random at both exit
        # shapes, while the column it sat in was never tested.
        bbeats = ("bottom BEATS" if r["blo"] > 0 else
                  "bottom LOSES" if r["bhi"] < 0 else "bottom flat")
        print(f"{name:22} {r['top']:>+8.3f}% {r['bottom']:>+8.3f}% {r['random']:>+8.3f}% "
              f"[{r['lo']:>+8.3f},{r['hi']:>+8.3f}]   {beats:15} {bbeats}")

    print("\nNote: an IC measured on a feature chosen AFTER seeing this table is not evidence. "
          "The features above were fixed in advance from the published-evidence review.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
