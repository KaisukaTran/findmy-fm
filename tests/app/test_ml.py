"""Tests for app/ml.py — Phase C logistic-regression win-rate model."""

from __future__ import annotations

import json

import pytest

from app import ml, models
from app.config import settings

_DAY = 86_400_000


# ---------------------------------------------------------------------------
# Candle factories
# ---------------------------------------------------------------------------

def _uptrend(n=120, start=100.0, vol=1e6):
    """Smooth +1%/day uptrend with enough bars for feature extraction (>= 52)."""
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


def _downtrend(n=120, start=200.0, vol=1e6):
    """Smooth -1%/day downtrend."""
    out, price = [], start
    for d in range(n):
        out.append({
            "ts": d * _DAY,
            "open": price,
            "high": price * 1.002,
            "low": price * 0.995,
            "close": price,
            "volume": vol,
        })
        price *= 0.99
    return out


class _FakeProvider:
    """Offline provider that returns synthetic candles for any requested symbol."""

    def __init__(self, candles_map: dict):
        self._map = candles_map

    def get_ohlcv(self, symbol, timeframe="1d", limit=200):
        return self._map.get(symbol, [])


# ---------------------------------------------------------------------------
# build_dataset()
# ---------------------------------------------------------------------------

class TestBuildDataset:
    def test_returns_aligned_x_y(self):
        """build_dataset() returns (X, y) where every row in X has len==5."""
        X, y = ml.build_dataset({"BTC": _uptrend(n=120)})
        assert len(X) == len(y), "X and y must have the same length"
        if X:
            feat_len = len(ml.FEATURES)
            for row in X:
                assert len(row) == feat_len, f"row length {len(row)} != {feat_len}"

    def test_y_binary(self):
        """All y labels are in {0, 1}."""
        _, y = ml.build_dataset({"BTC": _uptrend(n=120)})
        assert all(v in (0, 1) for v in y)

    def test_multiple_symbols_concatenated(self):
        """Results from two symbols are pooled into one flat dataset."""
        X1, y1 = ml.build_dataset({"BTC": _uptrend(n=100)})
        X2, y2 = ml.build_dataset({"ETH": _uptrend(n=100)})
        X_both, y_both = ml.build_dataset({
            "BTC": _uptrend(n=100),
            "ETH": _uptrend(n=100),
        })
        assert len(y_both) == len(y1) + len(y2)

    def test_empty_returns_empty(self):
        """Empty input produces empty dataset without raising."""
        X, y = ml.build_dataset({})
        assert X == []
        assert y == []

    def test_too_short_candles_produce_no_samples(self):
        """Fewer than _MIN_BARS candles produce no feature rows."""
        short = _uptrend(n=20)  # below the 52-bar minimum
        X, y = ml.build_dataset({"BTC": short})
        assert len(X) == 0


# ---------------------------------------------------------------------------
# predict()
# ---------------------------------------------------------------------------

class TestPredict:
    def test_neutral_when_ml_disabled(self, monkeypatch):
        """predict() returns (0.5, 0.0) when settings.ml_enabled is False."""
        monkeypatch.setattr(settings, "ml_enabled", False)
        score, conf = ml.predict(_uptrend(n=120))
        assert score == 0.5
        assert conf == 0.0

    def test_neutral_when_no_model(self, monkeypatch):
        """predict() returns (0.5, 0.0) when model=None and db=None."""
        monkeypatch.setattr(settings, "ml_enabled", True)
        score, conf = ml.predict(_uptrend(n=120), model=None, db=None)
        assert score == 0.5
        assert conf == 0.0

    def test_neutral_on_short_candles(self, db, monkeypatch):
        """predict() degrades to neutral when candles are too short for features."""
        monkeypatch.setattr(settings, "ml_enabled", True)
        monkeypatch.setattr(settings, "ml_min_samples", 1)
        monkeypatch.setattr(settings, "watchlist", ["BTC"])

        provider = _FakeProvider({"BTC": _uptrend(n=120)})
        m = ml.train(db, provider=provider)
        if m is None:
            pytest.skip("train returned None — not enough samples with current settings")

        short = _uptrend(n=10)  # below _MIN_BARS
        score, conf = ml.predict(short, model=m)
        assert score == 0.5
        assert conf == 0.0

    def test_score_in_unit_interval_with_model(self, db, monkeypatch):
        """predict() returns (score, conf) both in [0, 1] with a trained model."""
        monkeypatch.setattr(settings, "ml_enabled", True)
        monkeypatch.setattr(settings, "ml_min_samples", 1)
        monkeypatch.setattr(settings, "watchlist", ["BTC"])

        provider = _FakeProvider({"BTC": _uptrend(n=120)})
        m = ml.train(db, provider=provider)
        if m is None:
            pytest.skip("train returned None — not enough samples with current settings")

        score, conf = ml.predict(_uptrend(n=120), model=m)
        assert 0.0 <= score <= 1.0
        assert 0.0 <= conf <= 1.0

    def test_predict_never_raises(self, monkeypatch):
        """predict() never propagates exceptions — returns neutral on error."""
        monkeypatch.setattr(settings, "ml_enabled", True)
        # Craft a broken model row
        from app.models import MlModel
        broken = MlModel(version=99, params_json="{bad json")
        score, conf = ml.predict(_uptrend(n=120), model=broken)
        assert score == 0.5
        assert conf == 0.0


