"""
Tests for scripts/evaluate_configs.py — the CLI that ranks KSS configurations by profit per
dollar-day.

WHY this file exists (mirrors app/evaluate.py's four defects, plus the CLI-specific rules
from the task spec):

  - The default timeframe must be INTRADAY (1h), never 1d: on daily bars the minimum
    reportable time-to-take-profit is a whole day, while real live exits land in 5-7 hours —
    a 1-day-quantised denominator cannot rank anything.
  - Binance's 1000-kline cap must be handled by REUSING `app.data.providers` paging and
    `app.scanner._days_to_bars`'s bars-per-day conversion, never a second, possibly-divergent
    implementation of either.
  - The script must NEVER write to a database: no SQLAlchemy session, no models import, no
    commit — it is read-only market data in, a printed table out.
  - The pure sweep/format functions must never touch the network (candles are injected).

No network calls; no DB.
"""

from __future__ import annotations

from app.config import settings
from app.evaluate import ConfigEvaluation, ConfigScore
from scripts.evaluate_configs import (
    DEFAULT_TIMEFRAME,
    build_grid,
    fetch_candles,
    format_table,
    parse_args,
    rank,
    sweep,
)

_DAY = 86_400_000


def candle(day, close, high=None, low=None, open_=None):
    o = open_ if open_ is not None else close
    return {"ts": day * _DAY, "open": o, "high": high or close,
            "low": low if low is not None else close, "close": close, "volume": 1.0}


def _all_win_uptrend(n=60, start=100.0, step=0.01):
    out, price = [], start
    for d in range(n):
        out.append(candle(d, price, high=price, low=price * 0.999))
        price *= (1 + step)
    return out


# ---------------------------------------------------------------------------
# CLI defaults
# ---------------------------------------------------------------------------

def test_default_timeframe_is_intraday_1h():
    """The default timeframe must be 1h, not 1d — see module docstring."""
    assert DEFAULT_TIMEFRAME == "1h"
    args = parse_args([])
    assert args.timeframe == "1h"


def test_parse_args_grids_and_wave0_defaults():
    args = parse_args([])
    assert args.distances and args.tps and args.waves  # non-empty default grids
    assert args.wave0_usd > 0
    assert args.spacing_days is None  # "use the setting" sentinel, not silently 0


def test_parse_args_overrides():
    args = parse_args([
        "--symbols", "BTC,ETH", "--timeframe", "5m", "--lookback-days", "10",
        "--distances", "1.0,2.0", "--tps", "2.0", "--waves", "3",
        "--spacing-days", "0", "--wave0-usd", "50",
    ])
    assert args.symbols == "BTC,ETH"
    assert args.timeframe == "5m"
    assert args.lookback_days == 10
    assert args.distances == [1.0, 2.0]
    assert args.tps == [2.0]
    assert args.waves == [3]
    assert args.spacing_days == 0.0
    assert args.wave0_usd == 50.0


# ---------------------------------------------------------------------------
# build_grid / sweep / rank / format_table — pure, no network, no DB
# ---------------------------------------------------------------------------

def test_build_grid_is_the_cartesian_product():
    grid = build_grid([1.0, 2.0], [3.0], [5, 8])
    assert grid == [(1.0, 3.0, 5), (1.0, 3.0, 8), (2.0, 3.0, 5), (2.0, 3.0, 8)]


def test_sweep_never_touches_the_network(monkeypatch):
    """sweep() must work from injected candles alone — never call the data provider."""
    def _boom(*a, **kw):
        raise AssertionError("sweep() must not fetch data — candles must be injected")

    monkeypatch.setattr("app.data.providers.data_provider", _boom)
    monkeypatch.setattr("app.data.providers.get_provider", _boom)

    configs = build_grid([2.0], [3.0], [1])
    evaluations = sweep({"FAKE": _all_win_uptrend()}, configs, spacing_days=0)

    assert len(evaluations) == 1
    assert isinstance(evaluations[0], ConfigEvaluation)


def test_sweep_returns_one_evaluation_per_config():
    configs = build_grid([1.5, 2.0], [2.0, 3.0], [3])
    evaluations = sweep({"FAKE": _all_win_uptrend()}, configs, spacing_days=0)
    assert len(evaluations) == len(configs)
    got = {(ev.optimistic.distance_pct, ev.optimistic.tp_pct, ev.optimistic.max_waves)
           for ev in evaluations}
    assert got == set(configs)


