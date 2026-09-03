"""Close the "dead API key = total silence" gap.

Binance deletes an API key that has no IP whitelist after 90 days. It can also be revoked, or
stop working the instant the machine's public IP changes if a whitelist IS set (this box is on
a Vietnamese ISP IP that may be dynamic). When that happens EVERY signed call fails —
placements, cancels, and the reconciliation that books fills — so exits stop confirming while
positions are still open.

Before this file, the app had NO handling of authentication errors at all (zero references to
AuthenticationError/PermissionDenied/-2015/-2014/-1022/-2008 anywhere in app/), and
`app/notify.py` was only ever called on a FILL. So the failure mode was: the app looks alive,
`/health` returns {"status": "ok"}, no fills happen, and the owner is told nothing.

This covers:
  A. `execution.classify_credential_error` — recognise the ccxt exception TYPE and the numeric
     Binance body CODE, never a substring of the message (the existing `classify_rate_error`
     bug: a signed URL carries an orderId + a 13-digit timestamp, and grepping for "418" misread
     ~3% of ordinary network errors as an IP ban).
  B. `execution.note_credential_error` — alerts via Telegram, throttled to at most one message
     per `credential_alert_cooldown_min`, but keeps repeating while the condition persists
     rather than firing once and going quiet forever. NEVER gates anything — an exit must reach
     the venue exactly as fast as it otherwise would.
  C. `scheduler.check_ip_change` — a cheap, self-throttled, live-only public-IP check (the most
     likely cause once a whitelist is in place): never raises into the cycle, inert on paper,
     tolerates a failed lookup by skipping (never alerting on that), and alerts once when the IP
     actually changes.
  D. `/health` — now reports scheduler_running, last_cycle_at/last_cycle_seconds_ago, and
     credentials_ok, instead of a bare {"status": "ok"} that lies while the bot is dark.
"""

from __future__ import annotations

import time

import ccxt
import pytest
from fastapi.testclient import TestClient

from app import execution, runtime, scheduler
from app import notify as notify_module
from app.config import settings
from app.main import app as fastapi_app


@pytest.fixture(autouse=True)
def _clean():
    execution.reset_client_cache()
    scheduler.reset_ip_check_throttle()
    scheduler.stop()  # this file's own assertions on scheduler_running must not depend on order
    yield
    execution.reset_client_cache()
    scheduler.reset_ip_check_throttle()
    scheduler.stop()


def _auth_error(code: int, msg: str) -> Exception:
    """Shaped exactly as ccxt's binance.py raises it: AuthenticationError, JSON body verbatim."""
    return ccxt.AuthenticationError(f'binance {{"code":{code},"msg":"{msg}"}}')


# --- A: classification -------------------------------------------------------------------


def test_recognises_the_ccxt_authentication_error_type():
    assert execution.classify_credential_error(_auth_error(-2015, "Invalid API-key, IP, or permissions for action."))


def test_recognises_permission_denied_as_a_subclass_of_authentication_error():
    exc = ccxt.PermissionDenied('binance {"code":-2015,"msg":"Invalid API-key, IP, or permissions for action."}')
    assert execution.classify_credential_error(exc)


@pytest.mark.parametrize("code", [-2015, -2014, -1022, -2008])
def test_recognises_every_documented_credential_body_code(code):
    exc = ccxt.ExchangeError(f'binance {{"code":{code},"msg":"boom"}}')
    assert execution.classify_credential_error(exc)


def test_an_ordinary_exchange_error_is_not_a_credential_failure():
    exc = ccxt.ExchangeError('binance {"code":-1013,"msg":"Filter failure: LOT_SIZE"}')
    assert not execution.classify_credential_error(exc)


def test_a_network_error_is_not_a_credential_failure():
    assert not execution.classify_credential_error(ccxt.NetworkError("timed out"))


