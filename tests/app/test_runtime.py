"""Tests for app.runtime — persisted KV, master switch, freeze/unfreeze, sync."""

from __future__ import annotations

from app import runtime
from app.config import settings

# ---------------------------------------------------------------------------
# Generic KV helpers
# ---------------------------------------------------------------------------


def test_get_missing_returns_default(db):
    assert runtime.get(db, "no_such_key") is None
    assert runtime.get(db, "no_such_key", default="x") == "x"


def test_set_then_get_roundtrip(db):
    runtime.set(db, "test_key", "hello")
    assert runtime.get(db, "test_key") == "hello"


def test_set_upsert(db):
    runtime.set(db, "test_key", "v1")
    runtime.set(db, "test_key", "v2")
    assert runtime.get(db, "test_key") == "v2"


def test_get_bool_truthy_values(db):
    for truthy in ("1", "true", "True", "TRUE", "yes", "YES"):
        runtime.set(db, "flag", truthy)
        assert runtime.get_bool(db, "flag") is True


def test_get_bool_falsy_values(db):
    for falsy in ("0", "false", "no", "off"):
        runtime.set(db, "flag", falsy)
        assert runtime.get_bool(db, "flag") is False


def test_get_bool_missing_returns_default(db):
    assert runtime.get_bool(db, "absent", default=True) is True
    assert runtime.get_bool(db, "absent", default=False) is False


def test_set_bool_roundtrip(db):
    runtime.set_bool(db, "mybool", True)
    assert runtime.get_bool(db, "mybool") is True
    runtime.set_bool(db, "mybool", False)
    assert runtime.get_bool(db, "mybool") is False


# ---------------------------------------------------------------------------
# Master switch — full_auto_on / full_auto_off
# ---------------------------------------------------------------------------


def test_full_auto_on_sets_settings_and_persists(db, monkeypatch):
    monkeypatch.setattr(settings, "full_auto", False)
    monkeypatch.setattr(settings, "auto_trade", False)
    monkeypatch.setattr(settings, "autoapprove_enabled", False)

    result = runtime.full_auto_on(db)

    # in-memory settings updated
    assert settings.full_auto is True
    assert settings.auto_trade is True
    assert settings.autoapprove_enabled is True

    # persisted to DB
    assert runtime.get_bool(db, runtime.KEY_FULL_AUTO) is True

    # returned state reflects the change
    assert result["full_auto"] is True
    assert result["auto_trade"] is True
    assert result["autoapprove"] is True


def test_full_auto_off_clears_settings_and_persists(db, monkeypatch):
    monkeypatch.setattr(settings, "full_auto", True)
    monkeypatch.setattr(settings, "auto_trade", True)
    monkeypatch.setattr(settings, "autoapprove_enabled", True)

    result = runtime.full_auto_off(db)

    assert settings.full_auto is False
    assert settings.auto_trade is False
    assert settings.autoapprove_enabled is False
    assert runtime.get_bool(db, runtime.KEY_FULL_AUTO) is False
    assert result["full_auto"] is False


# ---------------------------------------------------------------------------
# Freeze / unfreeze roundtrip
# ---------------------------------------------------------------------------


def test_freeze_sets_frozen_flag(db):
    runtime.freeze(db, "test reason")
    assert runtime.is_frozen(db) is True
    assert runtime.get(db, runtime.KEY_FROZEN_REASON) == "test reason"
    assert runtime.get(db, runtime.KEY_FROZEN_AT) is not None


def test_unfreeze_clears_frozen_flag(db):
    runtime.freeze(db, "some reason")
    runtime.unfreeze(db)
    assert runtime.is_frozen(db) is False
    assert runtime.get(db, runtime.KEY_FROZEN_REASON) == ""
    assert runtime.get(db, runtime.KEY_FROZEN_AT) == ""


def test_is_frozen_false_by_default(db):
    assert runtime.is_frozen(db) is False


# ---------------------------------------------------------------------------
# sync_from_db — simulates a process restart
# ---------------------------------------------------------------------------


def test_sync_from_db_restores_full_auto(db, monkeypatch):
    """Persist full_auto=True, then simulate a restart by zeroing settings, call sync."""
    # Step 1: persist the flag
    runtime.full_auto_on(db)

    # Step 2: simulate restart — reset in-memory flags
    monkeypatch.setattr(settings, "full_auto", False)
    monkeypatch.setattr(settings, "auto_trade", False)
    monkeypatch.setattr(settings, "autoapprove_enabled", False)

    # Step 3: sync restores them
    runtime.sync_from_db(db)

    assert settings.full_auto is True
    assert settings.auto_trade is True
    assert settings.autoapprove_enabled is True


def test_sync_from_db_noop_when_flag_absent(db, monkeypatch):
    """When KEY_FULL_AUTO is absent, sync is a no-op (flags stay False)."""
    monkeypatch.setattr(settings, "full_auto", False)
    monkeypatch.setattr(settings, "auto_trade", False)
    monkeypatch.setattr(settings, "autoapprove_enabled", False)

    runtime.sync_from_db(db)  # nothing persisted yet

    assert settings.full_auto is False
    assert settings.auto_trade is False
    assert settings.autoapprove_enabled is False


def test_sync_from_db_noop_when_flag_false(db, monkeypatch):
    """When KEY_FULL_AUTO is explicitly False, sync leaves settings alone."""
    runtime.full_auto_off(db)
    monkeypatch.setattr(settings, "full_auto", False)
    monkeypatch.setattr(settings, "auto_trade", False)
    monkeypatch.setattr(settings, "autoapprove_enabled", False)

    runtime.sync_from_db(db)

    assert settings.auto_trade is False


# ---------------------------------------------------------------------------
# state() snapshot
# ---------------------------------------------------------------------------


def test_state_returns_correct_snapshot(db, monkeypatch):
    monkeypatch.setattr(settings, "full_auto", True)
    monkeypatch.setattr(settings, "auto_trade", True)
    monkeypatch.setattr(settings, "autoapprove_enabled", False)

    s = runtime.state(db)
    assert s["full_auto"] is True
    assert s["auto_trade"] is True
    assert s["autoapprove"] is False
    assert s["frozen"] is False
