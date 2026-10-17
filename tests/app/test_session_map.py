"""Session map ("Bản đồ phiên"): pure helpers + the /partials/session-map route. No network."""

from __future__ import annotations

import re
import subprocess
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import execution, session_map
from app.clock import utcnow
from app.data import candle_cache
from app.kss import chart_data, service
from app.main import app
from app.models import KssWave

H = 3600


def _inp(**kw) -> dict:
    base = {"id": 1, "symbol": "ABC", "filled": 3, "max_waves": 8, "entry": 100.0, "avg": 90.0,
            "tp": 99.0, "next_price": 80.0, "next_qty": 2.0, "qty": 10.0, "invested": 900.0,
            "created_ts": 1_000_000}
    base.update(kw)
    return base


# --- pure helpers -----------------------------------------------------------


def test_pct_maths_and_guards():
    assert session_map.pct(110.0, 100.0) == pytest.approx(10.0)
    assert session_map.pct(80.0, 100.0) == pytest.approx(-20.0)
    assert session_map.pct(None, 100.0) is None
    assert session_map.pct(100.0, 0.0) is None
    assert session_map.pct(0.0, 100.0) is None


@pytest.mark.parametrize("p,expect", [
    (10.0, (10.0, 0)), (-25.0, (-25.0, 0)), (25.0, (25.0, 0)),
    (40.0, (25.0, 1)), (-60.0, (-25.0, -1)), (None, (None, 0)),
])
def test_clamp_axis(p, expect):
    assert session_map.clamp_axis(p) == expect


def test_build_row_distances():
    r = session_map.build_row(_inp(), price=90.0, btc_now=110.0, btc_open=100.0,
                              free_cash=1000.0, now_ts=1_000_000 + 2 * 86400)
    assert r["to_tp_pct"] == pytest.approx(10.0)            # 99 / 90 - 1
    assert r["to_next_pct"] == pytest.approx(-11.111, abs=1e-2)
    assert r["avg_pct"] == pytest.approx(0.0)
    assert r["entry_pct"] == pytest.approx(11.111, abs=1e-2)
    assert r["coin_pct"] == pytest.approx(-10.0)             # 90 / 100 - 1
    assert r["btc_pct"] == pytest.approx(10.0)
    assert r["vs_btc"] == pytest.approx(-20.0)
    assert r["upnl"] == pytest.approx(0.0)                   # 10 * 90 - 900
    assert r["age_days"] == pytest.approx(2.0)
    assert r["next_cost"] == pytest.approx(160.0)
    assert r["starved"] is False


def test_starved_when_next_rung_costs_more_than_free_cash():
    r = session_map.build_row(_inp(), price=90.0, btc_now=None, btc_open=None,
                              free_cash=159.0, now_ts=1_000_000)
    assert r["starved"] is True
    r = session_map.build_row(_inp(), price=90.0, btc_now=None, btc_open=None,
                              free_cash=160.0, now_ts=1_000_000)
    assert r["starved"] is False
    # no pending rung -> nothing to starve
    r = session_map.build_row(_inp(next_price=0.0, next_qty=0.0), price=90.0, btc_now=None,
                              btc_open=None, free_cash=0.0, now_ts=1_000_000)
    assert r["starved"] is False and r["to_next_pct"] is None and r["next_cost"] is None


def test_vs_btc_missing_btc_is_none():
    r = session_map.build_row(_inp(), price=95.0, btc_now=None, btc_open=100.0,
                              free_cash=0.0, now_ts=1_000_000)
    assert r["btc_pct"] is None and r["vs_btc"] is None and r["coin_pct"] == pytest.approx(-5.0)
    r = session_map.build_row(_inp(), price=95.0, btc_now=100.0, btc_open=None,
                              free_cash=0.0, now_ts=1_000_000)
    assert r["btc_pct"] is None and r["vs_btc"] is None


def test_missing_price_gives_none_everywhere_and_no_crash():
    r = session_map.build_row(_inp(), price=0.0, btc_now=None, btc_open=None,
                              free_cash=0.0, now_ts=1_000_000)
    assert r["to_tp_pct"] is None and r["to_next_pct"] is None and r["coin_pct"] is None
    assert r["upnl"] is None
    assert "<svg" in session_map.range_bar_svg(r)             # still renders something


def test_depth_class():
    f = session_map.depth_class
    assert [f(n) for n in (1, 2, 3, 4, 5, 9)] == [
        "depth-ok", "depth-ok", "depth-warn", "depth-warn", "depth-bad", "depth-bad"]


