"""Phase D: scheduler cycle, auto-fill of due KSS orders, TP management."""

import pytest

from app import models, orders, scanner, scheduler
from app.config import settings
from app.data import candle_cache
from app.kss import service

_DAY = 86_400_000


def _uptrend(n=60, start=100.0, vol=1e6):
    out, price = [], start
    for d in range(n):
        out.append({"ts": d * _DAY, "open": price, "high": price,
                    "low": price * 0.999, "close": price, "volume": vol})
        price *= 1.01
    return out


class _FakeProvider:
    def get_ohlcv(self, symbol, timeframe="1d", limit=200):
        return _uptrend() if symbol == "BTC" else []

    def all_symbols(self, min_quote_volume=0.0):
        return ["BTC"]

    def top_symbols(self, n=10):
        return ["BTC"]

    def get_prices(self, symbols):
        return dict.fromkeys(symbols, 1.0)

    def get_exchange_info(self, symbol):
        return {"minQty": 0.00001, "stepSize": 0.00001, "maxQty": 10000.0}


@pytest.fixture
def env(monkeypatch):
    # The scan fetches candles via scanner._provider_factory (CcxtProvider), NOT data_provider,
    # so patch both — otherwise the cycle prefetch hits the live exchange and a market-reactive
    # gate (entry_momentum_gate) flips BTC to 'skip' whenever real BTC is short-term down.
    _fake = _FakeProvider()
    monkeypatch.setattr(scanner, "data_provider", lambda: _fake)
    monkeypatch.setattr(scanner, "_provider_factory", lambda _xid: _fake)
    candle_cache.clear()
    monkeypatch.setattr("app.kss.pyramid.get_exchange_info",
                        lambda s: {"minQty": 0.00001, "stepSize": 0.00001, "maxQty": 10000.0})
    monkeypatch.setattr("app.kss.pyramid.get_current_prices", lambda syms: dict.fromkeys(syms, 1.0))
    monkeypatch.setattr("app.orders.get_current_prices", lambda syms: dict.fromkeys(syms, 1.0))
    monkeypatch.setattr("app.market.get_current_prices", lambda syms: dict.fromkeys(syms, 1.0))
    monkeypatch.setattr(settings, "watchlist", ["BTC"])
    monkeypatch.setattr(settings, "min_confidence", 0.0)
    monkeypatch.setattr(settings, "min_win_rate", 0.0)
    monkeypatch.setattr(settings, "max_loss_rate", 100.0)
    # Neutralise the realistic-win-rate gates so the scan trades on synthetic data.
    monkeypatch.setattr(settings, "backtest_trial_spacing_days", 0.0)
    monkeypatch.setattr(settings, "min_trials", 0)
    monkeypatch.setattr(settings, "min_expectancy_pct", -100.0)
    monkeypatch.setattr(settings, "auto_trade", True)
    monkeypatch.setattr(settings, "block_downtrend_adx", 0.0)  # off: cycle test isn't about entry timing


def _new_session(db):
    row = service.create_session(
        db, symbol="BTC", entry_price=100.0, distance_pct=2, max_waves=3,
        isolated_fund=100000, tp_pct=3, timeout_x_min=999999.0, gap_y_min=0.0,
    )
    service.start_session(db, row.id)
    return row


def test_auto_fill_due_orders(db, env):
    row = _new_session(db)
    approved = orders.auto_fill_due_orders(db)  # wave0 limit 100 ≥ price 1.0 → BUY due
    assert approved
    assert db.query(models.Fill).count() == 1
    db.refresh(row)
    assert row.total_filled_qty > 0


def test_manage_queues_tp(db, env, monkeypatch):
    row = _new_session(db)
    orders.auto_fill_due_orders(db)
    monkeypatch.setattr("app.market.get_current_prices", lambda syms: dict.fromkeys(syms, 1e9))
    triggered = service.manage_open_sessions(db)
    assert row.id in triggered
    assert db.query(models.PendingOrder).filter(
        models.PendingOrder.source_ref == f"pyramid:{row.id}:tp").count() >= 1


