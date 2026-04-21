"""User repository – CRUD against the users table in the SOT database."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional
from dataclasses import dataclass

from services.auth.password import hash_password, verify_password

# Reuse same DB file as SOT
_DB_PATH = Path("db/sot.db")


def _conn() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(_DB_PATH))
    con.row_factory = sqlite3.Row
    return con


def ensure_table() -> None:
    """Create users table if it doesn't exist (idempotent)."""
    sql = Path("db/migrations/007_users.sql").read_text()
    with _conn() as con:
        con.executescript(sql)


@dataclass
class DBUser:
    id: int
    username: str
    password_hash: str
    role: str
    is_active: bool


def get_by_username(username: str) -> Optional[DBUser]:
    with _conn() as con:
        row = con.execute(
            "SELECT id, username, password_hash, role, is_active FROM users WHERE username = ?",
            (username,),
        ).fetchone()
    if row is None:
        return None
    return DBUser(
        id=row["id"],
        username=row["username"],
        password_hash=row["password_hash"],
        role=row["role"],
        is_active=bool(row["is_active"]),
    )


def create_user(username: str, plain_password: str, role: str = "trader") -> DBUser:
    hashed = hash_password(plain_password)
    with _conn() as con:
        cur = con.execute(
            "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
            (username, hashed, role),
        )
        con.commit()
        return get_by_username(username)  # type: ignore[return-value]


def list_users() -> list[DBUser]:
    with _conn() as con:
        rows = con.execute(
            "SELECT id, username, password_hash, role, is_active FROM users ORDER BY id"
        ).fetchall()
    return [DBUser(r["id"], r["username"], r["password_hash"], r["role"], bool(r["is_active"])) for r in rows]


def authenticate(username: str, plain_password: str) -> Optional[DBUser]:
    user = get_by_username(username)
    if user and user.is_active and verify_password(plain_password, user.password_hash):
        return user
    return None
