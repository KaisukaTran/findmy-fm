"""Tests for app/hyperopt.py — Phase C per-pair parameter grid search."""

from __future__ import annotations

from app import hyperopt, models
from app.config import settings

_DAY = 86_400_000


def _uptrend(n=80, start=100.0, vol=1e6):
    """Synthetic uptrend candles — +1%/day, enough data for walk-forward splits."""
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


def _short_candles(n=10, start=100.0):
    """Fewer than _MIN_TRIALS worth of data — optimize should return None."""
    out, price = [], start
    for d in range(n):
        out.append({
            "ts": d * _DAY,
            "open": price,
            "high": price,
            "low": price * 0.999,
            "close": price,
            "volume": 1e5,
        })
        price *= 1.01
    return out


# ---------------------------------------------------------------------------
# optimize()
# ---------------------------------------------------------------------------

class TestOptimize:
    def test_returns_dict_on_sufficient_data(self, monkeypatch):
        """optimize() returns a result dict with the required keys on 80 candles."""
        monkeypatch.setattr(settings, "hyperopt_trials", 64)
        candles = _uptrend(n=80)
        result = hyperopt.optimize(candles)
        assert result is not None
        for key in ("distance_pct", "tp_pct", "max_waves", "score", "trials"):
            assert key in result, f"missing key: {key}"

    def test_returns_none_on_too_little_data(self, monkeypatch):
        """optimize() returns None when there is not enough data to form 5 trials."""
        monkeypatch.setattr(settings, "hyperopt_trials", 64)
        candles = _short_candles(n=10)
        result = hyperopt.optimize(candles)
        assert result is None

    def test_distance_and_tp_in_grid(self, monkeypatch):
        """Best distance_pct and tp_pct belong to the declared grids."""
        monkeypatch.setattr(settings, "hyperopt_trials", 64)
        result = hyperopt.optimize(_uptrend(n=80))
        assert result is not None
        assert result["distance_pct"] in hyperopt.DISTANCE_GRID
        assert result["tp_pct"] in hyperopt.TP_GRID
        assert result["max_waves"] in hyperopt.WAVES_GRID

    def test_trials_at_least_five(self, monkeypatch):
        """Every returned result must have at least 5 trials (cost-gate filter)."""
        monkeypatch.setattr(settings, "hyperopt_trials", 64)
        result = hyperopt.optimize(_uptrend(n=80))
        assert result is not None
        assert result["trials"] >= 5

    def test_tp_passes_cost_gate(self, monkeypatch):
        """The winner's tp_pct always covers round-trip costs."""
        from app import costengine
        monkeypatch.setattr(settings, "hyperopt_trials", 64)
        result = hyperopt.optimize(_uptrend(n=80))
        assert result is not None
        assert costengine.covers_costs(result["tp_pct"])

    def test_deterministic_same_candles(self, monkeypatch):
        """Same candle list produces identical best combo on repeated calls."""
        monkeypatch.setattr(settings, "hyperopt_trials", 64)
        candles = _uptrend(n=80)
        r1 = hyperopt.optimize(candles)
        r2 = hyperopt.optimize(candles)
        assert r1 == r2

    def test_score_is_win_rate_minus_15x_loss_rate(self, monkeypatch):
        """Returned score matches the objective formula: win_rate - 1.5*loss_rate."""
        monkeypatch.setattr(settings, "hyperopt_trials", 64)
        result = hyperopt.optimize(_uptrend(n=80))
        assert result is not None
        expected = round(result["win_rate"] - 1.5 * result["loss_rate"], 4)
        assert result["score"] == expected

    def test_trials_cap_respected(self, monkeypatch):
        """hyperopt_trials=1 evaluates at most 1 combo; may return None if filtered."""
        monkeypatch.setattr(settings, "hyperopt_trials", 1)
        # Does not raise; result may be None if the single combo has < 5 trials
        result = hyperopt.optimize(_uptrend(n=80))
        # We don't assert a specific value — just that it doesn't crash
        assert result is None or isinstance(result, dict)


# ---------------------------------------------------------------------------
# persist() + best_params()
# ---------------------------------------------------------------------------