def test_normalize_sort_whitelist():
    assert session_map.normalize_sort("", "") == ("tp", "asc")
    assert session_map.normalize_sort("<script>", "desc") == ("tp", "asc")   # unknown -> default
    assert session_map.normalize_sort("upnl", "desc") == ("upnl", "desc")
    assert session_map.normalize_sort("upnl", "sideways") == ("upnl", "asc")
    for k in ("tp", "next", "upnl", "vsbtc", "age", "invested"):
        assert session_map.normalize_sort(k, "asc")[0] == k


def test_sort_rows_none_last_both_directions():
    rows = [{"to_tp_pct": 5.0, "id": 1}, {"to_tp_pct": None, "id": 2},
            {"to_tp_pct": 1.0, "id": 3}]
    assert [r["id"] for r in session_map.sort_rows(rows, "tp", "asc")] == [3, 1, 2]
    assert [r["id"] for r in session_map.sort_rows(rows, "tp", "desc")] == [1, 3, 2]


def test_sort_next_means_closest_first():
    rows = [{"to_next_pct": -20.0, "id": 1}, {"to_next_pct": -2.0, "id": 2}]
    assert [r["id"] for r in session_map.sort_rows(rows, "next", "asc")] == [2, 1]


def test_btc_at_picks_bar_containing_ts_and_none_when_older():
    t0 = 10 * H
    bars = [{"ts": (t0 + i * H) * 1000, "close": 100.0 + i} for i in range(5)]
    assert session_map.btc_at(bars, t0 + 2 * H + 1800) == 102.0
    assert session_map.btc_at(bars, t0 - 1) is None                       # older than the series
    assert session_map.btc_at(bars, t0 + 9 * H) is None                   # newer than the series
    assert session_map.btc_at([], t0) is None


def test_range_bar_svg_markers_and_off_scale():
    near = session_map.build_row(_inp(), price=90.0, btc_now=None, btc_open=None,
                                 free_cash=0.0, now_ts=1_000_000)
    svg = session_map.range_bar_svg(near)
    assert svg.startswith("<svg") and 'viewBox="0 0 260 22"' in svg
    for cls in ("smap-next", "smap-avg", "smap-entry", "smap-tp", "smap-now"):
        assert cls in svg
    assert "smap-off" not in svg
    far = session_map.build_row(_inp(next_price=30.0, tp=200.0), price=90.0, btc_now=None,
                                btc_open=None, free_cash=0.0, now_ts=1_000_000)
    assert far["to_next_pct"] < -25 and far["to_tp_pct"] > 25
    assert session_map.range_bar_svg(far).count("smap-off") == 2
    assert "style=" not in svg and "<script" not in svg


def test_summarize():
    mk = lambda **k: session_map.build_row(_inp(**k), price=90.0, btc_now=100.0,  # noqa: E731
                                           btc_open=100.0, free_cash=100.0, now_ts=1_000_000)
    rows = [mk(tp=92.0, next_price=70.0),                 # tp +2.2%, starved (cost 140)
            mk(tp=120.0, next_price=88.0, next_qty=1.0),  # next -2.2%, cost 88 -> funded
            mk(tp=200.0, next_price=0.0, next_qty=0.0)]
    s = session_map.summarize(rows, free_cash=100.0)
    assert s["total"] == 3 and s["near_tp"] == 1 and s["near_next"] == 1
    assert s["starved"] == 1 and s["free_cash"] == 100.0
    assert s["median_vs_btc"] == pytest.approx(-10.0)
    assert session_map.summarize([], 0.0)["median_vs_btc"] is None


# --- route ------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    monkeypatch.setattr("app.kss.pyramid.get_exchange_info",
                        lambda s: {"minQty": 1e-5, "stepSize": 1e-5, "maxQty": 1e6})
    monkeypatch.setattr("app.kss.pyramid.get_current_prices", lambda syms: dict.fromkeys(syms, 90.0))
    monkeypatch.setattr(session_map, "get_current_prices",
                        lambda syms: {s: (50000.0 if s == "BTC" else 90.0) for s in syms})
    monkeypatch.setattr(execution, "rate_hold_active", lambda: False)
    candle_cache.clear()


def _mk_session(db, symbol: str, hours_ago: float = 10.0, filled: int = 2, status="active"):
    now = utcnow()
    row = service.create_session(db, symbol=symbol, entry_price=100.0, distance_pct=7.0,
                                 max_waves=6, isolated_fund=1000.0, tp_pct=3.0,
                                 timeout_x_min=9999.0, gap_y_min=0.0)
    row.status = status
    row.created_at = now - timedelta(hours=hours_ago)
    for w in list(row.waves):
        db.delete(w)
    db.flush()
    qty = cost = 0.0
    px = 100.0
    for n in range(filled):
        db.add(KssWave(session_id=row.id, wave_num=n, quantity=1.0, target_price=px,
                       status="filled", filled_qty=1.0, filled_price=px,
                       filled_at=now - timedelta(hours=hours_ago - 1)))
        qty += 1.0
        cost += px
        px *= 0.93
    db.add(KssWave(session_id=row.id, wave_num=filled, quantity=2.0, target_price=px,
                   status="pending"))
    row.total_filled_qty, row.total_cost = qty, cost
    row.avg_price = cost / qty if qty else 0.0
    db.commit()
    return row


