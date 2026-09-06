"""
Does Grok's judgement beat the deterministic formula at picking coins?

Reads the verdicts recorded in SHADOW mode (Grok answers, nothing acts on it) and scores them
against what the coins actually did afterwards. Prints the number, the sample size it rests on,
and refuses to call a winner the data cannot support.

WHY THIS SCRIPT EXISTS IN THIS SHAPE
------------------------------------
An earlier analysis concluded Grok's veto "measures negative". That script no longer exists, and
when its arms were reconstructed from the book they turned out to be confounded with the
calendar: 66% of the vetoes fell in four days and 75% of the endorsements in seven others. It was
largely comparing two date ranges and calling the difference Grok. So:

  * Every comparison here is made WITHIN ONE SCAN — the same minute, the same market, the same
    deterministic short-list — and only scans containing both arms contribute. A verdict that
    labels the day cannot survive that.
  * The naive across-everything number is printed beside it, so the size of the confound is
    visible rather than argued about.
  * Significance comes from a permutation test that shuffles the verdict labels WITHIN each scan,
    which needs no distributional assumption and cannot be fooled by ties (a tie-handling bug in a
    hand-rolled rank statistic manufactured a signal in this repo once already).

WHAT IS BEING MEASURED
----------------------
Not "is Grok right about the chart" — the scanner owns that, and asking Grok for it is what
produced a 96.2% technical echo last time. The question is whether the EVENT verdict adds
information the formula does not have. Hence three arms, and hence `abstain` counting as its own
outcome: if Grok abstains on 95% of candidates, that is the answer, and a cheap one.

Usage:
    python scripts/grok_shadow_eval.py                     # live book, 3-day horizon
    python scripts/grok_shadow_eval.py --db data/findmy.db --horizon 7
    python scripts/grok_shadow_eval.py --since 2026-09-07  # only verdicts from the shadow run
"""

from __future__ import annotations

import argparse
import os
import random
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# The vocabulary the old prompt taught Grok to answer in. A reason built from these words is an
# echo of the scanner's own arithmetic, whatever the verdict; counting them is how we tell whether
# the new prompt actually changed the question being answered.
TA_WORDS = ("bb_pct", "overbought", "oversold", "rsi", "adx", "macd", "atr", "supertrend",
            "htf", "overext", "momentum", "pullback", "support", "resistance", "trend")
EVENT_WORDS = ("unlock", "vesting", "hack", "exploit", "depeg", "delist", "listing", "sec",
               "regulat", "lawsuit", "upgrade", "mainnet", "partnership", "funding", "airdrop",
               "halt", "insolven", "treasury", "news")


def _load_verdicts(db_path: str, since: str | None) -> list[dict]:
    """Every candidate that carries a Grok verdict, with the scan that produced it."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    cols = {r[1] for r in con.execute("PRAGMA table_info(candidates)")}
    if "grok_verdict" not in cols:
        raise SystemExit(
            f"{db_path} has no candidates.grok_verdict column — that database predates the "
            f"verdict recording. Point --db at the book the shadow run actually wrote.")
    where = "grok_verdict is not null"
    args: list = []
    if since:
        where += " and created_at >= ?"
        args.append(since)
    rows = con.execute(
        f"select scan_id, symbol, grok_verdict, reason, created_at from candidates "
        f"where {where} order by scan_id", args).fetchall()
    return [{"scan_id": s, "symbol": sym, "verdict": v, "reason": r or "", "at": t}
            for s, sym, v, r, t in rows]


def _forward_returns(symbols: list[str], horizon: int) -> dict[str, list[tuple[datetime, float]]]:
    """Daily closes per symbol, via the app's own cached prefetch (no new exchange path)."""
    from app import scanner
    from app.config import settings

    out: dict[str, list[tuple[datetime, float]]] = {}
    cmap = scanner._prefetch_candles(settings.data_exchange, symbols,
                                     settings.backtest_timeframe, 200)
    for sym in symbols:
        candles, _ok = cmap.get(sym, ([], False))
        series = []
        for c in candles:
            ts = c["ts"] if isinstance(c, dict) else c[0]
            close = c["close"] if isinstance(c, dict) else c[4]
            series.append((datetime.utcfromtimestamp(ts / 1000), float(close)))
        out[sym] = sorted(series)
    return out


def _return_after(series: list[tuple[datetime, float]], at: datetime, horizon: int) -> float | None:
    """Close-to-close return from the first bar at/after `at` to `horizon` bars later."""
    idx = next((i for i, (t, _) in enumerate(series) if t >= at), None)
    if idx is None or idx + horizon >= len(series):
        return None
    entry, exit_ = series[idx][1], series[idx + horizon][1]
    return (exit_ / entry - 1.0) * 100.0 if entry > 0 else None


def _within_scan_delta(scans: dict, arm_a: str, arm_b: str) -> tuple[list[float], int]:
    """Per-scan (mean return of arm_a − mean of arm_b), only where the scan holds both."""
    deltas = []
    for rows in scans.values():
        a = [r["ret"] for r in rows if r["verdict"] == arm_a]
        b = [r["ret"] for r in rows if r["verdict"] == arm_b]
        if a and b:
            deltas.append(statistics.mean(a) - statistics.mean(b))
    return deltas, len(deltas)


