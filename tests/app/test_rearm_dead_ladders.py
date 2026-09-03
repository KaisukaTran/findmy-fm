"""Unit tests for scripts/rearm_dead_ladders.py's pure math (P1 deliverable 5).

Loaded by file path (it's a standalone script, not part of the `app` package) so these tests
never import app.db/app.config — no DATABASE_URL games needed for the pure functions.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _ROOT / "scripts" / "rearm_dead_ladders.py"

_spec = importlib.util.spec_from_file_location("rearm_dead_ladders", _SCRIPT)
rearm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rearm)


def test_ladder_factor_etc5_numbers():
    """ETC#5: isolated_fund 144.09, distance 2%, max_waves 4 -> first_wave ~= 15."""
    factor = rearm.ladder_factor(2.0, 4)
    first_wave = 144.09 / factor
    assert first_wave == pytest.approx(15.0, abs=0.01)


def test_rung_cost_etc5_next_wave():
    """ETC#5: at first_wave ~15, the next rung (wave index 2, the one that died) costs
    ~$43.22 — comfortably within the ~$99.73 remaining fund (144.09 - two filled waves)."""
    first_wave = 144.09 / rearm.ladder_factor(2.0, 4)
    rung = rearm.rung_cost(first_wave, 2.0, 2)
    assert rung == pytest.approx(43.22, abs=0.01)

    remaining = 99.73
    assert rung < remaining  # affordable under Fix 2 (session-level first_wave_usd)


def test_ladder_factor_matches_estimate_total_cost_shape():
    """Cross-check against the frozen PyramidSession math directly (no step snapping, i.e.
    tiny step/minQty so rounding is a no-op): first_wave_usd * ladder_factor must reproduce
    estimate_total_cost's ladder sum, for arbitrary distance/max_waves."""
    from app.kss.pyramid import PyramidSession

    first_wave_usd = 22.5
    entry = 37.0
    distance = 3.0
    max_waves = 6
    s = PyramidSession(symbol="ZZZ", entry_price=entry, distance_pct=distance,
                       max_waves=max_waves, isolated_fund=1.0, tp_pct=1.0,
                       timeout_x_min=1.0, gap_y_min=0.0, first_wave_usd=first_wave_usd)
    s._step_size = 1e-12  # effectively no step-rounding, matching rung_cost's raw arithmetic
    s._min_qty = 0.0
    expected = s.estimate_total_cost()
    got = first_wave_usd * rearm.ladder_factor(distance, max_waves)
    assert got == pytest.approx(expected, rel=1e-6)


def test_ladder_factor_zero_max_waves_is_zero():
    assert rearm.ladder_factor(2.0, 0) == 0.0


# ---- end-to-end dry-run against a throwaway sqlite file (subprocess, real script entry) ----


def _make_test_db(path: Path) -> None:
    """Build a minimal sqlite file with one ACTIVE kss_sessions row shaped like ETC#5 —
    just enough columns for the script's query + report; the script only reads/writes
    kss_sessions, so nothing else needs to exist."""
    import sqlite3

    con = sqlite3.connect(path)
    con.execute(
        """
        CREATE TABLE kss_sessions (
            id INTEGER PRIMARY KEY,
            symbol VARCHAR(20) NOT NULL,
            entry_price FLOAT NOT NULL,
            distance_pct FLOAT NOT NULL,
            max_waves INTEGER NOT NULL,
            isolated_fund FLOAT NOT NULL,
            tp_pct FLOAT NOT NULL,
            timeout_x_min FLOAT NOT NULL,
            gap_y_min FLOAT NOT NULL,
            sl_pct FLOAT NOT NULL DEFAULT 0.0,
            trailing_pct FLOAT NOT NULL DEFAULT 0.0,
            peak_price FLOAT NOT NULL DEFAULT 0.0,
            trail_active BOOLEAN NOT NULL DEFAULT 0,
            trail_sl_price FLOAT NOT NULL DEFAULT 0.0,
            trail_dist_pct FLOAT NOT NULL DEFAULT 0.0,
            status VARCHAR(16) NOT NULL DEFAULT 'pending',
            strategy_mode TEXT DEFAULT 'dca_down',
            first_wave_usd FLOAT,
            current_wave INTEGER NOT NULL DEFAULT 0,
            avg_price FLOAT NOT NULL DEFAULT 0.0,
            total_filled_qty FLOAT NOT NULL DEFAULT 0.0,
            total_cost FLOAT NOT NULL DEFAULT 0.0,
            note TEXT,
            deadline_days INTEGER NOT NULL DEFAULT 30,
            deadline_at DATETIME,
            created_at DATETIME NOT NULL,
            started_at DATETIME,
            last_fill_at DATETIME
        )
        """
    )
    con.execute(
        "INSERT INTO kss_sessions (id, symbol, entry_price, distance_pct, max_waves, "
        "isolated_fund, tp_pct, timeout_x_min, gap_y_min, status, first_wave_usd, "
        "current_wave, total_cost, created_at) VALUES "
        "(5, 'ETC', 20.0, 2.0, 4, 144.09, 3.0, 30.0, 5.0, 'active', NULL, 1, 44.36, "
        "'2026-08-01T00:00:00')"
    )
    con.commit()
    con.close()


