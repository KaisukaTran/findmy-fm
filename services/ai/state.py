"""Persistent AI agent state using ai_agent_state table (DB-backed, multi-worker safe)."""

from datetime import datetime
from ._db import connect, read_connect


def _get(key: str, default: str = "") -> str:
    with read_connect() as con:
        if con is None:
            return default
        row = con.execute("SELECT value FROM ai_agent_state WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def _set(key: str, value: str) -> None:
    with connect() as con:
        con.execute(
            "INSERT INTO ai_agent_state(key,value,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, value, datetime.utcnow().isoformat()),
        )


def is_running() -> bool:
    return _get("running", "false") == "true"


def set_running(running: bool) -> None:
    _set("running", "true" if running else "false")


def cas_set_running(expected: bool, target: bool) -> bool:
    """Compare-and-swap: only flip running flag if it currently equals `expected`.
    Returns True on success, False on contention (someone else changed it first)."""
    expected_s = "true" if expected else "false"
    target_s = "true" if target else "false"
    with connect() as con:
        # ensure row exists
        con.execute(
            "INSERT OR IGNORE INTO ai_agent_state(key,value,updated_at) VALUES('running','false',?)",
            (datetime.utcnow().isoformat(),),
        )
        cur = con.execute(
            "UPDATE ai_agent_state SET value=?, updated_at=? WHERE key='running' AND value=?",
            (target_s, datetime.utcnow().isoformat(), expected_s),
        )
        return cur.rowcount > 0


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
