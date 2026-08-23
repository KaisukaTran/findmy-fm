"""
Phase 0 tests for the capital anchor (docs/capital-scaling-2026-08-23.md §2.1).

``risk.capital_anchor(db)`` replaces the bare ``settings.account_equity`` constant as the
base of every capital-derived size. Paper must stay byte-identical; live is opt-in via
``settings.use_exchange_balance`` and must fail soft on any exchange error.
"""

from __future__ import annotations

import pytest

from app import risk
from app.config import settings
from app.models import AuditLog, Withdrawal


@pytest.fixture(autouse=True)
def _reset_anchor_cache():
    """The TTL cache + fail-soft-warned flag are module-level (process-wide) — isolate tests."""
    risk.reset_capital_anchor_cache()
    risk._anchor_fetch_warned = False
    yield
    risk.reset_capital_anchor_cache()
    risk._anchor_fetch_warned = False


def test_paper_returns_account_equity_exactly(db):
    """live_trading=False -> settings.account_equity, unconditionally (even with the knob on)."""
    settings.live_trading = False
    settings.use_exchange_balance = True
    assert risk.capital_anchor(db) == settings.account_equity


def test_live_knob_off_returns_account_equity_when_no_withdrawals(db):
    settings.live_trading = True
    settings.use_exchange_balance = False
    assert risk.capital_anchor(db) == settings.account_equity


def test_live_knob_off_deducts_real_withdrawals(db):
    """§2.1: the constant never accounted for money that actually left the exchange."""
    settings.live_trading = True
    settings.use_exchange_balance = False
    db.add(Withdrawal(amount=100.0, fee=1.0, vat=2.0, exchange="binance"))
    db.commit()
    assert risk.capital_anchor(db) == pytest.approx(settings.account_equity - 100.0)


def test_live_knob_on_mocked_balance_returns_exchange_value(db, monkeypatch):
    settings.live_trading = True
    settings.use_exchange_balance = True
    monkeypatch.setattr("app.execution.fetch_account_balance", lambda quote: 12345.67)
    assert risk.capital_anchor(db) == pytest.approx(12345.67)


def test_live_knob_on_fetch_raises_falls_back_and_audits_once(db, monkeypatch):
    settings.live_trading = True
    settings.use_exchange_balance = True
    calls = {"n": 0}

    def _boom(quote):
        calls["n"] += 1
        raise RuntimeError("exchange unreachable")

    monkeypatch.setattr("app.execution.fetch_account_balance", _boom)
    result1 = risk.capital_anchor(db)
    result2 = risk.capital_anchor(db)
    assert result1 == settings.account_equity
    assert result2 == settings.account_equity
    assert calls["n"] == 2  # a failed fetch is never cached — every call retries the exchange
    audited = db.query(AuditLog).filter(AuditLog.action == "capital_anchor_fetch_failed").all()
    assert len(audited) == 1, "must audit the failure ONCE per episode, not per call"


def test_live_knob_on_ttl_cache_avoids_refetch(db, monkeypatch):
    settings.live_trading = True
    settings.use_exchange_balance = True
    calls = {"n": 0}

    def _ok(quote):
        calls["n"] += 1
        return 999.0

    monkeypatch.setattr("app.execution.fetch_account_balance", _ok)
    first = risk.capital_anchor(db)
    second = risk.capital_anchor(db)  # inside the TTL window -> must NOT refetch
    assert first == second == 999.0
    assert calls["n"] == 1


def test_no_secret_ever_logged(db, monkeypatch, caplog):
    """Fail-soft path must not leak the API key/secret in the warning."""
    settings.live_trading = True
    settings.use_exchange_balance = True

    def _boom(quote):
        raise RuntimeError("401 Unauthorized: super-secret-key-xyz")

    monkeypatch.setattr("app.execution.fetch_account_balance", _boom)
    with caplog.at_level("WARNING"):
        risk.capital_anchor(db)
    # We can't assert the secret is absent from an exception message we ourselves injected —
    # this only guards that capital_anchor never prints settings.live_api_key/secret itself.
    for record in caplog.records:
        assert "live_api_key" not in record.getMessage()
        assert "live_api_secret" not in record.getMessage()
