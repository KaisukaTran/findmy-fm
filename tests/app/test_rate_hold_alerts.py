"""Fix B item 3: a 418 (IP ban) / long -1015 hold currently produces ONE log line and total
silence otherwise — scanning stops (`rate_hold_active` gates the sweep), Telegram stays quiet,
the heartbeat stays green. `execution.note_rate_error` now fires a throttled Telegram "risk"
alert on the halt branch (and the -1015 long-window branch). All offline — no network, no real
keys."""

from __future__ import annotations

import ccxt
import pytest

from app import execution
from app import notify as notify_module
from app.config import settings


@pytest.fixture(autouse=True)
def _clean():
    execution.reset_client_cache()
    yield
    execution.reset_client_cache()


def _ban() -> Exception:
    exc = ccxt.DDoSProtection("binance 418 banned")
    exc.http_status_code = 418
    return exc


def _orders_exceeded(window: str) -> Exception:
    return ccxt.DDoSProtection(
        f'binance {{"code":-1015,"msg":"Too many new orders; current limit is 10 per {window}."}}'
    )


def test_first_418_sends_exactly_one_risk_event(monkeypatch):
    sent = []
    monkeypatch.setattr(notify_module, "send", lambda text, **k: sent.append(text) or True)
    monkeypatch.setattr(settings, "telegram_notify_risk", True)

    action = execution.note_rate_error(_ban())

    assert action == "halt"
    assert len(sent) == 1
    assert "418" in sent[0] or "BAN" in sent[0].upper()


def test_immediate_second_418_within_the_same_hold_does_not_re_alert(monkeypatch):
    sent = []
    monkeypatch.setattr(notify_module, "send", lambda text, **k: sent.append(text) or True)
    monkeypatch.setattr(settings, "telegram_notify_risk", True)

    execution.note_rate_error(_ban())
    execution.note_rate_error(_ban())  # same 300s floor, clock barely moved

    assert len(sent) == 1, "a second 418 landing inside the same hold must not re-alert"


def test_a_materially_longer_later_hold_fires_one_more_alert(monkeypatch):
    sent = []
    monkeypatch.setattr(notify_module, "send", lambda text, **k: sent.append(text) or True)
    monkeypatch.setattr(settings, "telegram_notify_risk", True)

    execution.note_rate_error(_ban())  # 300s floor
    execution.note_rate_error(_ban(), retry_after=3600.0)  # a much longer ban

    assert len(sent) == 2, "a hold that extends materially further out must alert again"


def test_a_generic_429_retry_never_alerts(monkeypatch):
    sent = []
    monkeypatch.setattr(notify_module, "send", lambda text, **k: sent.append(text) or True)
    monkeypatch.setattr(settings, "telegram_notify_risk", True)

    exc = ccxt.DDoSProtection("binance rate limited")
    exc.http_status_code = 429
    execution.note_rate_error(exc, retry_after=1.0)

    assert sent == [], "an ordinary short 429 retry must not page anyone"


def test_a_short_orders_exceeded_window_does_not_alert(monkeypatch):
    sent = []
    monkeypatch.setattr(notify_module, "send", lambda text, **k: sent.append(text) or True)
    monkeypatch.setattr(settings, "telegram_notify_risk", True)

    execution.note_rate_error(_orders_exceeded("10 seconds"))

    assert sent == [], "a 10s orders-budget breach clears itself before anyone could act"


def test_a_long_orders_exceeded_window_alerts(monkeypatch):
    sent = []
    monkeypatch.setattr(notify_module, "send", lambda text, **k: sent.append(text) or True)
    monkeypatch.setattr(settings, "telegram_notify_risk", True)

    action = execution.note_rate_error(_orders_exceeded("1 DAY"))

    assert action == "orders_exceeded"
    assert len(sent) == 1
    assert "1015" in sent[0] or "orders" in sent[0].lower()


def test_a_notify_failure_never_raises_into_the_caller(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("telegram is down")

    monkeypatch.setattr(notify_module, "send", _boom)
    monkeypatch.setattr(settings, "telegram_notify_risk", True)

    action = execution.note_rate_error(_ban())  # must not raise

    assert action == "halt"
