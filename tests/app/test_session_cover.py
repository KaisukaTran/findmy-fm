"""`session_cover_rungs` — the concurrent-session cap derived from capital (Kai 2026-10-04).

The rule: every open session must be payable down to rung R AT THE SAME TIME (a broad,
correlated dip). So the cap is ``floor(budget / ladder_cost(wave, distance, R))``, where budget
is ``equity × (100 − equity_backup_pct)%`` and the wave is the one a new session would open with.
``max_concurrent_sessions`` stays as the hard ceiling; ``session_cover_rungs = 0`` is off.

Why not "N sessions per $1k": the wave already scales with equity (``first_wave_pct``), so a
count that ALSO scales would grow exposure with the square of equity. Pricing the count off
the ladder keeps exposure/equity constant while the wave scales, and turns extra equity into
extra sessions once ``first_wave_max_usd`` caps the wave.
"""

from __future__ import annotations

import math

import pytest

from app import models, risk, runtime, scanner
from app.config import settings
from app.kss import service

DIST = 7.0


@pytest.fixture
def book(monkeypatch, db):
    """A $7,000 book: wave 0.4% = $28, 10% backup → budget $6,300."""

    def _set(*, equity: float = 7_000.0, cover: float = 2.0, ceiling: int = 500,
             wave_cap: float = 0.0):
        monkeypatch.setattr(risk, "account_equity", lambda _db: equity)
        monkeypatch.setattr(settings, "capital_scale_enabled", True)
        monkeypatch.setattr(settings, "first_wave_pct", 0.4)
        monkeypatch.setattr(settings, "first_wave_max_usd", wave_cap)
        monkeypatch.setattr(settings, "scan_min_notional", 1.0)
        monkeypatch.setattr(settings, "scan_distance_pct", DIST)
        monkeypatch.setattr(settings, "scan_max_waves", 10)
        monkeypatch.setattr(settings, "equity_backup_pct", 10.0)
        monkeypatch.setattr(settings, "max_concurrent_sessions", ceiling)
        monkeypatch.setattr(settings, "session_cover_rungs", cover)
        runtime.set(db, runtime.KEY_CAPITAL_SCALE_ANCHOR, equity)  # pin: no deadband surprise
    return _set


def _expected(equity: float, wave: float, rungs: int) -> int:
    budget = equity * 0.9
    return math.floor(budget / service.ladder_cost_for(wave, DIST, rungs) + 1e-9)


def test_off_returns_the_static_ceiling(book, db):
    book(cover=0.0, ceiling=80)
    cap, why = scanner.effective_max_sessions(db)
    assert cap == 80
    assert why == ""


def test_cover_two_rungs_prices_the_count_off_the_ladder(book, db):
    book(cover=2.0)
    cap, why = scanner.effective_max_sessions(db)
    assert cap == _expected(7_000.0, 28.0, 2)
    assert 70 <= cap <= 85  # sanity: ~78 at $7k, the number Kai approved
    assert "2" in why


def test_static_ceiling_still_binds(book, db):
    book(cover=2.0, ceiling=40)
    assert scanner.effective_max_sessions(db)[0] == 40


def test_fractional_cover_interpolates_between_rungs(book, db):
    book(cover=1.5)
    c1 = service.ladder_cost_for(28.0, DIST, 1)
    c2 = service.ladder_cost_for(28.0, DIST, 2)
    assert scanner.effective_max_sessions(db)[0] == math.floor(6_300.0 / ((c1 + c2) / 2) + 1e-9)


def test_cover_deeper_than_the_ladder_clamps_to_the_full_ladder(book, db):
    book(cover=50.0)
    assert scanner.effective_max_sessions(db)[0] == _expected(7_000.0, 28.0, 10)


def test_a_percent_sized_wave_keeps_the_count_flat_as_equity_grows(book, db):
    """Wave and budget both scale with equity → the count must not move (no squared exposure)."""
    book(equity=7_000.0)
    small = scanner.effective_max_sessions(db)[0]
    book(equity=14_000.0)
    assert abs(scanner.effective_max_sessions(db)[0] - small) <= 1


