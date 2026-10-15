"""Per-session time x price chart: JSON endpoint + the ladder partial. No network."""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app import execution
from app.clock import utcnow
from app.data import candle_cache
from app.kss import chart_data, service
from app.main import app
from app.models import Fill, KssWave

H = 3600


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    monkeypatch.setattr("app.kss.pyramid.get_exchange_info",
                        lambda s: {"minQty": 1e-5, "stepSize": 1e-5, "maxQty": 1e6})
    monkeypatch.setattr("app.kss.pyramid.get_current_prices", lambda syms: dict.fromkeys(syms, 90.0))
    monkeypatch.setattr(execution, "rate_hold_active", lambda: False)
    candle_cache.clear()


def _candles(start_ts: int, n: int, price: float, step: float = 0.0) -> list[dict]:
    out = []
    for i in range(n):
        p = price + step * i
        out.append({"ts": (start_ts + i * H) * 1000, "open": p, "high": p * 1.01,
                    "low": p * 0.99, "close": p, "volume": 1.0})
    return out


@pytest.fixture
def session(db):
    """A 7% ladder opened 10h ago: rungs 0..2 filled (100 / 93 / 86.49), rung 3 pending, TP sold."""
    now = utcnow()
    row = service.create_session(db, symbol="ABC", entry_price=100.0, distance_pct=7.0,
                                 max_waves=5, isolated_fund=1000.0, tp_pct=3.0,
                                 timeout_x_min=9999.0, gap_y_min=0.0)
    row.status = "active"
    row.created_at = now - timedelta(hours=10)
    for w in list(row.waves):
        db.delete(w)
    db.flush()
    spec = [(0, 1.0, 100.0, 9), (1, 2.0, 93.0, 6), (2, 3.0, 86.49, 3)]
    qty = cost = 0.0
    for n, q, px, h_ago in spec:
        db.add(KssWave(session_id=row.id, wave_num=n, quantity=q, target_price=px,
                       status="filled", filled_qty=q, filled_price=px,
                       filled_at=now - timedelta(hours=h_ago)))
        qty += q
        cost += q * px
    db.add(KssWave(session_id=row.id, wave_num=3, quantity=4.0, target_price=80.4357,
                   status="pending"))
    row.total_filled_qty, row.total_cost, row.avg_price = qty, cost, cost / qty
    db.add(Fill(symbol="ABC", side="SELL", quantity=qty, price=95.0, realized_pnl=12.5,
                source_ref=f"pyramid:{row.id}:tp", executed_at=now - timedelta(hours=1)))
    db.commit()
    return row


def _client_get(sid):
    with TestClient(app) as c:
        return c.get(f"/api/kss/sessions/{sid}/chart")


def _patch_candles(monkeypatch, fn):
    calls: list[str] = []

    def fake(exchange_id, symbol, timeframe, limit, factory, need_from_ms=None):
        # the chart must ask the cache to cover its whole window (see test_candle_cache_coverage)
        assert need_from_ms is not None
        calls.append(symbol)
        return fn(symbol, limit), False

    monkeypatch.setattr(candle_cache, "get_candles", fake)
    return calls


def _normal_candles(monkeypatch, session):
    base = chart_data.to_epoch(session.created_at) - 24 * H
    # 60 bars starting 40h before open: more than the window, so trimming is exercised
    def fn(symbol, limit):
        return _candles(base - 16 * H, 60, 100.0 if symbol == "ABC" else 50000.0, 1.0)
    return _patch_candles(monkeypatch, fn)


# --- pure helpers ----------------------------------------------------------


def test_wave_rows_gap_pct_for_seven_percent_ladder():
    waves = [
        {"wave_num": 1, "target_price": 93.0, "status": "pending"},
        {"wave_num": 0, "target_price": 100.0, "status": "filled", "filled_qty": 1.0,
         "filled_price": 100.0, "filled_time": "2026-10-01T10:00:00"},
        {"wave_num": 2, "target_price": 86.49, "status": "pending"},
    ]
    rows = chart_data.wave_rows(waves, 100.0)
    assert [r["n"] for r in rows] == [0, 1, 2]
    assert rows[0]["gap_pct"] is None
    assert rows[1]["gap_pct"] == pytest.approx(-7.0)
    assert rows[2]["gap_pct"] == pytest.approx(-7.0)
    assert rows[2]["from_entry_pct"] == pytest.approx(-13.51)
    assert rows[0]["filled_label"] == "01/10 10:00"
    assert rows[1]["filled_label"] == "—"


