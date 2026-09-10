"""Circuit breaker: the loss streak must count EXIT EVENTS, not raw SELL fills.

The bug locked down here was measured on the real book (2026-09-10). The only 4-long
"streak" in 125 historical exits was one market drop:

    INJ   pyramid:68:sl  -1.05  05:11:13
    UNI   pyramid:69:sl  -1.91  05:11:14
    ALICE pyramid:72:sl  -1.93  05:11:15   <- three seconds apart
    BABY  pyramid:73:sl  -1.24  05:24:50

One dip counted as four independent signals, and a session that exits through two fills
counted twice. Grouping by session and collapsing a ~300s window turns that into 2, while
a genuine streak spread over days (ZRO 16/08, FIL 18/08, KLAY 19/08) still counts 3. The
threshold does NOT move.
"""

from __future__ import annotations

from datetime import timedelta

from app import circuit, models, runtime
from app.clock import utcnow
from app.config import settings

BASE = utcnow()


def _exit(db, *, pnl: float, session: int | None, ago_sec: float, symbol: str = "BTC"):
    """One SELL fill, `ago_sec` before the shared BASE instant."""
    f = models.Fill(
        symbol=symbol,
        side="SELL",
        quantity=1.0,
        price=100.0,
        realized_pnl=pnl,
        source_ref=(f"pyramid:{session}:sl" if session is not None else None),
        executed_at=BASE - timedelta(seconds=ago_sec),
    )
    db.add(f)
    db.commit()
    return f


def _quiet_other_rules(monkeypatch):
    """Isolate the streak rule from drawdown / daily-loss (see test_circuit.py)."""
    monkeypatch.setattr(settings, "max_drawdown_pct", 100.0)
    monkeypatch.setattr(settings, "daily_loss_hard_pct", 100.0)


# ---------------------------------------------------------------------------
# Grouping: one session = one exit event
# ---------------------------------------------------------------------------


def test_two_exit_fills_of_one_session_are_one_event(db):
    _exit(db, pnl=-1.0, session=7, ago_sec=10)
    _exit(db, pnl=-1.0, session=7, ago_sec=100000)  # same session, far apart in time
    m = circuit.metrics(db)
    assert m["consecutive_losses"] == 2          # legacy over-counts
    assert m["consecutive_loss_events"] == 1     # one session, one outcome


def test_session_that_nets_positive_is_not_a_loss(db):
    """A partial take-profit then a small stop is ONE winning exit, not a loss."""
    _exit(db, pnl=+5.0, session=8, ago_sec=200000)
    _exit(db, pnl=-1.0, session=8, ago_sec=10)
    assert circuit.metrics(db)["consecutive_loss_events"] == 0


# ---------------------------------------------------------------------------
# Clustering: one market dip = one signal
# ---------------------------------------------------------------------------


def test_cascade_within_window_counts_once(db):
    for i, sec in enumerate((3, 2, 1)):
        _exit(db, pnl=-1.5, session=60 + i, ago_sec=sec, symbol=f"C{i}")
    m = circuit.metrics(db)
    assert m["consecutive_losses"] == 3
    assert m["consecutive_loss_events"] == 1


def test_historical_cascade_would_not_have_tripped(db, monkeypatch):
    """The exact 2026-08-22 shape: three stops in two seconds, a fourth 13 minutes later."""
    _quiet_other_rules(monkeypatch)
    monkeypatch.setattr(settings, "max_consecutive_losses", 4)
    monkeypatch.setattr(settings, "breaker_streak_shadow", False)

    _exit(db, pnl=-1.05, session=68, ago_sec=817, symbol="INJ")
    _exit(db, pnl=-1.91, session=69, ago_sec=816, symbol="UNI")
    _exit(db, pnl=-1.93, session=72, ago_sec=815, symbol="ALICE")
    _exit(db, pnl=-1.24, session=73, ago_sec=0, symbol="BABY")

    m = circuit.metrics(db)
    assert m["consecutive_losses"] == 4          # legacy: would freeze
    assert m["consecutive_loss_events"] == 2     # grouped: one dip plus one later stop
    assert circuit.evaluate(db)["frozen"] is False


def test_genuine_streak_over_days_still_counts(db, monkeypatch):
    """Losses days apart are independent signals - the brake must keep them."""
    _quiet_other_rules(monkeypatch)
    monkeypatch.setattr(settings, "max_consecutive_losses", 3)
    monkeypatch.setattr(settings, "breaker_streak_shadow", False)

    _exit(db, pnl=-7.3, session=90, ago_sec=3 * 86400, symbol="ZRO")
    _exit(db, pnl=-12.4, session=91, ago_sec=2 * 86400, symbol="FIL")
    _exit(db, pnl=-0.05, session=92, ago_sec=1 * 86400, symbol="KLAY")

    assert circuit.metrics(db)["consecutive_loss_events"] == 3
    assert circuit.evaluate(db)["frozen"] is True


