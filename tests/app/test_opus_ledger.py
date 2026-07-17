"""O-1: Opus cost metering (x2), hourly rollup, and the 24h KPI."""

from __future__ import annotations

from datetime import datetime, timedelta

from app.config import settings
from app.orchestrator import ledger, service
from app.orchestrator import models as om


def test_record_cost_is_doubled(db, monkeypatch):
    monkeypatch.setattr(settings, "opus_price_in_per_mtok", 15.0)
    monkeypatch.setattr(settings, "opus_price_out_per_mtok", 75.0)
    monkeypatch.setattr(settings, "opus_cost_multiplier", 2.0)
    # 1M in @15 + 1M out @75 = 90 raw → 180 billed
    row = ledger.record_cost(db, 1_000_000, 1_000_000)
    assert row.raw_cost == 90.0
    assert row.billed_cost == 180.0


def test_rollup_net_is_realized_minus_billed_cost(db, monkeypatch):
    monkeypatch.setattr(settings, "opus_allocation_usd", 2000.0)
    monkeypatch.setattr(settings, "opus_cost_multiplier", 2.0)
    monkeypatch.setattr(settings, "opus_price_in_per_mtok", 10.0)
    monkeypatch.setattr(settings, "opus_price_out_per_mtok", 10.0)

    now = datetime.utcnow()
    # a closed winning position this hour: +30 realized
    db.add(om.OpusPosition(symbol="BTC", state=om.OPUS_CLOSED, realized_pnl=30.0,
                           closed_at=now, qty=0.0, avg_price=0.0))
    db.commit()
    # an Opus call this hour costing 1M+1M tokens @10/10 = 20 raw → 40 billed
    ledger.record_cost(db, 1_000_000, 1_000_000)

    row = ledger.rollup_hour(db, now)
    assert row.gross_pnl == 30.0
    assert row.opus_cost_billed == 40.0
    assert row.net_pnl == -10.0  # 30 realized − 40 billed cost
    assert row.trades == 1 and row.win_trades == 1
    assert row.invested_capital == 2000.0
    assert abs(row.net_pct - (-10.0 / 2000.0 * 100.0)) < 1e-9


def test_kpi_24h_reads_rollups(db, monkeypatch):
    monkeypatch.setattr(settings, "opus_allocation_usd", 1000.0)
    # +25 net last hour → KPI = 25/1000 = 2.5%
    db.add(om.OpusMetricHourly(hour_ts=datetime.utcnow().replace(minute=0, second=0, microsecond=0),
                               net_pnl=25.0, invested_capital=1000.0))
    db.commit()
    assert abs(service.kpi_24h_pct(db) - 2.5) < 1e-6


def test_metrics_series_window(db):
    base = datetime.utcnow().replace(minute=0, second=0, microsecond=0)
    for i in range(60):
        db.add(om.OpusMetricHourly(hour_ts=base - timedelta(hours=i), net_pnl=float(i)))
    db.commit()
    rows = ledger.metrics_series(db, hours=24)
    assert len(rows) == 24
    # oldest → newest
    assert rows[0].hour_ts < rows[-1].hour_ts


def test_target_per_hour(db, monkeypatch):
    monkeypatch.setattr(settings, "opus_allocation_usd", 2400.0)
    monkeypatch.setattr(settings, "opus_kpi_target_pct", 1.0)
    # 2400 * 1% / 24h = 1.0 USD/hour
    assert abs(ledger.target_per_hour() - 1.0) < 1e-9


# --- P1 cost-truth v2: purpose filtering, cache tokens, new defaults, daily series ------


def test_grok_scanner_excluded_from_spend_today_and_rollup(db):
    """A "grok_scanner" row serves the KSS scanner, not OPUS — it must not count against
    OPUS's own cost cap (spend_today) nor its hourly rollup cost."""
    now = datetime.utcnow()
    db.add(om.OpusCostLedger(ts=now, input_tokens=1_000_000, output_tokens=0,
                              raw_cost=100.0, billed_cost=100.0, purpose="grok_scanner"))
    db.add(om.OpusCostLedger(ts=now, input_tokens=1_000_000, output_tokens=0,
                              raw_cost=1.0, billed_cost=1.0, purpose="decision"))
    db.commit()
    assert service.spend_today(db) == 1.0  # only the "decision" row counts

    row = ledger.rollup_hour(db, now)
    assert row.opus_cost_billed == 1.0


def test_own_purposes_grok_decision_distill_triage_count(db):
    """The other OPUS-own purposes (grok_decision/distill/triage) DO count."""
    now = datetime.utcnow()
    for purpose in ledger.OPUS_OWN_PURPOSES:
        db.add(om.OpusCostLedger(ts=now, billed_cost=2.0, purpose=purpose))
    db.commit()
    assert service.spend_today(db) == 2.0 * len(ledger.OPUS_OWN_PURPOSES)


def test_raw_cost_meters_cache_tokens():
    # price_in=5$/MTok -> cache_read (0.1x) = 0.5, cache_write (1.25x) = 6.25 per 1M tokens
    cost = ledger.raw_cost(
        0, 0, price_in=5.0, price_out=25.0,
        cache_read_tokens=1_000_000, cache_write_tokens=1_000_000,
    )
    assert abs(cost - 6.75) < 1e-9

    read_only = ledger.raw_cost(0, 0, price_in=5.0, price_out=25.0, cache_read_tokens=1_000_000)
    assert abs(read_only - 0.5) < 1e-9

    write_only = ledger.raw_cost(0, 0, price_in=5.0, price_out=25.0, cache_write_tokens=1_000_000)
    assert abs(write_only - 6.25) < 1e-9