def _btc_candles(monkeypatch, fn=None):
    calls: list[tuple] = []

    def fake(exchange_id, symbol, timeframe, limit, factory, need_from_ms=None):
        calls.append((symbol, limit, need_from_ms))
        if fn:
            return fn(), False
        end = chart_data.to_epoch(utcnow()) // H * H
        n = limit
        return [{"ts": (end - (n - 1 - i) * H) * 1000, "open": 49000.0, "high": 51000.0,
                 "low": 48000.0, "close": 50000.0, "volume": 1.0} for i in range(n)], False

    monkeypatch.setattr(candle_cache, "get_candles", fake)
    return calls


def _get(**params):
    with TestClient(app) as c:
        return c.get("/partials/session-map", params=params)


def test_route_lists_all_active_sessions_unpaginated(monkeypatch, db):
    _btc_candles(monkeypatch)
    for i in range(23):
        _mk_session(db, f"C{i:02d}")
    _mk_session(db, "DONE", status="completed")
    r = _get()
    assert r.status_code == 200
    html = r.text
    assert html.count('data-action="viewLadder"') == 23
    assert "DONE" not in html
    assert "<script" not in html and ' style="' not in html and "<style" not in html
    for head in ("Coin", "Rung", "Bản đồ giá", "TP cách", "Rung kế cách", "Vốn đã vào",
                 "uP&amp;L", "Coin từ lúc mở", "BTC cùng kỳ", "Coin − BTC", "Tuổi"):
        assert head in html
    assert "<svg" in html


def test_route_btc_fetched_once_with_coverage(monkeypatch, db):
    calls = _btc_candles(monkeypatch)
    for i in range(3):
        _mk_session(db, f"C{i}", hours_ago=10 + i)
    assert _get().status_code == 200
    assert [c[0] for c in calls] == ["BTC"]                  # one fetch, not one per row
    _sym, limit, need = calls[0]
    assert limit <= 1500 and need is not None


def test_route_vs_btc_value_rendered(monkeypatch, db):
    _btc_candles(monkeypatch)                                 # BTC flat 50000 -> 0%
    _mk_session(db, "AAA")                                    # coin 100 -> 90 => -10%
    html = _get().text
    assert "-10.0" in html


def test_route_btc_failure_still_200_with_dashes(monkeypatch, db):
    def boom(*a, **k):
        raise RuntimeError("exchange down")

    monkeypatch.setattr(candle_cache, "get_candles", boom)
    _mk_session(db, "AAA")
    r = _get()
    assert r.status_code == 200 and "AAA" in r.text and "—" in r.text


def test_route_rate_hold_makes_no_provider_call(monkeypatch, db):
    monkeypatch.setattr(execution, "rate_hold_active", lambda: True)
    calls = _btc_candles(monkeypatch)
    _mk_session(db, "AAA")
    r = _get()
    assert r.status_code == 200 and calls == []


def test_route_hostile_sort_param_is_ignored(monkeypatch, db):
    _btc_candles(monkeypatch)
    _mk_session(db, "AAA")
    for params in ({"sort": "<script>alert(1)</script>"}, {"sort": "tp", "dir": '"><img src=x>'}):
        r = _get(**params)
        assert r.status_code == 200
        assert "<script>alert" not in r.text and "<img src=x>" not in r.text
        assert "sort=tp&dir=asc" in r.text or "sort=tp&amp;dir=asc" in r.text


def test_route_sort_order_by_upnl_desc(monkeypatch, db):
    _btc_candles(monkeypatch)
    a = _mk_session(db, "LOWC", filled=1)     # invests 100 -> at 90: -10
    b = _mk_session(db, "HIGHC", filled=2)    # invests 193 qty 2 -> 180: -13
    assert a.id != b.id
    html = _get(sort="upnl", dir="desc").text
    assert html.index("LOWC") < html.index("HIGHC")
    html = _get(sort="upnl", dir="asc").text
    assert html.index("HIGHC") < html.index("LOWC")


def test_route_empty_state(monkeypatch):
    _btc_candles(monkeypatch)
    r = _get()
    assert r.status_code == 200 and "Chưa có phiên" in r.text


# --- stable order on auto-refresh ("keep"), reorder hint, flash hooks ----------


def _order(html: str) -> list[int]:
    return [int(x) for x in re.findall(r'data-action="viewLadder" data-id="(\d+)"', html)]


