"""Self-adjusting levels — stage 3: learn the take-profit multiple from closed sessions.

Stage 2 derives levels from volatility, which is a prediction. Stage 3 checks that prediction
against what actually happened and nudges the multiple:

  * sessions that keep timing out mean the target is too far — bring it in;
  * targets hit almost immediately mean it is too close — money is being left on the table.

Deliberately slow and bounded: a fixed minimum sample, one small step per run, hard clamps, and
every move recorded with the numbers behind it. It adjusts ONE knob — how ambitious the target
is — and never the risk gates.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app import autotune, models
from app.clock import utcnow
from app.config import settings


def _closed(db, status: str, *, hours: float, symbol: str = "X") -> models.KssSession:
    started = utcnow() - timedelta(hours=hours)
    row = models.KssSession(
        symbol=symbol, entry_price=100.0, distance_pct=2.0, max_waves=4,
        isolated_fund=150.0, tp_pct=3.0, timeout_x_min=60, gap_y_min=5,
        status=status, current_wave=1, avg_price=100.0, total_filled_qty=1.0,
        deadline_days=1, started_at=started, last_fill_at=utcnow(),
    )
    db.add(row)
    db.commit()
    return row


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setattr(settings, "autotune_enabled", True)
    monkeypatch.setattr(settings, "autotune_levels_enabled", True)
    monkeypatch.setattr(settings, "autotune_learn_enabled", True)
    monkeypatch.setattr(settings, "autotune_tp_atr_mult", 0.8)


def test_sessions_that_keep_timing_out_pull_the_target_in(db):
    for _ in range(autotune.LEARN_MIN_SESSIONS):
        _closed(db, models.SESSION_STOPPED, hours=30)

    change = autotune.learn_from_outcomes(db)

    assert change is not None
    assert settings.autotune_tp_atr_mult < 0.8
    assert "timed out" in change["reason"] or "expired" in change["reason"]


def test_targets_hit_almost_immediately_push_it_out(db):
    for _ in range(autotune.LEARN_MIN_SESSIONS):
        _closed(db, models.SESSION_TP_TRIGGERED, hours=1)

    autotune.learn_from_outcomes(db)

    assert settings.autotune_tp_atr_mult > 0.8


def test_a_healthy_mix_is_left_alone(db):
    """Most sessions taking a reasonable share of their deadline is the target working."""
    for _ in range(autotune.LEARN_MIN_SESSIONS):
        _closed(db, models.SESSION_TP_TRIGGERED, hours=12)  # half of a 1-day deadline

    assert autotune.learn_from_outcomes(db) is None
    assert settings.autotune_tp_atr_mult == 0.8


def test_it_waits_for_enough_evidence(db):
    for _ in range(autotune.LEARN_MIN_SESSIONS - 1):
        _closed(db, models.SESSION_STOPPED, hours=30)

    assert autotune.learn_from_outcomes(db) is None
    assert settings.autotune_tp_atr_mult == 0.8


def test_one_run_moves_it_by_at_most_one_step(db):
    for _ in range(autotune.LEARN_MIN_SESSIONS * 3):
        _closed(db, models.SESSION_STOPPED, hours=99)

    autotune.learn_from_outcomes(db)

    assert settings.autotune_tp_atr_mult == pytest.approx(0.8 - autotune.LEARN_STEP)


def test_the_multiple_stays_inside_its_clamps(db, monkeypatch):
    monkeypatch.setattr(settings, "autotune_tp_atr_mult", autotune.TP_MULT_MIN)
    for _ in range(autotune.LEARN_MIN_SESSIONS):
        _closed(db, models.SESSION_STOPPED, hours=99)

    autotune.learn_from_outcomes(db)

    assert settings.autotune_tp_atr_mult >= autotune.TP_MULT_MIN


def test_it_does_nothing_when_switched_off(db, monkeypatch):
    monkeypatch.setattr(settings, "autotune_learn_enabled", False)
    for _ in range(autotune.LEARN_MIN_SESSIONS):
        _closed(db, models.SESSION_STOPPED, hours=99)

    assert autotune.learn_from_outcomes(db) is None
    assert settings.autotune_tp_atr_mult == 0.8


def test_the_adjustment_is_audited_with_its_evidence(db):
    for _ in range(autotune.LEARN_MIN_SESSIONS):
        _closed(db, models.SESSION_STOPPED, hours=30)

    autotune.learn_from_outcomes(db)

    rows = db.query(models.AuditLog).filter(models.AuditLog.action == "autotune_learn").all()
    assert rows and "sessions" in (rows[-1].detail or "")


def test_still_running_sessions_are_not_evidence(db):
    for _ in range(autotune.LEARN_MIN_SESSIONS * 2):
        _closed(db, models.SESSION_ACTIVE, hours=99)

    assert autotune.learn_from_outcomes(db) is None
