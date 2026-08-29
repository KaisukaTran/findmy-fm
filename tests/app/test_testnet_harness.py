"""The testnet harnesses' shared helpers (scripts/testnet_lib.py).

The harnesses themselves need a real exchange, but the pieces that decide WHAT to send —
how much of the book to cross, with which flags, and when to refuse outright — are pure
logic and must hold without one. A wrong decision here trades against a real book.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_LIB = Path(__file__).resolve().parents[2] / "scripts" / "testnet_lib.py"
_spec = importlib.util.spec_from_file_location("testnet_lib", _LIB)
assert _spec and _spec.loader
testnet_lib = importlib.util.module_from_spec(_spec)
sys.modules["testnet_lib"] = testnet_lib
_spec.loader.exec_module(testnet_lib)


class FakeExchange:
    """Just enough of ccxt to record what the harness would send."""

    def __init__(self, bids: list[list[float]]):
        self.bids = bids
        self.orders: list[dict] = []

    def fetch_order_book(self, pair, depth=50):
        return {"bids": self.bids[:depth], "asks": []}

    def create_order(self, pair, order_type, side, qty, price, params=None):
        self.orders.append({"pair": pair, "type": order_type, "side": side, "quantity": qty,
                            "price": price, "params": params or {}})
        return {"id": "1", "status": "closed", "filled": qty}


def test_cross_fill_sells_the_whole_queue_at_or_above_the_rung():
    # Our rung rests at 0.0949 behind 20 units someone else bid at the same price: the venue
    # reaches ours only after that queue is consumed, so the counter order must cover both.
    ex = FakeExchange([[0.0949, 20.0], [0.0949, 158.0], [0.0948, 147.0]])

    out = testnet_lib.cross_fill(ex, "YB/USDT", 0.0949)

    assert out["quantity"] == pytest.approx(178.0)
    sent = ex.orders[0]
    assert (sent["side"], sent["type"]) == ("sell", "limit")
    assert sent["quantity"] == pytest.approx(178.0)
    assert sent["price"] == 0.0949


def test_cross_fill_ignores_bids_below_the_rung():
    ex = FakeExchange([[0.0949, 158.0], [0.0948, 147.0], [0.0947, 32274.8]])

    out = testnet_lib.cross_fill(ex, "YB/USDT", 0.0949)

    assert out["quantity"] == pytest.approx(158.0)


def test_cross_fill_partial_sells_only_the_asked_quantity():
    # A partial fill of our own rung: the venue books half now, the rest stays resting.
    ex = FakeExchange([[0.0949, 158.0]])

    out = testnet_lib.cross_fill(ex, "YB/USDT", 0.0949, qty=90.0)

    assert out["quantity"] == pytest.approx(90.0)
    assert ex.orders[0]["quantity"] == pytest.approx(90.0)


def test_cross_fill_partial_still_refuses_an_empty_book():
    ex = FakeExchange([[0.0948, 147.0]])

    with pytest.raises(RuntimeError, match="not on the book"):
        testnet_lib.cross_fill(ex, "YB/USDT", 0.0949, qty=90.0)
    assert ex.orders == []


def test_cross_fill_is_ioc_and_disables_self_trade_prevention():
    # The account default is EXPIRE_MAKER, which would expire OUR resting rung instead of
    # filling it; the taker order's mode decides, so it must say NONE. IOC keeps no part of
    # the counter order resting on the book.
    ex = FakeExchange([[0.0949, 158.0]])

    testnet_lib.cross_fill(ex, "YB/USDT", 0.0949)

    assert ex.orders[0]["params"] == {"timeInForce": "IOC", "selfTradePreventionMode": "NONE"}


def test_cross_fill_refuses_a_queue_deeper_than_the_cap():
    ex = FakeExchange([[0.0949, 10_000.0]])  # $949 — far past the cross cap

    with pytest.raises(RuntimeError, match="cross cap"):
        testnet_lib.cross_fill(ex, "YB/USDT", 0.0949, max_cross_usd=60.0)
    assert ex.orders == []  # refused BEFORE sending anything


def test_cross_fill_refuses_when_the_rung_is_not_on_the_book():
    ex = FakeExchange([[0.0948, 147.0]])  # nothing at or above the rung

    with pytest.raises(RuntimeError, match="not on the book"):
        testnet_lib.cross_fill(ex, "YB/USDT", 0.0949)
    assert ex.orders == []


def test_prepare_env_refuses_the_paper_and_live_books(tmp_path, monkeypatch):
    for name in ("findmy.db", "live.db"):
        book = tmp_path / name
        book.write_text("a real book")
        with pytest.raises(SystemExit):
            testnet_lib.prepare_env(str(book))
        assert book.exists()  # never wiped


def test_prepare_env_refuses_a_differently_cased_real_book(tmp_path):
    # Windows opens data/Live.db as the SAME file as data/live.db, so a case-sensitive guard
    # would let `--db data/Live.db` through and prepare_env would delete the live book.
    for name in ("Live.db", "LIVE.DB", "FindMy.Db"):
        book = tmp_path / name
        book.write_text("a real book")
        with pytest.raises(SystemExit):
            testnet_lib.prepare_env(str(book))
        assert book.exists()


def test_prepare_env_starts_from_an_empty_book_and_switches_the_model_on(tmp_path, monkeypatch):
    monkeypatch.delenv("MAKER_ORDERS", raising=False)
    monkeypatch.delenv("AUTO_TRADE", raising=False)
    db = tmp_path / "testnet_e2e.db"
    db.write_text("leftovers from a run that died mid-way")

    testnet_lib.prepare_env(str(db), KSS_FIRST_WAVE_USD="6")

    assert not db.exists()
    import os
    assert os.environ["DATABASE_URL"] == f"sqlite:///{db}"
    assert os.environ["MAKER_ORDERS"] == "true"
    assert os.environ["AUTO_TRADE"] == "true"
    assert os.environ["KSS_FIRST_WAVE_USD"] == "6"


def test_require_testnet_refuses_real_keys():
    class Execution:
        @staticmethod
        def live_enabled():
            return True

    class Settings:
        live_use_testnet = False

    with pytest.raises(SystemExit, match="refusing to trade with real keys"):
        testnet_lib.require_testnet(Execution, Settings)


def test_require_testnet_refuses_when_live_is_off():
    class Execution:
        @staticmethod
        def live_enabled():
            return False

    class Settings:
        live_use_testnet = True

    with pytest.raises(SystemExit, match="live is off"):
        testnet_lib.require_testnet(Execution, Settings)
