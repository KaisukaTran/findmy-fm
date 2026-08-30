"""What the exchange holds versus what the app thinks it holds.

The app's whole notion of what it owns comes from its own `Position` rows, built from fills it
booked. Anything the exchange holds that those rows do not name is invisible to every guard:
no take-profit, no stop, not counted in exposure, not in the capital base. That is how an
orphan hides — and this session already found one live (172 ARB booked into a session that had
already stopped), which only surfaced because a human looked.

So this asks the exchange directly and reports the difference. On real money it is the check
that catches an untracked position, a partially-filled order the app lost, or a manual trade.

It reports; it never trades. Selling an "untracked" asset automatically would be the worst
possible reflex — the app not knowing about something is not evidence that it should be sold.

Deliberately NOT wired into the capital anchor. Measured on the Binance TESTNET account
2026-08-31: the faucet pre-seeds ~$427,250 across hundreds of tokens (18,446 units each,
including joke assets), against an `ACCOUNT_EQUITY` of 2000. Anchoring capital to that would
size every order against money that does not represent the operator's capital at all.
"""

from __future__ import annotations

import pytest

from app import risk
from app.config import settings
from app.models import Position


@pytest.fixture(autouse=True)
def _live(monkeypatch):
    """Reconciliation only means anything against a real exchange account; one test turns this
    back off to prove the paper instance never asks."""
    monkeypatch.setattr(settings, "live_trading", True)


def _pos(db, symbol: str, qty: float, avg: float = 10.0) -> None:
    db.add(Position(symbol=symbol, quantity=qty, avg_entry_price=avg))
    db.commit()


def test_an_asset_the_app_does_not_track_is_reported(db, monkeypatch):
    """The failure this exists for: value sitting on the exchange that no guard can see."""
    monkeypatch.setattr(risk, "_exchange_balances", lambda: {"SOL": 5.0, "USDT": 1000.0})
    monkeypatch.setattr(risk, "_mark_prices", lambda syms: {"SOL": 100.0})

    got = risk.account_reconciliation(db, min_value_usd=1.0)

    assert [u["symbol"] for u in got["untracked"]] == ["SOL"]
    assert got["untracked"][0]["value_usd"] == pytest.approx(500.0)


def test_a_matching_position_is_not_reported(db, monkeypatch):
    _pos(db, "SOL", 5.0)
    monkeypatch.setattr(risk, "_exchange_balances", lambda: {"SOL": 5.0, "USDT": 1000.0})
    monkeypatch.setattr(risk, "_mark_prices", lambda syms: {"SOL": 100.0})

    got = risk.account_reconciliation(db, min_value_usd=1.0)

    assert got["untracked"] == [] and got["mismatched"] == []


def test_holding_more_than_the_app_believes_is_a_mismatch(db, monkeypatch):
    """The live ARB shape: the book says 172, the exchange says 344. The excess is unmanaged."""
    _pos(db, "ARB", 172.0)
    monkeypatch.setattr(risk, "_exchange_balances", lambda: {"ARB": 344.0, "USDT": 1000.0})
    monkeypatch.setattr(risk, "_mark_prices", lambda syms: {"ARB": 0.0865})

    got = risk.account_reconciliation(db, min_value_usd=1.0)

    assert len(got["mismatched"]) == 1
    row = got["mismatched"][0]
    assert row["symbol"] == "ARB"
    assert row["exchange_qty"] == pytest.approx(344.0)
    assert row["app_qty"] == pytest.approx(172.0)
    assert row["difference"] == pytest.approx(172.0)


def test_holding_less_than_the_app_believes_is_also_a_mismatch(db, monkeypatch):
    """The more dangerous direction — the app thinks it can sell something it does not have,
    so an exit would fail at the venue exactly when it is needed."""
    _pos(db, "SOL", 10.0)
    monkeypatch.setattr(risk, "_exchange_balances", lambda: {"SOL": 4.0, "USDT": 1000.0})
    monkeypatch.setattr(risk, "_mark_prices", lambda syms: {"SOL": 100.0})

    got = risk.account_reconciliation(db, min_value_usd=1.0)

    assert got["mismatched"][0]["difference"] == pytest.approx(-6.0)


def test_dust_is_not_reported(db, monkeypatch):
    """A testnet faucet leaves hundreds of near-worthless balances; so does real rounding."""
    monkeypatch.setattr(risk, "_exchange_balances",
                        lambda: {"XEC": 18446.0, "USDT": 1000.0})
    monkeypatch.setattr(risk, "_mark_prices", lambda syms: {"XEC": 0.000007})

    got = risk.account_reconciliation(db, min_value_usd=1.0)

    assert got["untracked"] == []


def test_the_quote_currency_is_reported_separately_not_as_an_orphan(db, monkeypatch):
    monkeypatch.setattr(risk, "_exchange_balances", lambda: {"USDT": 9919.76})
    monkeypatch.setattr(risk, "_mark_prices", lambda syms: {})

    got = risk.account_reconciliation(db, min_value_usd=1.0)

    assert got["quote_balance"] == pytest.approx(9919.76)
    assert got["untracked"] == []


def test_an_unreachable_exchange_reports_the_failure_rather_than_an_empty_book(db, monkeypatch):
    """An empty result would read as "nothing untracked", i.e. all clear — the wrong direction
    for a check whose whole job is to notice what is missing."""
    def boom():
        raise RuntimeError("network down")

    monkeypatch.setattr(risk, "_exchange_balances", boom)

    got = risk.account_reconciliation(db, min_value_usd=1.0)

    assert got["ok"] is False and "network down" in got["error"]
    assert got["untracked"] == [] and got["mismatched"] == []


def test_it_is_inert_on_paper(db, monkeypatch):
    """Paper has no exchange account; asking would be meaningless and would need live keys."""
    monkeypatch.setattr(settings, "live_trading", False)
    called: list[int] = []
    monkeypatch.setattr(risk, "_exchange_balances", lambda: called.append(1) or {})

    got = risk.account_reconciliation(db, min_value_usd=1.0)

    assert got["ok"] is False and called == []
