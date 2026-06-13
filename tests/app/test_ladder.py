"""Click-to-view price ladder: enriched status, SVG labels, and the partial route."""

from __future__ import annotations

import pytest

from app import charts
from app.config import settings
from app.kss import service
from app.models import KssWave


def test_price_ladder_svg_has_all_five_labels():
    svg = charts.price_ladder_svg({
        "avg_price": 100.0, "current_price": 105.0, "next_wave_price": 98.0,
        "estimated_tp_price": 103.0, "sl_price": 92.0, "waves": [],
    })
    assert svg.startswith("<svg")
    for label in ("Giá mua TB", "Giá hiện tại", "Sóng kế tiếp", "Chốt lời", "Cắt lỗ"):
        assert label in svg
    assert charts.price_ladder_svg({}).startswith("<p")  # empty -> placeholder


def test_ladder_status_enriches(db, monkeypatch):
    monkeypatch.setattr("app.kss.pyramid.get_exchange_info",
                        lambda s: {"minQty": 0.00001, "stepSize": 0.00001, "maxQty": 1e6})
    monkeypatch.setattr("app.kss.pyramid.get_current_prices", lambda syms: {"BTC": 100.0})
    monkeypatch.setattr(settings, "sl_pct", 8.0)
    row = service.create_session(db, symbol="BTC", entry_price=100.0, distance_pct=2.0,
                                 max_waves=5, isolated_fund=1000.0, tp_pct=3.0,
                                 timeout_x_min=9999.0, gap_y_min=0.0)
    row.status = "active"
    row.avg_price = 100.0
    row.total_filled_qty = 1.0
    row.total_cost = 100.0
    db.add(KssWave(session_id=row.id, wave_num=1, quantity=1.0, target_price=98.0, status="sent"))
    db.commit()
    st = service.ladder_status(db, row.id)
    assert st["sl_price"] == pytest.approx(92.0)        # 100 × (1 − 8%)
    assert st["next_wave_price"] == 98.0
    assert st["estimated_tp_price"] == pytest.approx(103.0)  # 100 × (1 + 3%)


def test_ladder_partial_by_symbol_without_session(monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app
    with TestClient(app) as c:
        r = c.get("/partials/ladder", params={"symbol": "DOGE"})
        assert r.status_code == 200
        assert "chưa có session" in r.text
