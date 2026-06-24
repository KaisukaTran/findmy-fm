"""
Tests for the scan mutex (no two scans at once → no SQLite 'database is locked') and the
tighter open selection: best-first ordering (win_rate_lb, trials, expectancy) + the
max_new_sessions_per_scan ramp cap.
"""

from __future__ import annotations

import pytest

from app import scanner
from app.config import settings


# ---------------------------------------------------------------------------
# Scan mutex
# ---------------------------------------------------------------------------


def test_run_scan_raises_when_already_locked(db):
    """A second concurrent scan fails fast with ScanInProgress instead of colliding."""
    assert scanner._scan_lock.acquire(blocking=False)
    try:
        with pytest.raises(scanner.ScanInProgress):
            scanner.run_scan(db)
    finally:
        scanner._scan_lock.release()


def test_scan_lock_is_free_after_failed_acquire(db):
    """A caller that lost the race must NOT release the lock it never held."""
    assert scanner._scan_lock.acquire(blocking=False)
    try:
        with pytest.raises(scanner.ScanInProgress):
            scanner.run_scan(db)
        # lock is still held by us — the failed caller left it alone
        assert scanner._scan_lock.locked() is True
    finally:
        scanner._scan_lock.release()
    assert scanner._scan_lock.locked() is False


# ---------------------------------------------------------------------------
# Open selection: best-first + per-scan cap
# ---------------------------------------------------------------------------


class _Cand:
    def __init__(self):
        self.reason = ""
        self.session_id = None


def _mk(sym, win_lb, trials, expectancy):
    return {
        "cand": _Cand(), "symbol": sym, "entry": 100.0,
        "distance_pct": 2.0, "tp_pct": 4.0, "max_waves": 6,
        "consensus": 60.0, "win_rate": 80.0, "loss_rate": 10.0, "net_edge": 3.0,
        "expectancy": expectancy, "ta": {}, "win_rate_lb": win_lb, "trials": trials,
    }


@pytest.fixture
def _open_env(monkeypatch):
    """Neutralise everything around the open decision so only ordering + cap are exercised."""
    monkeypatch.setattr("app.orchestrator.grok.scanner_enabled", lambda: False)
    monkeypatch.setattr(scanner.runtime, "is_frozen", lambda db: False)
    monkeypatch.setattr(scanner, "_symbol_at_cap", lambda db, s: False)
    monkeypatch.setattr(scanner, "_can_open", lambda db, need: (True, ""))
    monkeypatch.setattr(scanner.service, "projected_ladder_cost", lambda *a, **k: 50.0)
    monkeypatch.setattr(scanner.audit, "log", lambda *a, **k: None)
    opened: list[str] = []
    monkeypatch.setattr(scanner, "_open_session",
                        lambda db, sym, *a, **k: opened.append(sym) or 1)
    return opened


def test_per_scan_cap_opens_best_first(db, monkeypatch, _open_env):
    monkeypatch.setattr(settings, "max_new_sessions_per_scan", 2)
    to_open = [
        _mk("LOW", 60.0, 20, 3.0),
        _mk("HIGH", 90.0, 52, 3.7),
        _mk("MID", 75.0, 30, 3.5),
    ]
    scanner._review_and_open(db, to_open, "auto")
    # capped at 2, highest win_rate_lb first
    assert _open_env == ["HIGH", "MID"]


def test_opens_ranked_by_consensus_first(db, monkeypatch, _open_env):
    """Consensus leads the ranking now (the saturated win_rate_lb can't): a LOW-win_rate_lb /
    HIGH-consensus pair must open before a HIGH-win_rate_lb / LOW-consensus one."""
    monkeypatch.setattr(settings, "max_new_sessions_per_scan", 0)
    hi_cons = _mk("HICONS", 60.0, 20, 3.0); hi_cons["consensus"] = 80.0
    hi_wr = _mk("HIWR", 95.0, 52, 3.7);     hi_wr["consensus"] = 50.0
    scanner._review_and_open(db, [hi_wr, hi_cons], "auto")
    assert _open_env[0] == "HICONS"  # consensus wins despite lower win_rate_lb


def test_opens_ranked_by_worst_mae_after_consensus(db, monkeypatch, _open_env):
    """At equal consensus, the shallower-tail coin (worst_mae closer to 0) opens first — even if it
    has a lower win_rate_lb (worst_mae outranks win_rate_lb in the key)."""
    monkeypatch.setattr(settings, "max_new_sessions_per_scan", 0)
    deep = _mk("DEEP", 90.0, 52, 3.7); deep["consensus"] = 70.0; deep["worst_mae"] = -40.0
    shallow = _mk("SHALLOW", 60.0, 20, 3.0); shallow["consensus"] = 70.0; shallow["worst_mae"] = -5.0
    scanner._review_and_open(db, [deep, shallow], "auto")
    assert _open_env[0] == "SHALLOW"


def test_per_scan_cap_zero_means_no_limit(db, monkeypatch, _open_env):
    monkeypatch.setattr(settings, "max_new_sessions_per_scan", 0)
    to_open = [_mk("A", 60.0, 20, 3.0), _mk("B", 90.0, 52, 3.7), _mk("C", 75.0, 30, 3.5)]
    scanner._review_and_open(db, to_open, "auto")
    assert set(_open_env) == {"A", "B", "C"}
    assert _open_env[0] == "B"  # still best-first


