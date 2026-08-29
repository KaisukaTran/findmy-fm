"""Self-correcting entry/exit levels — stage 1: keep the gates SATISFIABLE.

The soak's most expensive failure was silent: min_expectancy_pct sat above the ceiling that
scan_tp_pct can ever produce, so every candidate was skipped, every scan, with no error
anywhere — 12 hours of that on paper before anyone noticed, and it repeated on live.

A gate no candidate can pass is not a risk control, it is a broken setting. Stage 1 detects
exactly that class of contradiction and moves the setting back into the reachable range,
loudly and in the audit log. It never TIGHTENS anything on its own and it never touches a
gate that is merely strict — only ones that are arithmetically impossible.
"""

from __future__ import annotations

import pytest

from app import autotune, costengine, runtime
from app.config import settings


def test_an_unsatisfiable_expectancy_gate_is_brought_under_the_ceiling(db, monkeypatch):
    monkeypatch.setattr(settings, "scan_tp_pct", 3.0)
    monkeypatch.setattr(settings, "min_expectancy_pct", 3.0)  # ceiling is 2.70
    monkeypatch.setattr(settings, "autotune_enabled", True)

    changes = autotune.enforce_consistency(db)

    ceiling = costengine.expectancy_ceiling_pct(3.0)
    assert settings.min_expectancy_pct < ceiling
    assert settings.min_expectancy_pct > 0
    assert any(c["setting"] == "min_expectancy_pct" for c in changes)
    assert changes[0]["reason"]


def test_a_reachable_gate_is_left_exactly_as_it_is(db, monkeypatch):
    monkeypatch.setattr(settings, "scan_tp_pct", 3.0)
    monkeypatch.setattr(settings, "min_expectancy_pct", 2.0)
    monkeypatch.setattr(settings, "autotune_enabled", True)

    changes = autotune.enforce_consistency(db)

    assert settings.min_expectancy_pct == 2.0
    assert changes == []


def test_a_take_profit_below_the_fee_floor_is_raised_to_it(db, monkeypatch):
    """A TP that cannot clear its own round-trip fee books a 'win' that loses money."""
    monkeypatch.setattr(settings, "autotune_enabled", True)
    floor = costengine.min_profit_pct()
    monkeypatch.setattr(settings, "scan_tp_pct", floor / 2)
    monkeypatch.setattr(settings, "min_expectancy_pct", 0.1)

    autotune.enforce_consistency(db)

    assert settings.scan_tp_pct >= floor


def test_it_does_nothing_at_all_when_disabled(db, monkeypatch):
    monkeypatch.setattr(settings, "scan_tp_pct", 3.0)
    monkeypatch.setattr(settings, "min_expectancy_pct", 3.0)
    monkeypatch.setattr(settings, "autotune_enabled", False)

    changes = autotune.enforce_consistency(db)

    assert changes == []
    assert settings.min_expectancy_pct == 3.0  # left broken, but left ALONE


def test_a_correction_is_persisted_so_a_restart_keeps_it(db, monkeypatch):
    monkeypatch.setattr(settings, "scan_tp_pct", 3.0)
    monkeypatch.setattr(settings, "min_expectancy_pct", 3.0)
    monkeypatch.setattr(settings, "autotune_enabled", True)

    autotune.enforce_consistency(db)

    assert runtime.get(db, "kss:min_expectancy_pct") is not None


def test_it_is_idempotent(db, monkeypatch):
    monkeypatch.setattr(settings, "scan_tp_pct", 3.0)
    monkeypatch.setattr(settings, "min_expectancy_pct", 3.0)
    monkeypatch.setattr(settings, "autotune_enabled", True)

    first = autotune.enforce_consistency(db)
    settled = settings.min_expectancy_pct
    second = autotune.enforce_consistency(db)

    assert first and second == []
    assert settings.min_expectancy_pct == settled


def test_it_never_tightens_a_gate(db, monkeypatch):
    """Stage 1 only ever RELAXES an impossible gate — it must not quietly raise one."""
    monkeypatch.setattr(settings, "scan_tp_pct", 10.0)
    monkeypatch.setattr(settings, "min_expectancy_pct", 0.6)
    monkeypatch.setattr(settings, "autotune_enabled", True)

    autotune.enforce_consistency(db)

    assert settings.min_expectancy_pct == 0.6


def test_the_correction_lands_in_the_audit_log(db, monkeypatch):
    from app.models import AuditLog

    monkeypatch.setattr(settings, "scan_tp_pct", 3.0)
    monkeypatch.setattr(settings, "min_expectancy_pct", 3.0)
    monkeypatch.setattr(settings, "autotune_enabled", True)

    autotune.enforce_consistency(db)

    rows = db.query(AuditLog).filter(AuditLog.action == "autotune").all()
    assert rows, "a setting the app changed by itself must be traceable"


@pytest.mark.parametrize("tp", [1.0, 3.0, 7.5, 20.0])
def test_the_result_is_always_a_reachable_gate(db, monkeypatch, tp):
    monkeypatch.setattr(settings, "scan_tp_pct", tp)
    monkeypatch.setattr(settings, "min_expectancy_pct", tp * 2)  # always impossible
    monkeypatch.setattr(settings, "autotune_enabled", True)

    autotune.enforce_consistency(db)

    assert not costengine.expectancy_gate_unsatisfiable(
        settings.min_expectancy_pct, settings.scan_tp_pct
    )