# The validity guard (fix round B item (b)) sanity-bounds a legacy row's derived
# first_wave_usd against the CURRENT settings.kss_first_wave_usd global — these end-to-end
# tests set it to 15 (matching ETC#5's own derived estimate) so the pre-existing "successful
# backfill" scenarios keep exercising exactly what they did before the guard existed.
_GLOBAL_MATCHING_ETC5 = {"KSS_FIRST_WAVE_USD": "15"}


def test_script_dry_run_reports_backfill_and_affordability(tmp_path):
    db_path = tmp_path / "rearm_test.db"
    _make_test_db(db_path)

    out = subprocess.run(
        [sys.executable, str(_SCRIPT), "--db", str(db_path)],
        capture_output=True, text=True, cwd=str(_ROOT), timeout=60,
        env={**os.environ, **_GLOBAL_MATCHING_ETC5},
    )
    assert out.returncode == 0, out.stderr
    assert "DRY-RUN" in out.stdout
    assert "ETC #5" in out.stdout
    assert "backfill" in out.stdout
    assert "affordable=yes" in out.stdout

    # Dry-run must NOT have written anything.
    import sqlite3
    con = sqlite3.connect(db_path)
    row = con.execute("SELECT first_wave_usd, isolated_fund FROM kss_sessions WHERE id=5").fetchone()
    con.close()
    assert row == (None, 144.09)


def test_script_apply_backfills_first_wave_usd(tmp_path):
    db_path = tmp_path / "rearm_test.db"
    _make_test_db(db_path)

    out = subprocess.run(
        [sys.executable, str(_SCRIPT), "--db", str(db_path), "--apply"],
        capture_output=True, text=True, cwd=str(_ROOT), timeout=60,
        env={**os.environ, **_GLOBAL_MATCHING_ETC5},
    )
    assert out.returncode == 0, out.stderr
    assert "APPLY" in out.stdout

    import sqlite3
    con = sqlite3.connect(db_path)
    row = con.execute("SELECT first_wave_usd, isolated_fund FROM kss_sessions WHERE id=5").fetchone()
    con.close()
    assert row[0] == pytest.approx(15.0, abs=0.01)
    assert row[1] == pytest.approx(144.09, abs=0.01)  # affordable already — no shortfall patch


# ---- item (a): a not-yet-booted / pre-migration DB must be caught up, not crash ----------


