"""Scanner orchestration tests with a fake offline provider."""

import pytest

from app import models, scanner
from app.config import settings
from app.data import candle_cache

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
    # Route BOTH the universe provider AND the candle source through the fake. The scan
    # fetches OHLCV via scanner._provider_factory (CcxtProvider) — NOT data_provider — so
    # patching data_provider alone leaves the candle prefetch hitting the live exchange,
    # making these mechanics tests depend on the real BTC trend (a market-reactive gate like
    # entry_momentum_gate would then flip BTC to 'skip' whenever BTC is short-term down).
    _fake = _FakeProvider()
    monkeypatch.setattr(scanner, "data_provider", lambda: _fake)
    monkeypatch.setattr(scanner, "_provider_factory", lambda _xid: _fake)
    candle_cache.clear()
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
    # Neutralise the entry-timing downtrend gate here (it has its own tests) so these
    # mechanics tests aren't affected by the synthetic candles' trend.
    monkeypatch.setattr(settings, "block_downtrend_adx", 0.0)


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


def test_scan_skips_symbol_already_at_cap(db, scan_env, monkeypatch):
    """K-1 root fix: with max_sessions_per_symbol=1, a coin that already has an ACTIVE
    session never gets a 2nd — two ladders would share one Position avg (blended cost
    basis → 'take-profit that realizes a loss' / K-2 TP deadlock)."""
    monkeypatch.setattr(settings, "max_sessions_per_symbol", 1)
    db.add(models.KssSession(
        symbol="BTC", entry_price=100, distance_pct=2, max_waves=5, isolated_fund=100,
        tp_pct=3, timeout_x_min=1, gap_y_min=0, status=models.SESSION_ACTIVE,
    ))
    db.commit()
    scanner.run_scan(db, mode="semi")
    cand = db.query(models.Candidate).filter_by(symbol="BTC").one()
    assert cand.session_id is None
    assert "per-symbol" in (cand.reason or "")
    assert db.query(models.AuditLog).filter_by(action="skipped_concentration").count() >= 1
    # exactly one ACTIVE BTC session remains (no duplicate opened)
    assert db.query(models.KssSession).filter_by(
        symbol="BTC", status=models.SESSION_ACTIVE).count() == 1


def test_scan_allows_second_session_when_cap_is_two(db, scan_env, monkeypatch):
    """Sanity: cap=2 still permits a 2nd ladder (documents the knob the .env bug used)."""
    monkeypatch.setattr(settings, "max_sessions_per_symbol", 2)
    db.add(models.KssSession(
        symbol="BTC", entry_price=100, distance_pct=2, max_waves=5, isolated_fund=100,
        tp_pct=3, timeout_x_min=1, gap_y_min=0, status=models.SESSION_ACTIVE,
    ))
    db.commit()
    scanner.run_scan(db, mode="semi")
    cand = db.query(models.Candidate).filter_by(symbol="BTC").one()
    assert cand.session_id is not None  # 1 < 2 → opens a 2nd


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


# --- B2 & B1 tests -----------------------------------------------------------


def test_days_to_bars_conversion():
    """B2: _days_to_bars(365, '4h') returns ~2190, _days_to_bars(365, '1d') == 365,
    and result never goes below _MIN_CANDLES (30) for small day counts."""
    # 365 days * 6 bars/day (4h) = 2190
    assert scanner._days_to_bars(365, "4h") == 2190
    # 365 days * 1 bar/day (1d) = 365
    assert scanner._days_to_bars(365, "1d") == 365
    # Small day counts should floor to _MIN_CANDLES (30)
    assert scanner._days_to_bars(1, "1d") == 30
    assert scanner._days_to_bars(5, "1d") == 30  # 5 < 30, so floor to 30
    assert scanner._days_to_bars(60, "1d") == 60  # 60 >= 30, so keep 60