def _three(db):
    """Three sessions whose upnl (desc) order is a > b > c (-9.5, -10, -13 at price 90)."""
    a = _mk_session(db, "AAA", filled=3)
    b = _mk_session(db, "BBB", filled=1)
    c = _mk_session(db, "CCC", filled=2)
    return a, b, c


def test_parse_keep_defensive():
    assert session_map.parse_keep("3, 1,x,<script>,99999999999999999999,-4,2,3") == [3, 1, 2]
    assert session_map.parse_keep("") == [] and session_map.parse_keep(None) == []
    assert len(session_map.parse_keep(",".join(str(i) for i in range(1, 2000)))) == 500


def test_route_keep_orders_rows_by_kept_sequence(monkeypatch, db):
    _btc_candles(monkeypatch)
    a, b, c = _three(db)
    keep = f"{c.id},{a.id},{b.id}"
    assert _order(_get(sort="upnl", dir="desc", keep=keep).text) == [c.id, a.id, b.id]
    # without keep the normal sort wins
    assert _order(_get(sort="upnl", dir="desc").text) == [a.id, b.id, c.id]


def test_route_keep_new_session_goes_last_and_gone_ids_drop(monkeypatch, db):
    _btc_candles(monkeypatch)
    a, b, c = _three(db)
    d = _mk_session(db, "DDD", filled=1)                 # not in keep; upnl ties A -> normal order
    keep = f"{c.id},{a.id},987654,{b.id}"
    assert _order(_get(sort="upnl", dir="desc", keep=keep).text) == [c.id, a.id, b.id, d.id]


def test_route_keep_junk_is_ignored_and_never_echoed(monkeypatch, db):
    _btc_candles(monkeypatch)
    a, b, c = _three(db)
    junk = "1,x,<script>alert(1)</script>,99999999999999999999"
    r = _get(sort="upnl", dir="desc", keep=junk)
    assert r.status_code == 200
    assert "alert(1)" not in r.text and "99999999999999999999" not in r.text
    assert len(_order(r.text)) == 3


def test_reorder_pending_flag_and_hint_control(monkeypatch, db):
    _btc_candles(monkeypatch)
    a, b, c = _three(db)
    same = f"{a.id},{b.id},{c.id}"
    ctx = session_map.build_map(db, "upnl", "desc", keep=[a.id, b.id, c.id])
    assert ctx["reorder_pending"] is False
    ctx = session_map.build_map(db, "upnl", "desc", keep=[c.id, a.id, b.id])
    assert ctx["reorder_pending"] is True
    assert session_map.build_map(db, "upnl", "desc")["reorder_pending"] is False

    ok = _get(sort="upnl", dir="desc", keep=same).text
    assert "smap-reorder" not in ok and "Sắp lại" not in ok
    html = _get(sort="upnl", dir="desc", keep=f"{c.id},{a.id},{b.id}").text
    assert "Thứ tự đã thay đổi" in html and "Sắp lại" in html
    m = re.search(r'<button[^>]*class="[^"]*smap-reorder[^"]*"[^>]*>', html)
    assert m, "reorder button missing"
    tag = m.group(0)
    assert 'hx-get="/partials/session-map?sort=upnl&amp;dir=desc"' in tag or \
           'hx-get="/partials/session-map?sort=upnl&dir=desc"' in tag
    assert "keep" not in tag
    assert 'hx-target="#smap-pane"' in tag and 'hx-swap="outerHTML"' in tag


def test_pane_markup_trigger_links_cells_and_csp(monkeypatch, db):
    _btc_candles(monkeypatch)
    a, b, c = _three(db)
    html = _get(sort="upnl", dir="desc", keep=f"{c.id},{a.id},{b.id}").text
    pane = re.search(r'<div id="smap-pane"[^>]*>', html).group(0)
    assert 'hx-trigger="every 30s"' in pane and "refresh-trading" not in pane
    assert "keep" not in pane
    for link in re.findall(r'<a class="th-sort"[^>]*>', html):
        assert "keep" not in link
    for k in ("tp", "next", "invested", "upnl", "coin", "btc", "vsbtc", "age"):
        assert f'data-sk="{k}"' in html
    assert "<script" not in html and ' style="' not in html and "<style" not in html


def test_app_js_syntax_and_hooks():
    js = Path(__file__).resolve().parents[2] / "app" / "static" / "app.js"
    text = js.read_text(encoding="utf-8")
    assert "smap-flash" in text and "keep" in text
    try:
        res = subprocess.run(["node", "--check", str(js)], capture_output=True, text=True)
    except FileNotFoundError:
        pytest.skip("node not installed")
    assert res.returncode == 0, res.stderr
