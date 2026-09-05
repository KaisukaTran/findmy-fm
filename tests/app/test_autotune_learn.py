"""Self-adjusting levels — stage 3: learn the take-profit multiple from closed sessions.

Stage 2 derives levels from volatility, which is a prediction. Stage 3 checks that prediction
against what actually happened and nudges the multiple:

  * sessions that keep timing out mean the target is too far — bring it in;
  * targets hit almost immediately mean it is too close — money is being left on the table.

The classification is read off the closed session's REAL exit fill (``pyramid:{id}:<reason>``
source_ref), never off its status — SESSION_STOPPED covers a hard stop, a trailing exit, a
manual stop AND a zero-fill zombie, and a real take-profit finishes on SESSION_COMPLETED, not
the transient SESSION_TP_TRIGGERED. Deliberately slow and bounded on top of that: a fixed
minimum sample, a requirement for NEW evidence since the last move, one small step per run,
hard clamps, and every move recorded with the numbers behind it.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app import autotune, models
from app.clock import utcnow
from app.config import settings

# ---------------------------------------------------------------------------
# Fixture helpers — a closed session plus the fill(s) that actually closed it.
# ---------------------------------------------------------------------------


def _session(db, status: str, *, deadline_days: int = 1, symbol: str = "X",
             started_days_ago: int = 5) -> models.KssSession:
    started = utcnow() - timedelta(days=started_days_ago)
    row = models.KssSession(
        symbol=symbol, entry_price=100.0, distance_pct=2.0, max_waves=4,
        isolated_fund=150.0, tp_pct=3.0, timeout_x_min=60, gap_y_min=5,
        status=status, current_wave=1, avg_price=100.0, total_filled_qty=1.0,
        deadline_days=deadline_days, started_at=started, last_fill_at=started,
    )
    db.add(row)
    db.commit()
    return row


def _exit_fill(db, row: models.KssSession, suffix: str, *, share: float = 0.0) -> None:
    """The SELL fill that closed *row*'s own pyramid ladder for reason *suffix*."""
    started = row.started_at or row.created_at
    deadline_h = max(row.deadline_days, 0) * 24.0 or 1.0
    executed_at = started + timedelta(hours=share * deadline_h)
    db.add(models.Fill(
        symbol=row.symbol, side="SELL", quantity=1.0, price=100.0,
        source_ref=f"pyramid:{row.id}:{suffix}", executed_at=executed_at,
    ))
    db.commit()


def _closed(db, status: str, *, exit_suffix: str, share: float = 0.0,
            deadline_days: int = 1, symbol: str = "X") -> models.KssSession:
    """A closed session WITH the real exit fill recorded — the shape the fix reads."""
    row = _session(db, status, deadline_days=deadline_days, symbol=symbol)
    _exit_fill(db, row, exit_suffix, share=share)
    return row


def _zombie(db, status: str, symbol: str = "X") -> models.KssSession:
    """A closed session that never placed a single fill (2026-08-29 zero-fill zombies)."""
    return _session(db, status, symbol=symbol)


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setattr(settings, "autotune_enabled", True)
    monkeypatch.setattr(settings, "autotune_levels_enabled", True)
    monkeypatch.setattr(settings, "autotune_learn_enabled", True)
    monkeypatch.setattr(settings, "autotune_tp_atr_mult", 0.8)


# ---------------------------------------------------------------------------
# Acceptance 1 (RED first) — a real take-profit is read off its exit fill, not off a
# status that does not survive the close.
# ---------------------------------------------------------------------------


def test_completed_take_profit_counts_as_a_hit(db):
    """Under the old code this reads hit_rate 0/10 (it filters SESSION_TP_TRIGGERED, a status
    a real take-profit does not rest on once the session is COMPLETED) and never raises."""
    for i in range(10):
        _closed(db, models.SESSION_COMPLETED, exit_suffix="tp", share=0.05, symbol=f"H{i}")

    change = autotune.learn_from_outcomes(db)

    assert change is not None
    assert settings.autotune_tp_atr_mult == pytest.approx(0.8 + autotune.LEARN_STEP)


# ---------------------------------------------------------------------------
# Acceptance 2 (RED first) — SESSION_STOPPED is not synonymous with "timed out".
# ---------------------------------------------------------------------------


def test_stopped_session_with_trail_sl_exit_is_not_a_timeout(db):
    """Under the old code every SESSION_STOPPED row counts as a timeout, so 10/10 trailing
    exits would read timeout_rate 1.0 and ratchet the target down for no reason."""
    for i in range(10):
        _closed(db, models.SESSION_STOPPED, exit_suffix="trail_sl", share=0.5, symbol=f"T{i}")

    change = autotune.learn_from_outcomes(db)

    assert change is None  # a trailing exit is neither a hit nor a timeout on this target
    assert settings.autotune_tp_atr_mult == 0.8


# ---------------------------------------------------------------------------
# Acceptance 3 — a zero-fill zombie is excluded from the sample entirely.
# ---------------------------------------------------------------------------