def _permutation_p(scans: dict, arm_a: str, arm_b: str, observed: float, trials: int = 5000) -> float:
    """Shuffle the labels WITHIN each scan; how often does chance beat what we saw?"""
    rng = random.Random(20260906)
    pool = [rows for rows in scans.values()
            if any(r["verdict"] == arm_a for r in rows) and any(r["verdict"] == arm_b for r in rows)]
    if not pool:
        return float("nan")
    hits = 0
    for _ in range(trials):
        deltas = []
        for rows in pool:
            labels = [r["verdict"] for r in rows]
            rng.shuffle(labels)
            a = [r["ret"] for r, lab in zip(rows, labels, strict=True) if lab == arm_a]
            b = [r["ret"] for r, lab in zip(rows, labels, strict=True) if lab == arm_b]
            if a and b:
                deltas.append(statistics.mean(a) - statistics.mean(b))
        if deltas and abs(statistics.mean(deltas)) >= abs(observed):
            hits += 1
    return hits / trials


def _reason_mix(rows: list[dict]) -> tuple[float, float]:
    """Share of reasons that are technical, and share that name an event."""
    said = [r["reason"].lower() for r in rows if r["reason"].strip()]
    if not said:
        return 0.0, 0.0
    ta = sum(any(w in s for w in TA_WORDS) for s in said)
    ev = sum(any(w in s for w in EVENT_WORDS) for s in said)
    return ta / len(said) * 100, ev / len(said) * 100


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="data/live.db")
    ap.add_argument("--horizon", type=int, default=3, help="bars ahead to score (default 3 days)")
    ap.add_argument("--since", default=None, help="ISO date; ignore verdicts before it")
    args = ap.parse_args()

    rows = _load_verdicts(args.db, args.since)
    if not rows:
        print("No Grok verdicts recorded yet. Nothing to measure — run the shadow gate first.")
        return 0

    counts = defaultdict(int)
    for r in rows:
        counts[r["verdict"]] += 1
    total = len(rows)
    print(f"\n=== Grok shadow verdicts: {args.db} ===")
    print(f"{total} verdicts over {len({r['scan_id'] for r in rows})} scans, "
          f"{rows[0]['at'][:10]} → {rows[-1]['at'][:10]}")
    for k in ("endorse", "veto", "abstain", "unavailable", "absent"):
        if counts[k]:
            print(f"  {k:<12} {counts[k]:>6}  ({counts[k]/total*100:>5.1f}%)")

    spoke = [r for r in rows if r["verdict"] in ("endorse", "veto")]
    ta_pct, ev_pct = _reason_mix(spoke)
    print(f"\nreasons given when it did NOT abstain (n={len(spoke)}):")
    print(f"  cite chart indicators : {ta_pct:>5.1f}%   <- the old prompt scored 96.2% here")
    print(f"  name a real event     : {ev_pct:>5.1f}%   <- the old prompt scored 0.0% here")

    print(f"\nfetching {args.horizon}-day forward returns...")
    symbols = sorted({r["symbol"] for r in rows})
    series = _forward_returns(symbols, args.horizon)
    scans: dict = defaultdict(list)
    scored = 0
    for r in rows:
        at = datetime.fromisoformat(r["at"].split(".")[0])
        ret = _return_after(series.get(r["symbol"], []), at, args.horizon)
        if ret is not None:
            r["ret"] = ret
            scans[r["scan_id"]].append(r)
            scored += 1
    print(f"  scored {scored}/{total} verdicts (the rest have no {args.horizon} bars of future yet)")

    for a, b in (("endorse", "veto"), ("endorse", "abstain"), ("abstain", "veto")):
        deltas, n = _within_scan_delta(scans, a, b)
        print(f"\n--- {a} vs {b} ---")
        if n < 20:
            print(f"  only {n} scans hold both arms. NOT ENOUGH to conclude anything — "
                  f"report the count, not a verdict.")
            if n == 0:
                continue
        obs = statistics.mean(deltas)
        p = _permutation_p(scans, a, b, obs)
        naive_a = [r["ret"] for rows_ in scans.values() for r in rows_ if r["verdict"] == a]
        naive_b = [r["ret"] for rows_ in scans.values() for r in rows_ if r["verdict"] == b]
        naive = (statistics.mean(naive_a) - statistics.mean(naive_b)) if naive_a and naive_b else float("nan")
        print(f"  within-scan  {a} − {b}: {obs:+.3f}% over {n} paired scans   (p={p:.3f})")
        print(f"  naive (all rows, confounded with the calendar): {naive:+.3f}%")
        if n >= 20 and p < 0.05:
            print(f"  -> {a} really did differ from {b}. Whether it is worth "
                  f"$111/month is a separate question.")
        elif n >= 20:
            print("  -> no detectable difference. Grok's verdict is not adding information here.")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