def test_open_session_resolves_settings_at_call_time(db, monkeypatch):
    """B1: _open_session resolves distance/tp/max_waves from settings at CALL time
    when those kwargs are omitted. Monkeypatch settings to sentinels and verify the
    created KssSession row uses the patched values."""
    from app.kss import service as kss_service  # noqa: F401 (import verifies availability)

    # Monkeypatch settings to sentinel values
    monkeypatch.setattr(settings, "scan_distance_pct", 2.5)
    monkeypatch.setattr(settings, "scan_tp_pct", 15.0)
    monkeypatch.setattr(settings, "scan_max_waves", 7)
    monkeypatch.setattr(settings, "scan_fund", 100.0)
    monkeypatch.setattr(settings, "deadline_days", 30)

    # Call _open_session WITHOUT passing distance_pct, tp_pct, max_waves;
    # they should be read from settings
    session_id = scanner._open_session(db, "TEST", entry=50.0, mode="semi")

    # Fetch the created session and verify it used the settings values
    sess = db.get(models.KssSession, session_id)
    assert sess is not None
    assert sess.distance_pct == 2.5
    assert sess.tp_pct == 15.0
    assert sess.max_waves == 7


# --- S2: candle cache tests --------------------------------------------------


def test_second_scan_zero_ohlcv_calls(db, scan_env, monkeypatch):  # noqa: ARG001
    """S2 acceptance: a second scan within the same bar period must make ZERO
    OHLCV network calls — all candles are served from the in-process cache.

    Strategy:
    - Replace the _FakeProvider.get_ohlcv with a counting spy so we can assert
      it is called exactly once (first scan) then zero times (second scan).
    - Patch candle_cache._provider_factory so it returns the spy provider.
    - Clear the cache before the first scan so the test is deterministic.
    """
    call_count = 0
    candles_data = _uptrend()

    class _SpyProvider:
        exchange_id = "kraken"
        quote = "USD"

        def pair(self, symbol: str) -> str:
            return f"{symbol}/USD"

        def get_ohlcv(self, _symbol, _timeframe="1d", _limit=200):
            nonlocal call_count
            call_count += 1
            return candles_data

        def all_symbols(self, _min_quote_volume=0.0):
            return ["BTC"]

        def top_symbols(self, _n=10):
            return []

        def get_prices(self, symbols):
            return dict.fromkeys(symbols, candles_data[-1]["close"])

        def get_exchange_info(self, _symbol):
            return {"minQty": 0.00001, "stepSize": 0.00001, "maxQty": 10000.0}

    spy = _SpyProvider()

    # Patch the provider factory inside candle_cache so parallel workers use our spy.
    monkeypatch.setattr(scanner, "_provider_factory", lambda exchange_id: spy)
    # Also patch data_provider so _universe + the provider reference use the spy.
    monkeypatch.setattr(scanner, "data_provider", lambda: spy)

    # S3 (cheap-gates-first) pre-blocks a symbol that already has an open session.
    # If scan 1 opened a session for BTC (which it does under default gate thresholds),
    # scan 2 would PRE-BLOCK BTC and skip it before the cache is consulted, so
    # cache_hits would be 0 for the wrong reason. Keep _can_open False so no session
    # opens — BTC is re-evaluated each scan and genuinely served from the warm cache.
    # (Makes the test independent of gate thresholds / .env.)
    monkeypatch.setattr(scanner, "_can_open", lambda *_a, **_k: (False, "test-no-open"))
    # …but let the scan RUN (the pre-scan capacity gate also consults _can_open; bypass it here
    # so we still exercise the fetch→cache path rather than short-circuiting the whole scan).
    monkeypatch.setattr(scanner, "_has_open_capacity", lambda *_a, **_k: (True, ""))

    # Clear the cache to ensure a cold start.
    candle_cache.clear()

    # First scan — should hit the network (cache cold).
    scanner.run_scan(db, mode="semi")
    calls_after_first = call_count
    assert calls_after_first >= 1, "first scan must fetch candles from the provider"

    # Second scan — cache is warm; no OHLCV network calls expected.
    call_count = 0
    scanner.run_scan(db, mode="semi")
    assert call_count == 0, (
        f"second scan within TTL must make zero OHLCV calls; got {call_count}"
    )

    # Verify the scan_cycle audit carries cache stats.
    cycle_logs = (
        db.query(models.AuditLog).filter_by(action="scan_cycle").all()
    )
    assert len(cycle_logs) >= 2
    # Second cycle: all hits, zero misses.
    last = cycle_logs[-1]
    import json as _json
    detail = _json.loads(last.detail or "{}")
    assert detail.get("cache_hits", -1) >= 1
    assert detail.get("cache_misses", -1) == 0
    assert "scan_duration_ms" in detail


