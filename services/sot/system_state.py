"""Persistent key-value system state stored in the main DB.

Used for flags (e.g. EMERGENCY_HALT) that must be visible across all
gunicorn workers — an in-memory variable would only live in one process.
"""

import os
import sqlite3
from pathlib import Path

_KEY_HALT = "emergency_halt"


def _db_path() -> str:
    url = os.getenv("DATABASE_URL") or os.getenv("SOT_DATABASE_URL") or "sqlite:///./data/findmy_fm_paper.db"
    # strip sqlite:/// prefix
    if url.startswith("sqlite:///"):
        path = url[len("sqlite:///"):]
        return path
    return "./data/findmy_fm_paper.db"


def _conn() -> sqlite3.Connection:
    path = _db_path()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    return con


def _ensure_row() -> None:
    with _conn() as con:
        con.execute(
            "INSERT OR IGNORE INTO system_state (key, value) VALUES (?, '0')",
            (_KEY_HALT,),
        )
        con.commit()


def is_halted() -> bool:
    try:
        with _conn() as con:
            row = con.execute(
                "SELECT value FROM system_state WHERE key = ?", (_KEY_HALT,)
            ).fetchone()
        return row is not None and row["value"] == "1"
    except Exception:
        return False  # fail-open: if DB is down, don't block everything


def set_halt(halted: bool) -> None:
    val = "1" if halted else "0"
    try:
        _ensure_row()
        with _conn() as con:
            con.execute(
                "UPDATE system_state SET value = ?, updated_at = datetime('now') WHERE key = ?",
                (val, _KEY_HALT),
            )
            con.commit()
    except Exception as e:
        raise RuntimeError(f"Failed to persist emergency halt state: {e}") from e