def test_does_not_bare_substring_match_the_message_text():
    """The exact bug class classify_rate_error already had once: a signed URL/message can
    contain digits that LOOK like a credential code without actually being one — only a real
    `"code":-2015`-shaped JSON body counts."""
    exc = ccxt.ExchangeError(
        "binance GET /api/v3/order?symbol=SOLUSDT&orderId=2015&timestamp=1735689600000"
        "&signature=deadbeef42 — connection reset"
    )
    assert not execution.classify_credential_error(exc)


# --- B: alert throttle, and that it NEVER gates anything -----------------------------------


def test_note_credential_error_returns_false_for_a_non_credential_exception():
    assert execution.note_credential_error(ccxt.NetworkError("offline")) is False


def test_note_credential_error_sends_exactly_one_telegram_alert(monkeypatch):
    sent = []
    monkeypatch.setattr(notify_module, "send", lambda text, **k: sent.append(text) or True)
    monkeypatch.setattr(settings, "telegram_notify_risk", True)

    result = execution.note_credential_error(_auth_error(-2015, "Invalid API-key, IP, or permissions for action."))

    assert result is True
    assert len(sent) == 1
    assert "credential" in sent[0].lower()


def test_repeated_failures_within_the_cooldown_do_not_spam(monkeypatch):
    sent = []
    monkeypatch.setattr(notify_module, "send", lambda text, **k: sent.append(text) or True)
    monkeypatch.setattr(settings, "telegram_notify_risk", True)
    monkeypatch.setattr(settings, "credential_alert_cooldown_min", 15.0)

    for _ in range(5):
        execution.note_credential_error(_auth_error(-2015, "dead key"))

    assert len(sent) == 1, "5 failures inside one cooldown window must produce ONE alert, not 5"


def test_the_alert_repeats_once_the_cooldown_elapses(monkeypatch):
    """It must not fire once and go quiet forever — a persisting problem keeps alerting."""
    sent = []
    monkeypatch.setattr(notify_module, "send", lambda text, **k: sent.append(text) or True)
    monkeypatch.setattr(settings, "telegram_notify_risk", True)
    monkeypatch.setattr(settings, "credential_alert_cooldown_min", 0.001)  # ~60ms

    execution.note_credential_error(_auth_error(-2015, "dead key"))
    time.sleep(0.08)
    execution.note_credential_error(_auth_error(-2015, "dead key"))

    assert len(sent) == 2, "the condition is still there — it must alert again after the cooldown"


def test_a_credential_failure_never_gates_the_rate_limit_path(monkeypatch):
    """The whole point of note_credential_error being ALERT-only: it must not touch the
    rate-limit hold that would otherwise slow a subsequent order."""
    monkeypatch.setattr(notify_module, "send", lambda *a, **k: True)

    execution.note_credential_error(_auth_error(-2015, "dead key"))

    assert execution.rate_limited_until() == 0.0
    execution.assert_not_rate_limited(urgent=False)  # must not raise


def test_a_credential_failure_does_not_block_a_sell_end_to_end(monkeypatch):
    """A SELL still reaches the venue even though the same call is failing on bad credentials —
    the exception propagates (the caller must know placement failed) but nothing HOLDS future
    calls back the way a rate-limit/order-budget breach would."""
    monkeypatch.setattr(notify_module, "send", lambda *a, **k: True)

    class _Ex:
        def create_order(self, pair, typ, side, qty, price=None, params=None):
            raise _auth_error(-2015, "Invalid API-key, IP, or permissions for action.")

    monkeypatch.setattr(execution, "_client", lambda: _Ex())
    monkeypatch.setattr(execution, "_market_filters", lambda e, p: {})

    with pytest.raises(ccxt.AuthenticationError):
        execution.place_live_order("SOL/USDT", "SELL", 1.0, 0.0, "MARKET")

    # The failure must not have installed any hold that would slow the NEXT attempt.
    assert execution.rate_limited_until() == 0.0
    execution.assert_order_budget_available(urgent=False)
    execution.assert_weight_budget_available(urgent=False)


# --- credentials_ok(): reflects CURRENT state, self-heals on the next success ----------------