# --- S3: cheap-gates-first ---------------------------------------------------


def _make_spy_provider(candles_data):
    """Return a (spy, call_count_getter) pair for OHLCV call counting."""
    state = {"calls": 0}

    class _SpyProvider:
        exchange_id = "kraken"
        quote = "USD"

        def pair(self, symbol: str) -> str:
            return f"{symbol}/USD"

        def get_ohlcv(self, _symbol, _timeframe="1d", _limit=200):
            state["calls"] += 1
            return candles_data

        def all_symbols(self, _min_quote_volume=0.0):
            return ["BTC"]

        def top_symbols(self, _n=10):
            return []

        def get_prices(self, symbols):
            return {s: candles_data[-1]["close"] for s in symbols if s in ("BTC",)}

        def get_exchange_info(self, _symbol):
            return {"minQty": 0.00001, "stepSize": 0.00001, "maxQty": 10000.0}

    return _SpyProvider(), lambda: state["calls"]


def test_pre_blocked_symbol_skips_ohlcv_and_creates_skip_candidate(
    db, scan_env, monkeypatch
):
    """S3(a): a cooldown-blocked symbol must trigger ZERO OHLCV fetches and still
    produce a Candidate row with decision='skip' and reason tagged 'pre-blocked'."""
    from app import runtime

    candles_data = _uptrend()
    spy, get_calls = _make_spy_provider(candles_data)

    monkeypatch.setattr(scanner, "_provider_factory", lambda _xid: spy)
    monkeypatch.setattr(scanner, "data_provider", lambda: spy)
    candle_cache.clear()

    # Put BTC into stop-loss cooldown (very long so it doesn't expire).
    monkeypatch.setattr(settings, "stop_cooldown_min", 9999)
    from datetime import datetime, timezone
    runtime.set(db, "stop_cooldown:BTC", datetime.now(timezone.utc).isoformat())
    db.commit()

    scanner.run_scan(db, mode="semi")

    # No OHLCV calls — blocked before the fetch stage.
    assert get_calls() == 0, f"expected 0 OHLCV calls for a blocked symbol; got {get_calls()}"

    # Candidate row must exist with decision=skip and pre-blocked tag.
    cand = db.query(models.Candidate).filter_by(symbol="BTC").one()
    assert cand.decision == "skip"
    assert cand.reason is not None and cand.reason.startswith("pre-blocked:")
    # win-rate fields default to 0 (not null, DB constraint).
    assert cand.win_rate == 0.0
    assert cand.win_rate_lb == 0.0
    assert cand.trials == 0
    assert cand.session_id is None

    # Audit trail: skipped_cooldown must have been logged.
    assert db.query(models.AuditLog).filter_by(action="skipped_cooldown").count() >= 1


def test_pre_blocked_loss_streak_skips_ohlcv(db, scan_env, monkeypatch):
    """S3(a) variant: loss-streak block also skips OHLCV and creates skip Candidate."""
    candles_data = _uptrend()
    spy, get_calls = _make_spy_provider(candles_data)

    monkeypatch.setattr(scanner, "_provider_factory", lambda _xid: spy)
    monkeypatch.setattr(scanner, "data_provider", lambda: spy)
    candle_cache.clear()

    monkeypatch.setattr(settings, "loss_block_enabled", True)
    monkeypatch.setattr(settings, "loss_streak_block_k", 2)

    _close(db, "BTC", -5.0, days_ago=2)
    _close(db, "BTC", -3.0, days_ago=1)

    scanner.run_scan(db, mode="semi")

    assert get_calls() == 0, f"expected 0 OHLCV calls for loss-streak blocked symbol; got {get_calls()}"

    cand = db.query(models.Candidate).filter_by(symbol="BTC").one()
    assert cand.decision == "skip"
    assert cand.reason is not None and "pre-blocked" in cand.reason
    assert cand.session_id is None