def _make_legacy_test_db_missing_first_wave_usd(path: Path) -> None:
    """Same shape as `_make_test_db` but WITHOUT the `first_wave_usd` column at all —
    simulates a DB that predates that additive migration. Before item (a), querying
    `KssSession` against this file raised a raw `sqlite3.OperationalError`."""
    import sqlite3

    con = sqlite3.connect(path)
    con.execute(
        """
        CREATE TABLE kss_sessions (
            id INTEGER PRIMARY KEY,
            symbol VARCHAR(20) NOT NULL,
            entry_price FLOAT NOT NULL,
            distance_pct FLOAT NOT NULL,
            max_waves INTEGER NOT NULL,
            isolated_fund FLOAT NOT NULL,
            tp_pct FLOAT NOT NULL,
            timeout_x_min FLOAT NOT NULL,
            gap_y_min FLOAT NOT NULL,
            sl_pct FLOAT NOT NULL DEFAULT 0.0,
            trailing_pct FLOAT NOT NULL DEFAULT 0.0,
            peak_price FLOAT NOT NULL DEFAULT 0.0,
            trail_active BOOLEAN NOT NULL DEFAULT 0,
            trail_sl_price FLOAT NOT NULL DEFAULT 0.0,
            trail_dist_pct FLOAT NOT NULL DEFAULT 0.0,
            status VARCHAR(16) NOT NULL DEFAULT 'pending',
            strategy_mode TEXT DEFAULT 'dca_down',
            current_wave INTEGER NOT NULL DEFAULT 0,
            avg_price FLOAT NOT NULL DEFAULT 0.0,
            total_filled_qty FLOAT NOT NULL DEFAULT 0.0,
            total_cost FLOAT NOT NULL DEFAULT 0.0,
            note TEXT,
            deadline_days INTEGER NOT NULL DEFAULT 30,
            deadline_at DATETIME,
            created_at DATETIME NOT NULL,
            started_at DATETIME,
            last_fill_at DATETIME
        )
        """
    )
    con.execute(
        "INSERT INTO kss_sessions (id, symbol, entry_price, distance_pct, max_waves, "
        "isolated_fund, tp_pct, timeout_x_min, gap_y_min, status, "
        "current_wave, total_cost, created_at) VALUES "
        "(5, 'ETC', 20.0, 2.0, 4, 144.09, 3.0, 30.0, 5.0, 'active', 1, 44.36, "
        "'2026-08-01T00:00:00')"
    )
    con.commit()
    con.close()


def test_script_survives_a_db_missing_the_first_wave_usd_column(tmp_path):
    db_path = tmp_path / "legacy.db"
    _make_legacy_test_db_missing_first_wave_usd(db_path)

    out = subprocess.run(
        [sys.executable, str(_SCRIPT), "--db", str(db_path)],
        capture_output=True, text=True, cwd=str(_ROOT), timeout=60,
        env={**os.environ, **_GLOBAL_MATCHING_ETC5},
    )
    assert out.returncode == 0, out.stderr
    assert "OperationalError" not in out.stderr
    assert "DRY-RUN" in out.stdout
    assert "backfill" in out.stdout


# ---- item (b): the backfill validity guard skips an implausible/unreferenceable estimate --


def test_script_skips_backfill_when_the_estimate_is_implausible_vs_the_global(tmp_path):
    """ETC#5's own derived estimate (~15) is far outside [0.2x,5x] of a global of 100
    ([20,500]) — this must be reported (SKIPPED), never silently written."""
    db_path = tmp_path / "rearm_test.db"
    _make_test_db(db_path)

    out = subprocess.run(
        [sys.executable, str(_SCRIPT), "--db", str(db_path), "--apply"],
        capture_output=True, text=True, cwd=str(_ROOT), timeout=60,
        env={**os.environ, "KSS_FIRST_WAVE_USD": "100"},
    )
    assert out.returncode == 0, out.stderr
    assert "SKIPPED" in out.stdout

    import sqlite3
    con = sqlite3.connect(db_path)
    row = con.execute("SELECT first_wave_usd, isolated_fund FROM kss_sessions WHERE id=5").fetchone()
    con.close()
    assert row == (None, 144.09), "a skipped row must have NOTHING written, even with --apply"


