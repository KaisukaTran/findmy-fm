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


def test_toggle_opus_endpoint_starts_loop(monkeypatch):
    """POST /api/opus must run async so loop.start()'s create_task has an event loop."""
    from fastapi.testclient import TestClient

    from app.main import app
    from app.orchestrator import loop as opus_loop

    calls = {"start": 0, "stop": 0}
    monkeypatch.setattr(opus_loop, "start", lambda: calls.__setitem__("start", calls["start"] + 1))
    monkeypatch.setattr(opus_loop, "stop", lambda: calls.__setitem__("stop", calls["stop"] + 1))
    with TestClient(app) as c:
        r = c.post("/api/opus", json={"enabled": True})
        assert r.status_code == 200 and r.json()["mode"] is True
        assert calls["start"] == 1
        r = c.post("/api/opus", json={"enabled": False})
        assert r.status_code == 200 and calls["stop"] == 1


def test_opus_daily_endpoint(monkeypatch):
    """GET /api/opus/daily returns the UTC-day rollup series."""
    from fastapi.testclient import TestClient

    from app.main import app
    from app.orchestrator import ledger as opus_ledger

    monkeypatch.setattr(
        opus_ledger, "daily_series",
        lambda db, days=14: [{"day": "2026-07-15", "gross": 1.0, "cost": 0.5, "net": 0.5,
                               "net_pct": 0.05, "trades": 1, "win_trades": 1}],
    )
    with TestClient(app) as c:
        r = c.get("/api/opus/daily")
        assert r.status_code == 200
        body = r.json()
        assert body["days"][0]["day"] == "2026-07-15"
        assert body["days"][0]["net"] == 0.5


def test_partial_opus_renders_daily_table(monkeypatch):
    """/partials/opus shows the daily KPI table (newest first) when daily_series has rows,
    and skips all-zero days."""
    from fastapi.testclient import TestClient

    from app.main import app
    from app.orchestrator import ledger as opus_ledger

    monkeypatch.setattr(
        opus_ledger, "daily_series",
        lambda db, days=14: [
            {"day": "2026-07-14", "gross": 0.0, "cost": 0.0, "net": 0.0,
             "net_pct": 0.0, "trades": 0, "win_trades": 0, "engine_pct": None},
            {"day": "2026-07-15", "gross": 12.0, "cost": 1.0, "net": 11.0,
             "net_pct": 0.55, "trades": 2, "win_trades": 1, "engine_pct": 0.123},
            {"day": "2026-07-16", "gross": -3.0, "cost": 0.5, "net": -3.5,
             "net_pct": -0.18, "trades": 1, "win_trades": 0, "engine_pct": None},
        ],
    )
    with TestClient(app) as c:
        html = c.get("/partials/opus").text
        assert "2026-07-15" in html and "2026-07-16" in html
        assert "2026-07-14" not in html  # all-zero day is skipped
        # newest first: 07-16 row appears before 07-15 row
        assert html.index("2026-07-16") < html.index("2026-07-15")
        assert "1/2" in html  # win_trades/trades for the 07-15 row


def test_partial_opus_empty_daily_shows_placeholder(monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app
    from app.orchestrator import ledger as opus_ledger

    monkeypatch.setattr(
        opus_ledger, "daily_series",
        lambda db, days=14: [
            {"day": "2026-07-16", "gross": 0.0, "cost": 0.0, "net": 0.0,
             "net_pct": 0.0, "trades": 0, "win_trades": 0},
        ],
    )
    with TestClient(app) as c:
        html = c.get("/partials/opus").text
        assert "Chưa có dữ liệu ngày nào." in html


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