def test_once_the_wave_is_capped_extra_equity_becomes_extra_sessions(book, db):
    book(equity=10_000.0, wave_cap=40.0)  # 0.4% of $10k = $40 = the cap
    at_cap = scanner.effective_max_sessions(db)[0]
    book(equity=20_000.0, wave_cap=40.0)
    doubled = scanner.effective_max_sessions(db)[0]
    assert doubled == _expected(20_000.0, 40.0, 2)
    assert doubled >= 2 * at_cap - 1


def test_can_open_refuses_at_the_derived_cap(book, db, monkeypatch):
    book(cover=2.0)
    cap = scanner.effective_max_sessions(db)[0]
    monkeypatch.setattr(settings, "session_cover_rungs", 0.0)
    monkeypatch.setattr(settings, "max_concurrent_sessions", 500)
    for i in range(cap):
        db.add(models.KssSession(symbol=f"C{i}USDT", entry_price=1.0, distance_pct=DIST,
                                 max_waves=10, isolated_fund=0.0, tp_pct=5.0,
                                 timeout_x_min=60, gap_y_min=30,
                                 status=models.SESSION_ACTIVE))
    db.commit()
    monkeypatch.setattr(settings, "session_cover_rungs", 2.0)
    ok, why = scanner._can_open(db, 0.0)
    assert not ok
    assert str(cap) in why


def test_knob_is_runtime_editable_and_persisted(db):
    assert "session_cover_rungs" in runtime.KSS_SETTING_FIELDS
    assert hasattr(settings, "session_cover_rungs")


# --- measured mode (Kai 2026-10-04): R = mean depth of the OPEN book, floored -----------------


def _open(db, depths):
    for i, d in enumerate(depths):
        db.add(models.KssSession(symbol=f"M{i}USDT", entry_price=1.0, distance_pct=DIST,
                                 max_waves=10, isolated_fund=0.0, tp_pct=5.0,
                                 timeout_x_min=60, gap_y_min=30, current_wave=d,
                                 status=models.SESSION_ACTIVE))
    db.commit()


def _cap_at(rungs: float) -> int:
    c1 = service.ladder_cost_for(28.0, DIST, int(rungs))
    c2 = service.ladder_cost_for(28.0, DIST, int(rungs) + 1)
    return math.floor(6_300.0 / (c1 + (rungs - int(rungs)) * (c2 - c1)) + 1e-9)


def test_measured_mode_uses_the_mean_depth_of_the_open_book(book, db, monkeypatch):
    book(cover=1.5)
    monkeypatch.setattr(settings, "session_cover_measured", True)
    _open(db, [1, 2, 3, 2])  # mean 2.0 > floor 1.5
    cap, why = scanner.effective_max_sessions(db)
    assert cap == _cap_at(2.0)
    assert "2" in why and "4" in why  # the measured R and how many sessions it came from


def test_measured_mode_never_goes_below_the_floor(book, db, monkeypatch):
    book(cover=1.5)
    monkeypatch.setattr(settings, "session_cover_measured", True)
    _open(db, [1, 1, 1, 2])  # mean 1.25 < floor
    assert scanner.effective_max_sessions(db)[0] == _cap_at(1.5)


def test_measured_mode_counts_an_unfilled_session_as_one_rung(book, db, monkeypatch):
    """A just-opened session (wave 0 still resting) still needs rung 1's money."""
    book(cover=1.0)
    monkeypatch.setattr(settings, "session_cover_measured", True)
    _open(db, [0, 0, 3, 3])  # counted as 1,1,3,3 → mean 2.0
    assert scanner.effective_max_sessions(db)[0] == _cap_at(2.0)


def test_measured_mode_with_an_empty_book_uses_the_floor(book, db, monkeypatch):
    book(cover=1.5)
    monkeypatch.setattr(settings, "session_cover_measured", True)
    assert scanner.effective_max_sessions(db)[0] == _cap_at(1.5)


def test_measured_knob_is_runtime_editable():
    assert "session_cover_measured" in runtime.KSS_SETTING_FIELDS
    assert settings.session_cover_measured is False  # ships off