def test_frozen_breaker_records_candidates_but_opens_no_session(
    db, scan_env, monkeypatch
):
    """S3(b): when the circuit-breaker is FROZEN, run_scan must record candidates with
    decision='trade' but open ZERO KSS sessions.  'skipped_frozen' audit must appear."""
    from app import runtime

    candles_data = _uptrend()
    spy, get_calls = _make_spy_provider(candles_data)

    monkeypatch.setattr(scanner, "_provider_factory", lambda _xid: spy)
    monkeypatch.setattr(scanner, "data_provider", lambda: spy)
    candle_cache.clear()

    # Freeze the breaker before the scan.
    runtime.freeze(db, "test: circuit open")
    db.commit()

    scanner.run_scan(db, mode="semi")

    # Candidate must exist (audit trail intact).
    cand = db.query(models.Candidate).filter_by(symbol="BTC").one()
    assert cand is not None

    # No KSS sessions opened.
    assert db.query(models.KssSession).count() == 0
    assert cand.session_id is None

    # skipped_frozen audit logged.
    assert db.query(models.AuditLog).filter_by(action="skipped_frozen").count() >= 1


# --- S5: Grok batch-cap and fail_mode tests ----------------------------------


def _make_multi_candidate_provider(n_symbols: int, candles_fn=None):
    """Return a provider serving `n_symbols` tradeable pairs (SYM0..SYMn-1)."""
    if candles_fn is None:
        candles_fn = _uptrend
    syms = [f"SYM{i}" for i in range(n_symbols)]
    candles = {s: candles_fn() for s in syms}

    class _P:
        exchange_id = "fake"
        quote = "USD"

        def pair(self, symbol):
            return f"{symbol}/USD"

        def get_ohlcv(self, symbol, timeframe="1d", limit=200):
            return candles.get(symbol, [])

        def top_symbols(self, n=10):
            return []

        def all_symbols(self, min_quote_volume=0.0):
            return syms

        def get_prices(self, symbols):
            return {s: candles[s][-1]["close"] for s in symbols if s in candles}

        def get_exchange_info(self, symbol):
            return {"minQty": 0.00001, "stepSize": 0.00001, "maxQty": 10000.0}

    return _P(), syms