def test_zero_fill_zombie_sessions_are_excluded_from_the_sample(db):
    for i in range(14):
        _closed(db, models.SESSION_COMPLETED, exit_suffix="tp", share=0.089, symbol=f"H{i}")
    for i in range(4):
        _closed(db, models.SESSION_STOPPED, exit_suffix="trail_sl", share=0.5, symbol=f"N{i}")
    # 20 sessions opened and swept without ever filling a single order. Folded into the
    # denominator they would drown the 14/18 real hit rate below the trigger.
    for i in range(20):
        _zombie(db, models.SESSION_STOPPED, symbol=f"Z{i}")

    change = autotune.learn_from_outcomes(db)

    assert change is not None  # 14/18 = 0.778 survives; 14/38 = 0.368 would not have fired
    assert settings.autotune_tp_atr_mult == pytest.approx(0.8 + autotune.LEARN_STEP)


# ---------------------------------------------------------------------------
# Acceptance 4 — only a `pyramid:{id}:deadline` exit counts as a timeout.
# ---------------------------------------------------------------------------


def test_only_a_deadline_exit_counts_as_a_timeout(db):
    for i in range(10):
        _closed(db, models.SESSION_STOPPED, exit_suffix="deadline", share=0.99, symbol=f"D{i}")

    change = autotune.learn_from_outcomes(db)

    assert change is not None
    assert settings.autotune_tp_atr_mult == pytest.approx(0.8 - autotune.LEARN_STEP)
    assert "timed out" in change["reason"]


# ---------------------------------------------------------------------------
# Acceptance 5 — the live-book replay: this is the shape the broken code got backwards.
# ---------------------------------------------------------------------------


def test_live_book_replay_fires_the_raise_branch(db):
    """2026-09-05 measured shape: 18 usable sessions, 14 real take-profits averaging 8.9% of
    their deadline, zero real timeouts. The broken code read this as hit_rate 0.0 and
    ratcheted the target DOWN seven times; the fix must raise it once."""
    for i in range(14):
        _closed(db, models.SESSION_COMPLETED, exit_suffix="tp", share=0.089, symbol=f"H{i}")
    for i in range(4):
        _closed(db, models.SESSION_STOPPED, exit_suffix="trail_sl", share=0.5, symbol=f"N{i}")

    change = autotune.learn_from_outcomes(db)

    assert change is not None
    assert settings.autotune_tp_atr_mult == pytest.approx(0.8 + autotune.LEARN_STEP)

    rows = db.query(models.AuditLog).filter(models.AuditLog.action == "autotune_learn").all()
    detail = rows[-1].detail or ""
    assert '"hits": 14' in detail
    assert '"timeouts": 0' in detail
    assert '"neither": 4' in detail
    assert '"excluded": 0' in detail
    assert '"hit_rate": 0.778' in detail


# ---------------------------------------------------------------------------
# Acceptance 6 — the watermark: no adjustment without NEW evidence.
# ---------------------------------------------------------------------------


def test_watermark_requires_new_evidence_before_adjusting_again(db):
    for i in range(14):
        _closed(db, models.SESSION_COMPLETED, exit_suffix="tp", share=0.089, symbol=f"H{i}")
    for i in range(4):
        _closed(db, models.SESSION_STOPPED, exit_suffix="trail_sl", share=0.5, symbol=f"N{i}")

    first = autotune.learn_from_outcomes(db)
    assert first is not None
    mult_after_first = settings.autotune_tp_atr_mult

    # Same evidence, same window — nothing new since the watermark.
    assert autotune.learn_from_outcomes(db) is None
    assert settings.autotune_tp_atr_mult == mult_after_first

    # Three MORE closed hit sessions: now there is new evidence, and it may move again.
    for i in range(3):
        _closed(db, models.SESSION_COMPLETED, exit_suffix="tp", share=0.05, symbol=f"H2{i}")

    second = autotune.learn_from_outcomes(db)
    assert second is not None
    assert settings.autotune_tp_atr_mult > mult_after_first


def test_an_absent_watermark_does_not_block_the_first_adjustment(db):
    for i in range(10):
        _closed(db, models.SESSION_COMPLETED, exit_suffix="tp", share=0.05, symbol=f"H{i}")

    assert autotune.learn_from_outcomes(db) is not None


# ---------------------------------------------------------------------------
# Acceptance 7 — the deadline share comes from the exit fill's own time, not last_fill_at
# (which an exit never updates).
# ---------------------------------------------------------------------------


def test_share_of_deadline_uses_the_exit_fills_time_not_last_fill_at(db):
    first_id = None
    for i in range(10):
        row = _session(db, models.SESSION_COMPLETED, symbol=f"H{i}")
        # last_fill_at is stale (the last DCA wave, long before the eventual close) and would
        # read as a "fast" 5% of the deadline if it were used for the share instead.
        row.last_fill_at = row.started_at + timedelta(hours=0.05 * 24)
        db.commit()
        _exit_fill(db, row, "tp", share=0.30)  # the real exit used 30% of the deadline
        if first_id is None:
            first_id = row.id

    outcome = next(o for o in autotune._closed_outcomes(db, 100) if o["id"] == first_id)
    assert outcome["share"] == pytest.approx(0.30, abs=1e-6)

    change = autotune.learn_from_outcomes(db)

    # 0.30 mean share is not "fast" (> FAST_HIT_SHARE 0.25) — reading last_fill_at's stale 0.05
    # instead would have wrongly fired the RAISE branch.
    assert change is None
    assert settings.autotune_tp_atr_mult == 0.8