def test_grok_batch_reviews_same_best_first_order(db, monkeypatch, _open_env):
    """Grok must review the SAME top candidates the open loop opens (win_lb→trials→E),
    not an expectancy-only order that can diverge from what actually opens."""
    monkeypatch.setattr(settings, "max_new_sessions_per_scan", 0)
    monkeypatch.setattr(settings, "grok_scanner_batch_max", 10)
    monkeypatch.setattr("app.orchestrator.grok.scanner_enabled", lambda: True)
    captured: dict = {}

    def _fake_review(db, items):
        captured["order"] = [it["symbol"] for it in items]
        return {}  # no verdicts → fail-open, opens proceed

    monkeypatch.setattr("app.orchestrator.grok.review_candidates", _fake_review)
    to_open = [_mk("LOW", 60.0, 20, 3.0), _mk("HIGH", 90.0, 52, 3.7), _mk("MID", 75.0, 30, 3.5)]
    scanner._review_and_open(db, to_open, "auto")
    assert captured["order"] == ["HIGH", "MID", "LOW"]


# ---------------------------------------------------------------------------
# Phase B: breadth-aware soft ramp of the per-scan open cap
# ---------------------------------------------------------------------------


def _c(closes):
    return [{"close": x} for x in closes]


def test_ramp_factor_curve():
    assert scanner._ramp_factor(0.30) == pytest.approx(0.2)   # weak breadth → min throttle
    assert scanner._ramp_factor(0.60) == pytest.approx(1.0)   # strong → full
    assert scanner._ramp_factor(0.05) == pytest.approx(0.2)   # clamp low
    assert scanner._ramp_factor(0.95) == pytest.approx(1.0)   # clamp high


def test_market_breadth_fraction_rising():
    cmap = {"A": (_c([1, 2]), True), "B": (_c([2, 1]), True), "C": (_c([1, 1]), True)}
    assert scanner._market_breadth(cmap, ["A", "B", "C"], 1) == pytest.approx(2 / 3)  # A,C ≥0


def test_effective_open_cap_throttles_in_weak_breadth(db, monkeypatch):
    monkeypatch.setattr(settings, "max_new_sessions_per_scan", 5)
    monkeypatch.setattr(settings, "rel_strength_lookback_bars", 1)
    monkeypatch.setattr(scanner.audit, "log", lambda *a, **k: None)
    weak = {f"C{i}": (_c([2, 1]), True) for i in range(10)}    # all falling → breadth 0 → factor 0.2
    monkeypatch.setattr(settings, "regime_ramp_enabled", True)
    assert scanner._effective_open_cap(db, weak, list(weak)) == 1   # 5×0.2 → max(1,1)
    monkeypatch.setattr(settings, "regime_ramp_enabled", False)
    assert scanner._effective_open_cap(db, weak, list(weak)) == 5   # off → unchanged base


# ---------------------------------------------------------------------------
# Phase C2: relative-quartile worst_mae gate
# ---------------------------------------------------------------------------


def test_mae_quartile_gate_drops_worst(db, monkeypatch):
    monkeypatch.setattr(settings, "mae_quartile_gate_enabled", True)
    monkeypatch.setattr(scanner.audit, "log", lambda *a, **k: None)
    cands = []
    for sym, wm in [("A", -5.0), ("B", -8.0), ("C", -12.0), ("D", -40.0)]:
        c = _mk(sym, 90.0, 50, 3.7); c["worst_mae"] = wm; cands.append(c)
    kept = {c["symbol"] for c in scanner._drop_worst_mae_quartile(db, cands)}
    assert "D" not in kept and {"A", "B", "C"} <= kept   # worst quartile (−40%) dropped


def test_mae_quartile_gate_noop_off_or_few(db, monkeypatch):
    monkeypatch.setattr(settings, "mae_quartile_gate_enabled", False)
    cands = [_mk("A", 90.0, 50, 3.7)]
    assert scanner._drop_worst_mae_quartile(db, cands) == cands          # off → unchanged
    monkeypatch.setattr(settings, "mae_quartile_gate_enabled", True)
    few = [_mk("A", 90.0, 50, 3.7), _mk("B", 90.0, 50, 3.7)]            # <4 → no-op
    assert scanner._drop_worst_mae_quartile(db, few) == few


# ---------------------------------------------------------------------------
# Pre-scan capacity gate: skip the whole scan when capital is saturated
# ---------------------------------------------------------------------------


def test_has_open_capacity_reflects_can_open(db, monkeypatch):
    monkeypatch.setattr(scanner.service, "projected_ladder_cost", lambda *a, **k: 100.0)
    monkeypatch.setattr(scanner, "_can_open", lambda db, need: (False, "vượt ngân sách"))
    ok, why = scanner._has_open_capacity(db)
    assert ok is False and "ngân sách" in why
    monkeypatch.setattr(scanner, "_can_open", lambda db, need: (True, ""))
    assert scanner._has_open_capacity(db)[0] is True


def test_scan_skipped_when_no_capacity(db, monkeypatch):
    """When nothing can open, run_scan records ONE skipped ScanRun and does NOT fetch/backtest."""
    from app.models import ScanRun

    monkeypatch.setattr(scanner, "data_provider", lambda: object())
    monkeypatch.setattr(scanner, "_has_open_capacity", lambda db: (False, "vượt ngân sách"))

    def _boom(*a, **k):
        raise AssertionError("universe scanned despite no open capacity")

    monkeypatch.setattr(scanner, "_universe", _boom)
    out = scanner.run_scan(db)
    assert out["candidates"] == []
    assert out.get("skipped") == "vượt ngân sách"
    row = db.query(ScanRun).filter(ScanRun.id == out["scan_id"]).one()
    assert row.universe_size == 0
