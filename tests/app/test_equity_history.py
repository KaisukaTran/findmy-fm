"""Equity history ("Dòng thời gian"): replay invariants, backfill, live writer, endpoints, UI.

No network: candles come from a fake provider injected by monkeypatching the scanner's provider
factory; prices for the live writer are monkeypatched."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import equity_history as eh
from app import execution, portfolio, scanner
from app.clock import utcnow
from app.config import settings
from app.main import app
from app.models import (
    AuditLog,
    Deposit,
    EquitySnapshot,
    Fill,
    KssSession,
    Position,
)

H = timedelta(hours=1)
T0 = datetime(2026, 9, 21, 9, 0, 0)       # an hour start


def _fill(sym, side, qty, price, fee, realized, at, ref=None):
    return {"symbol": sym, "side": side, "quantity": qty, "price": price, "fee": fee,
            "realized_pnl": realized, "executed_at": at, "source_ref": ref}


def _ses(symbol="AAA", status="active", created_at=T0, **kw):
    base = {"symbol": symbol, "status": status, "created_at": created_at, "entry_price": 100.0,
            "distance_pct": 2.0, "max_waves": 8, "isolated_fund": 1000.0, "tp_pct": 1.0,
            "timeout_x_min": 60.0, "gap_y_min": 0.0}
    base.update(kw)
    return KssSession(**base)


def _hours(n, start=T0):
    return [start + i * H for i in range(n)]


# Book: BUY 10@100 fee 1 / BUY 10@80 fee .8 / SELL 10@120 fee 1.2 -> avg 90.14, realized 297.9
def _fills():
    return [
        _fill("AAA", "BUY", 10, 100.0, 1.0, 0.0, T0 + timedelta(minutes=30)),
        _fill("AAA", "BUY", 10, 80.0, 0.8, 0.0, T0 + H + timedelta(minutes=10)),
        _fill("AAA", "SELL", 10, 120.0, 1.2, 297.9, T0 + 3 * H + timedelta(minutes=5)),
    ]


# --- replay (pure) ----------------------------------------------------------


def test_replay_final_cash_matches_ledger_identity_and_holdings():
    rows = eh.replay(_fills(), 7000.0, [(T0 + 2 * H, 500.0)], _hours(6),
                     lambda s, h: 110.0, [], lambda h: 60000.0)
    last = rows[-1]
    ledger = 7000 + 500 - (10 * 100 + 1.0) - (10 * 80 + 0.8) + (10 * 120 - 1.2)
    assert last["cash"] == pytest.approx(ledger)
    assert last["holdings"] == {"AAA": pytest.approx(10.0)}      # sum BUY - sum SELL
    assert last["realized_cum"] == pytest.approx(297.9)
    assert last["cost_basis"] == pytest.approx(900.9)            # half the 1801.8 average-cost pool
    assert last["market_value"] == pytest.approx(1100.0)
    assert last["unrealized"] == pytest.approx(1100.0 - 900.9)
    assert last["equity"] == pytest.approx(last["cash"] + 1100.0)
    assert last["btc_price"] == 60000.0


def test_replay_quiet_hour_equity_is_cash_plus_qty_times_close():
    closes = {T0 + 1 * H: 95.0, T0 + 2 * H: 97.0}
    rows = eh.replay(_fills(), 7000.0, [], _hours(3), lambda s, h: closes.get(h, 90.0), [],
                     lambda h: None)
    q = rows[2]                                  # hour T0+2h: 20 held, no fill in that hour
    assert q["holdings"] == {"AAA": pytest.approx(20.0)}
    assert q["market_value"] == pytest.approx(20 * 97.0)
    assert q["equity"] == pytest.approx(q["cash"] + 20 * 97.0)
    assert q["btc_price"] is None
    # cash = capital - cost pool (1000+1 + 800+.8)
    assert q["cash"] == pytest.approx(7000 - 1801.8)


def test_replay_hour_boundary_excludes_fill_at_next_hour_start():
    f = [_fill("AAA", "BUY", 1, 10.0, 0.0, 0.0, T0 + H)]          # exactly at the end of hour T0
    rows = eh.replay(f, 100.0, [], _hours(2), lambda s, h: 10.0, [], lambda h: None)
    assert rows[0]["holdings"] == {} and rows[0]["cash"] == pytest.approx(100.0)
    assert rows[1]["holdings"] == {"AAA": 1.0}


def test_replay_missing_candle_carries_last_close_forward_and_counts_it():
    closes = {T0: 100.0, T0 + 1 * H: 105.0}                       # no bar for T0+2h, T0+3h
    rows = eh.replay(_fills()[:2], 7000.0, [], _hours(4), lambda s, h: closes.get(h), [],
                     lambda h: None)
    assert rows[1]["fallbacks"] == 0
    assert rows[2]["market_value"] == pytest.approx(20 * 105.0)   # carried forward
    assert rows[2]["fallbacks"] == 1 and rows[2]["fallback_syms"] == ["AAA"]
    assert rows[3]["market_value"] == pytest.approx(20 * 105.0)


def test_replay_no_candle_at_all_values_at_last_fill_price():
    rows = eh.replay(_fills()[:2], 7000.0, [], _hours(3), lambda s, h: None, [],
                     lambda h: None)
    assert rows[1]["market_value"] == pytest.approx(20 * 80.0)    # last fill price
    assert rows[1]["fallbacks"] == 1


def test_replay_dust_quantity_is_flat():
    f = [_fill("AAA", "BUY", 1.0, 10.0, 0.0, 0.0, T0),
         _fill("AAA", "SELL", 1.0 - 5e-13, 10.0, 0.0, 0.0, T0 + timedelta(minutes=1))]
    rows = eh.replay(f, 100.0, [], _hours(1), lambda s, h: 50.0, [], lambda h: None)
    assert rows[0]["holdings"] == {}
    assert rows[0]["market_value"] == 0.0 and rows[0]["cost_basis"] == 0.0
    assert rows[0]["fallbacks"] == 0


def test_replay_oversell_is_clamped_like_the_position_book():
    f = [_fill("AAA", "BUY", 1.0, 10.0, 0.0, 0.0, T0),
         _fill("AAA", "SELL", 2.0, 12.0, 0.0, 2.0, T0 + timedelta(minutes=1))]
    rows = eh.replay(f, 100.0, [], _hours(1), lambda s, h: 12.0, [], lambda h: None)
    assert rows[0]["holdings"] == {} and rows[0]["cost_basis"] == 0.0
    assert rows[0]["cash"] == pytest.approx(100.0 + 2.0)          # capital + realized


def test_replay_active_sessions_from_spans():
    spans = [(T0 - H, None), (T0 + H, T0 + 3 * H), (T0 + 10 * H, None)]
    rows = eh.replay([], 100.0, [], _hours(5), lambda s, h: None, spans, lambda h: None)
    # end of hour 0 = T0+1h: span1 yes, span2 starts <= end yes (and end>) -> 2
    assert [r["active_sessions"] for r in rows] == [2, 2, 1, 1, 1]


# --- helpers for DB tests ---------------------------------------------------


NOW = datetime(2026, 9, 21, 15, 20, 0)          # last complete hour = 14:00 bar
LAST_H = datetime(2026, 9, 21, 14, 0, 0)


class FakeProvider:
    """Candles whose close equals 100 + hour-of-day for every symbol except BTC (50000)."""

    def __init__(self, skip=()):
        self.calls: list[tuple[str, str, int]] = []
        self.skip = set(skip)

    def get_ohlcv(self, symbol, timeframe, limit):
        self.calls.append((symbol, timeframe, limit))
        if symbol in self.skip:
            return []
        end = datetime(2026, 9, 21, 15, 0, 0)               # current (partial) bar
        out = []
        for k in range(limit):
            t = end - k * H
            base = 50000.0 if symbol == "BTC" else 100.0 + t.hour
            out.append({"ts": int((t - datetime(1970, 1, 1)).total_seconds() * 1000),
                        "open": base, "high": base, "low": base, "close": base, "volume": 1.0})
        return sorted(out, key=lambda c: c["ts"])


@pytest.fixture
def provider(monkeypatch):
    p = FakeProvider()
    monkeypatch.setattr(scanner, "_provider_factory", lambda exid: p)
    monkeypatch.setattr(execution, "rate_hold_active", lambda: False)
    return p


def _seed_fills(db, rows):
    for sym, side, qty, price, fee, realized, at, ref in rows:
        db.add(Fill(symbol=sym, side=side, quantity=qty, price=price, fee=fee,
                    realized_pnl=realized, executed_at=at, source_ref=ref))
    db.commit()


def _book_a(db):
    _seed_fills(db, [
        ("AAA", "BUY", 10, 100.0, 1.0, 0.0, datetime(2026, 9, 21, 9, 34), "pyramid:1:wave:0"),
        ("AAA", "BUY", 10, 80.0, 0.8, 0.0, datetime(2026, 9, 21, 10, 10), "pyramid:1:wave:1"),
        ("AAA", "SELL", 10, 120.0, 1.2, 297.9, datetime(2026, 9, 21, 12, 5), "pyramid:1:tp"),
    ])


# --- backfill -----------------------------------------------------------------


def test_backfill_writes_hours_and_is_idempotent(db, provider):
    _book_a(db)
    res = eh.backfill(db, now=NOW)
    n = db.query(EquitySnapshot).count()
    assert n == 6                                    # 09:00 .. 14:00
    assert res["hours_written"] == 6 and res["aborted"] is None
    assert res["symbols_fetched"] == 2               # BTC + AAA
    rows = db.query(EquitySnapshot).order_by(EquitySnapshot.ts).all()
    assert rows[0].ts == datetime(2026, 9, 21, 9) and rows[-1].ts == LAST_H
    assert all(r.source == "backfill" for r in rows)
    last = rows[-1]
    ledger = settings.account_equity - 1001 - 800.8 + 1198.8
    assert last.cash == pytest.approx(ledger)
    assert last.equity == pytest.approx(ledger + 10 * (100.0 + 14))
    assert last.btc_price == 50000.0
    calls = len(provider.calls)

    again = eh.backfill(db, now=NOW)
    assert db.query(EquitySnapshot).count() == n
    assert again["hours_written"] == 0 and again["hours_missing"] == 0
    assert len(provider.calls) == calls              # nothing missing -> no network
    audits = db.query(AuditLog).filter(AuditLog.action == "equity_backfill").all()
    assert len(audits) == 2 and '"hours_written": 6' in audits[0].detail


def test_backfill_never_overwrites_a_live_row(db, provider):
    _book_a(db)
    db.add(EquitySnapshot(ts=datetime(2026, 9, 21, 11), equity=1.23, cash=1.0, market_value=0.23,
                          cost_basis=0.0, unrealized=0.0, realized_cum=0.0, active_sessions=0,
                          btc_price=None, source="live"))
    db.commit()
    res = eh.backfill(db, now=NOW)
    assert res["hours_written"] == 5
    row = db.query(EquitySnapshot).filter_by(ts=datetime(2026, 9, 21, 11)).one()
    assert row.equity == 1.23 and row.source == "live"


def test_backfill_includes_deposits_and_session_counts(db, provider):
    _book_a(db)
    db.add(Deposit(amount=500.0, created_at=datetime(2026, 9, 21, 10, 30)))
    db.add(_ses(status="completed", created_at=datetime(2026, 9, 21, 9, 34)))
    db.commit()
    eh.backfill(db, now=NOW)
    rows = {r.ts.hour: r for r in db.query(EquitySnapshot).all()}
    assert rows[9].cash == pytest.approx(settings.account_equity - 1001)
    assert rows[10].cash == pytest.approx(settings.account_equity + 500 - 1001 - 800.8)
    assert rows[9].active_sessions == 1 and rows[10].active_sessions == 1
    assert rows[12].active_sessions == 0             # ended at its last SELL fill (12:05)... end


def test_backfill_missing_symbol_candles_use_fallback_and_are_reported(db, monkeypatch):
    p = FakeProvider(skip={"AAA"})
    monkeypatch.setattr(scanner, "_provider_factory", lambda exid: p)
    monkeypatch.setattr(execution, "rate_hold_active", lambda: False)
    _book_a(db)
    res = eh.backfill(db, now=NOW)
    assert res["symbols_missing"] == ["AAA"]
    assert res["hours_written"] == 6 and res["fallback_hours"] >= 1
    assert res["fallback_by_symbol"].get("AAA", 0) == res["fallback_hours"]
    r = db.query(EquitySnapshot).filter_by(ts=datetime(2026, 9, 21, 11)).one()
    assert r.market_value == pytest.approx(20 * 80.0)   # last fill price


def test_backfill_rate_hold_midway_keeps_only_fully_priced_rows(db, monkeypatch):
    p = FakeProvider()
    state = {"n": 0}
    orig = p.get_ohlcv

    def counting(symbol, tf, limit):
        state["n"] += 1
        return orig(symbol, tf, limit)

    p.get_ohlcv = counting                              # type: ignore[method-assign]
    monkeypatch.setattr(scanner, "_provider_factory", lambda exid: p)
    monkeypatch.setattr(execution, "rate_hold_active", lambda: state["n"] >= 2)  # after BTC, AAA
    _seed_fills(db, [
        ("AAA", "BUY", 1, 100.0, 0.0, 0.0, datetime(2026, 9, 21, 9, 34), None),
        ("BBB", "BUY", 1, 100.0, 0.0, 0.0, datetime(2026, 9, 21, 12, 10), None),
    ])
    res = eh.backfill(db, now=NOW)                      # must not raise
    assert res["aborted"] == "rate_hold"
    assert res["symbols_fetched"] == 2
    ts = sorted(r.ts.hour for r in db.query(EquitySnapshot).all())
    assert ts == [9, 10, 11]                            # BBB (never fetched) is held from 12:00
    a = db.query(AuditLog).filter(AuditLog.action == "equity_backfill").one()
    assert "rate_hold" in a.detail


def test_backfill_rate_hold_before_btc_writes_nothing(db, monkeypatch, provider):
    monkeypatch.setattr(execution, "rate_hold_active", lambda: True)
    _book_a(db)
    res = eh.backfill(db, now=NOW)
    assert res["aborted"] == "rate_hold" and res["hours_written"] == 0
    assert db.query(EquitySnapshot).count() == 0


def test_backfill_with_no_fills_is_a_noop(db, provider):
    res = eh.backfill(db, now=NOW)
    assert res["aborted"] == "no_fills" and db.query(EquitySnapshot).count() == 0


def test_backfill_since_limits_hours(db, provider):
    _book_a(db)
    res = eh.backfill(db, since=datetime(2026, 9, 21, 12, 30), now=NOW)
    assert res["hours_written"] == 3                    # 12:00, 13:00, 14:00
    # ...but the state still includes the earlier fills
    last = db.query(EquitySnapshot).order_by(EquitySnapshot.ts.desc()).first()
    assert last.realized_cum == pytest.approx(297.9)


# --- backfill runner / endpoints ---------------------------------------------


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _idle_backfill():
    eh._set_status(state="idle")
    yield
    deadline = 0
    while eh._run_lock.locked() and deadline < 200:
        import time
        time.sleep(0.01)
        deadline += 1
    eh._set_status(state="idle")


def test_post_backfill_returns_202_then_409_while_running(client, monkeypatch):
    gate = threading.Event()
    started = threading.Event()

    def slow(db, since=None, now=None):
        started.set()
        gate.wait(5)
        return {"hours_written": 3}

    monkeypatch.setattr(eh, "backfill", slow)
    r = client.post("/api/equity-history/backfill")
    assert r.status_code == 202 and r.json()["state"] == "running"
    assert started.wait(5)
    r2 = client.post("/api/equity-history/backfill")
    assert r2.status_code == 409
    assert r2.json()["status"]["state"] == "running"
    assert client.get("/api/equity-history/backfill").json()["state"] == "running"
    gate.set()
    for _ in range(200):
        s = client.get("/api/equity-history/backfill").json()
        if s["state"] != "running":
            break
        import time
        time.sleep(0.01)
    assert s["state"] == "done" and s["result"]["hours_written"] == 3
    # lock is released: a new one can start
    r3 = client.post("/api/equity-history/backfill")
    assert r3.status_code == 202


def test_backfill_thread_failure_sets_error_status(client, monkeypatch):
    def boom(db, since=None, now=None):
        raise RuntimeError("kaput")

    monkeypatch.setattr(eh, "backfill", boom)
    assert client.post("/api/equity-history/backfill").status_code == 202
    for _ in range(200):
        s = client.get("/api/equity-history/backfill").json()
        if s["state"] != "running":
            break
        import time
        time.sleep(0.01)
    assert s["state"] == "error" and "kaput" in s["error"]


def test_post_backfill_requires_the_api_key_when_auth_is_on(client, monkeypatch):
    monkeypatch.setattr(settings, "require_auth", True)
    r = client.post("/api/equity-history/backfill")
    assert r.status_code == 401
    assert eh.backfill_status()["state"] == "idle"


def test_post_backfill_bad_since_is_rejected(client):
    assert client.post("/api/equity-history/backfill?since=nonsense").status_code == 422


# --- live writer --------------------------------------------------------------


def _prices(monkeypatch, mapping):
    f = lambda syms, force=False: {s: mapping[s] for s in syms if s in mapping}  # noqa: E731
    monkeypatch.setattr(portfolio, "get_current_prices", f)
    monkeypatch.setattr(eh, "get_current_prices", f)


def _open_position(db):
    db.add(Position(symbol="AAA", quantity=10.0, avg_entry_price=100.0, total_cost=1000.0))
    db.add(_ses(created_at=datetime(2026, 9, 21, 9)))
    db.commit()


def test_live_writer_writes_once_per_hour_with_summary_numbers(db, monkeypatch):
    _open_position(db)
    _prices(monkeypatch, {"AAA": 90.0, "BTC": 61000.0})
    now = datetime(2026, 9, 21, 15, 3)
    row = eh.write_live_snapshot(db, now=now)
    assert row is not None and row.ts == datetime(2026, 9, 21, 14)
    s = portfolio.summary_view(db)
    assert row.equity == pytest.approx(s["total_equity"])
    assert row.cash == pytest.approx(s["cash"])
    assert row.unrealized == pytest.approx(-100.0)
    assert row.active_sessions == 1 and row.btc_price == 61000.0 and row.source == "live"
    assert eh.write_live_snapshot(db, now=now + timedelta(minutes=5)) is None
    assert db.query(EquitySnapshot).count() == 1
    nxt = eh.write_live_snapshot(db, now=datetime(2026, 9, 21, 16, 1))
    assert nxt is not None and db.query(EquitySnapshot).count() == 2


def test_live_writer_skips_when_too_late_for_the_hour(db, monkeypatch):
    _prices(monkeypatch, {"BTC": 1.0})
    assert eh.write_live_snapshot(db, now=datetime(2026, 9, 21, 15, 45)) is None
    assert db.query(EquitySnapshot).count() == 0


def test_live_writer_price_failure_does_not_raise_or_write(db, monkeypatch):
    _open_position(db)

    def boom(*a, **k):
        raise RuntimeError("exchange down")

    monkeypatch.setattr(portfolio, "get_current_prices", boom)
    monkeypatch.setattr(eh, "get_current_prices", boom)
    assert eh.write_live_snapshot(db, now=datetime(2026, 9, 21, 15, 2)) is None
    assert db.query(EquitySnapshot).count() == 0


def test_live_writer_missing_position_price_writes_nothing(db, monkeypatch):
    _open_position(db)
    _prices(monkeypatch, {"BTC": 61000.0})            # AAA has no price
    assert eh.write_live_snapshot(db, now=datetime(2026, 9, 21, 15, 2)) is None
    assert db.query(EquitySnapshot).count() == 0


def test_scheduler_equity_tick_swallows_errors(monkeypatch):
    from app import scheduler

    def boom(db, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(eh, "write_live_snapshot", boom)
    scheduler._equity_snapshot_tick()                  # must not raise


def test_scheduler_equity_tick_runs_the_writer(monkeypatch):
    from app import scheduler

    seen = []
    monkeypatch.setattr(eh, "write_live_snapshot", lambda db, **k: seen.append(1))
    scheduler._equity_snapshot_tick()
    assert seen == [1]


# --- data endpoint ------------------------------------------------------------


def _snap(db, ts, equity=7000.0, cash=1000.0, btc=100.0, realized=0.0, sessions=0):
    db.add(EquitySnapshot(ts=ts, equity=equity, cash=cash, market_value=equity - cash,
                          cost_basis=equity - cash, unrealized=0.0, realized_cum=realized,
                          active_sessions=sessions, btc_price=btc, source="backfill"))


def _epoch(dt):
    return eh.to_epoch(dt)


def test_series_days_filter_and_percent_bases(db, client):
    now = eh.hour_floor(utcnow())
    base = settings.account_equity
    _snap(db, now - timedelta(days=10), equity=base, cash=base, btc=100.0)         # reset point
    _snap(db, now - timedelta(hours=3), equity=base * 0.97, cash=base * 0.09, btc=110.0,
          realized=base * 0.01, sessions=5)
    _snap(db, now - timedelta(hours=2), equity=base * 1.03, cash=base * 0.2, btc=120.0)
    db.commit()
    d7 = client.get("/api/equity-history?days=7").json()
    assert d7["count"] == 2 and d7["days"] == "7"
    assert d7["equity_pct"][0]["value"] == pytest.approx(-3.0)
    assert d7["equity_pct"][1]["value"] == pytest.approx(3.0)
    assert d7["realized_pct"][0]["value"] == pytest.approx(1.0)
    # BTC base is the FIRST snapshot overall (100), not the first in range (110)
    assert d7["btc_pct"][0]["value"] == pytest.approx(10.0)
    assert d7["btc_pct"][1]["value"] == pytest.approx(20.0)
    assert d7["cash_pct"][0]["value"] == pytest.approx(9.0 / 0.97, rel=1e-6)
    assert d7["active_sessions"][0]["value"] == 5
    assert d7["equity"][0]["value"] == pytest.approx(base * 0.97)
    assert d7["cash"][0]["value"] == pytest.approx(base * 0.09)
    assert d7["start_capital"] == base
    alld = client.get("/api/equity-history?days=all").json()
    assert alld["count"] == 3 and alld["btc_pct"][0]["value"] == 0.0
    assert client.get("/api/equity-history?days=bogus").status_code == 422


def test_series_deposits_do_not_count_as_gain(db, client):
    now = eh.hour_floor(utcnow())
    base = settings.account_equity
    db.add(Deposit(amount=1000.0, created_at=now - timedelta(hours=5)))
    _snap(db, now - timedelta(hours=8), equity=base)
    _snap(db, now - timedelta(hours=2), equity=base + 1000.0)       # only the deposit
    db.commit()
    d = client.get("/api/equity-history?days=all").json()
    assert d["equity_pct"][0]["value"] == pytest.approx(0.0)
    assert d["equity_pct"][1]["value"] == pytest.approx(0.0)


def test_series_daily_events_align_to_hourly_times(db, client):
    today0 = eh.hour_floor(utcnow()).replace(hour=0)
    d1, d2 = today0 - timedelta(days=2), today0 - timedelta(days=1)
    for k in range(0, 72):
        _snap(db, d1 + k * H)
    _seed_fills(db, [
        ("AAA", "SELL", 1, 1.0, 0.0, 1.0, d1 + timedelta(hours=5), "pyramid:1:tp"),
        ("BBB", "SELL", 1, 1.0, 0.0, 1.0, d1 + timedelta(hours=6), "pyramid:2:manual_tp"),
        ("CCC", "SELL", 1, 1.0, 0.0, -1.0, d1 + timedelta(hours=7), "pyramid:3:sl"),   # not a TP
        ("AAA", "BUY", 1, 1.0, 0.0, 0.0, d1 + timedelta(hours=8), "pyramid:1:wave:1"),
        ("AAA", "BUY", 1, 1.0, 0.0, 0.0, d2 + timedelta(hours=1), "pyramid:1:wave:2"),
        ("AAA", "BUY", 1, 1.0, 0.0, 0.0, d2 + timedelta(hours=2), "pyramid:1:wave:3"),
    ])
    db.add(AuditLog(actor="orders", action="rung_starved", entity="1",
                    detail="{}", created_at=d2 + timedelta(hours=3)))
    db.add(AuditLog(actor="orders", action="other", entity="1", created_at=d2))
    db.commit()
    d = client.get("/api/equity-history?days=all").json()
    hourly = {p["time"] for p in d["equity_pct"]}
    assert d["daily"]["tp"] == [{"time": _epoch(d1), "value": 2}]
    assert d["daily"]["rungs"] == [{"time": _epoch(d1), "value": 1}, {"time": _epoch(d2), "value": 2}]
    assert d["daily"]["starved"] == [{"time": _epoch(d2), "value": 1}]
    for series in d["daily"].values():
        for p in series:
            assert p["time"] in hourly and p["time"] % 86400 == 0


def test_series_empty_when_no_snapshots(client):
    d = client.get("/api/equity-history").json()
    assert d["count"] == 0 and d["equity_pct"] == [] and d["daily"] == {"tp": [], "rungs": [], "starved": []}


# --- UI -------------------------------------------------------------------------


def test_timeline_partial_empty_state_has_hint_and_no_chart(client):
    r = client.get("/partials/timeline")
    assert r.status_code == 200
    assert "Chưa có lịch sử" in r.text and "POST /api/equity-history/backfill" in r.text
    assert "data-timeline-chart" not in r.text


def test_timeline_partial_with_data_is_csp_clean(db, client):
    _snap(db, eh.hour_floor(utcnow()) - H)
    db.commit()
    r = client.get("/partials/timeline")
    t = r.text
    assert "data-timeline-chart" in t and "data-timeline-legend" in t
    for days in ("7", "30", "all"):
        assert f'data-action="timelineRange" data-id="{days}"' in t
    assert "7 ngày" in t and "30 ngày" in t and "Tất cả" in t
    assert "<script" not in t.lower() and " style=" not in t and "<style" not in t.lower()


def test_dashboard_places_timeline_under_session_map(client):
    html = client.get("/").text
    a = html.index("/partials/session-map")
    b = html.index("/partials/timeline")
    c = html.index("/partials/capital")
    assert a < b < c


def test_performance_panel_uses_snapshots_and_explains_win_rate(db, client):
    now = eh.hour_floor(utcnow())
    for k in range(6):
        _snap(db, now - (6 - k) * H, equity=7000.0 - 100.0 * k)
    db.add(_ses("AAA", created_at=now - H))
    db.add(_ses("BBB", created_at=now - H))
    db.commit()
    t = client.get("/partials/performance?period=all").text
    assert "Equity (giá thị trường)" in t
    assert "chỉ tính lệnh đã đóng" in t and "đang mở 2 phiên" in t and "chưa chốt" in t
    assert "<svg" in t


def test_performance_panel_without_snapshots_keeps_realized_curve(client):
    t = client.get("/partials/performance?period=all").text
    assert "Equity (giá thị trường)" not in t
    assert "chỉ tính lệnh đã đóng" in t
    assert "đường cong theo lệnh đã chốt" in t.lower()


def test_curve_for_period_filters_and_appends_live_point(db):
    now = datetime(2026, 9, 21, 15, 30)
    _snap(db, datetime(2026, 9, 10), equity=1.0)
    _snap(db, datetime(2026, 9, 21, 10), equity=2.0)
    _snap(db, datetime(2026, 9, 21, 11), equity=3.0)
    db.commit()
    vals, times = eh.curve_for_period(db, "24h", now=now, live_equity=4.0)
    assert vals == [2.0, 3.0, 4.0] and times[-1] == now.isoformat()
    assert eh.curve_for_period(db, "all", now=now)[0] == [1.0, 2.0, 3.0]
    _snap(db, datetime(2026, 9, 5), equity=9.0)
    assert eh.curve_for_period(db, "30d", now=datetime(2026, 12, 1)) is None   # < 2 points