def test_grok_batch_capped_and_audited(db, monkeypatch):
    """When more candidates qualify than grok_scanner_batch_max, only the top-N (by expectancy)
    are sent to Grok and the truncation is audited as 'grok_batch_truncated'."""
    provider, syms = _make_multi_candidate_provider(12)
    monkeypatch.setattr(settings, "grok_scanner_batch_max", 8)  # force truncation at 8 of 12
    monkeypatch.setattr(settings, "block_downtrend_adx", 0.0)

    # Patch both data_provider (for _universe + sequential path) and _provider_factory
    # (for the S2 parallel candle-cache workers) so no real network calls are made.
    monkeypatch.setattr(scanner, "data_provider", lambda: provider)
    monkeypatch.setattr(scanner, "_provider_factory", lambda _xid: provider)
    monkeypatch.setattr("app.kss.pyramid.get_exchange_info",
                        lambda s: {"minQty": 0.00001, "stepSize": 0.00001, "maxQty": 10000.0})
    monkeypatch.setattr("app.kss.pyramid.get_current_prices", lambda s: dict.fromkeys(s, 1.0))
    monkeypatch.setattr("app.orders.get_current_prices", lambda s: dict.fromkeys(s, 1.0))
    monkeypatch.setattr(settings, "watchlist", syms)
    monkeypatch.setattr(settings, "scan_top_n", 0)
    monkeypatch.setattr(settings, "min_confidence", 0.0)
    monkeypatch.setattr(settings, "min_win_rate", 0.0)
    monkeypatch.setattr(settings, "backtest_trial_spacing_days", 0.0)
    monkeypatch.setattr(settings, "min_trials", 0)
    monkeypatch.setattr(settings, "min_expectancy_pct", -100.0)
    monkeypatch.setattr(settings, "auto_trade", False)
    monkeypatch.setattr(settings, "grok_scanner_fail_mode", "open")
    candle_cache.clear()

    captured: dict = {}

    def _capture(_db, items):
        captured["items"] = items
        return {}

    monkeypatch.setattr("app.orchestrator.grok.scanner_enabled", lambda: True)
    monkeypatch.setattr("app.orchestrator.grok.review_candidates", _capture)

    scanner.run_scan(db, mode="semi")

    # Grok received at most 8 items.
    assert len(captured.get("items", [])) == 8, (
        f"expected 8 items sent to Grok; got {len(captured.get('items', []))}"
    )

    # Truncation audit was emitted.
    trunc = db.query(models.AuditLog).filter_by(action="grok_batch_truncated").one()
    import json as _json
    detail = _json.loads(trunc.detail or "{}")
    assert detail["kept"] == 8
    assert detail["dropped"] >= 1


def test_grok_reviews_all_when_batch_max_covers(db, monkeypatch):
    """With grok_scanner_batch_max ≥ the candidate count, EVERY 'trade' candidate is sent to
    Grok in the one batched call (no truncation) — so none can open unreviewed."""
    provider, syms = _make_multi_candidate_provider(12)
    monkeypatch.setattr(scanner, "data_provider", lambda: provider)
    monkeypatch.setattr(scanner, "_provider_factory", lambda _xid: provider)
    monkeypatch.setattr("app.kss.pyramid.get_exchange_info",
                        lambda s: {"minQty": 0.00001, "stepSize": 0.00001, "maxQty": 10000.0})
    monkeypatch.setattr("app.kss.pyramid.get_current_prices", lambda s: dict.fromkeys(s, 1.0))
    monkeypatch.setattr("app.orders.get_current_prices", lambda s: dict.fromkeys(s, 1.0))
    monkeypatch.setattr(settings, "watchlist", syms)
    monkeypatch.setattr(settings, "scan_top_n", 0)
    monkeypatch.setattr(settings, "min_confidence", 0.0)
    monkeypatch.setattr(settings, "min_win_rate", 0.0)
    monkeypatch.setattr(settings, "backtest_trial_spacing_days", 0.0)
    monkeypatch.setattr(settings, "min_trials", 0)
    monkeypatch.setattr(settings, "min_expectancy_pct", -100.0)
    monkeypatch.setattr(settings, "auto_trade", False)
    monkeypatch.setattr(settings, "block_downtrend_adx", 0.0)
    monkeypatch.setattr(settings, "grok_scanner_batch_max", 60)  # covers all 12
    candle_cache.clear()

    captured: dict = {}
    monkeypatch.setattr("app.orchestrator.grok.scanner_enabled", lambda: True)
    monkeypatch.setattr("app.orchestrator.grok.review_candidates",
                        lambda _db, items: captured.update(items=items) or {})
    scanner.run_scan(db, mode="semi")
    assert len(captured.get("items", [])) == 12, "every candidate must reach Grok when cap covers them"
    assert db.query(models.AuditLog).filter_by(action="grok_batch_truncated").count() == 0


_DOWNTREND_TA = {
    "rsi": 45.0, "macd_h": -0.5, "bb_pct": 0.2, "bb_w": 5.0, "adx": 40.0, "di": "down",
    "atr_pct": 6.0, "st": "down", "htf": "down", "sr_res": 3.0, "sr_sup": 1.0,
    "vtrend": "down", "vol_r": 1.0,
}


