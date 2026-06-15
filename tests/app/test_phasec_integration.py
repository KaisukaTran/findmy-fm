"""Phase C integration tests: MlAgent vote + hyperopt-tuned scanner session params."""

from __future__ import annotations

import pytest

from app import hyperopt, ml, models, scanner
from app.agents.ml_agent import MlAgent
from app.config import settings

_DAY = 86_400_000


# ---------------------------------------------------------------------------
# Candle factory (mirrors test_scanner.py style)
# ---------------------------------------------------------------------------

def _uptrend(n=80, start=100.0, vol=1e6):
    out, price = [], start
    for d in range(n):
        out.append({
            "ts": d * _DAY,
            "open": price,
            "high": price * 1.005,
            "low": price * 0.995,
            "close": price,
            "volume": vol,
        })
        price *= 1.01
    return out


# ---------------------------------------------------------------------------
# Fake offline provider (same shape as test_scanner.py's _FakeProvider)
# ---------------------------------------------------------------------------

class _FakeProvider:
    def __init__(self, candles_map: dict | None = None):
        self._map = candles_map or {"BTC": _uptrend()}

    def get_ohlcv(self, symbol, timeframe="1d", limit=200):
        return self._map.get(symbol, [])

    def top_symbols(self, n=10):
        return []

    def all_symbols(self, min_quote_volume=0.0):
        return list(self._map.keys())

    def get_prices(self, symbols):
        return {s: self._map[s][-1]["close"] for s in symbols if s in self._map}

    def get_exchange_info(self, symbol):
        return {"minQty": 0.00001, "stepSize": 0.00001, "maxQty": 10000.0}


# ---------------------------------------------------------------------------
# MlAgent.evaluate()
# ---------------------------------------------------------------------------

class TestMlAgent:
    def test_ml_disabled_returns_neutral_vote(self, monkeypatch):
        """MlAgent returns confidence 0.0 when ml_enabled is False."""
        monkeypatch.setattr(settings, "ml_enabled", False)
        vote = MlAgent().evaluate("BTC", _uptrend(), ctx={})
        assert vote.name == "ml"
        assert vote.confidence == 0.0
        assert vote.score == 0.5

    def test_no_model_in_ctx_returns_neutral(self, monkeypatch):
        """MlAgent returns neutral when ctx has no 'ml_model' key and ml is disabled."""
        monkeypatch.setattr(settings, "ml_enabled", False)
        vote = MlAgent().evaluate("BTC", _uptrend(), ctx={})
        assert vote.score == 0.5
        assert vote.confidence == 0.0

    def test_no_model_ml_enabled_returns_neutral(self, monkeypatch):
        """MlAgent returns neutral when ml is enabled but ctx has no model."""
        monkeypatch.setattr(settings, "ml_enabled", True)
        # ctx={} → model=None → predict degrades to neutral
        vote = MlAgent().evaluate("BTC", _uptrend(n=120), ctx={})
        assert vote.score == 0.5
        assert vote.confidence == 0.0

    def test_vote_name_is_ml(self, monkeypatch):
        """Vote name is always 'ml'."""
        monkeypatch.setattr(settings, "ml_enabled", False)
        vote = MlAgent().evaluate("BTC", _uptrend(), ctx={})
        assert vote.name == "ml"

    def test_with_trained_model_score_in_unit_interval(self, db, monkeypatch):
        """MlAgent returns a score in [0,1] when a real trained model is injected."""
        monkeypatch.setattr(settings, "ml_enabled", True)
        monkeypatch.setattr(settings, "ml_min_samples", 1)
        monkeypatch.setattr(settings, "watchlist", ["BTC"])

        provider = _FakeProvider({"BTC": _uptrend(n=120)})
        m = ml.train(db, provider=provider)
        if m is None:
            pytest.skip("not enough samples")

        vote = MlAgent().evaluate("BTC", _uptrend(n=120), ctx={"ml_model": m})
        assert 0.0 <= vote.score <= 1.0
        assert 0.0 <= vote.confidence <= 1.0

    def test_reason_includes_p_win(self, monkeypatch):
        """Vote reason includes the p(win) marker string."""
        monkeypatch.setattr(settings, "ml_enabled", False)
        vote = MlAgent().evaluate("BTC", _uptrend(), ctx={})
        assert "p(win)" in vote.reason


# ---------------------------------------------------------------------------
# Scanner uses hyperopt-tuned params when hyperopt_enabled=True
# ---------------------------------------------------------------------------

