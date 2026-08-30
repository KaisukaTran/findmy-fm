"""The exchange client must survive clock drift and back off instead of hammering.

Three faults the Binance audit found in how we talk to the venue:

* **No clock sync.** ccxt's binance defaults leave `adjustForTimeDifference` off. Binance
  rejects any request whose timestamp is more than 1s ahead of server time with -1021, and a
  Windows host syncs its clock weekly. When it drifts, EVERY signed call fails — placements,
  cancels, and the reconciliation that books fills — so exits stop with positions open, and
  nothing retries or alerts.
* **A brand-new ccxt client per call.** `enableRateLimit`'s throttle is per instance, so it
  reset on every call and paced nothing; and every ccxt unified method starts with
  `load_markets()`, which on a cold instance is 3 HTTP requests (weight 20) — on EVERY order,
  cancel and fetch.
* **The rate-limit guard was dead code.** `classify_rate_error` and `weight_backoff_seconds`
  existed with no production caller, so a 429 or 418 arrived as a generic exception, was
  swallowed by a broad handler and retried next cycle — which is how a 2-minute Binance ban
  escalates toward a 3-day one.
"""

from __future__ import annotations

import ccxt
import pytest

from app import execution
from app.config import settings


def _venue_error(msg: str, status: int) -> Exception:
    """A real ccxt rate error carries the HTTP status; the classifier keys on that."""
    exc = ccxt.DDoSProtection(msg)
    exc.http_status_code = status
    return exc


@pytest.fixture(autouse=True)
def _reset_client_cache():
    execution.reset_client_cache()
    yield
    execution.reset_client_cache()


class _FakeCcxt:
    """Counts constructions so we can prove the client is reused."""

    built = 0

    def __init__(self, cfg):
        type(self).built += 1
        self.cfg = cfg
        self.sandboxed = False

    def set_sandbox_mode(self, on):
        self.sandboxed = on


def _patch_ccxt(monkeypatch):
    _FakeCcxt.built = 0
    monkeypatch.setattr(execution.ccxt, "binance", _FakeCcxt, raising=False)
    monkeypatch.setattr(settings, "live_exchange", "binance")
    monkeypatch.setattr(settings, "live_api_key", "k")
    monkeypatch.setattr(settings, "live_api_secret", "s")
    monkeypatch.setattr(settings, "live_use_testnet", True)


def test_the_client_is_built_once_and_reused(monkeypatch):
    _patch_ccxt(monkeypatch)

    a, b = execution._client(), execution._client()

    assert a is b
    assert _FakeCcxt.built == 1, "a fresh client per call reloads markets and paces nothing"


def test_the_client_asks_ccxt_to_correct_clock_drift(monkeypatch):
    _patch_ccxt(monkeypatch)

    ex = execution._client()

    assert ex.cfg.get("options", {}).get("adjustForTimeDifference") is True
    assert ex.cfg.get("options", {}).get("recvWindow") or ex.cfg.get("recvWindow")


def test_switching_between_testnet_and_real_gives_different_clients(monkeypatch):
    """Never hand a real-money client to code that asked for the sandbox."""
    _patch_ccxt(monkeypatch)
    testnet = execution._client()

    monkeypatch.setattr(settings, "live_use_testnet", False)
    real = execution._client()

    assert real is not testnet
    assert testnet.sandboxed is True and real.sandboxed is False


# --- rate limits: classify and back off, do not hammer ----------------------


def test_a_429_is_recognised_as_rate_limited():
    action, wait = execution.classify_rate_error(_venue_error("429 Too Many Requests", 429))
    assert action == "retry" and wait


def test_a_418_is_recognised_as_a_ban():
    action, _ = execution.classify_rate_error(_venue_error("418 I'm a teapot", 418))
    assert action == "halt"


def test_an_ordinary_error_is_neither():
    action, _ = execution.classify_rate_error(Exception('{"code":-1013,"msg":"Filter failure"}'))
    assert action == "raise"


def test_a_ban_halts_placement_instead_of_retrying(monkeypatch):
    """A 418 means we are already banned; the next request must not be sent at all."""
    _patch_ccxt(monkeypatch)
    execution.note_rate_error(_venue_error("418 banned", 418), retry_after=120)

    assert execution.rate_limited_until() > 0
    with pytest.raises(execution.RateLimited):
        execution.assert_not_rate_limited(urgent=False)


def test_a_ban_pauses_far_longer_than_a_rate_limit(monkeypatch):
    """429 is "slow down"; 418 is "you are banned". They must not cost the same wait — and a
    Retry-After of 0 still gets a floor, because retrying instantly is what earns the ban."""
    _patch_ccxt(monkeypatch)
    execution.note_rate_error(_venue_error("429", 429), retry_after=0)
    short = execution.rate_limited_until()

    execution.reset_client_cache()
    execution.note_rate_error(_venue_error("418 banned", 418))
    long = execution.rate_limited_until()

    assert short > 0, "even a zero Retry-After keeps a minimum pause"
    assert long > short


def test_an_ordinary_error_does_not_halt_anything(monkeypatch):
    _patch_ccxt(monkeypatch)
    execution.note_rate_error(Exception("some venue hiccup"), retry_after=None)

    execution.assert_not_rate_limited(urgent=False)
