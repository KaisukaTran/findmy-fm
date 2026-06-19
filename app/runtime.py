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

from collections.abc import Callable
from datetime import datetime

from sqlalchemy.orm import Session

from app.agents.aggregator import DEFAULT_WEIGHTS
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
KEY_GROK_ENABLED = "grok_enabled"
KEY_GROK_SCANNER = "grok_scanner_enabled"
KEY_TA_LIB = "ta_lib_enabled"
KEY_TA_EXTERNAL = "ta_external_enabled"
KEY_AUTOAPPROVE_ENABLED = "autoapprove_enabled"
KEY_AUTOAPPROVE_MAX = "autoapprove_max_notional"
KEY_CONSENSUS_WEIGHTS = "consensus_weights"  # S4: JSON dict of agent weights
KEY_GROK_FAIL_MODE = "grok_scanner_fail_mode"  # S5: "open" | "closed"
KEY_LIVE_TRADING = "live_trading"  # Phase 6: real-money master switch (default off)

def _to_bool(v: object) -> bool:
    """Bool-aware cast for the string-valued KV store. ``bool('0')`` is True (non-empty
    string), so a plain ``bool`` cast would corrupt a restored False — use this instead."""
    return str(v).strip().lower() in {"1", "true", "yes", "on"}


# Master KSS strategy knobs the dashboard edits (persisted as kss:<field>). Each maps to a
# settings field; the cast keeps them typed when restored from the string-valued KV store.
KSS_SETTING_FIELDS: dict[str, Callable[..., object]] = {
    "scan_distance_pct": float,
    "scan_tp_pct": float,
    "scan_max_waves": int,
    "scan_fund": float,
    "sl_pct": float,
    "trailing_pct": float,
    "deadline_days": int,
    "max_concurrent_sessions": int,
    "max_new_sessions_per_scan": int,  # cap NEW opens per scan (0=off); ramp gradually, best-first
    "max_sessions_per_symbol": int,  # K-1: 1 = one ladder per coin (no blended cost basis)
    "max_deployed_pct": float,
    "equity_backup_pct": float,
    "cash_floor_usd": float,  # hard floor: account cash may never drop below this (0 = never <0)
    "loss_streak_block_k": int,
    "loss_streak_window_days": int,
    "min_expectancy_pct": float,
    "max_avg_mae_pct": float,  # drawdown gate (0=off) + ranking: shallower backtest dip = better
    "min_win_rate": float,
    "min_confidence": float,  # S4: consensus threshold, now decoupled from backtest
    "min_trials": int,  # min backtest trials for a trustworthy edge (cut thin-sample noise)
    "block_downtrend_adx": float,  # hard veto: HTF+ST down & ADX≥this (0=off) — entry timing
    "entry_momentum_gate": _to_bool,  # veto open when ST down & MACDh<0 (don't buy a falling knife)
    "tp_fee_coverage": float,  # TP adds this × round-trip fee (1.2 = +120% of fees)
    "grok_scanner_fail_mode": str,  # S5: "open" | "closed"
    "grok_scanner_batch_max": int,  # how many candidates Grok reviews per scan (cover them all)
    "grok_live_search": _to_bool,  # let Grok use xAI Live Search (web+X+news) in the scan gate
    "grok_search_max_results": int,  # cap Live Search results per scan call
    "scan_max_symbols": int,
    "min_quote_volume": float,
    "kss_first_wave_usd": float,
    # Live-readiness knobs (1.9) — LIVE only, inert on paper. maker/testnet are bool (use
    # _to_bool, not bool, so a restored "0" stays False); timeout is seconds (0 = wait forever).
    "maker_orders": _to_bool,
    "order_fill_timeout_sec": int,
    "live_use_testnet": _to_bool,
}

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


def _xai_key_present() -> bool:
    """True when an xAI key is configured (so the Grok gates can actually run)."""
    key = settings.xai_api_key
    val = key.get_secret_value() if hasattr(key, "get_secret_value") else str(key or "")
    return bool(val and val.strip())


