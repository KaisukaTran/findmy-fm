"""P3: scanner funnel (`/partials/scan-funnel` + app/scanfunnel.py) —
docs/ui-rebuild-brief.md §5.1.1/§12/§13, the P3 funnel spec.

Covers: 200 on an empty DB (no 500 with zero rows), the window=24h|7d toggle
(both work AND the returned partial bakes the selected window into its own
hx-get), row counts are internally consistent (percentages derived from the
right denominator, counts sourced from audit_log — NOT candidates.decision),
window boundaries are respected, and the budget-skip row's warning class
fires only once it dominates the window's scan cycles.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

import app.portfolio as portfolio
from app import scanfunnel
from app.clock import utcnow
from app.main import app as fastapi_app
from app.models import AuditLog, Candidate, ScanRun


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(portfolio, "get_current_prices", lambda syms: dict.fromkeys(syms, 60000.0))
    with TestClient(fastapi_app) as c:
        yield c


def _audit(db, actor, action, created_at, entity=None, **detail):
    row = AuditLog(
        actor=actor, action=action, entity=entity,
        detail=json.dumps(detail) if detail else None, created_at=created_at,
    )
    db.add(row)
    db.flush()
    return row


def _scan_run(db, created_at, universe_size=10):
    row = ScanRun(mode="semi", universe_size=universe_size, started_at=created_at)
    db.add(row)
    db.flush()
    return row


def _candidate(db, scan_id, symbol, decision, created_at):
    row = Candidate(scan_id=scan_id, symbol=symbol, decision=decision, created_at=created_at)
    db.add(row)
    db.flush()
    return row


# --- endpoint: empty DB, no 500 ------------------------------------------


def test_scan_funnel_200_on_empty_db(client):
    for window in ("24h", "7d"):
        r = client.get(f"/partials/scan-funnel?window={window}")
        assert r.status_code == 200, r.text[:300]
        assert 'id="scan-funnel"' in r.text
        # every pipeline label must render even with zero rows
        assert "Ứng viên đã chấm" in r.text
        assert "Đã mở phiên" in r.text
        assert ">0<" in r.text or "0.0%" in r.text  # zero counts, not a crash


def test_scan_funnel_unknown_window_falls_back_to_24h(client):
    r = client.get("/partials/scan-funnel?window=bogus")
    assert r.status_code == 200
    assert 'window=24h' in r.text


# --- window toggle bakes the selected window into the partial's own hx-get ---


@pytest.mark.parametrize("window", ["24h", "7d"])
def test_scan_funnel_window_toggle_bakes_querystring(client, window):
    r = client.get(f"/partials/scan-funnel?window={window}")
    assert r.status_code == 200
    assert f'hx-get="/partials/scan-funnel?window={window}"' in r.text


# --- funnel_view(): counts from audit_log, not candidates.decision -------


def test_funnel_counts_from_audit_log_not_decision(db):
    """The late blocking branches (Grok veto, per-scan cap, per-symbol cap) never
    flip candidates.decision back to 'skip' — the funnel must still show them via
    their audit_log action, independent of what `decision` says."""
    now = utcnow()
    run = _scan_run(db, now)
    # Candidate.decision stays 'trade' even though it was later blocked by Grok —
    # this is exactly the ~60x undercount bug the funnel must not repeat.
    _candidate(db, run.id, "DOGE", "trade", now)
    _audit(db, "scanner", "candidate", now, entity="DOGE", decision="trade")
    _audit(db, "grok", "scanner_veto", now, entity="DOGE", reason="đà giảm mạnh")
    db.commit()

    f = scanfunnel.funnel_view(db, window="24h")
    rows = {r["key"]: r for r in f["rows"]}
    assert rows["scanner_veto"]["count"] == 1
    # candidates.decision='trade' still counts DOGE as "trade" (row 6) — the funnel
    # doesn't hide that mismatch, it just doesn't treat decision as truth for row 13.
    assert rows["trade"]["count"] == 1


def test_funnel_percentages_use_the_documented_denominator(db):
    now = utcnow()
    run = _scan_run(db, now)
    for i in range(4):
        _audit(db, "scanner", "candidate", now, entity=f"C{i}")
    _audit(db, "scanner", "skipped_thin_data", now, count=1)
    db.commit()

    f = scanfunnel.funnel_view(db, window="24h")
    rows = {r["key"]: r for r in f["rows"]}
    assert rows["candidate"]["count"] == 4
    assert rows["candidate"]["pct"] == 100.0
    # 1/4 candidates -> 25%, NOT relative to scan_runs.
    assert rows["skipped_thin_data"]["pct"] == pytest.approx(25.0)
    # scan_runs-based rows use the scan_runs denominator, not candidate_total.
    assert rows["scan_runs"]["pct"] == 100.0


def test_funnel_respects_the_time_window(db):
    now = utcnow()
    run_recent = _scan_run(db, now - timedelta(hours=1))
    run_old = _scan_run(db, now - timedelta(days=10))
    _audit(db, "scanner", "candidate", now - timedelta(hours=1), entity="BTC")
    _audit(db, "scanner", "candidate", now - timedelta(days=10), entity="OLD")
    db.commit()

    f24 = scanfunnel.funnel_view(db, window="24h")
    f7d = scanfunnel.funnel_view(db, window="7d")
    assert f24["scan_runs"] == 1  # only the recent run counts
    assert f24["candidate_total"] == 1
    assert f7d["scan_runs"] == 1  # the 10-day-old run is outside 7d too
    assert f7d["candidate_total"] == 1
    assert run_recent.id and run_old.id  # both rows exist, just filtered out


# --- the whole-cycle budget-skip row: warn class when it dominates -------


def test_budget_skip_row_warns_when_it_dominates_scan_cycles(db):
    now = utcnow()
    # 3 scan cycles total; 2 of them skipped the whole cycle for budget -> 2/3 > 50%.
    for _ in range(3):
        _scan_run(db, now)
    for _ in range(2):
        _audit(db, "scanner", "scan_skipped", now,
               reason="vượt ngân sách triển khai (giữ 25% dự phòng)")
    db.commit()

    f = scanfunnel.funnel_view(db, window="24h")
    rows = {r["key"]: r for r in f["rows"]}
    assert rows["scan_skipped_budget"]["count"] == 2
    assert rows["scan_skipped_budget"]["warn"] is True
    assert rows["scan_skipped_max_concurrent"]["count"] == 0
    assert rows["scan_skipped_max_concurrent"]["warn"] is False


def test_budget_skip_row_does_not_warn_when_it_does_not_dominate(db):
    now = utcnow()
    for _ in range(10):
        _scan_run(db, now)
    _audit(db, "scanner", "scan_skipped", now, reason="vượt ngân sách triển khai")
    db.commit()

    f = scanfunnel.funnel_view(db, window="24h")
    rows = {r["key"]: r for r in f["rows"]}
    assert rows["scan_skipped_budget"]["count"] == 1
    assert rows["scan_skipped_budget"]["warn"] is False


def test_budget_skip_row_renders_warning_class_over_http(client, db):
    now = utcnow()
    for _ in range(3):
        _scan_run(db, now)
    for _ in range(2):
        _audit(db, "scanner", "scan_skipped", now, reason="vượt ngân sách triển khai")
    db.commit()

    r = client.get("/partials/scan-funnel?window=24h")
    assert r.status_code == 200
    assert 'class="funnel-warn"' in r.text