def test_record_cost_persists_cache_tokens(db, monkeypatch):
    monkeypatch.setattr(settings, "opus_price_in_per_mtok", 5.0)
    monkeypatch.setattr(settings, "opus_price_out_per_mtok", 25.0)
    monkeypatch.setattr(settings, "opus_cost_multiplier", 2.0)
    row = ledger.record_cost(
        db, 100, 50, cache_read_tokens=1_000_000, cache_write_tokens=1_000_000,
    )
    assert row.cache_read_tokens == 1_000_000
    assert row.cache_write_tokens == 1_000_000
    assert row.raw_cost == row.billed_cost / settings.opus_cost_multiplier
    # sanity: raw includes the plain input/output cost plus the cache legs
    plain = 100 * 5.0 / 1e6 + 50 * 25.0 / 1e6
    assert abs(row.raw_cost - (plain + 0.5 + 6.25)) < 1e-9


def test_new_price_and_kpi_defaults():
    """A fresh Settings instance (model default, not the mutated singleton) reflects the
    2026-07-16 Opus 4.8 list price + the 3%% stretch KPI."""
    fields = type(settings).model_fields
    assert fields["opus_price_in_per_mtok"].default == 5.0
    assert fields["opus_price_out_per_mtok"].default == 25.0
    assert fields["opus_kpi_target_pct"].default == 3.0


def test_daily_series_aggregates_by_utc_calendar_day(db, monkeypatch):
    monkeypatch.setattr(settings, "opus_allocation_usd", 1000.0)
    today = datetime.utcnow().replace(minute=0, second=0, microsecond=0)
    yesterday = today - timedelta(days=1)
    # two hourly rows today
    db.add(om.OpusMetricHourly(hour_ts=today.replace(hour=1), gross_pnl=100.0,
                                opus_cost_billed=20.0, trades=2, win_trades=1))
    db.add(om.OpusMetricHourly(hour_ts=today.replace(hour=5), gross_pnl=50.0,
                                opus_cost_billed=10.0, trades=1, win_trades=1))
    # one hourly row yesterday
    db.add(om.OpusMetricHourly(hour_ts=yesterday.replace(hour=9), gross_pnl=30.0,
                                opus_cost_billed=5.0, trades=1, win_trades=0))
    db.commit()

    series = ledger.daily_series(db, days=2)
    assert len(series) == 2
    assert series[0]["day"] == yesterday.date().isoformat()
    assert series[1]["day"] == today.date().isoformat()

    assert series[0]["gross"] == 30.0
    assert series[0]["cost"] == 5.0
    assert series[0]["net"] == 25.0
    assert series[0]["trades"] == 1 and series[0]["win_trades"] == 0
    assert abs(series[0]["net_pct"] - 2.5) < 1e-9  # 25 / 1000 * 100

    assert series[1]["gross"] == 150.0
    assert series[1]["cost"] == 30.0
    assert series[1]["net"] == 120.0
    assert series[1]["trades"] == 3 and series[1]["win_trades"] == 2
    assert abs(series[1]["net_pct"] - 12.0) < 1e-9  # 120 / 1000 * 100


def test_engine_daily_pct_benchmark(db, monkeypatch):
    """P5: the engine benchmark sums only non-OPUS SELL realized PnL for the UTC day,
    against the engine's current equity; no equity → None."""
    from datetime import date

    from app.models import Fill
    from app.orchestrator import service as osvc

    today = datetime.utcnow().date()
    noon = datetime(today.year, today.month, today.day, 12)
    db.add(Fill(symbol="ETH", side="SELL", quantity=1.0, price=100.0,
                realized_pnl=50.0, strategy_name="KSS", executed_at=noon))
    db.add(Fill(symbol="BTC", side="SELL", quantity=1.0, price=100.0,
                realized_pnl=99.0, strategy_name="OPUS", executed_at=noon))  # excluded
    db.add(Fill(symbol="SOL", side="BUY", quantity=1.0, price=100.0,
                realized_pnl=10.0, strategy_name="KSS", executed_at=noon))  # not a SELL
    db.add(Fill(symbol="DOT", side="SELL", quantity=1.0, price=100.0,
                realized_pnl=25.0, strategy_name="KSS",
                executed_at=noon - timedelta(days=1)))  # yesterday
    db.commit()

    monkeypatch.setattr(osvc, "rulebased_equity", lambda _db: 10_000.0)
    pct = ledger.engine_daily_pct(db, today)
    assert pct is not None and abs(pct - 0.5) < 1e-9  # 50 / 10_000 × 100

    monkeypatch.setattr(osvc, "rulebased_equity", lambda _db: 0.0)
    assert ledger.engine_daily_pct(db, today) is None

    # rollup_day carries the benchmark alongside the OPUS columns
    monkeypatch.setattr(osvc, "rulebased_equity", lambda _db: 10_000.0)
    day = ledger.rollup_day(db, today)
    assert day["engine_pct"] == pct
