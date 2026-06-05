"""
Persistence layer for runtime automation state (FINDMY-FM lean rebuild).

Solves the lost-on-restart problem: the in-memory `settings` singleton resets to
its defaults every process start. This module mirrors the full-auto master switch
and circuit-breaker freeze state into the `runtime_config` SQLite table so that
`sync_from_db()` can restore them on startup.

Rule: all core functions accept an explicit `db: Session` so they are testable
without side-effects on the global session.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from app.config import settings
from app.models import RuntimeConfig

# ---------------------------------------------------------------------------
# Key constants
# ---------------------------------------------------------------------------

KEY_FULL_AUTO = "full_auto"
KEY_FROZEN = "breaker_frozen"
KEY_FROZEN_REASON = "breaker_frozen_reason"
KEY_FROZEN_AT = "breaker_frozen_at"  # ISO timestamp string
KEY_OPUS_MODE = "opus_mode"
KEY_OPUS_SHADOW = "opus_shadow"
KEY_AUTOAPPROVE_ENABLED = "autoapprove_enabled"
KEY_AUTOAPPROVE_MAX = "autoapprove_max_notional"

# ---------------------------------------------------------------------------
# Generic KV helpers
# ---------------------------------------------------------------------------


def get(db: Session, key: str, default: str | None = None) -> str | None:
    """Read a RuntimeConfig value by key, or return *default* if absent."""
    row: RuntimeConfig | None = db.get(RuntimeConfig, key)
    return row.value if row is not None else default


def set(db: Session, key: str, value: object) -> None:  # noqa: A001
    """Upsert a RuntimeConfig row, storing *value* as a string, then commit."""
    row: RuntimeConfig | None = db.get(RuntimeConfig, key)
    if row is None:
        row = RuntimeConfig(key=key, value=str(value), updated_at=datetime.utcnow())
        db.add(row)
    else:
        row.value = str(value)
        row.updated_at = datetime.utcnow()
    db.commit()


def get_bool(db: Session, key: str, default: bool = False) -> bool:
    """Return True when the stored value is '1', 'true', or 'yes' (case-insensitive)."""
    raw = get(db, key)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes"}


def set_bool(db: Session, key: str, value: bool) -> None:
    """Persist a boolean as '1' (True) or '0' (False)."""
    set(db, key, "1" if value else "0")


# ---------------------------------------------------------------------------
# Master switch — mutates in-memory settings AND persists
# ---------------------------------------------------------------------------


def full_auto_on(db: Session) -> dict:
    """Enable full-auto mode: sets settings flags and persists KEY_FULL_AUTO."""
    settings.full_auto = True
    settings.auto_trade = True
    settings.autoapprove_enabled = True
    set_bool(db, KEY_FULL_AUTO, True)
    return state(db)


def full_auto_off(db: Session) -> dict:
    """Disable full-auto mode: clears settings flags and persists KEY_FULL_AUTO."""
    settings.full_auto = False
    settings.auto_trade = False
    settings.autoapprove_enabled = False
    set_bool(db, KEY_FULL_AUTO, False)
    return state(db)


def opus_mode_on(db: Session) -> dict:
    """Enable OPUS orchestrator mode (independent of full_auto). Persisted."""
    settings.opus_mode = True
    set_bool(db, KEY_OPUS_MODE, True)
    return state(db)


def opus_mode_off(db: Session) -> dict:
    """Disable OPUS orchestrator mode. Persisted."""
    settings.opus_mode = False
    set_bool(db, KEY_OPUS_MODE, False)
    return state(db)


def set_autoapprove(db: Session, *, enabled: bool, max_notional: float | None) -> None:
    """Persist the auto-approval rule (enabled + max notional) so it survives restarts."""
    settings.autoapprove_enabled = enabled
    set_bool(db, KEY_AUTOAPPROVE_ENABLED, enabled)
    if max_notional is not None:
        settings.autoapprove_max_notional = max_notional
        set(db, KEY_AUTOAPPROVE_MAX, max_notional)


def opus_shadow_set(db: Session, shadow: bool) -> dict:
    """Set OPUS shadow mode (True = log intents but don't execute). Persisted."""
    settings.opus_shadow = shadow
    set_bool(db, KEY_OPUS_SHADOW, shadow)
    return state(db)


def state(db: Session) -> dict:
    """Return a snapshot of the current automation and breaker state."""
    return {
        "full_auto": settings.full_auto,
        "auto_trade": settings.auto_trade,
        "autoapprove": settings.autoapprove_enabled,
        "opus_mode": settings.opus_mode,
        "frozen": is_frozen(db),
        "frozen_reason": get(db, KEY_FROZEN_REASON),
        "frozen_at": get(db, KEY_FROZEN_AT),
    }


# ---------------------------------------------------------------------------
# Circuit-breaker freeze state
# ---------------------------------------------------------------------------


def is_frozen(db: Session) -> bool:
    """Return True when the circuit-breaker is currently frozen."""
    return get_bool(db, KEY_FROZEN)


def freeze(db: Session, reason: str) -> None:
    """Activate the circuit-breaker freeze with a human-readable *reason*."""
    set_bool(db, KEY_FROZEN, True)
    set(db, KEY_FROZEN_REASON, reason)
    set(db, KEY_FROZEN_AT, datetime.utcnow().isoformat())


def unfreeze(db: Session) -> None:
    """Clear the circuit-breaker freeze, resetting reason and timestamp."""
    set_bool(db, KEY_FROZEN, False)
    set(db, KEY_FROZEN_REASON, "")
    set(db, KEY_FROZEN_AT, "")


# ---------------------------------------------------------------------------
# Startup restore
# ---------------------------------------------------------------------------


def sync_from_db(db: Session) -> None:
    """Restore in-memory settings from persisted state (call once on startup).

    Reads KEY_FULL_AUTO; if True, activates full_auto/auto_trade/autoapprove_enabled
    on the settings singleton. Safe when the key is absent (defaults to no-op).
    Does not touch the scheduler — the caller manages the async loop.
    """
    if get_bool(db, KEY_FULL_AUTO, default=False):
        settings.full_auto = True
        settings.auto_trade = True
        settings.autoapprove_enabled = True
    settings.opus_mode = get_bool(db, KEY_OPUS_MODE, default=settings.opus_mode)
    settings.opus_shadow = get_bool(db, KEY_OPUS_SHADOW, default=settings.opus_shadow)
    # Auto-approval rule (persisted so a dashboard change survives a restart).
    if get(db, KEY_AUTOAPPROVE_ENABLED) is not None:
        settings.autoapprove_enabled = get_bool(db, KEY_AUTOAPPROVE_ENABLED)
    aa_max = get(db, KEY_AUTOAPPROVE_MAX)
    if aa_max is not None:
        try:
            settings.autoapprove_max_notional = float(aa_max)
        except ValueError:
            pass
