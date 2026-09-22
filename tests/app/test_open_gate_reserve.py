"""Fix A2 (open-gate reserve) + Fix A3 (isolated_fund priced at the real wave).

A3 — `service.projected_ladder_cost`/`projected_first_wave_cost` priced a hypothetical
session's ladder at the LIVE `settings.kss_first_wave_usd` unless a caller passed an explicit
`first_wave_usd`. Every scanner call site priced the RESERVATION that way while the session it
opened actually sized its rungs off `capital_scale.first_wave_usd(db)` (snapshotted onto the
row by `service.create_session`) — the two only agreed by coincidence (both $28) until capital
scaling resolves a different wave, at which point the reservation understates the real ladder
and the deepest rungs starve. Fixed by resolving the wave ONCE per candidate/session and
threading it through every pricing call.

A2 — `scanner._session_lock` used to "lend" a lightly-filled session's idle reservation back to
the open-gate budget, freeing everything above `total_cost` until a session crossed 50% spent
or `deep_ladder_lock_rungs`. But the Monte Carlo that justified `ladder_coverage_pct`
(`scripts/capital_portfolio_study.py`, `gate="reserve"`) never modelled that lending: it
pre-books `coverage_pct` of a session's FULL ladder the instant it opens and holds that flat
amount until close, regardless of fills. The app's gate is now the same flat reservation, so
"the gate is the one that was measured" — see `scanner._session_lock`'s docstring for the exact
formula.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.portfolio as portfolio
from app import capital_scale, models, risk, runtime, scanner
from app.config import settings
from app.kss import service
from app.main import app as fastapi_app

DISTANCE_PCT = 7.0
MAX_WAVES = 10


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(portfolio, "get_current_prices", lambda syms: dict.fromkeys(syms, 100.0))
    with TestClient(fastapi_app) as c:
        yield c


@pytest.fixture(autouse=True)
def _stub_network(monkeypatch):
    """`PyramidSession.__post_init__` and order queueing reach for exchange info / live prices
    over the network unless stubbed — same pattern as tests/app/test_scheduler.py."""
    monkeypatch.setattr(
        "app.kss.pyramid.get_exchange_info",
        lambda s: {"minQty": 0.00001, "stepSize": 0.00001, "maxQty": 1_000_000.0},
    )
    monkeypatch.setattr("app.kss.pyramid.get_current_prices", lambda syms: dict.fromkeys(syms, 1.0))
    monkeypatch.setattr("app.market.get_current_prices", lambda syms: dict.fromkeys(syms, 100.0))
    monkeypatch.setattr("app.orders.get_current_prices", lambda syms: dict.fromkeys(syms, 100.0))


def _walk_ladder_no_starve(py):
    """Fill every rung in sequence (far below TP, far below timeout) and assert none is ever
    refused for insufficient fund — the reservation must cover the WHOLE ladder it was priced
    for, not just however many rungs happened to fit."""
    current = 0
    while current < py.max_waves - 1:
        wave = py.waves[current]
        result = py.on_fill(
            wave.wave_num, wave.quantity, wave.target_price,
            current_market_price=wave.target_price * 0.01,
        )
        assert result.get("reason") != "insufficient_fund", (
            f"rung {current + 1}/{py.max_waves} starved: {result}"
        )
        current += 1
    return py


# --- Fix A3: the reservation must match the wave the session actually opens with -------------


class TestA3ReservationMatchesTheScaledWave:
    @staticmethod
    def _setup(monkeypatch, db, *, equity: float, enabled: bool) -> None:
        monkeypatch.setattr(risk, "account_equity", lambda _db: equity)
        monkeypatch.setattr(settings, "capital_scale_enabled", enabled)
        monkeypatch.setattr(settings, "first_wave_pct", 0.4)
        monkeypatch.setattr(settings, "first_wave_max_usd", 0.0)  # no cap: isolate the pct math
        monkeypatch.setattr(settings, "kss_first_wave_usd", 28.0)
        monkeypatch.setattr(settings, "scan_min_notional", 1.0)
        runtime.set(db, runtime.KEY_CAPITAL_SCALE_ANCHOR, equity)  # pin: no deadband surprise

    @pytest.mark.parametrize("equity, expected_wave", [(10_000.0, 40.0), (7_700.0, 30.8)])
    def test_scanner_opened_session_never_starves_at_a_scaled_wave(
        self, db, monkeypatch, equity, expected_wave,
    ):
        self._setup(monkeypatch, db, equity=equity, enabled=True)
        assert capital_scale.first_wave_usd(db).value == pytest.approx(expected_wave)

        session_id = scanner._open_session(
            db, "BTCUSDT", 100.0, "manual",
            distance_pct=DISTANCE_PCT, tp_pct=5.0, max_waves=MAX_WAVES,
        )
        row = db.get(models.KssSession, session_id)
        assert row.first_wave_usd == pytest.approx(expected_wave)

        full_ladder = service.projected_ladder_cost(
            "BTCUSDT", 100.0, DISTANCE_PCT, MAX_WAVES, first_wave_usd=expected_wave,
        )
        assert row.isolated_fund >= full_ladder - 1e-6, (
            "isolated_fund must reserve at least the full ladder at the wave the session "
            "actually opened with"
        )

        py = service._to_pyramid(row)
        _walk_ladder_no_starve(py)
        assert len(py.waves) == MAX_WAVES

    def test_capital_scale_off_is_byte_identical_to_todays_28_dollar_wave(self, db, monkeypatch):
        self._setup(monkeypatch, db, equity=200_000.0, enabled=False)

        session_id = scanner._open_session(
            db, "BTCUSDT", 100.0, "manual",
            distance_pct=DISTANCE_PCT, tp_pct=5.0, max_waves=MAX_WAVES,
        )
        row = db.get(models.KssSession, session_id)
        assert row.first_wave_usd == pytest.approx(settings.kss_first_wave_usd)

        full_ladder = service.projected_ladder_cost(
            "BTCUSDT", 100.0, DISTANCE_PCT, MAX_WAVES,
            first_wave_usd=settings.kss_first_wave_usd,
        )
        assert row.isolated_fund >= full_ladder - 1e-6

        py = service._to_pyramid(row)
        _walk_ladder_no_starve(py)
        assert len(py.waves) == MAX_WAVES


class _Cand:
    """Minimal stand-in for `models.Candidate` — `_review_and_open` only reads/writes
    `.reason` and `.session_id`."""

    def __init__(self):
        self.reason = ""
        self.session_id = None


class TestA3ReviewAndOpenThreadsTheResolvedWave:
    def test_isolated_fund_and_first_wave_usd_agree_at_a_scaled_wave(self, db, monkeypatch):
        """`_review_and_open` must resolve `capital_scale.first_wave_usd` ONCE per candidate
        and reuse that number for both the reservation and the session's own `first_wave_usd`
        — never re-resolve it independently at a later call site. Proven with a stub that
        returns a DIFFERENT number on every call: if the fix ever regresses to resolving the
        wave more than once (e.g. `_open_session` deriving its own instead of receiving the
        candidate's), `isolated_fund` and `first_wave_usd` disagree and this fails."""
        from app.orchestrator import grok

        monkeypatch.setattr(grok, "scanner_enabled", lambda: False)
        monkeypatch.setattr(scanner.runtime, "is_frozen", lambda _db: False)
        monkeypatch.setattr(scanner, "_symbol_at_cap", lambda _db, _s: False)
        monkeypatch.setattr(scanner, "_can_open", lambda _db, _need: (True, ""))

        calls = {"n": 0}
        stub_values = [40.0, 999.0, 12_345.0]  # only index 0 must ever reach the session

        def _fake_first_wave(_db):
            i = min(calls["n"], len(stub_values) - 1)
            calls["n"] += 1
            return capital_scale.Scaled(
                value=stub_values[i], pct=0.4, equity=10_000.0, absolute=28.0,
                floored=False, enabled=True,
            )

        monkeypatch.setattr(capital_scale, "first_wave_usd", _fake_first_wave)

        to_open = [{
            "cand": _Cand(), "symbol": "BTCUSDT", "entry": 100.0,
            "distance_pct": DISTANCE_PCT, "tp_pct": 5.0, "max_waves": MAX_WAVES,
            "consensus": 60.0, "win_rate": 80.0, "loss_rate": 10.0, "net_edge": 3.0,
            "expectancy": 3.0, "ta": {}, "win_rate_lb": 80.0, "trials": 10,
        }]

        scanner._review_and_open(db, to_open, "manual")

        session_id = to_open[0]["cand"].session_id
        assert session_id is not None, to_open[0]["cand"].reason
        row = db.get(models.KssSession, session_id)

        assert row.first_wave_usd == pytest.approx(40.0)
        expected_ladder = service.projected_ladder_cost(
            "BTCUSDT", 100.0, DISTANCE_PCT, MAX_WAVES, first_wave_usd=40.0,
        )
        expected_isolated = expected_ladder * (1.0 + settings.kss_ladder_reserve_slack_pct / 100.0)
        assert row.isolated_fund == pytest.approx(expected_isolated)


# --- Fix A2: the open-gate must reserve what the reserve-gate simulator measured -------------


def _shallow_session(db, symbol: str, reserved: float, used: float = 28.0,
                      current_wave: int = 0) -> models.KssSession:
    row = models.KssSession(
        symbol=symbol, entry_price=100.0, distance_pct=DISTANCE_PCT, max_waves=MAX_WAVES,
        isolated_fund=reserved, tp_pct=5.0, timeout_x_min=999_999.0, gap_y_min=0.0,
        status=models.SESSION_ACTIVE, total_cost=used, current_wave=current_wave,
    )
    db.add(row)
    return row


class TestA2ReserveGateMirrorsTheSimulator:
    @staticmethod
    def _setup(monkeypatch) -> None:
        # Paper's current posture (see docstring): $7,000 equity, 10 rungs @7%, 30% coverage,
        # 24.8% backup, deep-lock at 4 rungs.
        monkeypatch.setattr(risk, "account_equity", lambda _db: 7_000.0)
        monkeypatch.setattr(settings, "equity_backup_pct", 24.8)
        monkeypatch.setattr(settings, "ladder_coverage_pct", 30.0)
        monkeypatch.setattr(settings, "deep_ladder_lock_rungs", 4)
        monkeypatch.setattr(settings, "max_concurrent_sessions", 80)

    def test_twenty_five_shallow_sessions_saturate_the_gate(self, db, monkeypatch):
        self._setup(monkeypatch)
        reserved = service.ladder_cost_for(28.0, DISTANCE_PCT, MAX_WAVES)  # ~$1,013
        for i in range(25):
            _shallow_session(db, f"SYM{i}", reserved)
        db.commit()

        ok, why = scanner._can_open(db, reserved)

        assert not ok and "dự phòng" in why

    def test_the_gate_admits_exactly_fourteen_shallow_sessions_not_fifteen(self, db, monkeypatch):
        """Discriminating boundary case (2026-09-21 follow-up — the earlier "5 sessions still
        fit" version passed under every formula tried, old or new, since 5 sessions never got
        near saturating a $7,000 book under ANY of them, so it proved nothing about the fix).

        At this exact posture (`lock = min(reserved, used + 30% x reserved)` = $331.82/session,
        budget $5,264, a new candidate itself books 30% x $1,012.74 = $303.82): 14 sessions
        (locked $4,949.34) plus the new candidate ($303.82) = $5,253.18 <= budget -> admitted;
        15 sessions (locked $5,281.17) alone already exceeds the $5,264 budget -> refused. Under
        the OLD "lend the idle reservation" rule (spent-only, $28/session) 15 sessions would
        lock only $420 and this candidate would still be admitted — this is exactly the gap
        Fix A2 closes."""
        self._setup(monkeypatch)
        reserved = service.ladder_cost_for(28.0, DISTANCE_PCT, MAX_WAVES)
        for i in range(14):
            _shallow_session(db, f"SYM{i}", reserved)
        db.commit()

        ok, _ = scanner._can_open(db, reserved)

        assert ok

        _shallow_session(db, "SYM14", reserved)
        db.commit()

        ok, why = scanner._can_open(db, reserved)

        assert not ok and "dự phòng" in why

    def test_deep_session_still_locks_its_whole_reservation(self, monkeypatch):
        """The depth trigger is unchanged by A2 — never weaker than the coverage rule."""
        monkeypatch.setattr(settings, "ladder_coverage_pct", 30.0)
        monkeypatch.setattr(settings, "deep_ladder_lock_rungs", 4)
        row = models.KssSession(isolated_fund=1_000.0, total_cost=50.0, current_wave=4)

        assert scanner._session_lock(row) == 1_000.0

    def test_closed_sessions_lock_nothing(self, db, monkeypatch):
        """`_can_open` only sums ACTIVE sessions — a completed/stopped session's old
        reservation, however large, must never count against the budget again."""
        self._setup(monkeypatch)
        closed = models.KssSession(
            symbol="OLD", entry_price=100.0, distance_pct=DISTANCE_PCT, max_waves=MAX_WAVES,
            isolated_fund=1_000_000.0, tp_pct=5.0, timeout_x_min=999_999.0, gap_y_min=0.0,
            status=models.SESSION_COMPLETED, total_cost=1_000_000.0,
        )
        db.add(closed)
        db.commit()

        reserved = service.ladder_cost_for(28.0, DISTANCE_PCT, MAX_WAVES)
        ok, _ = scanner._can_open(db, reserved)

        assert ok


# --- Fix A3, routes.py: the settings pre-flight must judge the scaled wave too ----------------


class TestA3SettingsEndpointJudgesTheScaledWave:
    @staticmethod
    def _base(monkeypatch) -> None:
        monkeypatch.setattr(settings, "account_equity", 200_000.0)
        monkeypatch.setattr(settings, "equity_backup_pct", 24.8)
        monkeypatch.setattr(settings, "scan_distance_pct", 4.0)
        monkeypatch.setattr(settings, "scan_max_waves", 30)
        monkeypatch.setattr(settings, "kss_first_wave_usd", 17.0)
        monkeypatch.setattr(settings, "max_concurrent_sessions", 40)
        monkeypatch.setattr(settings, "ladder_coverage_pct", 100.0)
        monkeypatch.setattr(settings, "first_wave_max_usd", 0.0)

    def test_enabling_capital_scale_with_a_huge_pct_is_judged(self, client, monkeypatch):
        self._base(monkeypatch)
        # 5% of $200k = $10,000/wave — a ladder no budget check should let through silently.
        monkeypatch.setattr(settings, "first_wave_pct", 5.0)

        r = client.post("/api/kss-settings", json={"capital_scale_enabled": True})

        assert r.status_code == 400 and "ngân sách" in r.json()["detail"]

    def test_an_unrelated_edit_does_not_re_run_the_check(self, client, monkeypatch):
        self._base(monkeypatch)
        monkeypatch.setattr(settings, "first_wave_pct", 5.0)
        monkeypatch.setattr(settings, "capital_scale_enabled", True)  # already on, already bad

        r = client.post("/api/kss-settings", json={"scan_tp_pct": 10.0})

        assert r.status_code == 200, r.text
