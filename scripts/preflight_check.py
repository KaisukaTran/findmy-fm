#!/usr/bin/env python3
"""Pre-flight check script. Run before go-live to validate environment."""

import os
import sys
import sqlite3
from pathlib import Path

_root = Path(__file__).parent.parent
sys.path.insert(0, str(_root))
sys.path.insert(0, str(_root / "src"))

PASS = "\033[32m[PASS]\033[0m"
FAIL = "\033[31m[FAIL]\033[0m"
WARN = "\033[33m[WARN]\033[0m"

failures = []
warnings = []


def check(label: str, passed: bool, message: str = "", warn: bool = False):
    if passed:
        print(f"  {PASS} {label}")
    elif warn:
        print(f"  {WARN} {label}: {message}")
        warnings.append(label)
    else:
        print(f"  {FAIL} {label}: {message}")
        failures.append(label)


# ── Environment ──────────────────────────────────────────────────────────────
print("\n[1] Environment variables")

secret = os.getenv("APP_SECRET_KEY", "")
check("APP_SECRET_KEY set", bool(secret), "missing")
check("APP_SECRET_KEY length ≥ 32", len(secret) >= 32, f"only {len(secret)} chars")
KNOWN_WEAK = {"changeme", "secret", "your-secret-key", "dev-secret", ""}
check("APP_SECRET_KEY not default", secret.lower() not in KNOWN_WEAK, "using a known weak/default value")

db_url = os.getenv("DATABASE_URL", "")
check("DATABASE_URL set", bool(db_url), "missing")

live_mode = os.getenv("LIVE_TRADING_DRY_RUN", "true").lower()
check(
    "LIVE_TRADING_DRY_RUN is true (safe default)",
    live_mode in ("true", "1"),
    "set to false — real orders will be placed",
    warn=(live_mode not in ("true", "1")),
)

binance_key = os.getenv("BINANCE_API_KEY", "")
if live_mode not in ("true", "1"):
    check("BINANCE_API_KEY set for live mode", bool(binance_key), "required when dry_run=false")
else:
    check("BINANCE_API_KEY (paper mode — not required)", True)

# ── Database connectivity & schema ───────────────────────────────────────────
print("\n[2] Database")

db_path = db_url.replace("sqlite:///", "") if db_url.startswith("sqlite:///") else None

if db_path:
    try:
        conn = sqlite3.connect(db_path)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        required_tables = {
            "pending_orders", "system_state", "live_orders_audit",
            "users", "trades", "trade_pnl",
        }
        missing = required_tables - tables
        check("Required tables present", not missing, f"missing: {missing}")
        conn.close()
    except Exception as e:
        check("Database reachable", False, str(e))
else:
    check("Database reachable (non-SQLite, skipped)", True, warn=True)

# ── Alembic at head ───────────────────────────────────────────────────────────
print("\n[3] Alembic migrations")

try:
    from alembic.config import Config
    from alembic.runtime.migration import MigrationContext
    from sqlalchemy import create_engine

    _db_url = db_url or f"sqlite:///{_root}/data/trading.db"
    engine = create_engine(_db_url, connect_args={"check_same_thread": False})
    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn)
        current = ctx.get_current_revision()

    cfg = Config(str(_root / "alembic.ini"))
    from alembic.script import ScriptDirectory
    script = ScriptDirectory.from_config(cfg)
    head = script.get_current_head()

    check("Alembic at head", current == head, f"current={current}, head={head}")
    engine.dispose()
except Exception as e:
    check("Alembic check", False, str(e))

# ── Config sanity ─────────────────────────────────────────────────────────────
print("\n[4] Application config")

try:
    from findmy.config import settings
    check("Config loads", True)
    check(
        "max_position_size_pct > 0",
        settings.max_position_size_pct > 0,
        f"value={settings.max_position_size_pct}",
    )
    check(
        "max_daily_loss_pct > 0",
        settings.max_daily_loss_pct > 0,
        f"value={settings.max_daily_loss_pct}",
    )
    check(
        "max_orders_per_minute > 0",
        settings.max_orders_per_minute > 0,
        f"value={settings.max_orders_per_minute}",
    )
except Exception as e:
    check("Config loads", False, str(e))

# ── No demo users in production (warning only) ────────────────────────────────
print("\n[5] Security")

if db_path:
    try:
        conn = sqlite3.connect(db_path)
        demo_users = conn.execute(
            "SELECT username FROM users WHERE username IN ('trader1','trader2','demo')"
        ).fetchall()
        conn.close()
        check(
            "No demo users in DB",
            not demo_users,
            f"found: {[u[0] for u in demo_users]} — remove before production",
            warn=bool(demo_users),
        )
    except Exception:
        pass  # table may not exist yet

# ── Summary ───────────────────────────────────────────────────────────────────
print()
if failures:
    print(f"  RESULT: {len(failures)} FAILURE(S) — fix before go-live: {failures}")
    sys.exit(1)
elif warnings:
    print(f"  RESULT: PASSED with {len(warnings)} warning(s): {warnings}")
    sys.exit(0)
else:
    print("  RESULT: All checks passed. Ready for go-live.")
    sys.exit(0)
