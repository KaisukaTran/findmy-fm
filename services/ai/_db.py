"""Shared SQLite path resolver and connection helper for AI services.

Resolves the DB path against project root (not CWD) so it stays consistent
across systemd / Docker / pytest contexts. Enables WAL + busy timeout to
prevent 'database is locked' under concurrent agent loop + API writes.
"""

import os
import sqlite3
from pathlib import Path
from contextlib import contextmanager

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def resolve_db_path() -> str:
    url = os.getenv("DATABASE_URL") or os.getenv("SOT_DATABASE_URL") or "sqlite:///./data/findmy_fm_paper.db"
    if not url.startswith("sqlite:///"):
        return url  # non-sqlite — caller responsible
    raw = url[len("sqlite:///"):]
    p = Path(raw)
    if not p.is_absolute():
        p = (_PROJECT_ROOT / raw).resolve()
    return str(p)


@contextmanager
def connect():
    """Open SQLite connection with WAL + 30s busy timeout."""
    path = resolve_db_path()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=30.0)
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA busy_timeout=30000")
        yield con
        con.commit()
    finally:
        con.close()


@contextmanager
def read_connect():
    """Read-only connection — no auto-commit."""
    path = resolve_db_path()
    if not Path(path).exists():
        yield None
        return
    con = sqlite3.connect(path, timeout=30.0)
    try:
        con.row_factory = sqlite3.Row
        yield con
    finally:
        con.close()
