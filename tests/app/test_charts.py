"""Phase E: SVG charts (zero-JS) and the performance view."""

from app import charts, orders, portfolio


def test_equity_curve_svg():
    times = ["2026-06-04T09:00:00", "2026-06-04T10:30:00", "2026-06-04T12:00:00"]
    svg = charts.equity_curve_svg([10000.0, 10120.0, 10080.0], times)
    assert svg.startswith("<svg") and "polyline" in svg
    assert "polygon" in svg          # area fill
    assert "09:00" in svg and "12:00" in svg  # time axis labels
    assert "10,120.00" in svg        # value tick (##,###.##)
    assert charts.equity_curve_svg([]).startswith("<p")  # empty -> placeholder


def test_winloss_bars_svg():
    assert "win-rate 75%" in charts.winloss_bars_svg(3, 1)
    assert charts.winloss_bars_svg(0, 0).startswith("<p")


def test_pyramid_ladder_svg():
    status = {
        "entry_price": 100, "avg_price": 98, "estimated_tp_price": 101, "current_price": 99,
        "waves": [{"target_price": 100, "status": "filled"}, {"target_price": 96, "status": "sent"}],
    }
    svg = charts.pyramid_ladder_svg(status)
    assert svg.startswith("<svg") and "now" in svg and "tp" in svg


def test_performance_view(db, monkeypatch):
    monkeypatch.setattr("app.portfolio.get_current_prices", lambda syms: dict.fromkeys(syms, 1000.0))
    b, _ = orders.queue_order(db, symbol="ETH", side="BUY", quantity=1.0, price=1000.0)
    orders.approve_order(db, b.id)
    s, _ = orders.queue_order(db, symbol="ETH", side="SELL", quantity=1.0, price=1200.0)
    orders.approve_order(db, s.id)

    p = portfolio.performance_view(db)
    assert p["wins"] == 1 and p["losses"] == 0 and p["win_rate"] == 100.0
    assert len(p["equity_curve"]) >= 3
    assert len(p["equity_times"]) == len(p["equity_curve"])  # aligned for the time axis
    assert p["realized_pnl"] > 0
    assert p["max_drawdown_pct"] >= 0.0