def test_credentials_ok_defaults_true():
    assert execution.credentials_ok() is True


def test_credentials_ok_flips_false_on_a_credential_failure(monkeypatch):
    monkeypatch.setattr(notify_module, "send", lambda *a, **k: True)

    execution.note_credential_error(_auth_error(-2015, "dead key"))

    assert execution.credentials_ok() is False


def test_credentials_ok_self_heals_after_a_successful_signed_call(monkeypatch):
    monkeypatch.setattr(notify_module, "send", lambda *a, **k: True)
    execution.note_credential_error(_auth_error(-2015, "dead key"))
    assert execution.credentials_ok() is False

    class _Ex:
        def create_order(self, pair, typ, side, qty, price=None, params=None):
            return {"id": "1", "status": "closed", "filled": qty, "average": 10.0,
                    "amount": qty, "fee": {"cost": 0.0}}

    monkeypatch.setattr(execution, "_client", lambda: _Ex())
    monkeypatch.setattr(execution, "_market_filters", lambda e, p: {})

    execution.place_live_order("SOL/USDT", "SELL", 1.0, 0.0, "MARKET")

    assert execution.credentials_ok() is True


# --- C: public-IP change detection --------------------------------------------------------


def test_check_ip_change_is_a_noop_on_paper(db):
    calls = []

    def _boom():
        calls.append(1)
        raise AssertionError("must not run on paper")

    result = scheduler.check_ip_change(db, lookup=_boom)

    assert result is None
    assert calls == []
    assert runtime.get(db, scheduler.RUNTIME_KEY_LAST_PUBLIC_IP) is None


def test_check_ip_change_establishes_a_baseline_without_alerting(db, monkeypatch):
    monkeypatch.setattr(settings, "live_trading", True)
    sent = []
    monkeypatch.setattr(notify_module, "send", lambda text, **k: sent.append(text) or True)

    result = scheduler.check_ip_change(db, lookup=lambda: "5.5.5.5")

    assert result is None, "nothing to compare against yet — not itself a 'change'"
    assert sent == []
    assert runtime.get(db, scheduler.RUNTIME_KEY_LAST_PUBLIC_IP) == "5.5.5.5"


def test_check_ip_change_alerts_exactly_once_when_the_ip_actually_changes(db, monkeypatch):
    monkeypatch.setattr(settings, "live_trading", True)
    runtime.set(db, scheduler.RUNTIME_KEY_LAST_PUBLIC_IP, "1.1.1.1")
    sent = []
    monkeypatch.setattr(notify_module, "send", lambda text, **k: sent.append(text) or True)

    changed = scheduler.check_ip_change(db, lookup=lambda: "2.2.2.2")

    assert changed == "2.2.2.2"
    assert len(sent) == 1
    assert "1.1.1.1" in sent[0] and "2.2.2.2" in sent[0]
    assert runtime.get(db, scheduler.RUNTIME_KEY_LAST_PUBLIC_IP) == "2.2.2.2"


def test_check_ip_change_is_silent_when_the_ip_is_unchanged(db, monkeypatch):
    monkeypatch.setattr(settings, "live_trading", True)
    runtime.set(db, scheduler.RUNTIME_KEY_LAST_PUBLIC_IP, "1.1.1.1")
    sent = []
    monkeypatch.setattr(notify_module, "send", lambda text, **k: sent.append(text) or True)

    result = scheduler.check_ip_change(db, lookup=lambda: "1.1.1.1")

    assert result is None
    assert sent == []


def test_check_ip_change_skips_silently_when_the_lookup_fails(db, monkeypatch):
    """No network → skip. It must NEVER read a failed lookup as a change."""
    monkeypatch.setattr(settings, "live_trading", True)
    runtime.set(db, scheduler.RUNTIME_KEY_LAST_PUBLIC_IP, "1.1.1.1")
    sent = []
    monkeypatch.setattr(notify_module, "send", lambda text, **k: sent.append(text) or True)

    result = scheduler.check_ip_change(db, lookup=lambda: None)

    assert result is None
    assert sent == []
    assert runtime.get(db, scheduler.RUNTIME_KEY_LAST_PUBLIC_IP) == "1.1.1.1", "unchanged"


