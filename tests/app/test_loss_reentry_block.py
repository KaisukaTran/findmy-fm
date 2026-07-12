"""Loss re-entry escalation block (memory: loss-cases.md, kss-strategy.md).

Kai's rule: after a coin hits the HARD stop-loss, space out re-opening it, escalating by
how many times it has been stopped out — 1st hard-SL → block N weeks, 2nd → M weeks (≈2
months), 3rd → blacklist (indefinite, cleared only via manual pardon). "Large loss" == "was
stopped out at the hard SL" (source_ref …:sl); trailing exits (…:trail_sl) do NOT count.

Pure DB reads, no network. Mirrors test_kss_loss_guards.py.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app import scanner
from app.config import settings
from app.models import Fill


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    # Loss re-entry escalation under test — defaults Kai chose.
    monkeypatch.setattr(settings, "loss_reentry_enabled", True)
    monkeypatch.setattr(settings, "loss_reentry_weeks_1", 2)
    monkeypatch.setattr(settings, "loss_reentry_weeks_2", 8)
    monkeypatch.setattr(settings, "loss_reentry_blacklist_after", 3)
    monkeypatch.setattr(settings, "loss_reentry_pardon", "")
    # Isolate this gate: silence the neighbouring gates so _trade_block_reason returns OUR reason.
    monkeypatch.setattr(settings, "stop_cooldown_min", 0.0)
    monkeypatch.setattr(settings, "loss_block_enabled", False)


def _fill(db, symbol, days_ago, ref, *, side="SELL", pnl=-100.0):
    db.add(Fill(symbol=symbol, side=side, quantity=1.0, price=1.0, fee=0.0, slippage=0.0,
                realized_pnl=pnl, source_ref=ref, strategy_name="KSS",
                executed_at=datetime.now(timezone.utc) - timedelta(days=days_ago)))
    db.commit()


def _sl(db, symbol, days_ago, sid=1):
    _fill(db, symbol, days_ago, f"pyramid:{sid}:sl")


# ---- count / classification ----

def test_no_hard_sl_not_blocked(db):
    _fill(db, "AAA", 1, "pyramid:1:tp", pnl=50.0)          # a win, not a hard SL
    assert scanner._loss_reentry_block(db, "AAA") == (False, None)


def test_trail_sl_does_not_count(db):
    _fill(db, "AAA", 1, "pyramid:1:trail_sl", pnl=-100.0)  # trailing exit, NOT a hard SL
    _fill(db, "AAA", 2, "pyramid:2:trail_sl", pnl=-100.0)
    assert scanner._loss_reentry_block(db, "AAA")[0] is False


# ---- tier 1: 1 hard-SL → weeks_1 ----

def test_tier1_recent_blocks(db):
    _sl(db, "AAA", days_ago=3)                              # within 2 weeks
    blocked, reason = scanner._loss_reentry_block(db, "AAA")
    assert blocked is True and "2 tuần" in reason


def test_tier1_expired_allows(db):
    _sl(db, "AAA", days_ago=20)                             # past 2 weeks (14d) → decayed
    assert scanner._loss_reentry_block(db, "AAA")[0] is False


def test_win_after_sl_still_blocked_within_window(db):
    """Count-based (not streak): a later winning close does NOT lift the block early."""
    _sl(db, "AAA", days_ago=5)
    _fill(db, "AAA", 1, "pyramid:9:tp", pnl=200.0)         # a win 1 day ago
    assert scanner._loss_reentry_block(db, "AAA")[0] is True


# ---- tier 2: 2 hard-SL → weeks_2 ----

def test_tier2_recent_blocks(db):
    _sl(db, "AAA", days_ago=40, sid=1)
    _sl(db, "AAA", days_ago=10, sid=2)                      # most-recent 10d < 8 weeks (56d)
    blocked, reason = scanner._loss_reentry_block(db, "AAA")
    assert blocked is True and "8 tuần" in reason


def test_tier2_expired_allows(db):
    _sl(db, "AAA", days_ago=90, sid=1)
    _sl(db, "AAA", days_ago=60, sid=2)                      # most-recent 60d > 8 weeks (56d)
    assert scanner._loss_reentry_block(db, "AAA")[0] is False


# ---- tier 3: blacklist ----

def test_blacklist_after_three_regardless_of_age(db):
    _sl(db, "AAA", days_ago=200, sid=1)
    _sl(db, "AAA", days_ago=150, sid=2)
    _sl(db, "AAA", days_ago=120, sid=3)                    # 3rd → blacklist even though old
    blocked, reason = scanner._loss_reentry_block(db, "AAA")
    assert blocked is True and "blacklist" in reason.lower()


def test_pardon_exempts_blacklisted(db):
    for i, d in enumerate((30, 20, 10), start=1):
        _sl(db, "AAA", days_ago=d, sid=i)                  # 3 SL → would be blacklisted
    settings.loss_reentry_pardon = "aaa, bbb"              # case/space-insensitive
    assert scanner._loss_reentry_block(db, "AAA")[0] is False


def test_disabled_never_blocks(db):
    for i, d in enumerate((30, 20, 10), start=1):
        _sl(db, "AAA", days_ago=d, sid=i)
    settings.loss_reentry_enabled = False
    assert scanner._loss_reentry_block(db, "AAA")[0] is False


# ---- integration + audit + UI helper ----

def test_trade_block_reason_returns_loss_reentry(db):
    _sl(db, "C", days_ago=2)                               # the real C case: 1 recent hard-SL
    reason = scanner._trade_block_reason(db, "C")
    assert reason is not None and "tuần" in reason
    # audit row written
    from app.models import AuditLog
    assert db.query(AuditLog).filter(AuditLog.action == "skipped_loss_reentry",
                                     AuditLog.entity == "C").count() == 1


def test_blocklist_helper_lists_blocked(db):
    _sl(db, "AAA", days_ago=2)                             # tier1 blocked
    _fill(db, "BBB", 1, "pyramid:1:tp", pnl=10.0)         # clean, not blocked
    _sl(db, "CCC", days_ago=100)                          # tier1 expired → not blocked
    syms = {r["symbol"] for r in scanner.loss_reentry_blocklist(db)}
    assert "AAA" in syms and "BBB" not in syms and "CCC" not in syms


def test_knobs_persist_and_restore(db):
    """The 3-point wiring (config field ↔ KSS_SETTING_FIELDS ↔ KssSettingsBody) round-trips."""
    from app import runtime

    runtime.set_kss_settings(db, {"loss_reentry_weeks_1": 3, "loss_reentry_weeks_2": 12,
                                  "loss_reentry_blacklist_after": 4, "loss_reentry_enabled": False,
                                  "loss_reentry_pardon": "C, MIRA"})
    assert settings.loss_reentry_weeks_1 == 3
    assert settings.loss_reentry_weeks_2 == 12
    assert settings.loss_reentry_blacklist_after == 4
    assert settings.loss_reentry_enabled is False
    assert settings.loss_reentry_pardon == "C, MIRA"
    # persisted under the kss: prefix so it survives a restart
    assert runtime.get(db, "kss:loss_reentry_weeks_1") == "3"