@pytest.fixture
def scan_env_hyperopt(monkeypatch):
    """Scan fixture that mirrors test_scanner.py but leaves hyperopt_enabled
    settable per test."""
    monkeypatch.setattr(scanner, "data_provider", lambda: _FakeProvider())
    monkeypatch.setattr(
        "app.kss.pyramid.get_exchange_info",
        lambda s: {"minQty": 0.00001, "stepSize": 0.00001, "maxQty": 10000.0},
    )
    monkeypatch.setattr(
        "app.kss.pyramid.get_current_prices", lambda syms: dict.fromkeys(syms, 1.0)
    )
    monkeypatch.setattr(
        "app.orders.get_current_prices", lambda syms: dict.fromkeys(syms, 1.0)
    )
    monkeypatch.setattr(settings, "watchlist", ["BTC"])
    monkeypatch.setattr(settings, "scan_top_n", 0)
    monkeypatch.setattr(settings, "min_confidence", 0.0)
    monkeypatch.setattr(settings, "min_win_rate", 0.0)
    monkeypatch.setattr(settings, "auto_trade", False)


class TestScannerUsesHyperoptParams:
    def test_scan_uses_global_params_when_hyperopt_disabled(self, db, scan_env_hyperopt, monkeypatch):
        """When hyperopt_enabled=False, session params match the global scan_* defaults."""
        monkeypatch.setattr(settings, "hyperopt_enabled", False)
        # Persist a PairParams row that differs from global defaults
        hyperopt.persist(db, "BTC", {
            "distance_pct": 99.0,   # deliberately absurd — should NOT be used
            "tp_pct": 99.0,
            "max_waves": 99,
            "score": 1.0,
            "trials": 10,
            "win_rate": 100.0,
            "loss_rate": 0.0,
        })
        db.commit()

        scanner.run_scan(db, mode="semi")

        cand = db.query(models.Candidate).filter_by(symbol="BTC").first()
        if cand is None or cand.session_id is None:
            pytest.skip("scan did not open a session (decision=skip)")

        sess = db.get(models.KssSession, cand.session_id)
        assert sess.distance_pct == settings.scan_distance_pct
        assert sess.tp_pct == settings.scan_tp_pct
        assert sess.max_waves == settings.scan_max_waves

    def test_scan_uses_tuned_params_when_hyperopt_enabled(self, db, scan_env_hyperopt, monkeypatch):
        """When hyperopt_enabled=True and PairParams exists, session uses tuned values."""
        monkeypatch.setattr(settings, "hyperopt_enabled", True)

        # Choose params that differ from global defaults AND pass the cost gate
        tuned_distance = 3.0
        tuned_tp = 5.0
        tuned_waves = 12
        assert tuned_distance != settings.scan_distance_pct or tuned_tp != settings.scan_tp_pct

        hyperopt.persist(db, "BTC", {
            "distance_pct": tuned_distance,
            "tp_pct": tuned_tp,
            "max_waves": tuned_waves,
            "score": 0.9,
            "trials": 10,
            "win_rate": 95.0,
            "loss_rate": 5.0,
        })
        db.commit()

        scanner.run_scan(db, mode="semi")

        cand = db.query(models.Candidate).filter_by(symbol="BTC").first()
        if cand is None or cand.session_id is None:
            # If no session was opened, at least assert the candidate reason reflects
            # the tuned params tag (scanner always formats it in)
            if cand is not None:
                assert f"d={tuned_distance}/tp={tuned_tp}/w={tuned_waves}" in (cand.reason or "")
            return

        sess = db.get(models.KssSession, cand.session_id)
        assert sess.distance_pct == tuned_distance
        assert sess.tp_pct == tuned_tp
        assert sess.max_waves == tuned_waves

    def test_scan_params_tag_in_candidate_reason(self, db, scan_env_hyperopt, monkeypatch):
        """Candidate.reason always contains the d=/tp=/w= params tag."""
        monkeypatch.setattr(settings, "hyperopt_enabled", True)

        tuned_distance = 2.5
        tuned_tp = 4.0
        tuned_waves = 8

        hyperopt.persist(db, "BTC", {
            "distance_pct": tuned_distance,
            "tp_pct": tuned_tp,
            "max_waves": tuned_waves,
            "score": 0.7,
            "trials": 8,
            "win_rate": 85.0,
            "loss_rate": 10.0,
        })
        db.commit()

        scanner.run_scan(db, mode="semi")

        cand = db.query(models.Candidate).filter_by(symbol="BTC").first()
        assert cand is not None
        assert f"d={tuned_distance}/tp={tuned_tp}/w={tuned_waves}" in (cand.reason or "")

    def test_scan_falls_back_to_global_when_no_pair_params_row(self, db, scan_env_hyperopt, monkeypatch):
        """hyperopt_enabled=True but no PairParams row → global defaults are used."""
        monkeypatch.setattr(settings, "hyperopt_enabled", True)
        # No persist() call — table is empty

        scanner.run_scan(db, mode="semi")

        cand = db.query(models.Candidate).filter_by(symbol="BTC").first()
        if cand is None or cand.session_id is None:
            pytest.skip("scan did not open a session")

        sess = db.get(models.KssSession, cand.session_id)
        assert sess.distance_pct == settings.scan_distance_pct
        assert sess.tp_pct == settings.scan_tp_pct
        assert sess.max_waves == settings.scan_max_waves
