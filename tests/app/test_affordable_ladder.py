"""`affordable_max_waves` shortens a ladder the account cannot pay for — and never lengthens one.

This is the single exception to the shape rule (docs/capital-scaling-policy.md §1.1), so the
tests that matter most are the ones pinning how narrow it is: off by default, upper bound only,
and priced at the ALREADY-SHRUNKEN first wave rather than the raw knob.
"""
from __future__ import annotations

import pytest

from app import capital_scale
from app.config import settings
from app.kss import service as kss_service


def _cfg(monkeypatch, *, equity: float, ladders: int, wave: float = 28.0,
         scaling: bool = False) -> None:
    monkeypatch.setattr(settings, "account_equity", equity)
    monkeypatch.setattr(settings, "min_fundable_ladders", ladders)
    monkeypatch.setattr(settings, "kss_first_wave_usd", wave)
    monkeypatch.setattr(settings, "equity_backup_pct", 24.8)
    monkeypatch.setattr(settings, "capital_scale_enabled", scaling)
    monkeypatch.setattr(settings, "scan_min_notional", 10.0)


def test_off_by_default_is_a_straight_pass_through(db, monkeypatch):
    _cfg(monkeypatch, equity=5_000.0, ladders=0)
    assert kss_service.affordable_max_waves(db, 4.0, 30) == 30


def test_a_large_book_keeps_the_full_ladder(db, monkeypatch):
    """At $200,000 the rule must not touch anything — 30 rungs measured best there."""
    _cfg(monkeypatch, equity=200_000.0, ladders=4)
    assert kss_service.affordable_max_waves(db, 4.0, 30) == 30


def test_a_small_book_gets_a_shorter_ladder(db, monkeypatch):
    """$5,000: budget $3,760, a quarter of it is $940, and a 30-rung ladder costs $6,186."""
    _cfg(monkeypatch, equity=5_000.0, ladders=4)
    got = kss_service.affordable_max_waves(db, 4.0, 30)
    assert got < 30
    assert kss_service.ladder_cost_for(28.0, 4.0, got) <= 5_000.0 * 0.752 / 4
    # the next rung up must NOT fit — otherwise this is shortening more than necessary
    assert kss_service.ladder_cost_for(28.0, 4.0, got + 1) > 5_000.0 * 0.752 / 4


def test_it_never_lengthens(db, monkeypatch):
    """A tiny configured ladder on a huge book stays tiny — upper bound only."""
    _cfg(monkeypatch, equity=1_000_000.0, ladders=4)
    assert kss_service.affordable_max_waves(db, 4.0, 5) == 5


def test_it_prices_the_ladder_at_the_scaled_first_wave_not_the_raw_knob(db, monkeypatch):
    """Order matters: shrink the wave first (pure size), shorten only if still unaffordable.

    At $7,000 with scaling on, the first wave floors at scan_min_notional ($10) rather than $28,
    so the same budget buys a materially LONGER ladder than the raw knob would allow.
    """
    _cfg(monkeypatch, equity=7_000.0, ladders=4, scaling=False)
    unscaled = kss_service.affordable_max_waves(db, 4.0, 30)

    _cfg(monkeypatch, equity=7_000.0, ladders=4, scaling=True)
    assert capital_scale.first_wave_usd(db).value == pytest.approx(10.0)  # floored, not $0.98
    scaled = kss_service.affordable_max_waves(db, 4.0, 30)
    assert scaled > unscaled


def test_a_configured_ladder_of_one_is_returned_untouched(db, monkeypatch):
    _cfg(monkeypatch, equity=100.0, ladders=4)
    assert kss_service.affordable_max_waves(db, 4.0, 1) == 1


def test_an_unaffordable_book_leaves_the_shape_alone_for_the_real_gates(db, monkeypatch):
    """When not even one wave fits, inventing a one-rung strategy here would be worse than
    letting `_can_open` / `_apply_cash_cap` refuse the open on their own terms."""
    _cfg(monkeypatch, equity=1.0, ladders=4)
    assert kss_service.affordable_max_waves(db, 4.0, 30) == 30


def test_shortening_is_audited(db, monkeypatch):
    from app.models import AuditLog

    _cfg(monkeypatch, equity=5_000.0, ladders=4)
    kss_service.affordable_max_waves(db, 4.0, 30)
    db.commit()
    rows = db.query(AuditLog).filter(AuditLog.action == "ladder_shortened").all()
    assert len(rows) >= 1, "a silently shortened ladder is exactly the invisible-control bug"
