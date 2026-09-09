"""
If every session filled its ladder, could we pay for it?

Nothing asked that question until now, and the reason it matters is `scanner._session_lock`: a
session that has filled less than half its planned ladder locks only the cash actually deployed,
and its idle reservation is LENT to new sessions. That is a deliberate rule ("lend-the-idle",
written as a user spec), and it works — but it means the deployable-budget gate sees a fraction
of the real commitment. Measured on the live book 2026-09-09: ten open sessions reserved $2,303
and the gate saw **$739**, 32% of it. So the budget gate effectively never binds, and the true
ceiling on exposure is `max_concurrent_sessions × ladder`, not the budget.

That is safe at a $40 first wave (60 × $234 = $14k against a $150k budget) and becomes a trap as
the wave grows: at $428 the same 60 slots commit $150,000 — the entire budget — while the gate
still reports about a third of it. The app opens all 60, then one broadly-correlated dip asks
every ladder to fill at once, cash runs out, and `_apply_cash_cap` starts refusing rungs. The
ladders die silently at exactly the moment averaging down is the thing they exist for.

Two numbers that only mean something relative to each other, with nothing comparing them — the
fourth instance of that shape this week (DCA step vs stop-loss, the three trail knobs, a control
vs its effect, and now this). So this is the comparison, as a hard gate on the settings endpoint,
in the same idiom as `costengine.expectancy_gate_unsatisfiable`.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.portfolio as portfolio
from app import capital
from app.config import settings
from app.kss import service
from app.main import app as fastapi_app


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(portfolio, "get_current_prices", lambda syms: dict.fromkeys(syms, 100.0))
    with TestClient(fastapi_app) as c:
        yield c


class TestTheLadderRatioMirrorsTheFrozenMath:
    """`kss.service.ladder_cost_for` re-derives what `pyramid` charges for a full ladder. A mirror
    of frozen math that can drift silently is the very defect this file exists to prevent, so
    pin it against the real thing. (It lives in the KSS layer, not in `capital.py`: pricing a
    ladder needs the strategy's SHAPE, and `test_capital_scaling.py` forbids `capital` from
    taking a shape parameter — it caught the first version of this.)"""

    @pytest.mark.parametrize("distance,waves", [(2.0, 3), (3.2, 3), (6.0, 3), (3.2, 5), (10.0, 4)])
    def test_it_matches_projected_ladder_cost(self, distance, waves, monkeypatch):
        monkeypatch.setattr(settings, "kss_first_wave_usd", 40.0)
        real = service.projected_ladder_cost("PROBE", 1.0, distance, waves)
        mirrored = service.ladder_cost_for(40.0, distance, waves)
        assert mirrored == pytest.approx(real, rel=0.02), (
            f"the mirror drifted from the frozen math: {mirrored} vs {real}")

    def test_the_live_shape_lands_where_the_book_shows(self):
        # Ten open sessions reserve $221-$235 on a $40 first wave -> 5.5x to 5.9x.
        assert 5.2 <= service.ladder_cost_for(1.0, 3.2, 3) <= 6.0


class TestTheInvariantItself:
    def test_a_config_whose_full_ladders_fit_is_allowed(self):
        over, worst, budget = capital.ladder_budget_exceeded(
            max_concurrent=60, ladder_cost=876.0, equity=200_000.0, backup_pct=25.0)
        assert not over and worst == pytest.approx(52_560) and budget == pytest.approx(150_000)

    def test_spending_exactly_the_budget_is_allowed_but_one_dollar_more_is_not(self):
        # $150k IS the deployable budget — the 25% reserve is the slack, so committing it in
        # full is arithmetically payable. First-wave $428 lands exactly here (60 x $2,500);
        # that is the boundary, not a refusal, and saying otherwise would be off by a cent.
        exactly = capital.ladder_budget_exceeded(
            max_concurrent=60, ladder_cost=2_500.0, equity=200_000.0, backup_pct=25.0)
        assert exactly[0] is False and exactly[1] == pytest.approx(150_000)
        over, worst, _ = capital.ladder_budget_exceeded(
            max_concurrent=60, ladder_cost=2_600.0, equity=200_000.0, backup_pct=25.0)
        assert over and worst == pytest.approx(156_000)

    def test_it_reads_equity_not_a_constant(self):
        # The same config is fine on a big book and refused on a small one.
        args = {"max_concurrent": 60, "ladder_cost": 876.0, "backup_pct": 25.0}
        assert not capital.ladder_budget_exceeded(equity=200_000.0, **args)[0]
        assert capital.ladder_budget_exceeded(equity=50_000.0, **args)[0]


class TestTheSettingsEndpointEnforcesIt:
    @staticmethod
    def _base(monkeypatch):
        monkeypatch.setattr(settings, "account_equity", 200_000.0)
        monkeypatch.setattr(settings, "equity_backup_pct", 25.0)
        monkeypatch.setattr(settings, "scan_distance_pct", 3.2)
        monkeypatch.setattr(settings, "scan_max_waves", 3)
        monkeypatch.setattr(settings, "kss_first_wave_usd", 40.0)
        monkeypatch.setattr(settings, "max_concurrent_sessions", 60)

    def test_the_proposed_150_config_is_accepted(self, client, monkeypatch):
        self._base(monkeypatch)
        r = client.post("/api/kss-settings", json={"kss_first_wave_usd": 150.0})
        assert r.status_code == 200, r.text

    def test_a_first_wave_that_overcommits_the_book_is_refused(self, client, monkeypatch):
        self._base(monkeypatch)
        r = client.post("/api/kss-settings", json={"kss_first_wave_usd": 600.0})
        assert r.status_code == 400
        assert "ngân sách" in r.json()["detail"] or "budget" in r.json()["detail"].lower()

    def test_raising_the_session_count_is_judged_too(self, client, monkeypatch):
        # The same exposure can be reached from the other side.
        self._base(monkeypatch)
        monkeypatch.setattr(settings, "kss_first_wave_usd", 400.0)
        r = client.post("/api/kss-settings", json={"max_concurrent_sessions": 200})
        assert r.status_code == 400

    def test_an_unrelated_knob_is_not_blocked_by_an_already_bad_config(self, client, monkeypatch):
        # Same rule as the expectancy guard: only requests that TOUCH the pair are judged, or an
        # operator who inherits a bad config can no longer edit anything at all.
        self._base(monkeypatch)
        monkeypatch.setattr(settings, "kss_first_wave_usd", 5_000.0)   # already over
        r = client.post("/api/kss-settings", json={"scan_tp_pct": 4.0})
        assert r.status_code == 200, r.text
