"""TDD for app.lossautopsy — Loss Autopsy Phase 0 (session-level win/loss discrimination).

Read-only join of realized KSS exits back to entry-time Candidate metrics. See
app/lossautopsy.py docstring for the algorithm; this file exercises every
root-cause branch plus the summary/discrimination math.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from app import lossautopsy
from app.models import SESSION_COMPLETED, Candidate, Fill, KssSession, ScanRun

_REQUIRED = {
    "entry_price": 100.0, "distance_pct": 2.0, "isolated_fund": 1000.0, "tp_pct": 4.0,
    "timeout_x_min": 1440.0, "gap_y_min": 0.0,
}
_ENTRY = datetime(2026, 1, 1, 0, 0, 0)
_EXIT = datetime(2026, 1, 2, 0, 0, 0)


def _session(db, sid, **kw):
    d = {
        "id": sid, "symbol": "AAA", "max_waves": 8, "status": SESSION_COMPLETED,
        "strategy_mode": "dca_down", "current_wave": 1, "total_cost": 0.0,
        "started_at": _ENTRY,
        **_REQUIRED,
    }
    d.update(kw)
    row = KssSession(**d)
    db.add(row)
    return row


def _candidate(db, scan_id, session_id, **kw):
    d = {
        "scan_id": scan_id, "session_id": session_id, "symbol": "AAA",
        "consensus_pct": 0.0, "win_rate_lb": 0.0, "expectancy": 0.0,
        "trials": 0, "avg_mae": 0.0, "worst_mae": 0.0, "decision": "trade",
        "reason": "",
    }
    d.update(kw)
    db.add(Candidate(**d))


def _sell(db, symbol, pnl, ref, when=_EXIT):
    db.add(Fill(symbol=symbol, side="SELL", quantity=1.0, price=100.0, fee=0.1,
                realized_pnl=pnl, source_ref=ref, executed_at=when))


def _build_scenarios(db, monkeypatch):
    """One DB holding all six root-cause scenarios from the spec, so the size_outlier
    p95 threshold is computed across a realistic multi-session set (a single-session
    fixture would make that lone session its own p95 trivially)."""
    scan = ScanRun()
    db.add(scan)
    db.flush()

    # 1) winner — a real Candidate at entry, TP exit, positive PnL.
    _session(db, 1, total_cost=1000.0, current_wave=2, max_waves=8)
    _candidate(db, scan.id, 1, consensus_pct=60.0, win_rate_lb=90.0, expectancy=2.0,
               trials=10, avg_mae=-2.0, worst_mae=-5.0, reason="grok ok")
    _sell(db, "AAA", 50.0, "pyramid:1:tp")

    # 2) dup_wave loser. The UNIQUE(session_id, wave_num) constraint (the actual fix for
    # this historical bug — see tests/app/test_kss_dup_wave.py) means a real duplicate
    # KssWave row can no longer be persisted, so `_has_dup_wave` is stubbed for sid=2 to
    # exercise the root-cause *priority* logic in isolation from that (already-tested) guard.
    _session(db, 2, total_cost=800.0, current_wave=3, max_waves=8)
    monkeypatch.setattr(lossautopsy, "_has_dup_wave", lambda db, sid: sid == 2)
    _sell(db, "AAA", -100.0, "pyramid:2:sl")

    # 3) pyramid_up_reversal loser — base-only (current_wave<=1), stopped out.
    _session(db, 3, strategy_mode="pyramid_up", total_cost=900.0, current_wave=0, max_waves=5)
    _sell(db, "AAA", -30.0, "pyramid:3:sl")

    # 4) deep_ladder_sl loser — dca_down, stopped out past half the ladder.
    _session(db, 4, total_cost=850.0, current_wave=6, max_waves=8)
    _sell(db, "AAA", -40.0, "pyramid:4:sl")

    # 5) size_outlier loser — deploy_usd dwarfs every other session.
    _session(db, 5, total_cost=500_000.0, current_wave=1, max_waves=8)
    _candidate(db, scan.id, 5, consensus_pct=20.0, win_rate_lb=10.0, expectancy=-1.0,
               trials=5, avg_mae=-20.0, worst_mae=-30.0, reason="marginal")
    _sell(db, "AAA", -5000.0, "pyramid:5:sl")

    # 6) orphan — no session at all.
    _sell(db, "AAA", -35.0, "orphan:sl")

    db.commit()


def _case(a, sid):
    return next(c for c in a["cases"] if c["sid"] == sid)


def test_winner_session_has_no_root_cause(db, monkeypatch):
    _build_scenarios(db, monkeypatch)
    a = lossautopsy.autopsy(db)
    c = _case(a, 1)
    assert c["label"] == "win"
    assert c["root_cause"] == ""
    assert c["net"] == 50.0
    assert c["consensus"] == 60.0
    assert c["grok_endorsed"] is True
    assert c["exit_kind"] == "KSS-TP?"  # portfolio._loss_tag(":tp")


def test_dup_wave_root_cause_wins_priority(db, monkeypatch):
    _build_scenarios(db, monkeypatch)
    a = lossautopsy.autopsy(db)
    c = _case(a, 2)
    assert c["label"] == "loss"
    assert c["root_cause"] == "dup_wave"
    assert c["net"] == -100.0


def test_pyramid_up_reversal_root_cause(db, monkeypatch):
    _build_scenarios(db, monkeypatch)
    a = lossautopsy.autopsy(db)
    c = _case(a, 3)
    assert c["root_cause"] == "pyramid_up_reversal"
    assert c["net"] == -30.0


def test_deep_ladder_sl_root_cause(db, monkeypatch):
    _build_scenarios(db, monkeypatch)
    a = lossautopsy.autopsy(db)
    c = _case(a, 4)
    assert c["root_cause"] == "deep_ladder_sl"
    assert c["net"] == -40.0


def test_size_outlier_root_cause(db, monkeypatch):
    _build_scenarios(db, monkeypatch)
    a = lossautopsy.autopsy(db)
    c = _case(a, 5)
    # Whatever the exact percentile rule, session 5's $500K deploy dwarfs every other
    # session (max $1K) — it must be at/above the computed p95 and tagged accordingly.
    assert c["deploy_usd"] >= 500_000.0 * 0.99  # sanity: this IS the outlier value
    assert c["root_cause"] == "size_outlier"
    assert c["net"] == -5000.0


def test_orphan_case_sid_none(db, monkeypatch):
    _build_scenarios(db, monkeypatch)
    a = lossautopsy.autopsy(db)
    orphans = [c for c in a["cases"] if c["sid"] is None]
    assert len(orphans) == 1
    assert orphans[0]["root_cause"] == "orphan"
    assert orphans[0]["net"] == -35.0
    assert orphans[0]["entry_at"] == ""  # no KssSession -> no entry timestamp


def test_summary_counts_and_totals(db, monkeypatch):
    _build_scenarios(db, monkeypatch)
    a = lossautopsy.autopsy(db)
    s = a["summary"]
    assert s["sessions"] == 6  # 5 KSS sessions + 1 orphan bucket
    assert s["winners"] == 1
    assert s["losers"] == 5
    assert s["win_total"] == pytest.approx(50.0)
    assert s["loss_total"] == pytest.approx(-5205.0)
    assert s["net_realized"] == pytest.approx(-5155.0)


def test_cases_sorted_worst_first(db, monkeypatch):
    _build_scenarios(db, monkeypatch)
    a = lossautopsy.autopsy(db)
    nets = [c["net"] for c in a["cases"]]
    assert nets == sorted(nets)
    assert a["cases"][0]["sid"] == 5  # -5000 is the biggest loss


def test_by_root_cause_breakdown_losers_only(db, monkeypatch):
    _build_scenarios(db, monkeypatch)
    a = lossautopsy.autopsy(db)
    brc = a["by_root_cause"]
    assert set(brc) == {"dup_wave", "pyramid_up_reversal", "deep_ladder_sl",
                        "size_outlier", "orphan"}
    assert brc["size_outlier"]["count"] == 1
    assert brc["size_outlier"]["total"] == pytest.approx(-5000.0)
    # sorted ascending by total -> the worst single cause (size_outlier) comes first
    assert next(iter(brc)) == "size_outlier"


def test_discrimination_report_win_vs_loss(db, monkeypatch):
    _build_scenarios(db, monkeypatch)
    a = lossautopsy.autopsy(db)
    disc = a["discrimination"]["consensus"]
    assert disc["win"]["n"] >= 1
    assert disc["win"]["med"] == 60.0
    assert "loss" in disc
    assert disc["loss"]["n"] >= 1
    assert disc["loss"]["med"] == 20.0


def test_quantiles_known_values():
    xs = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
    q = lossautopsy._quantiles(xs)
    assert q == {"n": 10, "med": 5.5, "p25": 3.0, "p75": 8.0}


def test_quantiles_empty_is_none():
    assert lossautopsy._quantiles([]) is None


def test_empty_db_returns_zeroed_report(db):
    a = lossautopsy.autopsy(db)
    assert a["cases"] == []
    assert a["summary"] == {
        "sessions": 0, "winners": 0, "losers": 0,
        "net_realized": 0, "loss_total": 0, "win_total": 0,
    }
    assert a["by_root_cause"] == {}
    for metric in ("consensus", "win_rate_lb", "expectancy", "worst_mae", "avg_mae"):
        assert a["discrimination"][metric] == {"win": None, "loss": None}
