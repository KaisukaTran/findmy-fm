"""
FROZEN KSS contract — golden values that lock the Pyramid DCA math.

If any of these fail, a refactor changed KSS behavior: STOP and restore
app/kss/pyramid.py. The strategy math must never change (see kss-spec skill).
Build features AROUND pyramid.py, never inside it.
"""

import pytest

from app.kss.pyramid import PyramidSession, PyramidSessionStatus

_EX = {"minQty": 0.00001, "stepSize": 0.00001, "maxQty": 10000.0}


@pytest.fixture
def session(monkeypatch):
    monkeypatch.setattr("app.kss.pyramid.get_exchange_info", lambda s: _EX)
    monkeypatch.setattr("app.kss.pyramid.get_current_prices", lambda syms: {"BTC": 50000.0})
    s = PyramidSession(
        symbol="BTC", entry_price=50000.0, distance_pct=2.0, max_waves=5,
        isolated_fund=1000.0, tp_pct=3.0, timeout_x_min=30.0, gap_y_min=5.0,
    )
    s.id = 7
    return s


def test_status_values_frozen():
    assert {s.value for s in PyramidSessionStatus} == {
        "pending", "active", "stopped", "completed", "tp_triggered"
    }


def test_pip_size_and_wave_qty_frozen(session):
    assert session.pip_size == pytest.approx(0.00002)          # pip_multiplier(2) × minQty
    assert session.generate_wave(0).quantity == pytest.approx(0.00002)  # (0+1) pip
    assert session.generate_wave(1).quantity == pytest.approx(0.00004)  # (1+1) pip
    assert session.generate_wave(4).quantity == pytest.approx(0.00010)  # (4+1) pip


def test_wave_price_geometric_frozen(session):
    assert session.generate_wave(0).target_price == pytest.approx(50000.0)        # entry
    assert session.generate_wave(1).target_price == pytest.approx(49000.0)        # ×0.98
    assert session.generate_wave(4).target_price == pytest.approx(46118.41)       # ×0.98^4, 2dp


def test_wave_order_shape_frozen(session):
    o = session._wave_to_order(session.generate_wave(2))
    assert o["side"] == "BUY" and o["order_type"] == "LIMIT" and o["source"] == "kss"
    assert o["source_ref"] == "pyramid:7:wave:2"


def test_on_fill_avg_and_tp_frozen(session):
    session.start()
    session.on_fill(0, 0.00002, 50000.0, current_market_price=50000.0)
    assert session.avg_price == pytest.approx(50000.0)
    # TP triggers exactly at avg × (1 + tp/100) = 51500
    assert session.check_tp(51499.99) is None
    res = session.check_tp(51500.0)
    assert res["action"] == "tp_triggered"
    assert res["order"]["side"] == "SELL" and res["order"]["order_type"] == "MARKET"
    assert res["order"]["source_ref"] == "pyramid:7:tp"
    assert session.status == PyramidSessionStatus.TP_TRIGGERED


def test_estimate_total_cost_frozen(session):
    # Σ qty(n)·price(n) for n=0..4 with the frozen formulas.
    expected = sum(
        session.generate_wave(n).quantity * session.generate_wave(n).target_price
        for n in range(5)
    )
    assert session.estimate_total_cost(5) == pytest.approx(expected)
