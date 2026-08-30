"""A session must not open on a ladder too small to trade.

The 16h soak went 8.5 hours without a single fill. Five session slots were full, and one of
them was LTC with isolated_fund = $0.188: its wave 0 came out at the exchange minimum (0.001
LTC = $0.05), far under minNotional, so the venue rejected it every cycle forever. The session
could never hold anything and never closed — it just occupied a slot, and with all five slots
taken the scanner stops scanning entirely (capital saturated).

scan_min_notional already existed for exactly this idea and was simply not applied at the open.
"""

from __future__ import annotations

import pytest

from app import execution, scanner
from app.config import settings


@pytest.fixture(autouse=True)
def _live(monkeypatch):
    """minNotional is the exchange's rule, so the guard is live-only."""
    monkeypatch.setattr(execution, "live_enabled", lambda: True)


def test_a_ladder_under_the_min_notional_does_not_open(monkeypatch):
    monkeypatch.setattr(settings, "scan_min_notional", 10.0)

    assert scanner._ladder_too_small(0.188) is True
    assert scanner._ladder_too_small(0.0) is True


def test_a_real_ladder_opens(monkeypatch):
    monkeypatch.setattr(settings, "scan_min_notional", 10.0)

    assert scanner._ladder_too_small(142.19) is False
    assert scanner._ladder_too_small(10.0) is False


def test_the_check_is_off_when_the_floor_is_zero(monkeypatch):
    """0 = the operator has deliberately removed the floor."""
    monkeypatch.setattr(settings, "scan_min_notional", 0.0)

    assert scanner._ladder_too_small(0.188) is False


def test_paper_is_untouched(monkeypatch):
    """Paper fills whatever it is given — a tiny ladder there is a tiny simulated trade, not a
    slot that can never fill."""
    monkeypatch.setattr(execution, "live_enabled", lambda: False)
    monkeypatch.setattr(settings, "scan_min_notional", 10.0)

    assert scanner._ladder_too_small(0.188) is False