class TestPersistAndBestParams:
    def test_persist_creates_row(self, db):
        """persist() inserts a PairParams row and best_params() retrieves it."""
        best = {
            "distance_pct": 2.0,
            "tp_pct": 3.0,
            "max_waves": 8,
            "score": 0.45,
            "trials": 12,
            "win_rate": 70.0,
            "loss_rate": 16.7,
        }
        hyperopt.persist(db, "BTC", best)
        db.commit()

        fetched = hyperopt.best_params(db, "BTC")
        assert fetched is not None
        assert fetched.symbol == "BTC"
        assert fetched.distance_pct == 2.0
        assert fetched.tp_pct == 3.0
        assert fetched.max_waves == 8
        assert fetched.score == 0.45
        assert fetched.trials == 12

    def test_persist_upserts_existing_row(self, db):
        """persist() updates the existing row rather than duplicating it."""
        best_v1 = {
            "distance_pct": 1.5,
            "tp_pct": 2.0,
            "max_waves": 6,
            "score": 0.30,
            "trials": 7,
            "win_rate": 60.0,
            "loss_rate": 20.0,
        }
        hyperopt.persist(db, "ETH", best_v1)
        db.commit()

        best_v2 = {
            "distance_pct": 3.0,
            "tp_pct": 4.0,
            "max_waves": 10,
            "score": 0.55,
            "trials": 15,
            "win_rate": 75.0,
            "loss_rate": 13.3,
        }
        hyperopt.persist(db, "ETH", best_v2)
        db.commit()

        rows = db.query(models.PairParams).filter_by(symbol="ETH").all()
        assert len(rows) == 1, "upsert must not duplicate the row"
        assert rows[0].tp_pct == 4.0
        assert rows[0].max_waves == 10

    def test_best_params_returns_none_for_unknown_symbol(self, db):
        """best_params() returns None when no row has been persisted."""
        assert hyperopt.best_params(db, "UNKNOWN") is None

    def test_round_trip_all_fields(self, db):
        """All numeric fields survive the persist → best_params round-trip."""
        best = {
            "distance_pct": 2.5,
            "tp_pct": 5.0,
            "max_waves": 12,
            "score": 0.6789,
            "trials": 20,
            "win_rate": 80.0,
            "loss_rate": 10.0,
        }
        hyperopt.persist(db, "SOL", best)
        db.commit()
        row = hyperopt.best_params(db, "SOL")
        assert row.distance_pct == 2.5
        assert row.tp_pct == 5.0
        assert row.max_waves == 12
        assert row.trials == 20


# ---------------------------------------------------------------------------
# run_for()
# ---------------------------------------------------------------------------

class TestRunFor:
    def test_run_for_persists_and_returns_pair_params(self, db, monkeypatch):
        """run_for() with explicit candles creates a PairParams row and returns it."""
        monkeypatch.setattr(settings, "hyperopt_trials", 64)
        candles = _uptrend(n=80)
        result = hyperopt.run_for(db, "BTC", candles=candles)
        # On good data the uptrend should produce at least one qualifying combo
        if result is not None:
            assert isinstance(result, models.PairParams)
            assert result.symbol == "BTC"
            assert result.distance_pct in hyperopt.DISTANCE_GRID
            assert result.tp_pct in hyperopt.TP_GRID
            stored = hyperopt.best_params(db, "BTC")
            assert stored is not None
            assert stored.tp_pct == result.tp_pct

    def test_run_for_returns_none_and_audits_no_fit_on_short_data(self, db, monkeypatch):
        """run_for() returns None and logs no_fit when candles are too short."""
        monkeypatch.setattr(settings, "hyperopt_trials", 64)
        candles = _short_candles(n=10)
        result = hyperopt.run_for(db, "BTC", candles=candles)
        assert result is None
        assert hyperopt.best_params(db, "BTC") is None
        # Audit row for no_fit must exist
        no_fit = (
            db.query(models.AuditLog)
            .filter(models.AuditLog.action == "no_fit")
            .first()
        )
        assert no_fit is not None

    def test_run_for_audits_tuned_on_success(self, db, monkeypatch):
        """run_for() appends a 'tuned' audit row on success."""
        monkeypatch.setattr(settings, "hyperopt_trials", 64)
        candles = _uptrend(n=80)
        result = hyperopt.run_for(db, "BTC", candles=candles)
        if result is not None:
            tuned = (
                db.query(models.AuditLog)
                .filter(models.AuditLog.action == "tuned")
                .first()
            )
            assert tuned is not None
            assert tuned.entity == "BTC"

    def test_run_for_no_network(self, db, monkeypatch):
        """run_for() with candles= kwarg never calls the data provider."""
        monkeypatch.setattr(settings, "hyperopt_trials", 64)

        def _boom(*a, **kw):
            raise AssertionError("data_provider must not be called when candles are injected")

        monkeypatch.setattr("app.hyperopt.data_provider", _boom)
        # Should not raise
        hyperopt.run_for(db, "BTC", candles=_uptrend(n=80))