def full_auto_on(db: Session) -> dict:
    """Enable full-auto: scheduler + auto-trade + auto-approve as one, plus the Grok
    co-pilot + scanner gate when an xAI key is configured. Persists every flag so the
    whole stack survives a restart (the scheduler is auto-started by main.lifespan when
    KEY_FULL_AUTO is set). The scheduler loop itself is started by the caller/route."""
    settings.full_auto = True
    settings.auto_trade = True
    settings.autoapprove_enabled = True
    set_bool(db, KEY_FULL_AUTO, True)
    if _xai_key_present():
        grok_set(db, True)
        grok_scanner_set(db, True)
    return state(db)


def full_auto_off(db: Session) -> dict:
    """Disable full-auto: clears auto-trade/auto-approve and the Grok gates, and persists
    every flag so nothing silently re-enables on the next restart."""
    settings.full_auto = False
    settings.auto_trade = False
    settings.autoapprove_enabled = False
    set_bool(db, KEY_FULL_AUTO, False)
    grok_set(db, False)
    grok_scanner_set(db, False)
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


def set_live_trading(db: Session, enabled: bool) -> dict:
    """Persist the go-live master switch. Real placement still needs exchange keys; this
    only flips intent. The caller (route) is responsible for the typed-confirm gate."""
    settings.live_trading = enabled
    set_bool(db, KEY_LIVE_TRADING, enabled)
    return state(db)


def set_autoapprove(db: Session, *, enabled: bool, max_notional: float | None) -> None:
    """Persist the auto-approval rule (enabled + max notional) so it survives restarts.

    If max_notional is None, preserve the currently persisted value.
    """
    settings.autoapprove_enabled = enabled
    set_bool(db, KEY_AUTOAPPROVE_ENABLED, enabled)
    if max_notional is not None:
        settings.autoapprove_max_notional = max_notional
        set(db, KEY_AUTOAPPROVE_MAX, max_notional)
    else:
        # Preserve the existing persisted max_notional; don't let it revert to default.
        persisted = get(db, KEY_AUTOAPPROVE_MAX)
        if persisted is not None:
            try:
                settings.autoapprove_max_notional = float(persisted)
            except ValueError:
                pass  # Ignore corrupt values; keep the in-memory default


def kss_settings(db: Session) -> dict:  # noqa: ARG001 (db kept for a uniform signature)
    """Current master KSS knobs from the live settings singleton."""
    return {k: getattr(settings, k) for k in KSS_SETTING_FIELDS}


_KSS_ENUM_VALIDATORS: dict[str, set[str]] = {
    "grok_scanner_fail_mode": {"open", "closed"},
}


def set_kss_settings(db: Session, values: dict) -> dict:
    """Update + persist the master KSS knobs (applied to NEW sessions). Returns the new set."""
    for key, cast in KSS_SETTING_FIELDS.items():
        if values.get(key) is None:
            continue
        try:
            val = cast(values[key])
        except (TypeError, ValueError):
            continue
        # Validate enum-typed fields (e.g. grok_scanner_fail_mode: open|closed).
        allowed = _KSS_ENUM_VALIDATORS.get(key)
        if allowed is not None and val not in allowed:
            continue  # reject invalid enum values silently (bad input, not an error)
        setattr(settings, key, val)
        set(db, f"kss:{key}", val)
    return kss_settings(db)


def get_consensus_weights(db: Session) -> dict[str, float]:
    """Return the active consensus agent weights.

    Reads from runtime_config (KEY_CONSENSUS_WEIGHTS) so dashboard edits survive
    restarts.  Falls back to DEFAULT_WEIGHTS when no override is stored, keeping
    the backtest weight at 0 per the S4 contract.
    """
    import json as _json

    raw = get(db, KEY_CONSENSUS_WEIGHTS)
    if raw:
        try:
            stored = _json.loads(raw)
            # Merge: stored overrides defaults so new agents added later get a 0 weight
            # rather than being silently absent.
            merged = dict(DEFAULT_WEIGHTS)
            merged.update({k: float(v) for k, v in stored.items()})
            return merged
        except (ValueError, TypeError):
            pass
    return dict(DEFAULT_WEIGHTS)


