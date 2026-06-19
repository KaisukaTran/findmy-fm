"""Phase E: SVG charts (zero-JS) and the performance view."""

from app import charts, orders, portfolio


def test_equity_curve_svg(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "tz_offset_hours", 0)  # pin UTC for a stable assertion
    times = ["2026-06-04T09:00:00", "2026-06-04T10:30:00", "2026-06-04T12:00:00"]
    svg = charts.equity_curve_svg([10000.0, 10120.0, 10080.0], times)
    assert svg.startswith("<svg") and "polyline" in svg
    assert "polygon" in svg          # area fill
    assert "09:00" in svg and "12:00" in svg  # time axis labels (display zone)
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


def test_performance_view_expectancy_and_period(db, monkeypatch):
    """Phase 5: expectancy/profit-factor maths + the period window filter."""
    from datetime import datetime, timedelta

    from app.models import Fill

    monkeypatch.setattr("app.portfolio.get_current_prices", lambda syms: dict.fromkeys(syms, 1000.0))
    now = datetime.utcnow()
    # two recent closed trades (+200, -50) inside the 24h window
    db.add(Fill(symbol="ETH", side="SELL", quantity=1.0, price=1200.0, fee=0.0,
                realized_pnl=200.0, executed_at=now - timedelta(hours=1)))
    db.add(Fill(symbol="ETH", side="SELL", quantity=1.0, price=900.0, fee=0.0,
                realized_pnl=-50.0, executed_at=now - timedelta(hours=2)))
    # an old win (+999) outside the 24h window
    db.add(Fill(symbol="BTC", side="SELL", quantity=1.0, price=100.0, fee=0.0,
                realized_pnl=999.0, executed_at=now - timedelta(days=10)))
    db.commit()

    allp = portfolio.performance_view(db, period="all")
    assert allp["wins"] == 2 and allp["losses"] == 1
    assert allp["expectancy"] == round((200 - 50 + 999) / 3, 2)
    assert allp["profit_factor"] == round((200 + 999) / 50, 2)
    assert allp["avg_win"] == round((200 + 999) / 2, 2)
    assert allp["avg_loss"] == -50.0

    win24 = portfolio.performance_view(db, period="24h")
    assert win24["wins"] == 1 and win24["losses"] == 1 and win24["closed"] == 2
    assert win24["expectancy"] == 75.0  # (200 - 50) / 2
