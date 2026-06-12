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
    # Neutralise the realistic-win-rate gates so these tests exercise scan MECHANICS, not the
    # statistical gates (covered in test_backtest.py / test_agents.py): every-bar trials, no
    # min-trials floor, no expectancy floor.
    monkeypatch.setattr(settings, "backtest_trial_spacing_days", 0.0)
    monkeypatch.setattr(settings, "min_trials", 0)
    monkeypatch.setattr(settings, "min_expectancy_pct", -100.0)
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


# --- universe resilience -----------------------------------------------------


def test_universe_reuses_cache_on_provider_failure(db, monkeypatch):
    """A transient all_symbols() outage must not collapse the scan to the watchlist."""
    monkeypatch.setattr(settings, "watchlist", ["BTC", "ETH", "SOL"])
    monkeypatch.setattr(settings, "min_quote_volume", 0.0)
    monkeypatch.setattr(settings, "scan_max_symbols", 50)

    class _P:
        def __init__(self, syms):
            self.syms = syms

        def all_symbols(self, min_quote_volume=0.0):
            return self.syms

    extra = [f"C{i}" for i in range(40)]
    assert len(scanner._universe(db, _P(extra))) == 43  # 3 watchlist + 40 fetched (cached)

    # provider returns nothing → reuse the cached 40, log the degradation, no collapse to 3
    u = scanner._universe(db, _P([]))
    assert len(u) == 43
    deg = db.query(models.AuditLog).filter_by(action="universe_degraded").one()
    assert '"source": "cache"' in (deg.detail or "")


# --- loss-streak block -------------------------------------------------------

from datetime import datetime, timedelta  # noqa: E402


def _close(db, symbol, pnl, days_ago=0):
    """Insert a closing SELL fill with the given realized PnL `days_ago` days back."""
    db.add(models.Fill(symbol=symbol, side="SELL", quantity=1.0, price=1.0, fee=0.0,
                        realized_pnl=pnl,
                        executed_at=datetime.utcnow() - timedelta(days=days_ago)))
    db.commit()


def test_loss_streak_blocks_after_k(db, monkeypatch):
    monkeypatch.setattr(settings, "loss_block_enabled", True)
    monkeypatch.setattr(settings, "loss_streak_block_k", 2)
    monkeypatch.setattr(settings, "loss_streak_window_days", 14)
    _close(db, "ETH", -5.0, days_ago=2)
    assert scanner._loss_streak_block(db, "ETH") == (False, 1)  # only one loss
    _close(db, "ETH", -3.0, days_ago=1)
    assert scanner._loss_streak_block(db, "ETH") == (True, 2)   # two in a row → block


def test_winning_close_breaks_streak(db, monkeypatch):
    monkeypatch.setattr(settings, "loss_block_enabled", True)
    monkeypatch.setattr(settings, "loss_streak_block_k", 2)
    monkeypatch.setattr(settings, "loss_streak_window_days", 14)
    _close(db, "ETH", -5.0, days_ago=3)
    _close(db, "ETH", -3.0, days_ago=2)
    _close(db, "ETH", +4.0, days_ago=1)  # most-recent close is a WIN
    block, streak = scanner._loss_streak_block(db, "ETH")
    assert block is False and streak == 0


def test_loss_streak_decays_outside_window(db, monkeypatch):
    monkeypatch.setattr(settings, "loss_block_enabled", True)
    monkeypatch.setattr(settings, "loss_streak_block_k", 2)
    monkeypatch.setattr(settings, "loss_streak_window_days", 14)
    _close(db, "ETH", -5.0, days_ago=40)  # both losses are older than the 14d window
    _close(db, "ETH", -3.0, days_ago=30)
    assert scanner._loss_streak_block(db, "ETH") == (False, 0)


def test_scan_skips_pair_on_loss_streak(db, scan_env, monkeypatch):
    monkeypatch.setattr(settings, "loss_block_enabled", True)
    monkeypatch.setattr(settings, "loss_streak_block_k", 2)
    _close(db, "BTC", -5.0, days_ago=2)
    _close(db, "BTC", -3.0, days_ago=1)
    scanner.run_scan(db, mode="semi")
    cand = db.query(models.Candidate).filter_by(symbol="BTC").one()
    assert cand.session_id is None and "thua 2 lần liên tiếp" in cand.reason
    assert db.query(models.AuditLog).filter_by(action="skipped_loss_streak").count() == 1


# --- Grok scanner gate -------------------------------------------------------


def test_grok_veto_blocks_open(db, scan_env, monkeypatch):
    monkeypatch.setattr("app.orchestrator.grok.scanner_enabled", lambda: True)
    monkeypatch.setattr("app.orchestrator.grok.review_candidates",
                        lambda _db, items: {"BTC": {"endorse": False, "reason": "momentum xấu"}})
    scanner.run_scan(db, mode="semi")
    cand = db.query(models.Candidate).filter_by(symbol="BTC").one()
    assert cand.session_id is None and "Grok veto" in cand.reason
    assert db.query(models.AuditLog).filter_by(action="scanner_veto").count() == 1


def test_grok_endorse_opens_with_reason(db, scan_env, monkeypatch):
    monkeypatch.setattr("app.orchestrator.grok.scanner_enabled", lambda: True)
    monkeypatch.setattr("app.orchestrator.grok.review_candidates",
                        lambda _db, items: {"BTC": {"endorse": True, "reason": "dip sâu, edge ok"}})
    scanner.run_scan(db, mode="semi")
    cand = db.query(models.Candidate).filter_by(symbol="BTC").one()
    assert cand.session_id is not None and "Grok: dip sâu" in cand.reason


def test_grok_failure_is_fail_open(db, scan_env, monkeypatch):
    # review_candidates returns {} on any error → symbol absent → treated as endorsed.
    monkeypatch.setattr("app.orchestrator.grok.scanner_enabled", lambda: True)
    monkeypatch.setattr("app.orchestrator.grok.review_candidates", lambda _db, items: {})
    scanner.run_scan(db, mode="semi")
    cand = db.query(models.Candidate).filter_by(symbol="BTC").one()
    assert cand.session_id is not None  # opened despite no Grok verdict


def test_scanner_passes_ta_bundle_to_grok(db, scan_env, monkeypatch):
    """Each candidate handed to the Grok gate carries its TA evidence bundle, and the
    compact TA tag is surfaced on the candidate reason."""
    seen: dict = {}

    def _capture(_db, items):
        seen["items"] = items
        return {}

    monkeypatch.setattr("app.orchestrator.grok.scanner_enabled", lambda: True)
    monkeypatch.setattr("app.orchestrator.grok.review_candidates", _capture)
    scanner.run_scan(db, mode="semi")

    assert seen["items"], "expected at least one gate-bound candidate"
    ta = seen["items"][0]["ta"]
    assert {"rsi", "adx", "st", "htf", "macd_h"} <= set(ta)
    cand = db.query(models.Candidate).filter_by(symbol="BTC").one()
    assert "TA: RSI" in cand.reason