def test_downtrend_veto_unit(monkeypatch):
    """_downtrend_veto fires only when HTF+ST are BOTH down AND ADX ≥ threshold; 0 disables it."""
    monkeypatch.setattr(settings, "block_downtrend_adx", 25.0)
    assert scanner._downtrend_veto(_DOWNTREND_TA) is not None
    assert scanner._downtrend_veto({**_DOWNTREND_TA, "htf": "up"}) is None   # HTF not down
    assert scanner._downtrend_veto({**_DOWNTREND_TA, "st": "up"}) is None    # ST not down
    assert scanner._downtrend_veto({**_DOWNTREND_TA, "adx": 10.0}) is None   # weak trend
    monkeypatch.setattr(settings, "block_downtrend_adx", 0.0)
    assert scanner._downtrend_veto(_DOWNTREND_TA) is None                    # gate disabled


def test_scan_skips_confirmed_downtrend(db, scan_env, monkeypatch):
    """A 'trade' candidate in a confirmed downtrend (HTF+ST down, strong ADX) is blocked by the
    hard entry-timing gate, never opens, and is audited as 'skipped_downtrend'."""
    monkeypatch.setattr(settings, "block_downtrend_adx", 25.0)
    monkeypatch.setattr(scanner.ta_bundle, "build", lambda *a, **k: dict(_DOWNTREND_TA))
    scanner.run_scan(db, mode="semi")
    cand = db.query(models.Candidate).filter_by(symbol="BTC").one()
    assert cand.decision == "skip"
    assert cand.session_id is None
    assert "downtrend" in (cand.reason or "")
    assert db.query(models.AuditLog).filter_by(action="skipped_downtrend").count() >= 1


def test_grok_fail_mode_open_missing_verdict_opens(db, scan_env, monkeypatch):
    """S5 item 3 (a): fail_mode='open' — a symbol absent from the Grok verdict map
    is treated as endorsed and the session opens (today's behaviour preserved)."""
    monkeypatch.setattr(settings, "grok_scanner_fail_mode", "open")
    monkeypatch.setattr("app.orchestrator.grok.scanner_enabled", lambda: True)
    # Return empty dict → BTC has no verdict.
    monkeypatch.setattr("app.orchestrator.grok.review_candidates", lambda _db, items: {})

    scanner.run_scan(db, mode="semi")

    cand = db.query(models.Candidate).filter_by(symbol="BTC").one()
    # Session must have opened despite absent verdict (fail-open).
    assert cand.session_id is not None, "fail_mode=open must open when verdict is absent"
    # No skipped_grok_unverified audit should appear.
    assert db.query(models.AuditLog).filter_by(action="skipped_grok_unverified").count() == 0


def test_grok_fail_mode_closed_missing_verdict_blocks(db, scan_env, monkeypatch):
    """S5 item 3 (b): fail_mode='closed' — a symbol without an explicit endorse verdict
    must NOT open and must be audited as 'skipped_grok_unverified'."""
    monkeypatch.setattr(settings, "grok_scanner_fail_mode", "closed")
    monkeypatch.setattr("app.orchestrator.grok.scanner_enabled", lambda: True)
    # Return empty dict → BTC has no verdict.
    monkeypatch.setattr("app.orchestrator.grok.review_candidates", lambda _db, items: {})

    scanner.run_scan(db, mode="semi")

    cand = db.query(models.Candidate).filter_by(symbol="BTC").one()
    # Session must NOT have opened.
    assert cand.session_id is None, "fail_mode=closed must block when verdict is absent"
    assert "grok_unverified" in (cand.reason or "")
    # Audit must record the block.
    assert db.query(models.AuditLog).filter_by(action="skipped_grok_unverified").count() >= 1


# --- Capital accounting: projected reservation + lend-the-idle open-gate -----


