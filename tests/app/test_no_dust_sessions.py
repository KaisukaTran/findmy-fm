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


def test_an_order_under_the_min_notional_does_not_open(monkeypatch):
    monkeypatch.setattr(settings, "scan_min_notional", 10.0)

    assert scanner._first_wave_too_small(0.188) is True
    assert scanner._first_wave_too_small(0.0) is True


def test_a_real_first_wave_opens(monkeypatch):
    monkeypatch.setattr(settings, "scan_min_notional", 10.0)

    assert scanner._first_wave_too_small(40.0) is False
    assert scanner._first_wave_too_small(10.0) is False


def test_the_check_is_off_when_the_floor_is_zero(monkeypatch):
    """0 = the operator has deliberately removed the floor."""
    monkeypatch.setattr(settings, "scan_min_notional", 0.0)

    assert scanner._first_wave_too_small(0.188) is False


def test_paper_is_untouched(monkeypatch):
    """Paper fills whatever it is given — a tiny order there is a tiny simulated trade, not a
    slot that can never fill."""
    monkeypatch.setattr(execution, "live_enabled", lambda: False)
    monkeypatch.setattr(settings, "scan_min_notional", 10.0)

    assert scanner._first_wave_too_small(0.188) is False


# --- the venue's rule is per ORDER, so the floor must measure wave 0 -----------


def test_the_floor_is_measured_against_wave_0_not_the_whole_ladder(monkeypatch):
    """The guard measured the LADDER while the exchange measures each ORDER.

    KSS splits the fund 1:2:3:… across waves, so wave 0 is roughly a TENTH of the ladder. A
    ladder just over the floor therefore sends a wave 0 far under it, the venue answers -1013
    NOTIONAL every cycle, and the session sits ACTIVE holding a concurrency slot it can never
    use — exactly how 8.5 hours of the first soak were lost behind one $0.19 LTC session
    (audit `open_execute_failed`, 2026-08-29 18:50:51).

    Reachable on the DEFAULT config: with `kss_first_wave_usd = 0` the legacy path sizes wave 0
    as `pip_multiplier × minQty`. Measured against the live venue on 2026-08-30, BTC/USDT gives
    wave 0 = $1.56 on a $15.03 ladder and BNB/USDT $1.39 on $13.33 — both sail past a $10
    LADDER floor while sitting under Binance's $5 minNotional.
    """
    monkeypatch.setattr(settings, "scan_min_notional", 10.0)

    assert scanner._first_wave_too_small(1.56) is True, "BTC on a $15.03 ladder"
    assert scanner._first_wave_too_small(1.39) is True, "BNB on a $13.33 ladder"


def test_the_first_wave_cost_is_the_wave_0_slice_of_the_ladder(monkeypatch):
    """`projected_first_wave_cost` must come from the SAME frozen pyramid math as
    `projected_ladder_cost`, not a re-derived divisor — wave 0's share depends on both the wave
    count and the distance decay."""
    from app.kss import service as kss

    monkeypatch.setattr(settings, "kss_first_wave_usd", 40.0)

    first = kss.projected_first_wave_cost("SOL", 100.0, 2.0, 4)
    ladder = kss.projected_ladder_cost("SOL", 100.0, 2.0, 4)

    assert first == pytest.approx(40.0, rel=1e-6), "the configured wave-0 USD"
    assert first < ladder
    assert ladder == pytest.approx(first * 9.606, rel=1e-3), "1:2:3:4 rungs decayed by 2%"


# --- a symbol whose OWN take-profit can never clear the gate must say so ------


def test_a_symbol_whose_own_ceiling_is_under_the_gate_is_reported(monkeypatch):
    """The unsatisfiable-gate alarm compares `min_expectancy_pct` against the ceiling of the
    GLOBAL `scan_tp_pct`, but every symbol is backtested at its OWN autotuned take-profit.

    Expectancy tops out at `tp - round_trip_cost`, so a symbol whose autotuned tp is low has a
    lower personal ceiling — and if that sits under the gate it can NEVER pass, no matter what
    the market does. Measured on the live book 2026-08-30: TRX's autotuned tp is 1.24, ceiling
    1.24 - 0.30 = 0.94, against a 2.16 gate. It was excluded from every scan, forever, in
    complete silence, while the global alarm stayed quiet because the global ceiling (2.70) is
    comfortably above the gate. That is precisely the "skips 100% of the universe silently"
    failure the alarm exists to catch.
    """
    from app import costengine, scanner

    monkeypatch.setattr(settings, "min_expectancy_pct", 2.16)

    assert scanner._tp_cannot_clear_gate(1.2413) is True, "TRX: ceiling 0.94 under a 2.16 gate"
    assert scanner._tp_cannot_clear_gate(3.1195) is False, "BTC: ceiling 2.82 clears it"
    # and it must agree with the arithmetic it is derived from
    assert costengine.expectancy_ceiling_pct(1.2413) < settings.min_expectancy_pct


def test_the_per_symbol_check_is_off_when_the_gate_is(monkeypatch):
    from app import scanner

    monkeypatch.setattr(settings, "min_expectancy_pct", 0.0)

    assert scanner._tp_cannot_clear_gate(1.2413) is False