def test_cluster_spans_at_most_the_window(db):
    """Chaining is deliberately NOT used: a slow bleed must not fuse into one cluster."""
    for i, sec in enumerate((0, 250, 500, 750)):  # 250s apart, 750s total span
        _exit(db, pnl=-1.0, session=100 + i, ago_sec=sec, symbol=f"S{i}")
    # A 300s window measured from each cluster's newest member -> {0,250} and {500,750}
    assert circuit.metrics(db)["consecutive_loss_events"] == 2


def test_win_still_breaks_the_streak(db):
    _exit(db, pnl=-1.0, session=110, ago_sec=3 * 86400)
    _exit(db, pnl=+9.0, session=111, ago_sec=2 * 86400)
    _exit(db, pnl=-1.0, session=112, ago_sec=1 * 86400)
    assert circuit.metrics(db)["consecutive_loss_events"] == 1


def test_fills_outside_any_session_stay_separate(db):
    """A manual sell has no pyramid source_ref - it must not collapse into its neighbours."""
    _exit(db, pnl=-1.0, session=None, ago_sec=3 * 86400, symbol="AAA")
    _exit(db, pnl=-1.0, session=None, ago_sec=2 * 86400, symbol="BBB")
    assert circuit.metrics(db)["consecutive_loss_events"] == 2


def test_scan_window_reaches_past_the_legacy_twenty_fills(db):
    """Grouping collapses many fills into few events, so the fill scan must be deeper."""
    for i in range(30):
        _exit(db, pnl=-1.0, session=200, ago_sec=86400 + i, symbol="DEEP")  # all ONE session
    _exit(db, pnl=-2.0, session=201, ago_sec=10, symbol="LATER")
    m = circuit.metrics(db)
    assert m["consecutive_losses"] == 20        # legacy caps its own scan at 20
    assert m["consecutive_loss_events"] == 2    # one long session plus one later stop


# ---------------------------------------------------------------------------
# Shadow mode - the new rule measures, the legacy rule still decides
# ---------------------------------------------------------------------------


def test_shadow_on_keeps_the_legacy_rule_in_charge(db, monkeypatch):
    _quiet_other_rules(monkeypatch)
    monkeypatch.setattr(settings, "max_consecutive_losses", 4)
    monkeypatch.setattr(settings, "breaker_streak_shadow", True)

    for i, sec in enumerate((4, 3, 2, 1)):
        _exit(db, pnl=-1.0, session=300 + i, ago_sec=sec, symbol=f"X{i}")

    result = circuit.evaluate(db)
    assert result["consecutive_loss_events"] == 1  # measured
    assert result["frozen"] is True                # but the old rule still drives


def test_shadow_divergence_is_audited_once(db, monkeypatch):
    _quiet_other_rules(monkeypatch)
    monkeypatch.setattr(settings, "max_consecutive_losses", 4)
    monkeypatch.setattr(settings, "breaker_streak_shadow", True)

    for i, sec in enumerate((4, 3, 2, 1)):
        _exit(db, pnl=-1.0, session=400 + i, ago_sec=sec, symbol=f"Y{i}")

    circuit.evaluate(db)
    circuit.evaluate(db)  # a divergence that persists must not spam the audit log

    rows = (
        db.query(models.AuditLog)
        .filter(models.AuditLog.action == "shadow_divergence")
        .all()
    )
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# The string-matching trap: reason CODES drive the decision, not the wording
# ---------------------------------------------------------------------------


def test_rearm_keys_off_the_reason_code_not_its_wording(db, monkeypatch):
    """Renaming the displayed reason must never turn a streak freeze into a deadlock.

    `blocking` used to be `[r for r in reasons if "consecutive_losses" not in r]` - a
    substring match on human text. Editing that text locked the account once already.
    """
    monkeypatch.setattr(settings, "breaker_cooldown_min", 0)
    monkeypatch.setattr(circuit, "metrics", lambda db: {
        "drawdown_pct": 0.1, "daily_loss_pct": 0.0,
        "consecutive_losses": 99, "consecutive_loss_events": 99,
    })
    monkeypatch.setattr(circuit, "_TEXT_LOSS_STREAK", "chuoi thua {n} >= {limit}")

    circuit.evaluate(db)                            # freezes on the streak
    assert runtime.is_frozen(db) is True
    assert circuit.evaluate(db)["frozen"] is False  # cooldown 0 -> stale streak releases


def test_evaluate_tolerates_metrics_without_the_new_key(db, monkeypatch):
    """test_breaker_deadlock.py patches metrics() with the three legacy keys only."""
    monkeypatch.setattr(settings, "max_consecutive_losses", 4)
    monkeypatch.setattr(circuit, "metrics", lambda db: {
        "drawdown_pct": 0.1, "daily_loss_pct": 0.0, "consecutive_losses": 4,
    })
    assert circuit.evaluate(db)["frozen"] is True