def test_script_skips_all_backfills_when_the_global_first_wave_usd_is_zero(tmp_path):
    """No plausible USD-ladder reference at all (global 0, e.g. legacy pip sizing) -> every
    legacy NULL row is skipped, with a message, never guessed at."""
    db_path = tmp_path / "rearm_test.db"
    _make_test_db(db_path)

    env = dict(os.environ)
    env.pop("KSS_FIRST_WAVE_USD", None)  # ensure it is unset -> settings default (0.0)
    out = subprocess.run(
        [sys.executable, str(_SCRIPT), "--db", str(db_path), "--apply"],
        capture_output=True, text=True, cwd=str(_ROOT), timeout=60, env=env,
    )
    assert out.returncode == 0, out.stderr
    assert "SKIPPED" in out.stdout
    assert "global is currently 0" in out.stdout

    import sqlite3
    con = sqlite3.connect(db_path)
    row = con.execute("SELECT first_wave_usd FROM kss_sessions WHERE id=5").fetchone()
    con.close()
    assert row == (None,)


# ---- item (c): the shortfall top-up is PADDED for real stepSize/precision snapping --------


def _make_padding_test_db(path: Path) -> None:
    """One ACTIVE session with first_wave_usd already SET (no backfill involved) and a SMALL
    shortfall on its next rung — isolates the padded top-up arithmetic from the backfill
    guard."""
    import sqlite3

    con = sqlite3.connect(path)
    con.execute(
        """
        CREATE TABLE kss_sessions (
            id INTEGER PRIMARY KEY,
            symbol VARCHAR(20) NOT NULL,
            entry_price FLOAT NOT NULL,
            distance_pct FLOAT NOT NULL,
            max_waves INTEGER NOT NULL,
            isolated_fund FLOAT NOT NULL,
            tp_pct FLOAT NOT NULL,
            timeout_x_min FLOAT NOT NULL,
            gap_y_min FLOAT NOT NULL,
            sl_pct FLOAT NOT NULL DEFAULT 0.0,
            trailing_pct FLOAT NOT NULL DEFAULT 0.0,
            peak_price FLOAT NOT NULL DEFAULT 0.0,
            trail_active BOOLEAN NOT NULL DEFAULT 0,
            trail_sl_price FLOAT NOT NULL DEFAULT 0.0,
            trail_dist_pct FLOAT NOT NULL DEFAULT 0.0,
            status VARCHAR(16) NOT NULL DEFAULT 'pending',
            strategy_mode TEXT DEFAULT 'dca_down',
            first_wave_usd FLOAT,
            current_wave INTEGER NOT NULL DEFAULT 0,
            avg_price FLOAT NOT NULL DEFAULT 0.0,
            total_filled_qty FLOAT NOT NULL DEFAULT 0.0,
            total_cost FLOAT NOT NULL DEFAULT 0.0,
            note TEXT,
            deadline_days INTEGER NOT NULL DEFAULT 30,
            deadline_at DATETIME,
            created_at DATETIME NOT NULL,
            started_at DATETIME,
            last_fill_at DATETIME
        )
        """
    )
    con.execute(
        "INSERT INTO kss_sessions (id, symbol, entry_price, distance_pct, max_waves, "
        "isolated_fund, tp_pct, timeout_x_min, gap_y_min, status, first_wave_usd, "
        "current_wave, total_cost, created_at) VALUES "
        "(9, 'PAD', 10.0, 2.0, 4, 100.0, 3.0, 30.0, 5.0, 'active', 15.0, 1, 58.0, "
        "'2026-08-01T00:00:00')"
    )
    con.commit()
    con.close()


def test_script_pads_the_shortfall_top_up_for_real_step_precision_snapping(tmp_path):
    db_path = tmp_path / "pad_test.db"
    _make_padding_test_db(db_path)

    new_cost = rearm.rung_cost(15.0, 2.0, 2)      # the next (wave-2) rung's raw cost
    remaining = 100.0 - 58.0                       # isolated_fund - total_cost
    shortfall = new_cost - remaining
    assert 0 < shortfall <= rearm._MAX_AUTO_PATCH_USD  # exercises the auto-patch branch
    slack_pct = 1.0
    pad = max(rearm._MIN_PAD_USD, new_cost * slack_pct / 100.0)
    expected_isolated_fund = 100.0 + shortfall + pad

    out = subprocess.run(
        [sys.executable, str(_SCRIPT), "--db", str(db_path), "--apply"],
        capture_output=True, text=True, cwd=str(_ROOT), timeout=60,
        env={**os.environ, "KSS_LADDER_RESERVE_SLACK_PCT": str(slack_pct)},
    )
    assert out.returncode == 0, out.stderr
    assert "affordable=no" in out.stdout
    assert "pad" in out.stdout

    import sqlite3
    con = sqlite3.connect(db_path)
    row = con.execute("SELECT isolated_fund FROM kss_sessions WHERE id=9").fetchone()
    con.close()
    assert row[0] == pytest.approx(expected_isolated_fund, abs=1e-6)