def test_run_cycle_full_auto(db, env):
    summary = scheduler.run_cycle(db)
    assert summary["scan_id"] is not None
    assert db.query(models.Fill).count() >= 1          # wave 0 auto-filled
    assert db.query(models.AuditLog).filter_by(action="cycle").count() == 1


# --- C4: outbound heartbeat (dead-man's switch) --------------------------

def _run_inline(target, args=()):
    """Test double for scheduler._spawn_daemon: runs synchronously so the ping is observable
    without racing a real background thread (and without touching the global threading.Thread
    that the scanner's own OHLCV fetch pool also constructs from)."""
    target(*args)


class _SpyResponse:
    """Context-manager stand-in for the object urlopen() returns — records read() and
    whether it was closed via __exit__, so a test can prove the connection is released."""

    def __init__(self):
        self.read_calls = 0
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True
        return False

    def read(self, n=-1):
        self.read_calls += 1
        return b"x"


def test_heartbeat_pings_when_url_set(db, env, monkeypatch):
    monkeypatch.setattr(settings, "heartbeat_url", "https://hc-ping.com/test-id")
    calls = []
    resp = _SpyResponse()

    def _urlopen(url, timeout=5):
        calls.append((url, timeout))
        return resp

    monkeypatch.setattr(scheduler.urllib.request, "urlopen", _urlopen)
    monkeypatch.setattr(scheduler, "_spawn_daemon", _run_inline)

    scheduler.run_cycle(db)

    assert calls == [("https://hc-ping.com/test-id", 5)]
    assert resp.read_calls == 1, "the response body must be read"
    assert resp.closed is True, "the connection must be closed via the context manager"


def test_heartbeat_off_when_url_empty(db, env, monkeypatch):
    monkeypatch.setattr(settings, "heartbeat_url", "")
    spawn_calls = []
    monkeypatch.setattr(scheduler, "_spawn_daemon",
                         lambda target, args=(): spawn_calls.append((target, args)))

    scheduler.run_cycle(db)

    assert spawn_calls == []


def test_heartbeat_ping_failure_never_breaks_the_cycle(db, env, monkeypatch):
    monkeypatch.setattr(settings, "heartbeat_url", "https://hc-ping.com/test-id")

    def _boom(url, timeout=5):
        raise OSError("network down")

    monkeypatch.setattr(scheduler.urllib.request, "urlopen", _boom)
    monkeypatch.setattr(scheduler, "_spawn_daemon", _run_inline)

    summary = scheduler.run_cycle(db)  # must not raise

    assert summary["scan_id"] is not None


def test_heartbeat_refuses_a_non_http_scheme(db, env, monkeypatch):
    """`file://`/etc must never be opened — only http/https are legitimate monitor URLs."""
    monkeypatch.setattr(settings, "heartbeat_url", "file:///etc/passwd")
    calls = []
    monkeypatch.setattr(scheduler.urllib.request, "urlopen",
                         lambda url, timeout=5: calls.append(url))
    monkeypatch.setattr(scheduler, "_spawn_daemon", _run_inline)

    scheduler.run_cycle(db)

    assert calls == [], "urlopen must never be called for a non-http(s) scheme"


def test_heartbeat_opens_and_closes_an_http_url(monkeypatch):
    resp = _SpyResponse()
    monkeypatch.setattr(scheduler.urllib.request, "urlopen", lambda url, timeout=5: resp)

    scheduler._fire_heartbeat_ping("http://hc-ping.com/test-id")

    assert resp.read_calls == 1
    assert resp.closed is True


