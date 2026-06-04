"""
Phase C learned win-rate model.

Logistic regression trained on KSS backtest labels, implemented in pure Python
(no numpy / sklearn). Features are derived from the same indicator helpers used
by the agent layer so the model is consistent with the rest of the pipeline.

Public API
----------
train(db, *, provider=None) -> MlModel | None
load_latest(db) -> MlModel | None
predict(candles, model=None, db=None) -> tuple[float, float]  # (score, confidence)
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from app.agents.base import clamp, closes, realized_vol_pct, returns, rsi, sma
from app.backtest import simulate_kss
from app.config import settings
from app.data.providers import Candle

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from app.models import MlModel

# ---------------------------------------------------------------------------
# Feature spec
# ---------------------------------------------------------------------------

FEATURES: list[str] = [
    "rsi_14",          # momentum / overbought-oversold
    "close_vs_sma20",  # trend: (close/sma20) - 1
    "close_vs_sma50",  # slower trend: (close/sma50) - 1
    "vol_pct_30",      # realized volatility (pct, 30-bar)
    "last_return",     # last 1-bar return
]

_MIN_BARS = 52  # need at least sma50 + a few bars for rsi


def _features_at(candles: list[Candle], i: int) -> list[float] | None:
    """Compute a fixed-length feature vector for candle index `i`.

    Returns None when there are not enough bars before `i`.
    """
    if i < _MIN_BARS:
        return None
    window = candles[: i + 1]
    cs = closes(window)

    rsi_val = rsi(cs, 14)
    sma20 = sma(cs, 20)
    sma50 = sma(cs, 50)
    close = cs[-1]
    vol = realized_vol_pct(cs, 30)
    rets = returns(cs)
    last_ret = rets[-1] if rets else 0.0

    # Guard division by zero
    c_vs_20 = (close / sma20 - 1.0) if sma20 else 0.0
    c_vs_50 = (close / sma50 - 1.0) if sma50 else 0.0

    return [rsi_val, c_vs_20, c_vs_50, vol, last_ret]


# ---------------------------------------------------------------------------
# Dataset construction (pure, no network)
# ---------------------------------------------------------------------------


def build_dataset(
    symbol_candles: dict[str, list[Candle]],
) -> tuple[list[list[float]], list[int]]:
    """Build (X, y) from a mapping of symbol -> candle list.

    Rolls over every valid candle index, computes features, runs simulate_kss
    to produce the label (1 = TP hit, 0 = deadline hit without TP), and skips
    incomplete trials where neither outcome was reached.
    """
    X: list[list[float]] = []
    y: list[int] = []

    for candles in symbol_candles.values():
        n = len(candles)
        for i in range(_MIN_BARS, n):
            feats = _features_at(candles, i)
            if feats is None:
                continue
            result = simulate_kss(
                candles,
                start=i,
                distance_pct=settings.scan_distance_pct,
                max_waves=settings.scan_max_waves,
                tp_pct=settings.scan_tp_pct,
                deadline_days=float(settings.deadline_days),
            )
            # Skip incomplete trials (not enough look-ahead)
            if not result.tp_hit and not result.hit_deadline:
                continue
            X.append(feats)
            y.append(1 if result.tp_hit else 0)

    return X, y


# ---------------------------------------------------------------------------
# Pure-Python logistic regression
# ---------------------------------------------------------------------------


def _standardize(
    X: list[list[float]],
) -> tuple[list[list[float]], list[float], list[float]]:
    """Standardize features to zero mean and unit std (per column).

    Returns (X_scaled, means, stds). When std == 0 the feature is left as-is
    (treated as constant) to avoid division by zero.
    """
    if not X:
        return X, [], []
    n_feat = len(X[0])
    means: list[float] = []
    stds: list[float] = []

    for j in range(n_feat):
        col = [row[j] for row in X]
        mu = sum(col) / len(col)
        variance = sum((v - mu) ** 2 for v in col) / len(col)
        sigma = math.sqrt(variance)
        means.append(mu)
        stds.append(sigma)

    X_scaled: list[list[float]] = []
    for row in X:
        scaled_row = []
        for j in range(n_feat):
            sigma = stds[j]
            scaled_row.append((row[j] - means[j]) / sigma if sigma else row[j] - means[j])
        X_scaled.append(scaled_row)

    return X_scaled, means, stds


def _sigmoid(z: float) -> float:
    """Numerically stable sigmoid."""
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    e = math.exp(z)
    return e / (1.0 + e)


def _fit_logistic(
    X: list[list[float]],
    y: list[int],
    epochs: int = 300,
    lr: float = 0.1,
) -> tuple[list[float], float]:
    """Train logistic regression via batch gradient descent.

    Weights init at 0 for determinism. Returns (weights, bias).
    """
    if not X:
        return [], 0.0
    n_feat = len(X[0])
    n = len(X)
    weights = [0.0] * n_feat
    bias = 0.0

    for _ in range(epochs):
        dw = [0.0] * n_feat
        db = 0.0
        for xi, yi in zip(X, y, strict=False):
            z = sum(weights[j] * xi[j] for j in range(n_feat)) + bias
            pred = _sigmoid(z)
            err = pred - yi
            for j in range(n_feat):
                dw[j] += err * xi[j]
            db += err
        for j in range(n_feat):
            weights[j] -= lr * dw[j] / n
        bias -= lr * db / n

    return weights, bias


def _training_accuracy(
    X: list[list[float]], y: list[int], weights: list[float], bias: float
) -> float:
    """Fraction of correct predictions on the training set."""
    if not X:
        return 0.0
    correct = 0
    for xi, yi in zip(X, y, strict=False):
        z = sum(weights[j] * xi[j] for j in range(len(weights))) + bias
        pred = 1 if _sigmoid(z) >= 0.5 else 0
        if pred == yi:
            correct += 1
    return correct / len(X)


# ---------------------------------------------------------------------------
# DB-backed train / load / predict
# ---------------------------------------------------------------------------


def train(db: Session, *, provider=None) -> MlModel | None:
    """Fetch candles for the watchlist, build dataset, fit model, persist.

    Returns the new MlModel row, or None when there are too few samples.
    Commits internally; callers should not wrap this in an outer transaction.
    """
    from sqlalchemy import func

    from app import audit
    from app.models import MlModel as _MlModel

    prov = provider or __import__(
        "app.data.providers", fromlist=["data_provider"]
    ).data_provider()

    symbol_candles: dict[str, list[Candle]] = {}
    for sym in settings.watchlist:
        candles = prov.get_ohlcv(sym, settings.backtest_timeframe, settings.backtest_lookback_days)
        if candles:
            symbol_candles[sym] = candles

    X, y = build_dataset(symbol_candles)

    if len(y) < settings.ml_min_samples:
        audit.log(db, "ml", "skip_small", n=len(y))
        db.commit()
        return None

    X_scaled, means, stds = _standardize(X)
    weights, bias = _fit_logistic(X_scaled, y)
    metric = _training_accuracy(X_scaled, y, weights, bias)

    max_ver = db.query(func.max(_MlModel.version)).scalar() or 0
    version = max_ver + 1

    params = {
        "weights": weights,
        "bias": bias,
        "mean": means,
        "std": stds,
        "features": FEATURES,
        "version": version,
    }

    row = _MlModel(
        version=version,
        params_json=json.dumps(params),
        metric=round(metric, 6),
        n_samples=len(y),
        trained_at=datetime.now(timezone.utc),
    )
    db.add(row)
    db.flush()
    audit.log(db, "ml", "trained", n=len(y), metric=round(metric, 4), version=version)
    db.commit()
    return row


def load_latest(db: Session) -> MlModel | None:
    """Return the most-recently inserted MlModel row, or None."""
    from app.models import MlModel as _MlModel

    return db.query(_MlModel).order_by(_MlModel.id.desc()).first()


def predict(
    candles: list[Candle],
    model: MlModel | None = None,
    db: Session | None = None,
) -> tuple[float, float]:
    """Score the latest candle against the trained model.

    Returns (score, confidence) both in [0, 1]. Degrades to neutral (0.5, 0.0)
    when ml_enabled is False, no model is available, or features are unavailable.
    Never raises.
    """
    _neutral = (0.5, 0.0)
    try:
        if not settings.ml_enabled:
            return _neutral

        if model is None:
            if db is None:
                return _neutral
            model = load_latest(db)
        if model is None:
            return _neutral

        feats = _features_at(candles, len(candles) - 1)
        if feats is None:
            return _neutral

        params = json.loads(model.params_json or "{}")
        weights: list[float] = params.get("weights", [])
        bias: float = params.get("bias", 0.0)
        means: list[float] = params.get("mean", [])
        stds: list[float] = params.get("std", [])

        if not weights or len(weights) != len(feats):
            return _neutral

        # Standardize using stored statistics
        scaled: list[float] = []
        for j, v in enumerate(feats):
            mu = means[j] if j < len(means) else 0.0
            sigma = stds[j] if j < len(stds) else 0.0
            scaled.append((v - mu) / sigma if sigma else v - mu)

        z = sum(weights[j] * scaled[j] for j in range(len(weights))) + bias
        score = _sigmoid(z)
        confidence = clamp(2.0 * abs(score - 0.5))
        return (round(score, 6), round(confidence, 6))

    except Exception:  # never raise out of predict
        return _neutral