def test_rank_sorts_descending_by_pessimistic_profit_per_dollar_day():
    def _score(ppd: float) -> ConfigScore:
        return ConfigScore(
            distance_pct=2.0, tp_pct=3.0, max_waves=5, pessimistic=True,
            effective_tp_pct=3.2, cost_pct=0.3, spacing_days=7.0,
            symbols=1, trials=10, wins=10, losses=0, stops=0, flats=0,
            win_rate=100.0, loss_rate=0.0, expectancy=2.9, avg_mae=0.0, worst_mae=0.0,
            avg_waves_filled=1.0, total_pnl_usd=10.0, total_capital_days=100.0,
            profit_per_dollar_day=ppd,
        )

    low = ConfigEvaluation(optimistic=_score(0.01), pessimistic=_score(0.001))
    high = ConfigEvaluation(optimistic=_score(0.01), pessimistic=_score(0.05))
    mid = ConfigEvaluation(optimistic=_score(0.01), pessimistic=_score(0.02))

    ranked = rank([low, high, mid])
    assert [e.pessimistic.profit_per_dollar_day for e in ranked] == [0.05, 0.02, 0.001]


def test_format_table_reports_both_bounds_for_every_config():
    """Defect #4, at the CLI layer: the printed table must never collapse to one number per
    config — both the optimistic and pessimistic profit_per_dollar_day must be visible."""
    configs = build_grid([2.0], [2.5], [5])
    evaluations = sweep(
        {"FAKE": [candle(0, 100.0), candle(1, 95.0, high=104.0, low=90.0, open_=97.0)]},
        configs, sl_pct=3.0, spacing_days=0,
    )
    table = format_table(evaluations)
    ev = evaluations[0]
    assert ev.optimistic.win_rate != ev.pessimistic.win_rate  # fixture genuinely diverges
    assert f"{ev.optimistic.profit_per_dollar_day:>10.5f}" in table
    assert f"{ev.pessimistic.profit_per_dollar_day:>10.5f}" in table


# ---------------------------------------------------------------------------
# fetch_candles — the one network-touching function; verified via an injected stub provider
# ---------------------------------------------------------------------------

def test_fetch_candles_uses_scanner_days_to_bars_for_the_limit(monkeypatch):
    """The Binance 1000-kline cap is handled by reusing app.scanner._days_to_bars (which
    itself defers to app.data.providers' own paging) — not a second implementation. On the
    default 1h timeframe with intraday_max_bars=3000 (default), 365 lookback days must
    request exactly 3000 bars, not 365 (a raw days-as-bars bug) and not 8760 (uncapped)."""
    calls: list[tuple[str, str, int]] = []

    class _StubProvider:
        def get_ohlcv(self, symbol: str, timeframe: str, limit: int):
            calls.append((symbol, timeframe, limit))
            return [candle(0, 100.0), candle(1, 101.0)]

    monkeypatch.setattr("scripts.evaluate_configs.data_provider", lambda: _StubProvider())
    monkeypatch.setattr(settings, "intraday_max_bars", 3000)

    result = fetch_candles(["BTC", "ETH"], "1h", 365)

    assert calls == [("BTC", "1h", 3000), ("ETH", "1h", 3000)]
    assert set(result.keys()) == {"BTC", "ETH"}


def test_fetch_candles_drops_symbols_with_no_data(monkeypatch):
    class _StubProvider:
        def get_ohlcv(self, symbol: str, timeframe: str, limit: int):
            return [] if symbol == "DEAD" else [candle(0, 100.0)]

    monkeypatch.setattr("scripts.evaluate_configs.data_provider", lambda: _StubProvider())

    result = fetch_candles(["BTC", "DEAD"], "1h", 30)
    assert set(result.keys()) == {"BTC"}


# ---------------------------------------------------------------------------
# The script never writes to the database.
# ---------------------------------------------------------------------------

def test_script_source_has_no_database_writes():
    """Static guard: the script must not import a DB session, models, or call commit — a
    read-only evaluation tool must not be able to mutate the database even by accident."""
    import scripts.evaluate_configs as mod

    source_path = mod.__file__
    with open(source_path, encoding="utf-8") as f:
        source = f.read()

    forbidden = ["SessionLocal", "app.models", "app.db", ".commit(", ".add(", ".flush("]
    for token in forbidden:
        assert token not in source, f"evaluate_configs.py must not reference {token!r}"
