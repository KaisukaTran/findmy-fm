"""O-0 scaffolding: OPUS tables, switch persistence, capital envelope, state."""

from __future__ import annotations

from app import portfolio, runtime
from app.config import settings
from app.orchestrator import models as om
from app.orchestrator import service as opus


def test_opus_tables_exist(db):
    # All three additive tables are queryable (create_all built them).
    assert db.query(om.OpusPosition).count() == 0
    assert db.query(om.OpusCostLedger).count() == 0
    assert db.query(om.OpusMetricHourly).count() == 0


def test_opus_toggle_persists(db):
    assert runtime.get_bool(db, runtime.KEY_OPUS_MODE, default=False) is False
    runtime.opus_mode_on(db)
    assert settings.opus_mode is True
    assert runtime.get_bool(db, runtime.KEY_OPUS_MODE) is True
    # a fresh settings read on restart is simulated by sync_from_db
    settings.opus_mode = False
    runtime.sync_from_db(db)
    assert settings.opus_mode is True
    runtime.opus_mode_off(db)
    assert settings.opus_mode is False
    assert runtime.get_bool(db, runtime.KEY_OPUS_MODE) is False


def test_capital_envelope_is_disjoint(db, monkeypatch):
    monkeypatch.setattr(settings, "opus_allocation_usd", 2000.0)
    monkeypatch.setattr(portfolio, "equity", lambda _db: 10000.0)
    assert opus.allocation() == 2000.0
    assert opus.rulebased_equity(db) == 8000.0  # equity - allocation


def test_rulebased_equity_never_negative(db, monkeypatch):
    monkeypatch.setattr(settings, "opus_allocation_usd", 50000.0)
    monkeypatch.setattr(portfolio, "equity", lambda _db: 10000.0)
    assert opus.rulebased_equity(db) == 0.0


def test_state_shape(db, monkeypatch):
    monkeypatch.setattr(settings, "opus_allocation_usd", 2000.0)
    monkeypatch.setattr(settings, "opus_daily_cost_cap_usd", 5.0)
    s = opus.state(db)
    assert s["allocation_usd"] == 2000.0
    assert s["deployed_usd"] == 0.0
    assert s["free_usd"] == 2000.0
    assert s["open_positions"] == 0
    assert s["spend_today_usd"] == 0.0
    assert s["cost_cap_reached"] is False
    assert s["kpi_24h_pct"] == 0.0
    assert s["kpi_target_pct"] == settings.opus_kpi_target_pct


def test_cost_cap_and_spend(db, monkeypatch):
    monkeypatch.setattr(settings, "opus_daily_cost_cap_usd", 5.0)
    db.add(om.OpusCostLedger(input_tokens=1000, output_tokens=500, raw_cost=2.0, billed_cost=4.0))
    db.commit()
    assert opus.spend_today(db) == 4.0
    assert opus.cost_cap_reached(db) is False
    db.add(om.OpusCostLedger(input_tokens=1000, output_tokens=500, raw_cost=1.0, billed_cost=2.0))
    db.commit()
    assert opus.spend_today(db) == 6.0
    assert opus.cost_cap_reached(db) is True
