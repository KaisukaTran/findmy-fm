"""Consultant agent registry — loads enabled consultants from DB."""

import sqlite3
import json
import os
import logging
from pathlib import Path
from .base import ConsultantAgent

logger = logging.getLogger(__name__)


def _db_path() -> str:
    url = os.getenv("DATABASE_URL") or os.getenv("SOT_DATABASE_URL") or "sqlite:///./data/findmy_fm_paper.db"
    return url[len("sqlite:///"):] if url.startswith("sqlite:///") else "./data/findmy_fm_paper.db"


def get_enabled_consultants() -> list[ConsultantAgent]:
    """Load and instantiate all enabled consultant agents from DB."""
    path = _db_path()
    if not Path(path).exists():
        return []
    try:
        with sqlite3.connect(path) as con:
            con.row_factory = sqlite3.Row
            rows = con.execute(
                "SELECT * FROM ai_consultants WHERE enabled=1"
            ).fetchall()
    except Exception:
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
    path = _db_path()
    if not Path(path).exists():
        return []
    with sqlite3.connect(path) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute("SELECT * FROM ai_consultants ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def add_consultant(name: str, type_: str, config: dict, enabled: bool = True) -> dict:
    from datetime import datetime
    path = _db_path()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as con:
        con.execute(
            "INSERT INTO ai_consultants(name, type, config_json, enabled, created_at) VALUES(?,?,?,?,?)",
            (name, type_, json.dumps(config), 1 if enabled else 0, datetime.utcnow().isoformat()),
        )
        con.commit()
    return {"name": name, "type": type_, "enabled": enabled}


def toggle_consultant(consultant_id: int, enabled: bool) -> bool:
    path = _db_path()
    if not Path(path).exists():
        return False
    with sqlite3.connect(path) as con:
        cur = con.execute(
            "UPDATE ai_consultants SET enabled=? WHERE id=?",
            (1 if enabled else 0, consultant_id),
        )
        con.commit()
    return cur.rowcount > 0


def remove_consultant(consultant_id: int) -> bool:
    path = _db_path()
    if not Path(path).exists():
        return False
    with sqlite3.connect(path) as con:
        cur = con.execute("DELETE FROM ai_consultants WHERE id=?", (consultant_id,))
        con.commit()
    return cur.rowcount > 0