# ---- item (d): PENDING sessions join the backfill sweep, but never get the top-up ---------


def _make_pending_test_db(path: Path) -> None:
    """One PENDING session, first_wave_usd NULL, no rungs placed yet (current_wave=0,
    total_cost=0) — item (d): included in the backfill sweep, excluded from the top-up."""
    import sqlite3

    con = sqlite3.connect(path)
    con.execute(
        """
        CREATE TABLE kss_sessions (
            id INTEGER PRIMARY KEY,
            symbol VARCHAR(20) NOT NULL,
            entry_price FLOAT NOT NULL,
            distance_pct FLOAT NOT NULL,
            max_waves INTEGER NOT NULL,
            isolated_fund FLOAT NOT NULL,
            tp_pct FLOAT NOT NULL,
            timeout_x_min FLOAT NOT NULL,
            gap_y_min FLOAT NOT NULL,
            sl_pct FLOAT NOT NULL DEFAULT 0.0,
            trailing_pct FLOAT NOT NULL DEFAULT 0.0,
            peak_price FLOAT NOT NULL DEFAULT 0.0,
            trail_active BOOLEAN NOT NULL DEFAULT 0,
            trail_sl_price FLOAT NOT NULL DEFAULT 0.0,
            trail_dist_pct FLOAT NOT NULL DEFAULT 0.0,
            status VARCHAR(16) NOT NULL DEFAULT 'pending',
            strategy_mode TEXT DEFAULT 'dca_down',
            first_wave_usd FLOAT,
            current_wave INTEGER NOT NULL DEFAULT 0,
            avg_price FLOAT NOT NULL DEFAULT 0.0,
            total_filled_qty FLOAT NOT NULL DEFAULT 0.0,
            total_cost FLOAT NOT NULL DEFAULT 0.0,
            note TEXT,
            deadline_days INTEGER NOT NULL DEFAULT 30,
            deadline_at DATETIME,
            created_at DATETIME NOT NULL,
            started_at DATETIME,
            last_fill_at DATETIME
        )
        """
    )
    con.execute(
        "INSERT INTO kss_sessions (id, symbol, entry_price, distance_pct, max_waves, "
        "isolated_fund, tp_pct, timeout_x_min, gap_y_min, status, first_wave_usd, "
        "current_wave, total_cost, created_at) VALUES "
        "(7, 'PEND', 20.0, 2.0, 4, 144.09, 3.0, 30.0, 5.0, 'pending', NULL, 0, 0.0, "
        "'2026-08-01T00:00:00')"
    )
    con.commit()
    con.close()


def test_script_includes_pending_sessions_in_the_sweep_but_skips_their_top_up(tmp_path):
    db_path = tmp_path / "pending_test.db"
    _make_pending_test_db(db_path)

    out = subprocess.run(
        [sys.executable, str(_SCRIPT), "--db", str(db_path), "--apply"],
        capture_output=True, text=True, cwd=str(_ROOT), timeout=60,
        env={**os.environ, **_GLOBAL_MATCHING_ETC5},
    )
    assert out.returncode == 0, out.stderr
    assert "PEND #7" in out.stdout
    assert "backfill" in out.stdout
    assert "no rungs placed yet" in out.stdout

    import sqlite3
    con = sqlite3.connect(db_path)
    row = con.execute("SELECT first_wave_usd, isolated_fund FROM kss_sessions WHERE id=7").fetchone()
    con.close()
    assert row[0] == pytest.approx(15.0, abs=0.01)      # backfilled
    assert row[1] == pytest.approx(144.09, abs=0.01)    # untouched — no top-up for PENDING
