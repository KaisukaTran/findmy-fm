"""
Turning OFF a profit feature must never turn off protection.

`run_position_guard` routes each session one of two ways: `if row.trail_active` hands it to the
dynamic channel, and the `elif` gives everything else the fast avg-anchored hard-SL net that runs
every ~90s. `_evaluate_dynamic_exit` returns immediately when `kss_dynamic_tp_enabled` is off — so
a session that armed while the feature was ON, and is still open when it is switched OFF, matches
the `if`, gets nothing done, and never reaches the `elif`. Its hard stop degrades from the 90s
guard to the 15-minute cycle, silently.

Found live 2026-09-09, minutes after switching the feature off to run an A/B: PUMP #47 was armed,
at wave 2 of 3, and left with no sub-cycle protection at all. The feature switch is a
profit-taking preference; the hard SL is the disaster floor. A preference must never be able to
disarm the floor — the same rule as "exits are never gated", one level down.
"""

from __future__ import annotations

import pytest

from app import models
from app.config import settings
from app.kss import service
from app.models import KssSession


@pytest.fixture
def armed_session(db):
    row = KssSession(
        symbol="SOL", entry_price=10.0, distance_pct=3.0, max_waves=3, isolated_fund=300.0,
        tp_pct=5.0, timeout_x_min=60, gap_y_min=5, status=models.SESSION_ACTIVE,
        current_wave=1, avg_price=10.0, total_filled_qty=3.0, total_cost=30.0,
        trail_active=True, trail_sl_price=10.2, peak_price=10.5,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _run_guard(db, monkeypatch, price: float):
    monkeypatch.setattr("app.market.get_current_prices",
                        lambda syms, force=False: dict.fromkeys(syms, price))
    monkeypatch.setattr("app.orders.approve_order", lambda db_, oid, reviewer=None: None)
    return service.run_position_guard(db)


class TestAnArmedSessionKeepsItsHardStopWhenTheFeatureIsOff:
    def test_the_hard_sl_still_fires_with_the_dynamic_exit_disabled(
        self, db, armed_session, monkeypatch
    ):
        # avg 10.0, sl_pct 8% -> floor 9.20. Price below it must be cut by the 90s guard.
        monkeypatch.setattr(settings, "kss_dynamic_tp_enabled", False)
        monkeypatch.setattr(settings, "sl_pct", 8.0)
        _run_guard(db, monkeypatch, 9.0)
        db.refresh(armed_session)
        assert armed_session.status != models.SESSION_ACTIVE, (
            "an armed session lost its 90s hard-stop net when the profit feature was switched off")

    def test_it_is_not_cut_while_it_is_above_the_floor(self, db, armed_session, monkeypatch):
        monkeypatch.setattr(settings, "kss_dynamic_tp_enabled", False)
        monkeypatch.setattr(settings, "sl_pct", 8.0)
        _run_guard(db, monkeypatch, 9.9)
        db.refresh(armed_session)
        assert armed_session.status == models.SESSION_ACTIVE

    def test_with_the_feature_ON_the_trail_channel_still_owns_it(
        self, db, armed_session, monkeypatch
    ):
        # The fix must not steal armed sessions away from the dynamic channel when it IS on:
        # price at the carried trail stop exits via the channel, above the hard floor.
        monkeypatch.setattr(settings, "kss_dynamic_tp_enabled", True)
        monkeypatch.setattr(settings, "sl_pct", 8.0)
        monkeypatch.setattr(service, "_tp_clears_cost", lambda db_, sym, px: True)
        _run_guard(db, monkeypatch, 10.15)          # <= trail_sl 10.2, well above the 9.2 floor
        db.refresh(armed_session)
        assert armed_session.status != models.SESSION_ACTIVE
