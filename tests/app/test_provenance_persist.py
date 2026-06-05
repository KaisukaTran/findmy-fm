"""Source tagging (OPUS/KSS) on positions+trades, and persisted auto-approve rule."""

from __future__ import annotations

from app import portfolio, runtime
from app.config import settings
from app.models import SESSION_ACTIVE, Fill, KssSession, Position
from app.orchestrator import models as om


def test_order_source_mapping():
    assert portfolio.order_source("opus:5:open") == "OPUS"
    assert portfolio.order_source("pyramid:3:tp") == "KSS"
    assert portfolio.order_source(None) == "manual"
    assert portfolio.order_source("something") == "auto"


def test_positions_tagged_by_owner(db, monkeypatch):
    monkeypatch.setattr(portfolio, "get_current_prices", lambda syms: dict.fromkeys(syms, 100.0))
    db.add(Position(symbol="BTC", quantity=1.0, avg_entry_price=90.0, total_cost=90.0))
    db.add(Position(symbol="ETH", quantity=2.0, avg_entry_price=50.0, total_cost=100.0))
    db.add(om.OpusPosition(symbol="BTC", state=om.OPUS_RIDE, qty=1.0, avg_price=90.0))
    db.add(KssSession(symbol="ETH", entry_price=50, distance_pct=2, max_waves=5,
                      isolated_fund=100, tp_pct=3, timeout_x_min=1, gap_y_min=0,
                      status=SESSION_ACTIVE))
    db.commit()
    rows = {r["symbol"]: r["sources"] for r in portfolio.positions_view(db)}
    assert rows["BTC"] == ["OPUS"]
    assert rows["ETH"] == ["KSS"]


def test_trades_tagged_with_source(db):
    db.add(Fill(symbol="BTC", side="SELL", quantity=1, price=100, source_ref="opus:1:close"))
    db.add(Fill(symbol="ETH", side="BUY", quantity=1, price=50, source_ref="pyramid:2:wave:0"))
    db.commit()
    srcs = {r["symbol"]: r["source"] for r in portfolio.trades_view(db)}
    assert srcs["BTC"] == "OPUS" and srcs["ETH"] == "KSS"


def test_autoapprove_max_persists_across_restart(db):
    runtime.set_autoapprove(db, enabled=True, max_notional=75.0)
    assert settings.autoapprove_max_notional == 75.0
    # simulate restart: settings reset to default, then sync from runtime_config
    settings.autoapprove_max_notional = 50.0
    settings.autoapprove_enabled = False
    runtime.sync_from_db(db)
    assert settings.autoapprove_max_notional == 75.0
    assert settings.autoapprove_enabled is True
