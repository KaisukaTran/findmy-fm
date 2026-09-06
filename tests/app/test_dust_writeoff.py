"""
Tests for the dust write-off on a filled take-profit (2026-09-06).

WHY this file exists — live order 118, FIL session 35:
    Binance takes a spot BUY's commission out of the ASSET BOUGHT, so a filled position is
    almost never step-legal. 51.07 FIL requested arrived as 50.993395; the take-profit could
    only offer a step-legal 50.99; 0.003395 FIL (~$0.0027) was left behind. The completion test
    was `remaining > 1e-9` — pure float-noise tolerance — so the session stayed ACTIVE holding
    that sliver. The trailing channel then fired an exit on the dust, the venue rejected the
    size, and the retry re-alerted on every restart (the alert throttle is in-memory).

    The fix must cut exactly one way: write off what the venue would refuse, and NEVER shorten
    a real partial fill, which under the resting maker model is routine in a thin book.
"""

from __future__ import annotations

import pytest

from app import models
from app.kss import service
from app.models import SESSION_ACTIVE, SESSION_COMPLETED, KssSession

FILTERS = {"minQty": 0.01, "stepSize": 0.01, "minNotional": 5.0, "maxQty": 10000.0}


@pytest.fixture
def fil_session(db):
    """The live shape: 50.993395 FIL held, take-profit about to fill 50.99 of it."""
    row = KssSession(
        symbol="FIL", entry_price=0.7833, distance_pct=3.22, max_waves=3,
        isolated_fund=232.11, tp_pct=5.47, timeout_x_min=10080.0, gap_y_min=0.0,
        status=SESSION_ACTIVE, current_wave=1, avg_price=0.7844,
        total_filled_qty=50.993395, total_cost=40.0,
    )
    db.add(row)
    db.commit()
    return row


class TestRemainderIsUnsellable:
    def test_the_live_fil_sliver_is_dust(self, fil_session, monkeypatch):
        monkeypatch.setattr("app.market.get_exchange_info", lambda s: FILTERS)
        assert service._remainder_is_unsellable(fil_session, 0.003395, 0.8293)

    def test_a_real_partial_fill_is_not_dust(self, fil_session, monkeypatch):
        # Half the position left: a resting maker exit filling in pieces. Writing this off
        # would abandon $20 of inventory with no managed exit — the opposite failure.
        monkeypatch.setattr("app.market.get_exchange_info", lambda s: FILTERS)
        assert not service._remainder_is_unsellable(fil_session, 25.0, 0.8293)

    def test_a_sellable_small_remainder_is_not_dust(self, fil_session, monkeypatch):
        # 0.5 FIL ~ $0.41: under 1% of the position, but a legal quantity above every venue
        # floor except notional... so notional is what must decide. Raise it above $0.41.
        monkeypatch.setattr("app.market.get_exchange_info",
                            lambda s: {**FILTERS, "minNotional": 0.10})
        assert not service._remainder_is_unsellable(fil_session, 0.5, 0.8293)

    def test_the_size_belt_overrides_a_huge_venue_floor(self, fil_session, monkeypatch):
        # A lookup failure (or an odd venue) reporting minNotional = $1000 must NOT license
        # writing off a real slice of the position.
        monkeypatch.setattr("app.market.get_exchange_info",
                            lambda s: {**FILTERS, "minNotional": 1000.0})
        assert not service._remainder_is_unsellable(fil_session, 25.0, 0.8293)

    def test_below_min_qty_is_dust(self, fil_session, monkeypatch):
        monkeypatch.setattr("app.market.get_exchange_info",
                            lambda s: {**FILTERS, "stepSize": 0.0, "minNotional": 0.0})
        assert service._remainder_is_unsellable(fil_session, 0.005, 0.8293)

    def test_zero_and_negative_are_dust(self, fil_session):
        assert service._remainder_is_unsellable(fil_session, 0.0, 0.8293)
        assert service._remainder_is_unsellable(fil_session, -1e-12, 0.8293)


class TestTakeProfitCompletion:
    def _fill(self, db, row, qty, price=0.8293):
        return service.handle_fill_event(db, f"pyramid:{row.id}:tp", qty, price)

    def test_a_tp_leaving_dust_completes_the_session(self, db, fil_session, monkeypatch):
        monkeypatch.setattr("app.market.get_exchange_info", lambda s: FILTERS)
        out = self._fill(db, fil_session, 50.99)

        assert out["action"] == "completed"
        assert fil_session.status == SESSION_COMPLETED
        # And it says so out loud: a written-off remainder must be auditable, not silent.
        writeoff = (db.query(models.AuditLog)
                    .filter(models.AuditLog.action == "dust_writeoff").one())
        assert writeoff.entity == f"session:{fil_session.id}"
        assert "FIL" in (writeoff.detail or "")

    def test_a_partial_tp_still_keeps_the_session_alive(self, db, fil_session, monkeypatch):
        monkeypatch.setattr("app.market.get_exchange_info", lambda s: FILTERS)
        out = self._fill(db, fil_session, 25.0)

        assert out["action"] == "partial_tp"
        assert fil_session.status == SESSION_ACTIVE
        assert round(fil_session.total_filled_qty, 6) == round(50.993395 - 25.0, 6)
        assert not db.query(models.AuditLog).filter(
            models.AuditLog.action == "dust_writeoff").all()

    def test_an_exact_fill_completes_without_a_writeoff_row(self, db, fil_session, monkeypatch):
        monkeypatch.setattr("app.market.get_exchange_info", lambda s: FILTERS)
        out = self._fill(db, fil_session, 50.993395)

        assert out["action"] == "completed"
        assert fil_session.status == SESSION_COMPLETED
        assert not db.query(models.AuditLog).filter(
            models.AuditLog.action == "dust_writeoff").all()
