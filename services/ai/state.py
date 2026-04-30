"""Persistent AI agent state using ai_agent_state table (DB-backed, multi-worker safe)."""

import sqlite3
import os
from datetime import datetime
from pathlib import Path


def _db_path() -> str:
    url = os.getenv("DATABASE_URL") or os.getenv("SOT_DATABASE_URL") or "sqlite:///./data/findmy_fm_paper.db"
    return url[len("sqlite:///"):] if url.startswith("sqlite:///") else "./data/findmy_fm_paper.db"


def _get(key: str, default: str = "") -> str:
    path = _db_path()
    if not Path(path).exists():
        return default
    with sqlite3.connect(path) as con:
        row = con.execute("SELECT value FROM ai_agent_state WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def _set(key: str, value: str) -> None:
    path = _db_path()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as con:
        con.execute(
            "INSERT INTO ai_agent_state(key,value,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, value, datetime.utcnow().isoformat()),
        )
        con.commit()


def is_running() -> bool:
    return _get("running", "false") == "true"


def set_running(running: bool) -> None:
    _set("running", "true" if running else "false")


def get_mode() -> str:
    return _get("mode", "paper")


def set_mode(mode: str) -> None:
    _set("mode", mode)


def get_paper_start_date() -> str:
    return _get("paper_start_date", "")


def set_paper_start_date(date_str: str) -> None:
    _set("paper_start_date", date_str)


def get_last_action() -> str:
    return _get("last_action", "")


def set_last_action(action: str) -> None:
    _set("last_action", action)
