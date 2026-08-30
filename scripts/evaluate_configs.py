"""
scripts/evaluate_configs.py — rank KSS configurations by profit per dollar-day.

`app.evaluate.score_config` is the ruler; this CLI is just the thing that turns the crank
over a symbol list and a grid of (distance_pct, tp_pct, max_waves) combos, and prints a
table ranked by `profit_per_dollar_day` — the metric that accounts for how much capital a
config actually ties up and for how long, not just its win rate.

Defaults to an INTRADAY timeframe (1h): on daily bars the minimum reportable time-to-TP is a
whole day while real live exits land in 5-7 hours, so a 1-day-quantised denominator cannot
rank anything meaningfully. Paging past Binance's 1000-kline cap is handled entirely by
`app.data.providers.CcxtProvider.get_ohlcv` — this script does not reinvent it, and the
bars-per-day → `limit` conversion reuses `app.scanner._days_to_bars` (same one the live
scanner uses) rather than a second, possibly-divergent implementation.

This script NEVER writes to the database — it has no SQLAlchemy session, no models import,
and no commit. It is read-only market data in, a printed table out.

Usage:
    .venv/Scripts/python.exe scripts/evaluate_configs.py [--symbols BTC,ETH,SOL]
        [--timeframe 1h] [--lookback-days 60]
        [--distances 1.5,2.0,3.0] [--tps 1.5,2.0,3.0] [--waves 2,5,8]
        [--wave0-usd 100] [--spacing-days 7]
"""

from __future__ import annotations

import argparse
import sys
from itertools import product
from pathlib import Path

# Make the project root importable when running as a standalone script.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import settings  # noqa: E402
from app.data.providers import Candle, data_provider  # noqa: E402
from app.evaluate import ConfigEvaluation, evaluate_config  # noqa: E402
from app.scanner import _days_to_bars  # noqa: E402

# A deliberately small default grid — the cartesian product with --distances/--tps/--waves
# fans out fast, and every combo re-runs a full walk-forward backtest per symbol per bound.
DEFAULT_DISTANCES: list[float] = [1.5, 2.0, 3.0]
DEFAULT_TPS: list[float] = [1.5, 2.0, 3.0]
DEFAULT_WAVES: list[int] = [2, 5, 8]

DEFAULT_TIMEFRAME = "1h"  # intraday — see module docstring for why 1d cannot rank anything
DEFAULT_LOOKBACK_DAYS = 60
DEFAULT_WAVE0_USD = 100.0


# ---------------------------------------------------------------------------
# Pure functions (no network, no DB) — the part that is unit-testable.
# ---------------------------------------------------------------------------


def build_grid(
    distances: list[float], tps: list[float], waves: list[int]
) -> list[tuple[float, float, int]]:
    """Cartesian product of the three grids, in a deterministic (stable) order."""
    return list(product(distances, tps, waves))


def sweep(
    candles_by_symbol: dict[str, list[Candle]],
    configs: list[tuple[float, float, int]],
    **score_kwargs: object,
) -> list[ConfigEvaluation]:
    """Score every (distance_pct, tp_pct, max_waves) combo in `configs` against
    `candles_by_symbol`, under BOTH intrabar bounds. Pure — no network, no DB; the caller
    supplies candles already fetched. `score_kwargs` forwards to `evaluate_config`
    (e.g. `wave0_notional_usd`, `spacing_days`)."""
    return [
        evaluate_config(candles_by_symbol, distance_pct, tp_pct, max_waves, **score_kwargs)
        for distance_pct, tp_pct, max_waves in configs
    ]


def rank(evaluations: list[ConfigEvaluation]) -> list[ConfigEvaluation]:
    """Sort descending by the PESSIMISTIC profit_per_dollar_day — the conservative bound is
    the one a ranking should trust when the two disagree; ties fall back to optimistic."""
    return sorted(
        evaluations,
        key=lambda e: (e.pessimistic.profit_per_dollar_day, e.optimistic.profit_per_dollar_day),
        reverse=True,
    )


_ROW_FMT = (
    "{dist:>5.1f} {tp:>5.1f} {waves:>3d}  "
    "{opt_ppd:>10.5f} {pess_ppd:>10.5f}  "
    "{opt_wr:>6.1f} {pess_wr:>6.1f}  "
    "{opt_exp:>7.3f} {pess_exp:>7.3f}  "
    "{trials:>6d} {symbols:>4d} {avg_waves:>5.2f}"
)
_HEADER = (
    f"{'dist':>5} {'tp':>5} {'wv':>3}  "
    f"{'opt_$/d-day':>10} {'pess_$/d-day':>10}  "
    f"{'opt_wr%':>6} {'pess_wr%':>6}  "
    f"{'opt_exp%':>7} {'pess_exp%':>7}  "
    f"{'trials':>6} {'syms':>4} {'avgWv':>5}"
)


