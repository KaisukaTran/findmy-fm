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