def test_check_ip_change_never_raises_when_the_lookup_itself_raises(db, monkeypatch):
    monkeypatch.setattr(settings, "live_trading", True)

    def _boom():
        raise OSError("no network")

    result = scheduler.check_ip_change(db, lookup=_boom)  # must not raise

    assert result is None


def test_check_ip_change_is_throttled_to_once_per_interval(db, monkeypatch):
    monkeypatch.setattr(settings, "live_trading", True)
    calls = []

    def _lookup():
        calls.append(1)
        return "9.9.9.9"

    scheduler.check_ip_change(db, lookup=_lookup)
    scheduler.check_ip_change(db, lookup=_lookup)  # immediately again — must be skipped

    assert calls == [1]


def test_run_cycle_actually_persists_the_ip_when_live(db, monkeypatch):
    """Wiring check: run_cycle must really call check_ip_change, not just have it exist
    unused (the exact anti-pattern `classify_rate_error` had before `note_rate_error` wired it
    in — 'the classifier already existed; nothing called it')."""
    from app import scanner as scanner_mod

    class _FakeProvider:
        def get_ohlcv(self, symbol, timeframe="1d", limit=200):
            return []

        def all_symbols(self, min_quote_volume=0.0):
            return []

        def top_symbols(self, n=10):
            return []

        def get_prices(self, symbols):
            return {}

        def get_exchange_info(self, symbol):
            return {"minQty": 0.00001, "stepSize": 0.00001, "maxQty": 10000.0}

    _fake = _FakeProvider()
    monkeypatch.setattr(scanner_mod, "data_provider", lambda: _fake)
    monkeypatch.setattr(scanner_mod, "_provider_factory", lambda _xid: _fake)
    monkeypatch.setattr(settings, "watchlist", [])
    monkeypatch.setattr(settings, "live_trading", True)
    monkeypatch.setattr(scheduler, "fetch_public_ip", lambda timeout=5.0: "3.3.3.3")

    scheduler.run_cycle(db)

    assert runtime.get(db, scheduler.RUNTIME_KEY_LAST_PUBLIC_IP) == "3.3.3.3"


def test_run_cycle_never_checks_the_ip_on_paper(db, monkeypatch):
    from app import scanner as scanner_mod

    class _FakeProvider:
        def get_ohlcv(self, symbol, timeframe="1d", limit=200):
            return []

        def all_symbols(self, min_quote_volume=0.0):
            return []

        def top_symbols(self, n=10):
            return []

        def get_prices(self, symbols):
            return {}

        def get_exchange_info(self, symbol):
            return {"minQty": 0.00001, "stepSize": 0.00001, "maxQty": 10000.0}

    _fake = _FakeProvider()
    monkeypatch.setattr(scanner_mod, "data_provider", lambda: _fake)
    monkeypatch.setattr(scanner_mod, "_provider_factory", lambda _xid: _fake)
    monkeypatch.setattr(settings, "watchlist", [])

    # A side-effecting call counter, not a raise: check_ip_change catches every exception
    # internally (by design — it must never break the cycle), so a raising stub here would be
    # swallowed and this test would pass whether or not the paper-skip guard actually exists.
    calls = []

    def _tracked(timeout=5.0):
        calls.append(1)
        return "6.6.6.6"

    monkeypatch.setattr(scheduler, "fetch_public_ip", _tracked)

    scheduler.run_cycle(db)  # settings.live_trading defaults False — must not raise

    assert calls == [], "the lookup must not run at all on paper"
    assert runtime.get(db, scheduler.RUNTIME_KEY_LAST_PUBLIC_IP) is None


# --- D: /health tells the truth ------------------------------------------------------------


@pytest.fixture
def client():
    with TestClient(fastapi_app) as c:
        yield c


