"""`first_wave_max_usd`: a dollar ceiling on the %-of-equity first wave.

Why it exists: with a pure percentage the budget gate and the ladder cost both scale with equity,
so equity cancels out and the book holds a FIXED number of sessions forever (~17 at 10 rungs @7%).
A ceiling lets profit past `cap / pct` open new sessions instead of growing each one.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app import capital_scale, risk, runtime
from app.config import settings
from app.kss import service as kss_service


def _setup(monkeypatch: pytest.MonkeyPatch, db: Session, *, equity: float, cap: float,
           enabled: bool = True) -> None:
    monkeypatch.setattr(risk, "account_equity", lambda _db: equity)
    monkeypatch.setattr(settings, "capital_scale_enabled", enabled)
    monkeypatch.setattr(settings, "first_wave_pct", 0.4)
    monkeypatch.setattr(settings, "first_wave_max_usd", cap)
    monkeypatch.setattr(settings, "kss_first_wave_usd", 28.0)
    monkeypatch.setattr(settings, "scan_min_notional", 10.0)
    runtime.set(db, runtime.KEY_CAPITAL_SCALE_ANCHOR, equity)  # pin: no deadband surprises


def test_below_the_threshold_the_percentage_rules(db, monkeypatch):
    _setup(monkeypatch, db, equity=7_000.0, cap=40.0)   # 0.4% of $7k = $28 < $40
    got = capital_scale.first_wave_usd(db)
    assert got.value == pytest.approx(28.0)
    assert got.capped is False


def test_past_the_threshold_the_cap_binds(db, monkeypatch):
    _setup(monkeypatch, db, equity=20_000.0, cap=40.0)  # 0.4% of $20k = $80 > $40
    got = capital_scale.first_wave_usd(db)
    assert got.value == pytest.approx(40.0)
    assert got.capped is True
    assert got.floored is False


def test_zero_means_no_cap(db, monkeypatch):
    _setup(monkeypatch, db, equity=20_000.0, cap=0.0)
    got = capital_scale.first_wave_usd(db)
    assert got.value == pytest.approx(80.0)
    assert got.capped is False


def test_the_exchange_floor_beats_a_cap_set_below_it(db, monkeypatch):
    _setup(monkeypatch, db, equity=20_000.0, cap=5.0)
    assert capital_scale.first_wave_usd(db).value == pytest.approx(10.0)


def test_the_cap_is_ignored_while_scaling_is_off(db, monkeypatch):
    """Off must stay a true no-op: the absolute knob is already a fixed dollar figure."""
    _setup(monkeypatch, db, equity=20_000.0, cap=5.0, enabled=False)
    got = capital_scale.first_wave_usd(db)
    assert got.value == pytest.approx(28.0)
    assert got.capped is False


def test_a_capped_wave_makes_room_for_more_sessions(db, monkeypatch):
    """The point of the feature, in the currency the scanner uses: a smaller wave means a cheaper
    ladder, so the same budget reserves room for more sessions once the cap binds."""
    _setup(monkeypatch, db, equity=20_000.0, cap=0.0)
    uncapped = kss_service.ladder_cost_for(capital_scale.first_wave_usd(db).value, 7.0, 10)
    _setup(monkeypatch, db, equity=20_000.0, cap=40.0)
    capped = kss_service.ladder_cost_for(capital_scale.first_wave_usd(db).value, 7.0, 10)
    assert capped == pytest.approx(uncapped / 2)  # $40 vs $80 wave -> half the ladder, 2x sessions


def test_new_sessions_open_at_the_capped_wave(db, monkeypatch):
    _setup(monkeypatch, db, equity=20_000.0, cap=40.0)
    monkeypatch.setattr("app.kss.pyramid.get_exchange_info",
                        lambda s: {"minQty": 0.00001, "stepSize": 0.00001, "maxQty": 1e6})
    row = kss_service.create_session(db, symbol="ETH", entry_price=3000.0, distance_pct=7.0,
                                     max_waves=10, isolated_fund=1_000.0, tp_pct=5.0,
                                     timeout_x_min=9999.0, gap_y_min=0.0)
    assert row.first_wave_usd == pytest.approx(40.0)