def test_avg_steps_weighted_and_pinned():
    rows = [
        {"n": 0, "filled_at": 1000, "filled_qty": 1.0, "filled_price": 100.0},
        {"n": 1, "filled_at": 2000, "filled_qty": 2.0, "filled_price": 90.0},
        {"n": 2, "filled_at": 2000, "filled_qty": 1.0, "filled_price": 80.0},  # same second
        {"n": 3, "filled_at": None, "filled_qty": 0.0, "filled_price": None},
    ]
    out = chart_data.avg_steps(rows, avg_price=90.0, now_ts=3000)
    assert [p["time"] for p in out] == [1000, 2000, 3000]
    assert out[0]["value"] == pytest.approx(100.0)
    assert out[1]["value"] == 90.0 and out[2]["value"] == 90.0


def test_rel_series_starts_at_zero_and_skips_prior_bars():
    bars = [{"time": t, "close": c} for t, c in ((0, 50.0), (H, 100.0), (2 * H, 110.0))]
    out = chart_data.rel_series(bars, open_ts=H)
    assert out == [{"time": H, "value": 0.0}, {"time": 2 * H, "value": pytest.approx(10.0)}]
    assert chart_data.rel_series([], 0) == []


# --- endpoint --------------------------------------------------------------


def test_chart_payload(monkeypatch, session):
    calls = _normal_candles(monkeypatch, session)
    r = _client_get(session.id)
    assert r.status_code == 200
    d = r.json()
    assert d["candles_error"] is None
    assert d["symbol"] == "ABC" and d["max_waves"] == 5
    assert set(calls) == {"ABC", "BTC"}

    created = d["created_at"]
    times = [c["time"] for c in d["candles"]]
    assert times == sorted(set(times))
    assert all(isinstance(t, int) for t in times)
    assert times[0] >= created - 24 * H                      # trimmed to the window
    assert set(d["candles"][0]) == {"time", "open", "high", "low", "close"}

    steps = d["avg_steps"]
    assert len(steps) >= 4                                   # 3 fills + the "now" point
    vals = [p["value"] for p in steps]
    assert all(b <= a + 1e-9 for a, b in zip(vals, vals[1:], strict=False))
    assert vals[-1] == pytest.approx(session.avg_price)
    assert [p["time"] for p in steps] == sorted({p["time"] for p in steps})

    assert d["rel"]["coin"][0]["value"] == 0.0
    assert d["rel"]["btc"][0]["value"] == 0.0
    assert d["rel"]["coin"][0]["time"] == d["rel"]["btc"][0]["time"] >= created

    gaps = {w["n"]: w["gap_pct"] for w in d["waves"]}
    assert gaps[0] is None and gaps[1] == pytest.approx(-7.0)

    assert len(d["exits"]) == 1
    assert d["exits"][0]["kind"] == "tp" and d["exits"][0]["price"] == 95.0
    assert d["exits"][0]["realized_pnl"] == 12.5


def test_btc_session_reuses_coin_series(monkeypatch, session, db):
    session.symbol = "BTC"
    db.commit()
    calls = _normal_candles(monkeypatch, session)
    d = _client_get(session.id).json()
    assert calls == ["BTC"]
    assert d["rel"]["btc"] == d["rel"]["coin"] and d["rel"]["coin"]


def test_provider_error_degrades_to_empty(monkeypatch, session):
    def boom(*a, **k):
        raise RuntimeError("exchange down")

    monkeypatch.setattr(candle_cache, "get_candles", boom)
    r = _client_get(session.id)
    assert r.status_code == 200
    d = r.json()
    assert d["candles"] == [] and d["candles_error"]
    assert d["rel"] == {"coin": [], "btc": []}
    assert len(d["waves"]) == 4                              # the rest of the payload survives


def test_rate_hold_makes_no_provider_call(monkeypatch, session):
    monkeypatch.setattr(execution, "rate_hold_active", lambda: True)
    calls = _normal_candles(monkeypatch, session)
    d = _client_get(session.id).json()
    assert calls == []
    assert d["candles"] == [] and "giới hạn" in d["candles_error"]


def test_unknown_session_is_404():
    assert _client_get(999999).status_code == 404


# --- partial ---------------------------------------------------------------


def test_ladder_partial_renders_chart_shell_and_rung_table(session):
    with TestClient(app) as c:
        r = c.get("/partials/ladder", params={"session": session.id})
    assert r.status_code == 200
    html = r.text
    assert f'data-session-chart="{session.id}"' in html
    for head in ("Rung", "Giá đặt", "Cách rung trước %", "So với giá vào %",
                 "Trạng thái", "Giá khớp", "Thời điểm khớp"):
        assert head in html
    assert "-7.00" in html
    assert "Biểu đồ: TradingView Lightweight Charts™" in html
    assert "Coin so với BTC" in html
    assert "<script" not in html
    assert ' style="' not in html
