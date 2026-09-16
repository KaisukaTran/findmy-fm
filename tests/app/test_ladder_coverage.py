"""Pre-booking only part of every ladder (`ladder_coverage_pct`), with a deep-ladder priority.

WHY. The book reserved the FULL 30-rung ladder for every slot: 40 sessions x $3,756 = $145k of a
$150k budget, which is what pinned the session cap at 40 while $198k of cash sat unused and the
deepest ladder the book has ever filled was 4 rungs (rung 30 needs a -69% move). Kai's decision
(2026-09-16): pre-book ~30% of each ladder - enough to fund every session to about rung 13 (-40%)
- and let the rest of the money open more sessions.

The freed money is only safe because of the guarantee half, so the two ship together:
`deep_ladder_lock_rungs` makes a session that has actually filled K rungs lock its WHOLE remaining
ladder against the budget, which stops new opens exactly when existing ladders start needing the
cash. `cash_floor_usd` (already enforced per-BUY) is the hard floor underneath both.

Defaults are today's behaviour on purpose (coverage 100%, deep lock off): a knob whose default
changes behaviour is the 2026-09-15 reconcile-cadence trap.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.portfolio as portfolio
from app import capital, models, scanner
from app.config import settings
from app.kss import service
from app.main import app as fastapi_app


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(portfolio, "get_current_prices", lambda syms: dict.fromkeys(syms, 100.0))
    with TestClient(fastapi_app) as c:
        yield c


class TestTheCoverageFraction:
    def test_default_is_todays_arithmetic(self):
        assert settings.ladder_coverage_pct == 100.0
        over, worst, budget = capital.ladder_budget_exceeded(
            max_concurrent=60, ladder_cost=2_600.0, equity=200_000.0, backup_pct=25.0)
        assert over and worst == pytest.approx(156_000) and budget == pytest.approx(150_000)

    def test_covering_thirty_percent_books_thirty_percent(self):
        over, worst, _ = capital.ladder_budget_exceeded(
            max_concurrent=60, ladder_cost=2_600.0, equity=200_000.0, backup_pct=25.0,
            coverage_pct=30.0)
        assert not over and worst == pytest.approx(46_800)

    def test_the_decided_paper_posture_fits_and_the_old_one_would_not(self):
        # Kai 2026-09-16: 80 sessions, $28 first wave, 30 rungs / 4%, 24.8% backup.
        ladder = service.ladder_cost_for(28.0, 4.0, 30)
        args = {"max_concurrent": 80, "ladder_cost": ladder, "equity": 199_980.0,
                "backup_pct": 24.8}
        assert not capital.ladder_budget_exceeded(**args, coverage_pct=30.0)[0]
        assert capital.ladder_budget_exceeded(**args, coverage_pct=100.0)[0]

    def test_coverage_cannot_erase_the_gate(self):
        # 0 (or negative) would make every configuration payable, which is the silent-disable
        # failure this gate exists to prevent, so it clamps back to full coverage.
        over, worst, _ = capital.ladder_budget_exceeded(
            max_concurrent=60, ladder_cost=2_600.0, equity=200_000.0, backup_pct=25.0,
            coverage_pct=0.0)
        assert over and worst == pytest.approx(156_000)


class TestTheScannerGateUsesIt:
    def test_default_coverage_leaves_the_gate_where_it_was(self, db, monkeypatch):
        monkeypatch.setattr("app.risk.account_equity", lambda _db: 1000.0)
        monkeypatch.setattr(settings, "equity_backup_pct", 25.0)  # budget 750
        monkeypatch.setattr(settings, "max_concurrent_sessions", 100)
        monkeypatch.setattr(settings, "ladder_coverage_pct", 100.0)
        assert scanner._can_open(db, 700.0)[0]
        assert not scanner._can_open(db, 800.0)[0]

    def test_a_thirty_percent_book_admits_a_ladder_three_times_the_budget(self, db, monkeypatch):
        monkeypatch.setattr("app.risk.account_equity", lambda _db: 1000.0)
        monkeypatch.setattr(settings, "equity_backup_pct", 25.0)  # budget 750
        monkeypatch.setattr(settings, "max_concurrent_sessions", 100)
        monkeypatch.setattr(settings, "ladder_coverage_pct", 30.0)
        ok, _ = scanner._can_open(db, 2_000.0)  # books 600
        assert ok
        assert not scanner._can_open(db, 3_000.0)[0]  # books 900 > 750

    def test_the_concurrency_cap_still_binds(self, db, monkeypatch):
        monkeypatch.setattr("app.risk.account_equity", lambda _db: 1_000_000.0)
        monkeypatch.setattr(settings, "ladder_coverage_pct", 1.0)
        monkeypatch.setattr(settings, "max_concurrent_sessions", 0)
        ok, why = scanner._can_open(db, 1.0)
        assert not ok and "max concurrent" in why


class TestTheDeepLadderGuarantee:
    @staticmethod
    def _session(rungs: int, reserved: float = 1000.0, used: float = 100.0):
        return models.KssSession(isolated_fund=reserved, total_cost=used, current_wave=rungs)

    def test_off_by_default_keeps_the_fifty_percent_money_rule(self):
        assert settings.deep_ladder_lock_rungs == 0
        assert scanner._session_lock(self._session(rungs=29)) == 100.0
        assert scanner._session_lock(self._session(rungs=0, used=600.0)) == 1000.0

    def test_a_session_at_the_rung_threshold_locks_its_whole_ladder(self, monkeypatch):
        monkeypatch.setattr(settings, "deep_ladder_lock_rungs", 10)
        assert scanner._session_lock(self._session(rungs=10)) == 1000.0
        assert scanner._session_lock(self._session(rungs=11)) == 1000.0

    def test_a_shallow_session_still_lends_its_idle_reservation(self, monkeypatch):
        monkeypatch.setattr(settings, "deep_ladder_lock_rungs", 10)
        assert scanner._session_lock(self._session(rungs=9)) == 100.0

    def test_deep_sessions_stop_new_opens_even_under_a_thin_coverage(self, db, monkeypatch):
        """The guarantee: freed money is reclaimed the moment ladders actually go deep."""
        monkeypatch.setattr("app.risk.account_equity", lambda _db: 1000.0)
        monkeypatch.setattr(settings, "equity_backup_pct", 25.0)  # budget 750
        monkeypatch.setattr(settings, "max_concurrent_sessions", 100)
        monkeypatch.setattr(settings, "ladder_coverage_pct", 30.0)
        monkeypatch.setattr(settings, "deep_ladder_lock_rungs", 10)
        row = models.KssSession(
            symbol="DEEP", entry_price=1.0, distance_pct=4.0, max_waves=30,
            isolated_fund=700.0, tp_pct=5.0, timeout_x_min=1440.0, gap_y_min=0.0,
            status=models.SESSION_ACTIVE, total_cost=120.0, current_wave=12,
        )
        db.add(row)
        db.commit()
        ok, why = scanner._can_open(db, 200.0)  # would book 60 on top of a 700 lock
        assert not ok and "dự phòng" in why


class TestTheSettingsEndpointJudgesTheSameNumbers:
    @staticmethod
    def _base(monkeypatch):
        monkeypatch.setattr(settings, "account_equity", 200_000.0)
        monkeypatch.setattr(settings, "equity_backup_pct", 24.8)
        monkeypatch.setattr(settings, "scan_distance_pct", 4.0)
        monkeypatch.setattr(settings, "scan_max_waves", 30)
        monkeypatch.setattr(settings, "kss_first_wave_usd", 17.0)
        monkeypatch.setattr(settings, "max_concurrent_sessions", 40)
        monkeypatch.setattr(settings, "ladder_coverage_pct", 100.0)

    def test_eighty_slots_are_refused_at_full_coverage(self, client, monkeypatch):
        self._base(monkeypatch)
        r = client.post("/api/kss-settings", json={"max_concurrent_sessions": 80})
        assert r.status_code == 400 and "ngân sách" in r.json()["detail"]

    def test_the_same_request_passes_once_coverage_is_thirty(self, client, monkeypatch):
        self._base(monkeypatch)
        r = client.post("/api/kss-settings",
                        json={"max_concurrent_sessions": 80, "kss_first_wave_usd": 28.0,
                              "ladder_coverage_pct": 30.0})
        assert r.status_code == 200, r.text

    def test_lowering_coverage_alone_is_judged(self, client, monkeypatch):
        """Coverage is an input to the invariant, so touching it must re-run the check."""
        self._base(monkeypatch)
        monkeypatch.setattr(settings, "kss_first_wave_usd", 400.0)  # 40 x $88k ladders
        r = client.post("/api/kss-settings", json={"ladder_coverage_pct": 90.0})
        assert r.status_code == 400