def test_expired_veto_is_cleared_and_refilled(db, env, monkeypatch):
    """A stale Guardian veto must not deadlock a due KSS DCA wave: the TTL expires
    it, the cycle re-enables the order, and (price being due) it fills."""
    from datetime import datetime, timedelta

    monkeypatch.setattr("app.guardian.enabled", lambda: False)  # deterministic, no re-veto
    monkeypatch.setattr(settings, "guardian_veto_ttl_min", 30)
    row = _new_session(db)
    order = db.query(models.PendingOrder).filter_by(
        source_ref=f"pyramid:{row.id}:wave:0").one()
    order.auto_veto = True
    order.auto_veto_reason = "stale veto"
    order.auto_veto_at = datetime.utcnow() - timedelta(minutes=31)
    db.commit()

    scheduler.run_cycle(db)

    db.refresh(order)
    assert not order.auto_veto
    assert db.query(models.AuditLog).filter_by(action="veto_expired").count() == 1
    assert order.status == models.EXECUTED  # cleared veto → auto-filled (price due)


def test_fresh_veto_survives_within_ttl(db, env, monkeypatch):
    """A veto younger than the TTL is left in place — it still blocks auto-fill."""
    from datetime import datetime

    monkeypatch.setattr("app.guardian.enabled", lambda: False)
    monkeypatch.setattr(settings, "guardian_veto_ttl_min", 30)
    row = _new_session(db)
    order = db.query(models.PendingOrder).filter_by(
        source_ref=f"pyramid:{row.id}:wave:0").one()
    order.auto_veto = True
    order.auto_veto_reason = "fresh veto"
    order.auto_veto_at = datetime.utcnow()
    db.commit()

    scheduler.run_cycle(db)

    db.refresh(order)
    assert order.auto_veto
    assert order.status == models.PENDING  # still blocked, not filled


# --- singleton lock: only one process may run the scan loop --------------------

def test_singleton_lock_blocks_a_second_holder():
    """Acquiring the lock fails while another holder binds the same port, then succeeds
    once it is freed — this is what stops two app processes running two schedulers
    (which race scanner._can_open and overshoot max_concurrent_sessions)."""
    import socket as _socket

    saved = scheduler._lock_sock
    scheduler._lock_sock = None
    # pick a free ephemeral port and hold it (simulating the OTHER process)
    other = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    other.bind(("127.0.0.1", 0))
    other.listen(1)
    port = other.getsockname()[1]
    try:
        assert scheduler._acquire_singleton_lock(port) is False  # other holds it
        other.close()
        assert scheduler._acquire_singleton_lock(port) is True   # now free
        assert scheduler._acquire_singleton_lock(port) is True   # idempotent
    finally:
        try:
            other.close()
        except OSError:
            pass
        scheduler._release_singleton_lock()
        scheduler._lock_sock = saved


def test_distinct_lock_ports_allow_parallel_instances():
    """Two instances (paper vs live) each with a DISTINCT scheduler_lock_port can BOTH hold a lock —
    the mutex is per-port, so different ports never collide. This is what lets paper + live run side
    by side with both schedulers active."""
    import socket as _socket

    saved = scheduler._lock_sock
    scheduler._lock_sock = None
    # instance A (paper) holds an ephemeral port; instance B (live) takes a different one.
    a = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    a.bind(("127.0.0.1", 0))
    a.listen(1)
    port_a = a.getsockname()[1]
    probe = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port_b = probe.getsockname()[1]
    probe.close()
    try:
        assert port_a != port_b
        assert scheduler._acquire_singleton_lock(port_b) is True   # B gets its own lock
        # A's port is still independently held — different port = different mutex.
        s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        with pytest.raises(OSError):
            s.bind(("127.0.0.1", port_a))
        s.close()
    finally:
        a.close()
        scheduler._release_singleton_lock()
        scheduler._lock_sock = saved


def test_start_uses_configured_lock_port(monkeypatch):
    """scheduler.start() must lock on settings.scheduler_lock_port (not the hardcoded default),
    so each instance's env-set port is honoured."""
    captured = {}

    def _fake_acquire(port=scheduler._SINGLETON_PORT):
        captured["port"] = port
        return False  # False → start() returns without creating an asyncio task

    monkeypatch.setattr(scheduler, "_task", None)
    monkeypatch.setattr(scheduler, "_acquire_singleton_lock", _fake_acquire)
    monkeypatch.setattr(settings, "scheduler_lock_port", 8802)
    assert scheduler.start() is False
    assert captured["port"] == 8802