def format_table(evaluations: list[ConfigEvaluation]) -> str:
    """Render a ranked table. Reports BOTH intrabar bounds side by side for every config
    (defect #4 in app/evaluate.py) — never a single number that hides which bound made it."""
    lines = [_HEADER, "-" * len(_HEADER)]
    for ev in rank(evaluations):
        o, p = ev.optimistic, ev.pessimistic
        lines.append(_ROW_FMT.format(
            dist=o.distance_pct, tp=o.tp_pct, waves=o.max_waves,
            opt_ppd=o.profit_per_dollar_day, pess_ppd=p.profit_per_dollar_day,
            opt_wr=o.win_rate, pess_wr=p.win_rate,
            opt_exp=o.expectancy, pess_exp=p.expectancy,
            trials=o.trials, symbols=o.symbols, avg_waves=o.avg_waves_filled,
        ))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Network I/O (kept out of the pure functions above so they stay unit-testable).
# ---------------------------------------------------------------------------


def fetch_candles(
    symbols: list[str], timeframe: str, lookback_days: int
) -> dict[str, list[Candle]]:
    """Fetch OHLCV for every symbol via the configured data provider.

    Converts the calendar-days lookback to a bar count with `app.scanner._days_to_bars` (the
    SAME conversion the live scanner uses — 365 calendar days is 365 daily bars but 8760
    hourly ones) and lets `CcxtProvider.get_ohlcv` do its own paging past Binance's 1000-kline
    cap. Symbols with no data (a provider failure) are silently dropped, not crashed on —
    matching `estimate_win_rate`'s own graceful-empty-candles behaviour.
    """
    limit = _days_to_bars(lookback_days, timeframe)
    provider = data_provider()
    out: dict[str, list[Candle]] = {}
    for symbol in symbols:
        candles = provider.get_ohlcv(symbol, timeframe, limit)
        if candles:
            out[symbol] = candles
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _float_list(raw: str) -> list[float]:
    return [float(x) for x in raw.split(",") if x.strip()]


def _int_list(raw: str) -> list[int]:
    return [int(x) for x in raw.split(",") if x.strip()]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Rank KSS configurations by profit per dollar-day (both intrabar bounds)."
    )
    parser.add_argument(
        "--symbols", type=str, default=",".join(settings.watchlist),
        help="Comma-separated base symbols (default: settings.watchlist).",
    )
    parser.add_argument(
        "--timeframe", type=str, default=DEFAULT_TIMEFRAME,
        help=f"Candle timeframe (default: {DEFAULT_TIMEFRAME} — intraday, see module docstring).",
    )
    parser.add_argument(
        "--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS,
        help=f"Calendar days of history to fetch (default: {DEFAULT_LOOKBACK_DAYS}).",
    )
    parser.add_argument("--distances", type=_float_list, default=DEFAULT_DISTANCES,
                        help="Comma-separated distance_pct grid.")
    parser.add_argument("--tps", type=_float_list, default=DEFAULT_TPS,
                        help="Comma-separated nominal tp_pct grid (fee buffer added on top).")
    parser.add_argument("--waves", type=_int_list, default=DEFAULT_WAVES,
                        help="Comma-separated max_waves grid.")
    parser.add_argument(
        "--spacing-days", type=float, default=None,
        help="Override settings.backtest_trial_spacing_days (default: use the setting).",
    )
    parser.add_argument(
        "--wave0-usd", type=float, default=DEFAULT_WAVE0_USD,
        help=f"Dollar notional of wave 0, for capital-days scoring (default: {DEFAULT_WAVE0_USD}).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    configs = build_grid(args.distances, args.tps, args.waves)

    print(f"Fetching {args.timeframe} candles for {len(symbols)} symbol(s), "
          f"{args.lookback_days}d lookback: {', '.join(symbols)}")
    candles_by_symbol = fetch_candles(symbols, args.timeframe, args.lookback_days)
    missing = [s for s in symbols if s not in candles_by_symbol]
    if missing:
        print(f"  (no data for: {', '.join(missing)})")
    if not candles_by_symbol:
        print("No candle data fetched for any symbol — nothing to evaluate.")
        return

    print(f"Scoring {len(configs)} configuration(s) × 2 intrabar bounds "
          f"× {len(candles_by_symbol)} symbol(s)...")
    score_kwargs: dict[str, object] = {"wave0_notional_usd": args.wave0_usd}
    if args.spacing_days is not None:
        score_kwargs["spacing_days"] = args.spacing_days
    evaluations = sweep(candles_by_symbol, configs, **score_kwargs)

    print()
    print(format_table(evaluations))


if __name__ == "__main__":
    main()
