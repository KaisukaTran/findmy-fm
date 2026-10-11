"""Shared setup for the entry-pump-gate study (2026-10-11). READ-ONLY research; no app/ edits.

Import this module FIRST in every script of the study: it points the app at a throw-away
SQLite file and forces paper mode BEFORE any `app.*` import (an ad-hoc script that imports app
modules without this can write into the running paper DB -- see memory adhoc-script-hits-prod-db).
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STUDY = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(STUDY))
sys.path.insert(0, str(ROOT / "scripts"))

# Must happen before any `app` import.
os.environ["DATABASE_URL"] = f"sqlite:///{tempfile.mkdtemp(prefix='epg_')}/throwaway.db"
os.environ["LIVE_TRADING"] = "false"
os.environ["REQUIRE_AUTH"] = "false"

MARKET_DB = ROOT / "data" / "research" / "market.db"
PAPER_DB = ROOT / "data" / "findmy.db"

H = 3_600_000
D = 86_400_000
WINDOW_START_TS = 1_704_067_200_000  # 2024-01-01T00:00Z
WINDOW_END_TS = 1_785_538_800_000    # 2026-07-31T23:00Z = last 1h bar open
ENTRY_FIRST_DAY_TS = WINDOW_START_TS + 9 * D   # 9 days of 1h warm-up (>=216 bars for RSI/7d)


def ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)


def effective_config() -> dict:
    """Paper's effective knobs: app/config.py defaults + .env, overlaid with the persisted
    `kss:<field>` rows of data/findmy.db exactly as app/runtime.py does at start-up (read-only)."""
    from app import runtime
    from app.config import settings

    applied: dict[str, object] = {}
    con = ro(PAPER_DB)
    try:
        rows = dict(con.execute("select key, value from runtime_config where key like 'kss:%'").fetchall())
    finally:
        con.close()
    for key, cast in runtime.KSS_SETTING_FIELDS.items():
        raw = rows.get(f"kss:{key}")
        if raw is None:
            continue
        try:
            val = cast(raw)
        except (TypeError, ValueError):
            continue
        setattr(settings, key, val)
        applied[key] = val
    # knobs that are persisted under kss: but not in KSS_SETTING_FIELDS (read directly)
    for k, v in rows.items():
        f = k[4:]
        if f in applied or not hasattr(settings, f):
            continue
        cur = getattr(settings, f)
        try:
            val = (str(v).lower() in ("true", "1")) if isinstance(cur, bool) else type(cur)(v)
        except (TypeError, ValueError):
            continue
        setattr(settings, f, val)
        applied[f] = val
    return applied
