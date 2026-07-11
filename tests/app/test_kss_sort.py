"""
list_sessions ordering: full-ladder ACTIVE sessions (every wave filled → no auto-DCA
left) bubble to the top of the active group so a manual DCA+ is easy to spot, while
completed/other-status sessions still sort after all active ones.
"""

from __future__ import annotations

from datetime import timedelta

from app import models
from app.clock import utcnow
from app.kss import service
from app.models import SESSION_ACTIVE, SESSION_COMPLETED, WAVE_FILLED


def _mk_session(db, symbol, status, max_waves, filled, created_offset_min):
    """Create a KssSession + `filled` filled waves directly (no fill flow needed)."""
    s = models.KssSession(
        symbol=symbol, entry_price=100.0, distance_pct=2.0, max_waves=max_waves,
        isolated_fund=1000.0, tp_pct=3.0, timeout_x_min=30.0, gap_y_min=5.0,
        status=status, created_at=utcnow() + timedelta(minutes=created_offset_min),
    )
    db.add(s)
    db.flush()
    for n in range(filled):
        db.add(models.KssWave(
            session_id=s.id, wave_num=n, quantity=0.1,
            target_price=100.0, status=WAVE_FILLED,
        ))
    db.commit()
    return s


def test_full_ladder_active_sessions_sort_first(db, monkeypatch):
    monkeypatch.setattr("app.kss.pyramid.get_current_prices", lambda syms: {})
    # AAA: active + FULL (2/2), but OLDER than BBB — without full_first, created_at desc
    # would put BBB first.
    _mk_session(db, "AAA", SESSION_ACTIVE, max_waves=2, filled=2, created_offset_min=0)
    # BBB: active, NOT full (1/3), newer.
    _mk_session(db, "BBB", SESSION_ACTIVE, max_waves=3, filled=1, created_offset_min=10)
    # CCC: full (1/1) but COMPLETED — must still sort after every active session.
    _mk_session(db, "CCC", SESSION_COMPLETED, max_waves=1, filled=1, created_offset_min=20)

    syms = [s["symbol"] for s in service.list_sessions(db)]

    assert syms.index("AAA") < syms.index("BBB"), "full active must precede non-full active"
    assert syms.index("BBB") < syms.index("CCC"), "active must precede completed"


def test_created_at_still_breaks_ties_within_same_fullness(db, monkeypatch):
    monkeypatch.setattr("app.kss.pyramid.get_current_prices", lambda syms: {})
    # Two non-full active sessions: newer one first (created_at desc tiebreak preserved).
    _mk_session(db, "OLD", SESSION_ACTIVE, max_waves=5, filled=1, created_offset_min=0)
    _mk_session(db, "NEW", SESSION_ACTIVE, max_waves=5, filled=1, created_offset_min=10)

    syms = [s["symbol"] for s in service.list_sessions(db)]

    assert syms.index("NEW") < syms.index("OLD")