def test_health_still_returns_the_existing_status_ok(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"


def test_health_reports_credentials_ok_true_by_default(client):
    body = client.get("/health").json()
    assert body["credentials_ok"] is True


def test_health_reports_credentials_ok_false_after_a_credential_failure(client, monkeypatch):
    monkeypatch.setattr(notify_module, "send", lambda *a, **k: True)
    execution.note_credential_error(_auth_error(-2015, "dead key"))

    body = client.get("/health").json()

    assert body["credentials_ok"] is False


def test_health_reports_scheduler_state(client):
    body = client.get("/health").json()

    assert "scheduler_running" in body
    assert body["scheduler_running"] is False  # not started in this test process
    assert "last_cycle_at" in body
    assert "last_cycle_seconds_ago" in body


def test_health_reports_how_long_ago_the_last_cycle_completed(client, monkeypatch):
    from datetime import timedelta

    from app.clock import utcnow

    stamp = (utcnow() - timedelta(seconds=42)).isoformat()
    monkeypatch.setattr(scheduler, "status", lambda: {
        "scheduler_running": True, "interval_min": 15, "last_cycle_at": stamp, "last_summary": {},
    })

    body = client.get("/health").json()

    assert body["scheduler_running"] is True
    assert body["last_cycle_at"] == stamp
    assert body["last_cycle_seconds_ago"] == pytest.approx(42.0, abs=2.0)


# --- 2026-09-03 hang hardening: /health.stalled — a wedged (not crashed) process --------------


def _health_status(*, last_cycle_at=None, last_guard_at=None):
    return {
        "scheduler_running": True, "interval_min": settings.scan_interval_min,
        "last_cycle_at": last_cycle_at, "last_guard_at": last_guard_at, "last_summary": {},
    }


def test_health_reports_guard_seconds_ago(client):
    body = client.get("/health").json()
    assert "guard_seconds_ago" in body
    assert "stalled" in body


def test_health_not_stalled_with_fresh_timestamps(client, monkeypatch):
    from datetime import timedelta

    from app.clock import utcnow

    now_ish = (utcnow() - timedelta(seconds=5)).isoformat()
    monkeypatch.setattr(scheduler, "status",
                         lambda: _health_status(last_cycle_at=now_ish, last_guard_at=now_ish))

    body = client.get("/health").json()

    assert body["stalled"] is False


def test_health_stalled_true_on_an_old_cycle_timestamp(client, monkeypatch):
    from datetime import timedelta

    from app.clock import utcnow

    monkeypatch.setattr(settings, "scan_interval_min", 15)  # threshold = max(3*15*60, 900) = 2700s
    old = (utcnow() - timedelta(seconds=2701)).isoformat()
    fresh = utcnow().isoformat()
    monkeypatch.setattr(scheduler, "status",
                         lambda: _health_status(last_cycle_at=old, last_guard_at=fresh))

    body = client.get("/health").json()

    assert body["stalled"] is True


def test_health_stalled_true_on_an_old_guard_timestamp(client, monkeypatch):
    from datetime import timedelta

    from app.clock import utcnow

    monkeypatch.setattr(settings, "kss_exit_check_sec", 90)  # threshold = max(10*90, 600) = 900s
    old = (utcnow() - timedelta(seconds=901)).isoformat()
    fresh = utcnow().isoformat()
    monkeypatch.setattr(scheduler, "status",
                         lambda: _health_status(last_cycle_at=fresh, last_guard_at=old))

    body = client.get("/health").json()

    assert body["stalled"] is True


def test_health_not_stalled_when_the_scheduler_never_ran(client, monkeypatch):
    """A fresh boot (neither loop has completed a pass yet) is not a stall — None means
    'hasn't run yet', not 'went silent'."""
    monkeypatch.setattr(scheduler, "status",
                         lambda: _health_status(last_cycle_at=None, last_guard_at=None))

    body = client.get("/health").json()

    assert body["stalled"] is False
    assert body["last_cycle_seconds_ago"] is None
    assert body["guard_seconds_ago"] is None
