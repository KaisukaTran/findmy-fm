"""Float-residue positions: root fix on SELL, startup normalisation, dust hidden in the table,
snapshot writer ignores priceless dust."""

from __future__ import annotations

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app import db as dbmod
from app import equity_history as eh
from app import orders, portfolio
from app.main import app
from app.models import AuditLog, Position


def _pos(db, sym, qty, cost=None, avg=10.0):
    db.add(Position(symbol=sym, quantity=qty, avg_entry_price=avg,
                    total_cost=qty * avg if cost is None else cost))
    db.commit()


def test_full_close_with_float_residue_is_exactly_flat(db):
    orders._update_position(db, "ALLO", "BUY", 104.60000000000001, 1.0, 0.0)
    realized = orders._update_position(db, "ALLO", "SELL", 104.6, 1.02, 0.0)
    pos = db.query(Position).filter_by(symbol="ALLO").one()
    assert pos.quantity == 0.0 and pos.total_cost == 0.0 and pos.avg_entry_price == 0.0
    assert realized == pytest.approx(104.6 * 0.02)


def test_partial_sell_keeps_remainder(db):
    orders._update_position(db, "ALLO", "BUY", 104.6, 1.0, 0.0)
    realized = orders._update_position(db, "ALLO", "SELL", 50.0, 1.1, 0.0)
    pos = db.query(Position).filter_by(symbol="ALLO").one()
    assert pos.quantity == pytest.approx(54.6) and pos.avg_entry_price == pytest.approx(1.0)
    assert realized == pytest.approx(5.0)


def test_normalisation_only_touches_float_residue_and_is_idempotent(db):
    _pos(db, "RES", 1.4e-14)
    _pos(db, "DUST", 0.0259)
    _pos(db, "REAL", 104.6)
    assert dbmod._clear_position_float_residue() == 1
    db.expire_all()
    res = db.query(Position).filter_by(symbol="RES").one()
    assert (res.quantity, res.total_cost, res.avg_entry_price) == (0.0, 0.0, 0.0)
    assert db.query(Position).filter_by(symbol="DUST").one().quantity == 0.0259
    assert db.query(Position).filter_by(symbol="REAL").one().quantity == 104.6
    assert dbmod._clear_position_float_residue() == 0
    assert db.query(AuditLog).filter_by(action="positions_float_residue_cleared").count() == 1


def test_dust_flag_summary_and_toggle(db, monkeypatch):
    monkeypatch.setattr(portfolio, "get_current_prices",
                        lambda syms: dict.fromkeys(syms, 1.0))
    _pos(db, "REAL", 100.0)
    _pos(db, "TINY", 0.0034)          # $0.0034 < $0.01
    s = portfolio.summary_view(db)
    assert s["positions_count"] == 1 and s["dust_positions_count"] == 1
    c = TestClient(app)
    hidden = c.get("/partials/positions").text
    assert "REAL" in hidden and "TINY" not in hidden and "Hiện bụi (1)" in hidden
    shown = c.get("/partials/positions?dust=1").text
    assert "TINY" in shown and "Ẩn bụi (1)" in shown


def test_snapshot_ignores_priceless_dust_but_not_real_position(db, monkeypatch):
    f = lambda syms, force=False: {"BTC": 61000.0}  # noqa: E731  (no position has a price)
    monkeypatch.setattr(portfolio, "get_current_prices", f)
    monkeypatch.setattr(eh, "get_current_prices", f)
    now = datetime(2026, 9, 21, 15, 3)
    _pos(db, "DUST", 0.0003, cost=0.003)
    assert eh.write_live_snapshot(db, now=now) is not None
    _pos(db, "REAL", 5.0)
    assert eh.write_live_snapshot(db, now=datetime(2026, 9, 21, 16, 3)) is None
