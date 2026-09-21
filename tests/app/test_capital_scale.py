"""Tests for app/capital_scale.py — Phase 1 (see module docstring): pure math + persistence
of the equity anchor, called by nothing yet. Every test here proves the module is SAFE to
wire up later: off is a no-op, on reproduces today's numbers exactly at today's equity, floors
bite before dust orders would, the anchor is deadbanded, and the module cannot mutate a
setting or take a shape parameter.
"""

from __future__ import annotations

import inspect

import pytest
from sqlalchemy.orm import Session

from app import capital_scale, risk, runtime
from app.config import settings
from app.models import AuditLog


def _stub_equity(monkeypatch: pytest.MonkeyPatch, value: float) -> None:
    """Bypass the real portfolio calc so a test can hand anchored_equity() an exact number."""
    monkeypatch.setattr(risk, "account_equity", lambda db: value)


HELPERS = (
    capital_scale.first_wave_usd,
    capital_scale.cash_floor_usd,
    capital_scale.session_deploy_cap_usd,
    capital_scale.live_order_notional_cap_usd,
    capital_scale.autoapprove_notional_cap_usd,
)


# --- 1. off is off -----------------------------------------------------------


def test_off_is_off(db: Session, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "capital_scale_enabled", False)
    for equity in (0.0, 1.0, 200_000.0, 50_000_000.0):
        _stub_equity(monkeypatch, equity)
        for helper in HELPERS:
            got = helper(db)
            assert got.enabled is False
            assert got.value == got.absolute


# --- 2. parity at $200,000 ----------------------------------------------------


def test_parity_at_200k_equity(db: Session, monkeypatch: pytest.MonkeyPatch):
    """At the live equity this feature was added at, every percentage knob's default resolves
    to exactly today's absolute knob — the property that makes flipping the switch safe."""
    monkeypatch.setattr(settings, "capital_scale_enabled", True)
    _stub_equity(monkeypatch, 200_000.0)

    assert capital_scale.first_wave_usd(db).value == pytest.approx(28.0, abs=1e-9)
    assert capital_scale.cash_floor_usd(db).value == pytest.approx(40_000.0, abs=1e-9)
    assert capital_scale.live_order_notional_cap_usd(db).value == pytest.approx(500.0, abs=1e-9)
    assert capital_scale.autoapprove_notional_cap_usd(db).value == pytest.approx(5_000.0, abs=1e-9)
    # max_session_deploy_pct defaults to 0 (off), matching max_session_deploy_usd = 0 (off).
    deploy = capital_scale.session_deploy_cap_usd(db)
    assert deploy.enabled is False
    assert deploy.value == settings.max_session_deploy_usd


# --- 3. floors bite ------------------------------------------------------------


def test_first_wave_floor_bites_at_low_equity(db: Session, monkeypatch: pytest.MonkeyPatch):
    """7,000 * 0.014%% = $0.98 — dust the venue would reject. The floor must win."""
    monkeypatch.setattr(settings, "capital_scale_enabled", True)
    _stub_equity(monkeypatch, 7_000.0)

    got = capital_scale.first_wave_usd(db)

    assert got.value == settings.scan_min_notional
    assert got.floored is True


# --- 4. deadband ---------------------------------------------------------------


def test_deadband_holds_small_moves_and_moves_on_a_big_one(
    db: Session, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(settings, "capital_scale_deadband_pct", 10.0)

    _stub_equity(monkeypatch, 100_000.0)
    first = capital_scale.anchored_equity(db)
    assert first == 100_000.0

    # +5%: inside the deadband, anchor must not move.
    _stub_equity(monkeypatch, 105_000.0)
    assert capital_scale.anchored_equity(db) == 100_000.0
    assert db.query(AuditLog).filter(AuditLog.action == "anchor_moved").count() == 0

    # +11% from the ORIGINAL anchor: outside the deadband, must move and log.
    _stub_equity(monkeypatch, 111_000.0)
    assert capital_scale.anchored_equity(db) == 111_000.0
    assert db.query(AuditLog).filter(AuditLog.action == "anchor_moved").count() == 1

    # -5% from the new anchor (111,000): inside the deadband, must hold.
    _stub_equity(monkeypatch, 105_450.0)  # 111_000 * 0.95
    assert capital_scale.anchored_equity(db) == 111_000.0
    assert db.query(AuditLog).filter(AuditLog.action == "anchor_moved").count() == 1

    # -11% from 111,000: outside the deadband in the DOWN direction, must move and log again.
    _stub_equity(monkeypatch, 98_790.0)  # 111_000 * 0.89
    assert capital_scale.anchored_equity(db) == pytest.approx(98_790.0)
    assert db.query(AuditLog).filter(AuditLog.action == "anchor_moved").count() == 2


# --- 5. no anchor yet ------------------------------------------------------------


def test_first_call_with_no_stored_anchor_stores_and_returns_live(
    db: Session, monkeypatch: pytest.MonkeyPatch
):
    assert runtime.get(db, runtime.KEY_CAPITAL_SCALE_ANCHOR) is None
    _stub_equity(monkeypatch, 42_000.0)

    got = capital_scale.anchored_equity(db)

    assert got == 42_000.0
    assert runtime.get(db, runtime.KEY_CAPITAL_SCALE_ANCHOR) == "42000.0"


# --- 6. shape is untouchable -----------------------------------------------------
#
# The actual guard lives in tests/app/test_capital_scaling.py
# (test_no_public_function_decides_the_shape_of_the_strategy), extended to walk
# app.capital_scale too — "don't write a weaker parallel one" per spec. This test just
# pins that the extension exists and covers this module, so a future edit can't quietly
# narrow it back to app.capital alone.


def test_the_shared_shape_guard_covers_this_module():
    # No `__init__.py` under tests/app (pytest's "prepend" import mode), so the sibling test
    # module is importable by its bare name, the same name pytest itself collects it under.
    import test_capital_scaling as shared

    src = inspect.getsource(shared.test_no_public_function_decides_the_shape_of_the_strategy)
    assert "capital_scale" in src


# --- 7. module writes no settings --------------------------------------------------


def test_never_calls_runtime_set_with_any_key_but_the_anchor(
    db: Session, monkeypatch: pytest.MonkeyPatch
):
    calls: list[str] = []
    real_set = runtime.set

    def _spy(db_arg, key, value):
        calls.append(key)
        return real_set(db_arg, key, value)

    monkeypatch.setattr(runtime, "set", _spy)
    monkeypatch.setattr(settings, "capital_scale_enabled", True)
    monkeypatch.setattr(settings, "capital_scale_deadband_pct", 10.0)

    # Exercise every public entry point, including a deadband-crossing anchor move.
    _stub_equity(monkeypatch, 10_000.0)
    for helper in HELPERS:
        helper(db)
    _stub_equity(monkeypatch, 20_000.0)  # +100%: forces the anchor to move
    for helper in HELPERS:
        helper(db)

    assert calls, "expected at least one runtime.set call (the anchor bookkeeping write)"
    assert set(calls) == {runtime.KEY_CAPITAL_SCALE_ANCHOR}