def _active_kss(db, *, reserved: float, used: float, symbol: str = "AAA") -> models.KssSession:
    """Persist an ACTIVE KSS session with a given reservation and deployed cost."""
    row = models.KssSession(
        symbol=symbol, entry_price=100.0, distance_pct=2.0, max_waves=10,
        isolated_fund=reserved, tp_pct=3.0, timeout_x_min=1440.0, gap_y_min=0.0,
        status=models.SESSION_ACTIVE, total_cost=used,
    )
    db.add(row)
    db.commit()
    return row


def test_session_lock_lends_idle_reservation():
    """<50% filled locks only the deployed cash; >=50% filled locks the full reservation."""
    shallow = models.KssSession(isolated_fund=1000.0, total_cost=200.0)  # 20% used
    assert scanner._session_lock(shallow) == 200.0
    deep = models.KssSession(isolated_fund=1000.0, total_cost=600.0)  # 60% used
    assert scanner._session_lock(deep) == 1000.0
    at_half = models.KssSession(isolated_fund=1000.0, total_cost=500.0)  # exactly 50%
    assert scanner._session_lock(at_half) == 1000.0
    no_reserve = models.KssSession(isolated_fund=0.0, total_cost=42.0)
    assert scanner._session_lock(no_reserve) == 42.0


def test_can_open_budgets_on_live_equity_minus_backup(db, monkeypatch):
    """Budget = live equity × (100 − equity_backup_pct)%, NOT static account_equity."""
    monkeypatch.setattr("app.risk.account_equity", lambda _db: 1000.0)
    monkeypatch.setattr(settings, "equity_backup_pct", 25.0)
    monkeypatch.setattr(settings, "max_concurrent_sessions", 100)
    # No active sessions → budget is 750. 700 fits, 800 does not.
    ok, _ = scanner._can_open(db, 700.0)
    assert ok
    ok, why = scanner._can_open(db, 800.0)
    assert not ok and "dự phòng" in why


def test_can_open_reuses_idle_reservation(db, monkeypatch):
    """A lightly-filled session's idle reservation is lent to a new session (req b)."""
    monkeypatch.setattr("app.risk.account_equity", lambda _db: 1000.0)
    monkeypatch.setattr(settings, "equity_backup_pct", 25.0)  # budget 750
    monkeypatch.setattr(settings, "max_concurrent_sessions", 100)
    # Reserves 1000 but only used 100 (<50%) → locks 100, leaving 650 of the 750 budget.
    _active_kss(db, reserved=1000.0, used=100.0)
    ok, _ = scanner._can_open(db, 600.0)  # 100 + 600 = 700 <= 750
    assert ok, "idle reservation of a <50%-filled session must be reusable"
    # Old flat-reservation logic would have summed 1000 and blocked this outright.


def test_can_open_locks_full_reservation_when_deep(db, monkeypatch):
    """Once a session crosses 50% filled it locks its whole reservation (protect the DCA plan)."""
    monkeypatch.setattr("app.risk.account_equity", lambda _db: 1000.0)
    monkeypatch.setattr(settings, "equity_backup_pct", 25.0)  # budget 750
    monkeypatch.setattr(settings, "max_concurrent_sessions", 100)
    _active_kss(db, reserved=1000.0, used=600.0)  # >=50% → locks full 1000 > 750
    ok, why = scanner._can_open(db, 10.0)
    assert not ok and "dự phòng" in why


def test_projected_ladder_cost_matches_pyramid(monkeypatch):
    """The reserved fund equals the frozen pyramid's full-ladder estimate, and is positive."""
    from app.kss import service
    from app.kss.pyramid import PyramidSession

    monkeypatch.setattr(settings, "kss_first_wave_usd", 50.0)
    got = service.projected_ladder_cost("BTC", 100.0, 2.0, 10)
    expected = PyramidSession(
        symbol="BTC", entry_price=100.0, distance_pct=2.0, max_waves=10,
        isolated_fund=1.0, tp_pct=1.0, timeout_x_min=1.0, gap_y_min=0.0,
    ).estimate_total_cost()
    assert got == expected
    assert got > 0