# ---------------------------------------------------------------------------
# Acceptance 8 — clamps hold at both ends, and the whole thing is a no-op when disabled.
# ---------------------------------------------------------------------------


def test_the_multiple_stays_inside_the_lower_clamp(db, monkeypatch):
    monkeypatch.setattr(settings, "autotune_tp_atr_mult", autotune.TP_MULT_MIN)
    for i in range(10):
        _closed(db, models.SESSION_STOPPED, exit_suffix="deadline", share=0.99, symbol=f"D{i}")

    autotune.learn_from_outcomes(db)

    assert settings.autotune_tp_atr_mult >= autotune.TP_MULT_MIN


def test_the_multiple_stays_inside_the_upper_clamp(db, monkeypatch):
    monkeypatch.setattr(settings, "autotune_tp_atr_mult", autotune.TP_MULT_MAX)
    for i in range(14):
        _closed(db, models.SESSION_COMPLETED, exit_suffix="tp", share=0.05, symbol=f"H{i}")
    for i in range(4):
        _closed(db, models.SESSION_STOPPED, exit_suffix="trail_sl", share=0.5, symbol=f"N{i}")

    autotune.learn_from_outcomes(db)

    assert settings.autotune_tp_atr_mult <= autotune.TP_MULT_MAX


def test_it_does_nothing_when_switched_off(db, monkeypatch):
    monkeypatch.setattr(settings, "autotune_learn_enabled", False)
    for i in range(10):
        _closed(db, models.SESSION_STOPPED, exit_suffix="deadline", share=0.99, symbol=f"D{i}")

    assert autotune.learn_from_outcomes(db) is None
    assert settings.autotune_tp_atr_mult == 0.8


# ---------------------------------------------------------------------------
# Remaining regression coverage carried over from before the fix.
# ---------------------------------------------------------------------------


def test_a_healthy_mix_is_left_alone(db):
    """Most sessions taking a reasonable share of their deadline is the target working."""
    for i in range(10):
        _closed(db, models.SESSION_COMPLETED, exit_suffix="tp", share=0.5, symbol=f"H{i}")

    assert autotune.learn_from_outcomes(db) is None
    assert settings.autotune_tp_atr_mult == 0.8


def test_it_waits_for_enough_evidence(db):
    for i in range(autotune.LEARN_MIN_SESSIONS - 1):
        _closed(db, models.SESSION_STOPPED, exit_suffix="deadline", share=0.99, symbol=f"D{i}")

    assert autotune.learn_from_outcomes(db) is None
    assert settings.autotune_tp_atr_mult == 0.8


def test_one_run_moves_it_by_at_most_one_step(db):
    for i in range(autotune.LEARN_MIN_SESSIONS * 3):
        _closed(db, models.SESSION_STOPPED, exit_suffix="deadline", share=0.99, symbol=f"D{i}")

    autotune.learn_from_outcomes(db)

    assert settings.autotune_tp_atr_mult == pytest.approx(0.8 - autotune.LEARN_STEP)


def test_the_adjustment_is_audited_with_its_evidence(db):
    for i in range(10):
        _closed(db, models.SESSION_STOPPED, exit_suffix="deadline", share=0.99, symbol=f"D{i}")

    autotune.learn_from_outcomes(db)

    rows = db.query(models.AuditLog).filter(models.AuditLog.action == "autotune_learn").all()
    assert rows and "sessions" in (rows[-1].detail or "")
    assert "new_since_watermark" in (rows[-1].detail or "")


def test_still_running_sessions_are_not_evidence(db):
    for i in range(autotune.LEARN_MIN_SESSIONS * 2):
        _session(db, models.SESSION_ACTIVE, symbol=f"A{i}")

    assert autotune.learn_from_outcomes(db) is None


def test_zombie_sessions_cannot_unlock_a_step_on_unchanged_evidence(db):
    """New-evidence is counted over USABLE sessions only. A zero-fill zombie says nothing
    about a take-profit target, so a run of them must not buy another adjustment off the same
    real results — that is the re-reading defect in a smaller costume."""
    for i in range(14):
        _closed(db, models.SESSION_COMPLETED, exit_suffix="tp", share=0.089, symbol=f"H{i}")
    for i in range(4):
        _closed(db, models.SESSION_STOPPED, exit_suffix="trail_sl", share=0.5, symbol=f"N{i}")

    assert autotune.learn_from_outcomes(db) is not None
    mult = settings.autotune_tp_atr_mult
    assert autotune.learn_from_outcomes(db) is None  # nothing new

    # Three brand-new sessions that never placed an order: newer ids, but no evidence.
    for i in range(3):
        _zombie(db, models.SESSION_STOPPED, symbol=f"Z{i}")

    assert autotune.learn_from_outcomes(db) is None
    assert settings.autotune_tp_atr_mult == mult