def set_consensus_weights(db: Session, weights: dict[str, float]) -> dict[str, float]:
    """Persist agent consensus weights.  Returns the saved weights dict.

    Only the five signal-agent keys are accepted; the backtest key is always
    forced to 0 regardless of what is submitted (gates own the backtest evidence).
    """
    import json as _json

    allowed = {"trend", "dip", "volatility", "liquidity", "ml"}
    cleaned: dict[str, float] = dict(DEFAULT_WEIGHTS)  # start from defaults (backtest=0)
    for k, v in weights.items():
        if k in allowed:
            try:
                cleaned[k] = float(v)
            except (TypeError, ValueError):
                pass
    cleaned["backtest"] = 0.0  # invariant: backtest never scores in consensus
    set(db, KEY_CONSENSUS_WEIGHTS, _json.dumps(cleaned))
    return cleaned


def grok_set(db: Session, enabled: bool) -> dict:
    """Enable/disable the Grok co-pilot. Persisted."""
    settings.grok_enabled = enabled
    set_bool(db, KEY_GROK_ENABLED, enabled)
    return state(db)


def grok_scanner_set(db: Session, enabled: bool) -> dict:
    """Enable/disable the Grok scanner gate (independent of OPUS mode). Persisted."""
    settings.grok_scanner_enabled = enabled
    set_bool(db, KEY_GROK_SCANNER, enabled)
    return state(db)


def ta_source_set(db: Session, *, lib: bool | None = None, external: bool | None = None) -> dict:
    """Toggle the optional TA overlays feeding the Grok gate (Tier 2 lib / Tier 3 external).
    Tier 1 pure-Python indicators are always on, so these only add/remove overlays. Persisted."""
    if lib is not None:
        settings.ta_lib_enabled = lib
        set_bool(db, KEY_TA_LIB, lib)
    if external is not None:
        settings.ta_external_enabled = external
        set_bool(db, KEY_TA_EXTERNAL, external)
    return state(db)


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
        "grok_scanner": settings.grok_scanner_enabled,
        "ta_lib": settings.ta_lib_enabled,
        "ta_external": settings.ta_external_enabled,
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
    # Full-auto may come from persisted state (the dashboard switch) OR from the
    # environment (FULL_AUTO=true in .env) — honour either, and cascade the same flags
    # full_auto_on() sets so a fresh boot behaves exactly like a clicked one.
    full_auto = settings.full_auto or get_bool(db, KEY_FULL_AUTO, default=False)
    if full_auto:
        settings.full_auto = True
        settings.auto_trade = True
        settings.autoapprove_enabled = True
    settings.opus_mode = get_bool(db, KEY_OPUS_MODE, default=settings.opus_mode)
    settings.opus_shadow = get_bool(db, KEY_OPUS_SHADOW, default=settings.opus_shadow)
    settings.grok_enabled = get_bool(db, KEY_GROK_ENABLED, default=settings.grok_enabled)
    settings.grok_scanner_enabled = get_bool(db, KEY_GROK_SCANNER, default=settings.grok_scanner_enabled)
    # Under full-auto the Grok gates are part of the bundle when an xAI key is configured.
    if full_auto and _xai_key_present():
        settings.grok_enabled = True
        settings.grok_scanner_enabled = True
    settings.ta_lib_enabled = get_bool(db, KEY_TA_LIB, default=settings.ta_lib_enabled)
    settings.ta_external_enabled = get_bool(db, KEY_TA_EXTERNAL, default=settings.ta_external_enabled)
    # Go-live master switch — persisted operator choice; .env default is OFF. Even when
    # restored True, real placement is inert unless exchange keys are configured.
    settings.live_trading = get_bool(db, KEY_LIVE_TRADING, default=settings.live_trading)
    # Auto-approval rule (persisted so a dashboard change survives a restart).
    if get(db, KEY_AUTOAPPROVE_ENABLED) is not None:
        settings.autoapprove_enabled = get_bool(db, KEY_AUTOAPPROVE_ENABLED)
    aa_max = get(db, KEY_AUTOAPPROVE_MAX)
    if aa_max is not None:
        try:
            settings.autoapprove_max_notional = float(aa_max)
        except ValueError:
            pass
    # Master KSS knobs (persisted dashboard edits).
    for key, cast in KSS_SETTING_FIELDS.items():
        raw = get(db, f"kss:{key}")
        if raw is not None:
            try:
                val = cast(raw)
            except (TypeError, ValueError):
                continue
            allowed = _KSS_ENUM_VALIDATORS.get(key)
            if allowed is not None and val not in allowed:
                continue  # ignore corrupt/invalid stored enum values
            setattr(settings, key, val)
