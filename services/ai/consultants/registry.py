"""Consultant agent registry — loads enabled consultants from DB."""

import sqlite3
import json
import logging
from datetime import datetime

from .base import ConsultantAgent
from .._db import connect, read_connect

logger = logging.getLogger(__name__)


class DuplicateConsultantError(ValueError):
    """Raised when a consultant with the same name already exists."""


def get_enabled_consultants() -> list[ConsultantAgent]:
    """Load and instantiate all enabled consultant agents from DB."""
    with read_connect() as con:
        if con is None:
            return []
        try:
            rows = con.execute("SELECT * FROM ai_consultants WHERE enabled=1").fetchall()
        except sqlite3.OperationalError:
            return []

    agents = []
    for row in rows:
        try:
            config = json.loads(row["config_json"] or "{}")
            agent = _build(row["name"], row["type"], config)
            if agent:
                agents.append(agent)
        except Exception as e:
            logger.warning(f"Failed to build consultant {row['name']}: {e}")
    return agents


def _build(name: str, type_: str, config: dict) -> ConsultantAgent | None:
    if type_ == "technical":
        from .technical import TechnicalConsultant
        return TechnicalConsultant(name=name, config=config)
    if type_ == "llm":
        from .llm import LLMConsultant
        return LLMConsultant(name=name, config=config)
    logger.warning(f"Unknown consultant type: {type_}")
    return None


def list_consultants() -> list[dict]:
    with read_connect() as con:
        if con is None:
            return []
        try:
            rows = con.execute("SELECT * FROM ai_consultants ORDER BY id").fetchall()
        except sqlite3.OperationalError:
            return []
    return [dict(r) for r in rows]


def add_consultant(name: str, type_: str, config: dict, enabled: bool = True) -> dict:
    try:
        with connect() as con:
            con.execute(
                "INSERT INTO ai_consultants(name, type, config_json, enabled, created_at) VALUES(?,?,?,?,?)",
                (name, type_, json.dumps(config), 1 if enabled else 0, datetime.utcnow().isoformat()),
            )
    except sqlite3.IntegrityError as e:
        raise DuplicateConsultantError(f"Consultant '{name}' already exists") from e
    return {"name": name, "type": type_, "enabled": enabled}


def toggle_consultant(consultant_id: int, enabled: bool) -> bool:
    with connect() as con:
        cur = con.execute(
            "UPDATE ai_consultants SET enabled=? WHERE id=?",
            (1 if enabled else 0, consultant_id),
        )
        return cur.rowcount > 0


def remove_consultant(consultant_id: int) -> bool:
    with connect() as con:
        cur = con.execute("DELETE FROM ai_consultants WHERE id=?", (consultant_id,))
        return cur.rowcount > 0
