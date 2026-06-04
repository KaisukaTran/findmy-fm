"""Scanner orchestration tests with a fake offline provider."""

import pytest

from app import models, scanner
from app.config import settings

_DAY = 86_400_000


def _uptrend(n=60, start=100.0, vol=1e6):
    out, price = [], start
    for d in range(n):
        out.append({"ts": d * _DAY, "open": price, "high": price,
                    "low": price * 0.999, "close": price, "volume": vol})
        price *= 1.01
    return out


class _FakeProvider:
    def __init__(self):
        self._candles = {"BTC": _uptrend()}

    def get_ohlcv(self, symbol, timeframe="1d", limit=200):
        return self._candles.get(symbol, [])

    def top_symbols(self, n=10):
        return []

    def all_symbols(self, min_quote_volume=0.0):
        return ["BTC"]

    def get_prices(self, symbols):
        return {s: self._candles[s][-1]["close"] for s in symbols if s in self._candles}

    def get_exchange_info(self, symbol):
        return {"minQty": 0.00001, "stepSize": 0.00001, "maxQty": 10000.0}


@pytest.fixture
def scan_env(monkeypatch):
    monkeypatch.setattr(scanner, "data_provider", lambda: _FakeProvider())
    monkeypatch.setattr("app.kss.pyramid.get_exchange_info",
                        lambda s: {"minQty": 0.00001, "stepSize": 0.00001, "maxQty": 10000.0})
    monkeypatch.setattr("app.kss.pyramid.get_current_prices", lambda syms: dict.fromkeys(syms, 1.0))
    monkeypatch.setattr("app.orders.get_current_prices", lambda syms: dict.fromkeys(syms, 1.0))
    monkeypatch.setattr(settings, "watchlist", ["BTC"])
    monkeypatch.setattr(settings, "scan_top_n", 0)
    monkeypatch.setattr(settings, "min_confidence", 0.0)
    monkeypatch.setattr(settings, "min_win_rate", 0.0)
    monkeypatch.setattr(settings, "auto_trade", False)


def test_scan_persists_audit_and_creates_pending_session(db, scan_env):
    scanner.run_scan(db, mode="semi")

    assert db.query(models.ScanRun).count() == 1
    assert db.query(models.AgentVoteRecord).filter_by(symbol="BTC").count() == 6  # 5 signal (incl. ml) + backtest

    cand = db.query(models.Candidate).filter_by(symbol="BTC").one()
    assert cand.decision == "trade" and cand.session_id is not None

    # semi-auto: wave 0 is queued for approval, NOT executed
    sess = db.get(models.KssSession, cand.session_id)
    assert sess.status == models.SESSION_ACTIVE and sess.deadline_at is not None
    assert db.query(models.PendingOrder).filter_by(status=models.PENDING).count() >= 1
    assert db.query(models.Fill).count() == 0

    actions = {a.action for a in db.query(models.AuditLog).all()}
    assert {"scan_start", "candidate", "session_open"} <= actions


def test_full_auto_executes(db, scan_env, monkeypatch):
    monkeypatch.setattr(settings, "auto_trade", True)
    scanner.run_scan(db)  # mode defaults to "auto"

    assert db.query(models.Fill).count() >= 1  # wave 0 auto-approved + filled
    assert db.query(models.AuditLog).filter_by(action="auto_approve").count() == 1


def test_high_thresholds_skip(db, scan_env, monkeypatch):
    monkeypatch.setattr(settings, "min_win_rate", 101.0)  # impossible
    scanner.run_scan(db, mode="semi")
    cand = db.query(models.Candidate).filter_by(symbol="BTC").one()
    assert cand.decision == "skip" and cand.session_id is None
    assert db.query(models.KssSession).count() == 0