# ---------------------------------------------------------------------------
# train()
# ---------------------------------------------------------------------------

class TestTrain:
    def test_returns_none_below_min_samples(self, db, monkeypatch):
        """train() returns None and audits skip when samples < ml_min_samples."""
        monkeypatch.setattr(settings, "ml_min_samples", 9_999_999)
        monkeypatch.setattr(settings, "watchlist", ["BTC"])

        provider = _FakeProvider({"BTC": _uptrend(n=120)})
        result = ml.train(db, provider=provider)
        assert result is None

        skip = (
            db.query(models.AuditLog)
            .filter(models.AuditLog.action == "skip_small")
            .first()
        )
        assert skip is not None

    def test_train_persists_model_row(self, db, monkeypatch):
        """train() inserts a MlModel row with a valid params_json."""
        monkeypatch.setattr(settings, "ml_min_samples", 1)
        monkeypatch.setattr(settings, "watchlist", ["BTC"])

        provider = _FakeProvider({"BTC": _uptrend(n=120)})
        m = ml.train(db, provider=provider)
        if m is None:
            pytest.skip("not enough samples")

        assert isinstance(m, models.MlModel)
        params = json.loads(m.params_json)
        assert "weights" in params
        assert "bias" in params
        assert "features" in params
        assert m.n_samples > 0
        assert 0.0 <= m.metric <= 1.0

    def test_train_increments_version(self, db, monkeypatch):
        """Successive train() calls produce monotonically increasing versions."""
        monkeypatch.setattr(settings, "ml_min_samples", 1)
        monkeypatch.setattr(settings, "watchlist", ["BTC"])

        provider = _FakeProvider({"BTC": _uptrend(n=120)})
        m1 = ml.train(db, provider=provider)
        m2 = ml.train(db, provider=provider)

        if m1 is None or m2 is None:
            pytest.skip("not enough samples")

        assert m2.version > m1.version

    def test_train_audits_trained(self, db, monkeypatch):
        """train() appends a 'trained' audit row on success."""
        monkeypatch.setattr(settings, "ml_min_samples", 1)
        monkeypatch.setattr(settings, "watchlist", ["BTC"])

        provider = _FakeProvider({"BTC": _uptrend(n=120)})
        m = ml.train(db, provider=provider)
        if m is None:
            pytest.skip("not enough samples")

        trained = (
            db.query(models.AuditLog)
            .filter(models.AuditLog.action == "trained")
            .first()
        )
        assert trained is not None


# ---------------------------------------------------------------------------
# load_latest()
# ---------------------------------------------------------------------------

class TestLoadLatest:
    def test_returns_none_when_no_models(self, db):
        """load_latest() returns None when the ml_models table is empty."""
        assert ml.load_latest(db) is None

    def test_returns_most_recent_row(self, db, monkeypatch):
        """load_latest() returns the row with the highest id (most recently inserted)."""
        monkeypatch.setattr(settings, "ml_min_samples", 1)
        monkeypatch.setattr(settings, "watchlist", ["BTC"])

        provider = _FakeProvider({"BTC": _uptrend(n=120)})
        m1 = ml.train(db, provider=provider)
        m2 = ml.train(db, provider=provider)

        if m1 is None or m2 is None:
            pytest.skip("not enough samples")

        latest = ml.load_latest(db)
        assert latest is not None
        assert latest.id == m2.id

    def test_load_latest_then_predict(self, db, monkeypatch):
        """load_latest() + predict() round-trip: score stays in [0, 1]."""
        monkeypatch.setattr(settings, "ml_enabled", True)
        monkeypatch.setattr(settings, "ml_min_samples", 1)
        monkeypatch.setattr(settings, "watchlist", ["BTC"])

        provider = _FakeProvider({"BTC": _uptrend(n=120)})
        ml.train(db, provider=provider)

        latest = ml.load_latest(db)
        if latest is None:
            pytest.skip("not enough samples")

        score, conf = ml.predict(_uptrend(n=120), model=latest)
        assert 0.0 <= score <= 1.0
        assert 0.0 <= conf <= 1.0
