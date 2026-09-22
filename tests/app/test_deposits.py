"""
Tests for the "record a deposit" feature (app/deposits.py, app/models.py::Deposit).

Covers: the capital anchor picking a deposit up on paper, the capital-scale sizing anchor
re-adopting equity immediately (bypassing its usual 10% deadband) ONLY for a real deposit
event, ROI/drawdown/breaker flow-safety (a deposit must never look like profit, recovery, or a
fresh drawdown), the API's validation/auth/idempotency, and template escaping.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app import capital_scale, circuit, costs, deposits, orders, portfolio, risk, runtime
from app.config import settings
from app.kss import service as kss_service
from app.main import app as fastapi_app
from app.models import AuditLog, Deposit, Fill, Position, Withdrawal

# Fixed historical anchor for tests that build a multi-event timeline: `_nav_walk`'s final
# point is always stamped at the REAL wall-clock "now", so backdating with
# `datetime.utcnow() + timedelta(days=N)` risks landing in the FUTURE relative to that final
# point (inverting the intended order) if N is anything but tiny. Building forward from a fixed
# past epoch instead keeps every event safely before "now" regardless of N.
_T0 = datetime(2026, 1, 1)


def _sell_fill(db, pnl: float, t: datetime | None = None, side: str = "SELL") -> Fill:
    f = Fill(symbol="BTC", side=side, quantity=1.0, price=100.0, realized_pnl=pnl,
              executed_at=t or datetime.utcnow())
    db.add(f)
    db.commit()
    return f


def _deposit_at(db, amount: float, t: datetime, note: str | None = None) -> Deposit:
    """Insert a Deposit with an explicit, backdated `created_at` — bypasses
    `deposits.record_deposit` (which always stamps "now") for tests that need a deposit to sit
    at a specific point in history relative to other events. NOTE: this does NOT set
    `equity_before` (mirrors a pre-this-column legacy row) — use `_deposit_via_record` for a
    test that needs the mark-to-market snapshot a REAL deposit gets."""
    d = Deposit(amount=amount, note=note, created_at=t)
    db.add(d)
    db.commit()
    db.refresh(d)
    return d


def _deposit_via_record(db, amount: float, t: datetime, note: str | None = None) -> Deposit:
    """Record a real deposit (so `equity_before` is snapshotted off the DB's actual state at
    call time, exactly as production does), then relabel its timestamp — the standard way to
    place a correctly-snapshotted flow at a specific point in a synthetic history without
    simulating real wall-clock delay."""
    d = deposits.record_deposit(db, amount, note=note)
    d.created_at = t
    db.commit()
    return d


def _withdrawal_via_record(db, amount: float, t: datetime, note: str | None = None) -> Withdrawal:
    """The withdrawal mirror of `_deposit_via_record`."""
    w = costs.record_withdrawal(db, amount, note=note)
    w.created_at = t
    db.commit()
    return w


def _position(db, symbol: str = "ETH", cost: float = 3500.0, entry: float = 100.0) -> Position:
    p = Position(symbol=symbol, quantity=cost / entry, avg_entry_price=entry, total_cost=cost)
    db.add(p)
    db.commit()
    return p


@pytest.fixture
def client():
    with TestClient(fastapi_app) as c:
        yield c


# --- capital_anchor picks a deposit up (paper) --------------------------------


def test_capital_anchor_includes_deposits_on_paper(db, monkeypatch):
    monkeypatch.setattr(settings, "live_trading", False)
    monkeypatch.setattr(settings, "account_equity", 7000.0)
    assert risk.capital_anchor(db) == 7000.0

    deposits.record_deposit(db, 500.0, note="monthly top-up")

    assert risk.capital_anchor(db) == pytest.approx(7500.0)
    assert risk.total_deposited(db) == pytest.approx(500.0)


def test_record_deposit_persists_and_audits(db):
    d = deposits.record_deposit(db, 250.0, note="  extra cash  ")
    assert isinstance(d, Deposit)
    assert d.amount == 250.0
    assert d.note == "extra cash"  # stripped
    row = db.query(AuditLog).filter(AuditLog.action == "deposit_recorded").one()
    assert row.entity == str(d.id)


def test_record_deposit_does_not_count_as_profit(db, monkeypatch):
    monkeypatch.setattr(settings, "account_equity", 7000.0)
    before = portfolio.summary_view(db)["realized_pnl"]
    deposits.record_deposit(db, 500.0)
    after = portfolio.summary_view(db)["realized_pnl"]
    assert after == before == 0.0


# --- capital_scale: a deposit re-anchors immediately, bypassing the deadband -----------


def test_scaled_first_wave_reacts_immediately_to_a_deposit(db, monkeypatch):
    """$500 on a $7,000 book is a 7.14% move — inside the default 10% deadband. An ordinary
    equity drift of that size would be ignored (see the next test); a recorded DEPOSIT must
    not be."""
    monkeypatch.setattr(settings, "live_trading", False)
    monkeypatch.setattr(settings, "account_equity", 7000.0)
    monkeypatch.setattr(settings, "capital_scale_enabled", True)
    monkeypatch.setattr(settings, "capital_scale_deadband_pct", 10.0)
    monkeypatch.setattr(settings, "first_wave_pct", 1.0)  # 1% of anchored equity
    monkeypatch.setattr(settings, "first_wave_max_usd", 0.0)  # no ceiling in play
    monkeypatch.setattr(settings, "scan_min_notional", 1.0)  # keep the floor well under $70

    before = capital_scale.first_wave_usd(db)
    assert before.value == pytest.approx(70.0)  # 1% of $7,000

    deposits.record_deposit(db, 500.0, note="monthly top-up")

    after = capital_scale.first_wave_usd(db)
    assert after.value == pytest.approx(75.0)  # 1% of the new $7,500 anchor, not still $70


def test_ordinary_pnl_move_inside_deadband_does_not_reanchor(db, monkeypatch):
    """The counterexample: the SAME size move (+7.14%), with no deposit recorded, must be
    swallowed by the deadband — proving the deposit path is a deliberate bypass, not a general
    loosening of it."""
    monkeypatch.setattr(settings, "capital_scale_deadband_pct", 10.0)
    monkeypatch.setattr(risk, "account_equity", lambda d: 7000.0)

    first = capital_scale.anchored_equity(db)
    assert first == 7000.0

    monkeypatch.setattr(risk, "account_equity", lambda d: 7500.0)  # +7.14%, no deposit recorded
    assert capital_scale.anchored_equity(db) == 7000.0


def test_reanchor_writes_only_the_capital_scale_anchor_key(db, monkeypatch):
    """The deposit re-anchor must not reach into any other runtime key (mirrors
    test_capital_scale.py's guarantee about capital_scale's OWN call paths)."""
    monkeypatch.setattr(settings, "account_equity", 7000.0)
    calls: list[str] = []
    real_set = runtime.set

    def _spy(db_arg, key, value):
        calls.append(key)
        return real_set(db_arg, key, value)

    monkeypatch.setattr(runtime, "set", _spy)
    deposits.record_deposit(db, 500.0)
    assert calls == [runtime.KEY_CAPITAL_SCALE_ANCHOR]


# --- ROI / drawdown flow-safety ------------------------------------------------


def test_equity_jumps_but_profit_and_drawdown_do_not(db, monkeypatch):
    monkeypatch.setattr(settings, "live_trading", False)
    monkeypatch.setattr(settings, "account_equity", 7000.0)
    now = datetime.utcnow()
    _sell_fill(db, -350.0, now - timedelta(minutes=5))  # a real 5% loss, BEFORE the deposit —
    # explicit ordering so the assertion never depends on how two `utcnow()` calls a few lines
    # apart happen to compare on a coarse clock.

    before_perf = portfolio.performance_view(db)
    before_summary = portfolio.summary_view(db)
    assert before_perf["max_drawdown_pct"] == pytest.approx(5.0)

    deposits.record_deposit(db, 500.0, note="top-up")

    after_perf = portfolio.performance_view(db)
    after_summary = portfolio.summary_view(db)

    # Equity jumps by exactly the deposit.
    assert after_summary["total_equity"] == pytest.approx(before_summary["total_equity"] + 500.0)
    # Profit (realized P&L) is untouched — a deposit books no fill.
    assert after_perf["realized_pnl"] == pytest.approx(before_perf["realized_pnl"])
    assert after_summary["realized_pnl"] == pytest.approx(before_summary["realized_pnl"])
    # Drawdown is flow-adjusted: bit-for-bit identical before/after the deposit.
    assert after_perf["max_drawdown_pct"] == pytest.approx(before_perf["max_drawdown_pct"])
    assert after_perf["current_drawdown_pct"] == pytest.approx(before_perf["current_drawdown_pct"])


def test_roi_base_grows_with_contributed_capital(db, monkeypatch):
    """Chosen ROI method (see app/portfolio.py::summary_view): profit / (base + Σ deposits),
    a capital-weighted return — NOT time-weighted. A deposit alone therefore DOES move
    `realized_pct` (the denominator grows while profit does not) — this is the documented
    trade-off, distinct from `max_drawdown_pct`/`current_drawdown_pct`, which must NOT move."""
    monkeypatch.setattr(settings, "account_equity", 7000.0)
    _sell_fill(db, 100.0)  # a realized gain to express as a %
    before_pct = portfolio.summary_view(db)["realized_pct"]

    deposits.record_deposit(db, 500.0)

    after_pct = portfolio.summary_view(db)["realized_pct"]
    assert before_pct == pytest.approx(100.0 / 7000.0 * 100)
    assert after_pct == pytest.approx(100.0 / 7500.0 * 100)
    assert after_pct < before_pct  # diluted, not inflated — profit itself is unchanged


def test_breaker_does_not_trip_or_untrip_on_deposit_alone(db, monkeypatch):
    monkeypatch.setattr(settings, "live_trading", False)
    monkeypatch.setattr(settings, "account_equity", 7000.0)
    monkeypatch.setattr(settings, "max_drawdown_pct", 15.0)
    # Silence the other two legs — not under test here. `daily_loss_pct` legitimately scales
    # with current equity (the same reasoning as risk.check_daily_loss / check_position_size),
    # so it is expected to move a little on a deposit; it is not part of this feature's
    # flow-safety contract the way `drawdown_pct` explicitly is.
    monkeypatch.setattr(settings, "daily_loss_hard_pct", 100.0)
    monkeypatch.setattr(settings, "max_consecutive_losses", 999)

    now = datetime.utcnow()
    _sell_fill(db, -350.0, now - timedelta(minutes=5))  # 5% drawdown BEFORE the deposit, well
    # below the 15% limit either side of it — explicit ordering, see the comment on
    # test_equity_jumps_but_profit_and_drawdown_do_not.

    before = circuit.evaluate(db)
    assert before["frozen"] is False
    before_dd = circuit.metrics(db)["drawdown_pct"]

    deposits.record_deposit(db, 500.0)

    after = circuit.evaluate(db)
    assert after["frozen"] is False
    assert circuit.metrics(db)["drawdown_pct"] == pytest.approx(before_dd)


# --- H1/H2 cross-check (2026-09-21): time-weighted unit-NAV + capital_anchor cash bugs ------
#
# An adversarial cross-check found the FIRST fix (subtract the deposit only from the curve's
# final point) still overstated drawdown whenever a flow landed BEFORE the loss it was meant to
# cover: every earlier curve point stayed at the bare `settings.account_equity`, so a $7,000
# book with 12 monthly $1,000 deposits read a real 10% loss as a 27.14% drawdown — enough to
# falsely freeze the circuit breaker (H1) — and separately, `orders._free_cash` /
# `kss.service._idle_deployable` still read `settings.account_equity` instead of
# `risk.capital_anchor(db)`, so a deposit's cash could never actually be spent (H2). Both are
# reproduced and fixed below; these tests port the cross-check's own numeric cases (its scratch
# file lives outside the repo, in the session scratchpad).
#
# Fix: `portfolio._nav_walk` walks fills AND capital flows (deposits; withdrawals where
# `risk.capital_anchor` subtracts them) in chronological order, mutual-fund style — a flow
# buys/redeems units at the NAV just before it lands, so it can never move NAV/unit, only
# realized P&L can. Drawdown is read off that NAV series, not the dollar curve.


def test_xchk_a_deposit_then_10pct_real_loss(db, monkeypatch):
    monkeypatch.setattr(settings, "live_trading", False)
    monkeypatch.setattr(settings, "account_equity", 7000.0)
    deposits.record_deposit(db, 1000.0)
    _sell_fill(db, -800.0)  # real capital 8,000; losing 800 is exactly 10%

    p = portfolio.performance_view(db)
    assert p["current_drawdown_pct"] == pytest.approx(10.0, abs=0.01)
    assert p["max_drawdown_pct"] == pytest.approx(10.0, abs=0.01)


def test_xchk_b_peak_before_deposit(db, monkeypatch):
    """A peak/trough that happened BEFORE a later deposit must read its own true %, not be
    diluted by capital that did not exist yet at the time."""
    monkeypatch.setattr(settings, "live_trading", False)
    monkeypatch.setattr(settings, "account_equity", 7000.0)
    now = datetime.utcnow()
    _sell_fill(db, 700.0, now - timedelta(days=5))   # peak 7,700
    _sell_fill(db, -700.0, now - timedelta(days=4))  # back to 7,000 -> 9.09% dd off that peak
    deposits.record_deposit(db, 1000.0)              # lands AFTER both fills

    p = portfolio.performance_view(db)
    assert p["current_drawdown_pct"] == pytest.approx(700 / 7700 * 100, abs=0.01)


def test_xchk_c_withdrawal_symmetric(db, monkeypatch):
    """The mirror image of (a): a REAL loss, then a withdrawal of the operator's own money.
    The withdrawal must not manufacture extra drawdown on top of the real 10% loss (H1 named
    this direction explicitly: 'withdrawal case under-reports (unsafe)' for the OLD fix, i.e.
    it could hide a real loss — the new one must show the TRUE % either way)."""
    monkeypatch.setattr(settings, "live_trading", True)
    monkeypatch.setattr(settings, "use_exchange_balance", False)
    monkeypatch.setattr(settings, "account_equity", 7000.0)
    now = datetime.utcnow()
    _sell_fill(db, -700.0, now - timedelta(minutes=10))  # a real 10% loss
    db.add(Withdrawal(amount=1000.0, fee=0.0, vat=0.0, exchange="binance",
                       created_at=now - timedelta(minutes=5)))
    db.commit()

    p = portfolio.performance_view(db)
    assert p["current_drawdown_pct"] == pytest.approx(10.0, abs=0.01)


def test_xchk_d_twelve_deposits_then_10pct_loss_no_breaker_trip(db, monkeypatch):
    monkeypatch.setattr(settings, "live_trading", False)
    monkeypatch.setattr(settings, "account_equity", 7000.0)
    monkeypatch.setattr(settings, "max_drawdown_pct", 15.0)
    # Silence the other two legs — this test is about the drawdown leg specifically.
    # `daily_loss_pct` legitimately scales with current equity (see the comment on
    # test_breaker_does_not_trip_or_untrip_on_deposit_alone); a real $1,900 same-day loss is
    # ~11% of the post-deposit book, which is not what this test is checking.
    monkeypatch.setattr(settings, "daily_loss_hard_pct", 100.0)
    monkeypatch.setattr(settings, "max_consecutive_losses", 999)
    now = datetime.utcnow()
    for i in range(12):
        _deposit_at(db, 1000.0, now - timedelta(days=30 * (12 - i)), note=f"m{i}")
    _sell_fill(db, -1900.0, now)  # 10% of the true $19,000 book

    p = portfolio.performance_view(db)
    m = circuit.metrics(db)
    assert p["current_drawdown_pct"] == pytest.approx(10.0, abs=0.01)
    assert m["drawdown_pct"] == pytest.approx(10.0, abs=0.01)
    assert circuit.evaluate(db)["frozen"] is False  # 10% < the 15% limit — must NOT trip


def test_xchk_d2_profit_after_deposits_not_overstated(db, monkeypatch):
    """Same 12-deposit book, but the swing is a PROFIT peak followed by giving it back — the
    true peak (20,900) must include the deposits that had already landed by the time it was
    set, or the % reads too high (the cross-check's own scratch test created its 12 deposits
    with a default `created_at` of "now", i.e. AFTER the two explicitly-backdated fills below —
    self-contradictory against its own comment ('peak 19,000+1,900 true'); backdated here to
    actually land before both fills, matching the intent, not the accident)."""
    monkeypatch.setattr(settings, "live_trading", False)
    monkeypatch.setattr(settings, "account_equity", 7000.0)
    now = datetime.utcnow()
    for i in range(12):
        _deposit_at(db, 1000.0, now - timedelta(days=30 * (12 - i)), note=f"m{i}")
    _sell_fill(db, 1900.0, now - timedelta(hours=2))   # true peak 19,000 + 1,900 = 20,900
    _sell_fill(db, -1900.0, now - timedelta(hours=1))  # gives it all back

    p = portfolio.performance_view(db)
    assert p["current_drawdown_pct"] == pytest.approx(1900 / 20900 * 100, abs=0.01)


def test_xchk_no_flow_book_drawdown_matches_the_old_dollar_only_formula(db, monkeypatch):
    """Regression: a book with NO deposits/withdrawals must read byte-identical drawdown % to
    the pre-NAV-walk dollar-only formula — the unit-NAV series is just equity/CONSTANT when no
    flow ever changes `units`, so every ratio (peak/current) is preserved exactly."""
    monkeypatch.setattr(settings, "live_trading", False)
    monkeypatch.setattr(settings, "account_equity", 10_000.0)
    now = datetime.utcnow()
    _sell_fill(db, 1000.0, now - timedelta(days=2))   # 11,000 (new peak)
    _sell_fill(db, -1100.0, now - timedelta(days=1))  # 9,900

    p = portfolio.performance_view(db)
    old_style_dd = (11_000.0 - 9_900.0) / 11_000.0 * 100  # the formula before this fix
    assert p["current_drawdown_pct"] == pytest.approx(old_style_dd, abs=0.01)
    assert p["max_drawdown_pct"] == pytest.approx(old_style_dd, abs=0.01)


def test_xchk_cash_spendable_after_deposit(db, monkeypatch):
    """H2: `orders._free_cash` used the bare `settings.account_equity` constant, so a deposit's
    cash was invisible to it (a BUY could never spend it) — must now read the capital anchor."""
    monkeypatch.setattr(settings, "live_trading", False)
    monkeypatch.setattr(settings, "account_equity", 7000.0)
    deposits.record_deposit(db, 1000.0)
    assert orders._free_cash(db) == pytest.approx(8000.0)


def test_xchk_idle_deployable_after_deposit(db, monkeypatch):
    """H2, other call site: manual DCA+ sizing off the same stale constant."""
    monkeypatch.setattr(settings, "live_trading", False)
    monkeypatch.setattr(settings, "account_equity", 7000.0)
    deposits.record_deposit(db, 1000.0)
    assert kss_service._idle_deployable(db) == pytest.approx(8000.0)


def test_xchk_dup_window_same_amount_other_day(db):
    """The 10s duplicate guard must not falsely fire across unrelated months: same amount
    recorded 30 days later is a legitimate NEW deposit, not a double-submit; a differing note
    right away is also never a duplicate."""
    d = deposits.record_deposit(db, 500.0)
    d.created_at = datetime.utcnow() - timedelta(days=30)
    db.commit()
    deposits.record_deposit(db, 500.0)          # same amount, 30 days later -> not a duplicate
    deposits.record_deposit(db, 500.0, note="x")  # same amount, different note -> not a duplicate
    assert db.query(Deposit).count() == 3


def test_deposit_makes_previously_unaffordable_buy_possible(db, monkeypatch):
    """H2, end-to-end: a BUY `_apply_cash_cap` would refuse outright (cash below min-notional)
    must pass once a deposit provides the missing cash."""
    monkeypatch.setattr(settings, "live_trading", False)
    monkeypatch.setattr(settings, "account_equity", 5.0)
    monkeypatch.setattr(settings, "cash_floor_usd", 0.0)
    monkeypatch.setattr(settings, "capital_scale_enabled", False)
    monkeypatch.setattr(settings, "scan_min_notional", 10.0)

    o, _ = orders.queue_order(db, symbol="BTC", side="BUY", quantity=1.0, price=100.0)
    with pytest.raises(orders.InsufficientCashError):
        orders.approve_order(db, o.id)
    db.refresh(o)
    assert o.status == "pending"

    deposits.record_deposit(db, 1000.0, note="top-up")

    fill = orders.approve_order(db, o.id)
    assert fill.quantity == pytest.approx(1.0)  # the full order now fits


def test_cash_floor_pct_applies_to_the_new_anchor_immediately(db, monkeypatch):
    """`capital_scale.cash_floor_usd` resolves against `capital_scale.anchored_equity`, which
    `deposits.record_deposit` force-reanchors — so the %-of-equity cash floor itself must move
    the instant a deposit lands, the same way `first_wave_usd` already does."""
    monkeypatch.setattr(settings, "live_trading", False)
    monkeypatch.setattr(settings, "account_equity", 7000.0)
    monkeypatch.setattr(settings, "capital_scale_enabled", True)
    monkeypatch.setattr(settings, "capital_scale_deadband_pct", 10.0)
    monkeypatch.setattr(settings, "cash_floor_pct", 20.0)

    before = capital_scale.cash_floor_usd(db)
    assert before.value == pytest.approx(1400.0)  # 20% of $7,000

    deposits.record_deposit(db, 500.0)  # +7.14% — inside the deadband, but a real deposit

    after = capital_scale.cash_floor_usd(db)
    assert after.value == pytest.approx(1500.0)  # 20% of the new $7,500 anchor


# --- round-2 cross-check (2026-09-21): unrealized-P&L flow pricing + edge cases -------------
#
# An adversarial cross-check found the round-1 NAV walk still overstated recovery whenever a
# flow landed while a position was underwater: units were priced off the running `equity`
# tracker, which only ever accumulates REALIZED fills, so an open position's unrealized loss
# was invisible to it. With SL=0 a real loss is almost always unrealized, so a routine monthly
# deposit would dilute a genuine drawdown right under the breaker's 15% freeze — $7,000 book,
# $3,500 at -20% (a true 10% drawdown), then a $7,000 deposit read 5.0% instead of 10.0%.
#
# Fix: `Deposit`/`Withdrawal.equity_before` snapshot the TRUE mark-to-market total equity
# (`summary_view`'s own number, including unrealized P&L) at record time; `_nav_walk` prices
# that flow's units off it instead of the realized-only tracker. `_deposit_via_record` /
# `_withdrawal_via_record` above go through the real recording functions (so the snapshot is
# genuine) and only relabel the timestamp afterward, the same way a raw `Deposit(created_at=…)`
# row would for a test that doesn't care about the snapshot.


def test_deposit_during_unrealized_drawdown_no_longer_dilutes_it(db, monkeypatch):
    """The bug, reproduced and fixed: $3,500 of $7,000 sits in a position marked -20%
    (unrealized -$700, a true 10% drawdown) when a $7,000 deposit lands. Before this fix the
    deposit's units were priced off the realized-only tracker (still $7,000, nav=1.0) and read
    5.0%; the snapshot must keep it at 10.0%."""
    monkeypatch.setattr(settings, "live_trading", False)
    monkeypatch.setattr(settings, "account_equity", 7000.0)
    prices = {"ETH": 80.0}  # entry 100 -> -20% unrealized
    monkeypatch.setattr(portfolio, "get_current_prices", lambda syms: dict.fromkeys(syms, prices["ETH"]))

    _position(db)
    _sell_fill(db, 0.0, _T0, side="BUY")  # opens the position; no realized P&L of its own
    _deposit_via_record(db, 7000.0, _T0 + timedelta(days=1))

    p = portfolio.performance_view(db)
    m = circuit.metrics(db)
    assert p["current_drawdown_pct"] == pytest.approx(10.0, abs=0.01)
    assert m["drawdown_pct"] == pytest.approx(10.0, abs=0.01)


def test_deposit_during_realized_drawdown_still_reads_full_pct(db, monkeypatch):
    """The non-unrealized counterpart: a REALIZED loss (no open position at all) was already
    correct before this fix (the running tracker sees a realized fill directly) — pinned here
    so the fix's fallback path for a plain deposit stays proven, not just the new branch."""
    monkeypatch.setattr(settings, "live_trading", False)
    monkeypatch.setattr(settings, "account_equity", 7000.0)
    _sell_fill(db, -1400.0, _T0)          # a real 20% loss
    _deposit_via_record(db, 5000.0, _T0 + timedelta(days=1))

    assert portfolio.performance_view(db)["current_drawdown_pct"] == pytest.approx(20.0, abs=0.01)


def test_deposit_then_recovery_gives_the_correct_time_weighted_return(db, monkeypatch):
    """Continuation of the unrealized-drawdown case: the position recovers to break-even
    AFTER the deposit. A correct time-weighted NAV reads this as ~5.3% off the true peak (NAV
    0.947), not 0% (which would be true only if the deposit had diluted the earlier loss) and
    not 10% (which would double-count it)."""
    monkeypatch.setattr(settings, "live_trading", False)
    monkeypatch.setattr(settings, "account_equity", 7000.0)
    prices = {"ETH": 80.0}
    monkeypatch.setattr(portfolio, "get_current_prices", lambda syms: dict.fromkeys(syms, prices["ETH"]))

    _position(db)
    _sell_fill(db, 0.0, _T0, side="BUY")
    _deposit_via_record(db, 7000.0, _T0 + timedelta(days=1))

    prices["ETH"] = 100.0  # position recovers to break-even (still unrealized, no new fill)
    p = portfolio.performance_view(db)
    assert p["current_drawdown_pct"] == pytest.approx(5.26, abs=0.05)  # (1 - 0.947) * 100


# --- round-3 cross-check (2026-09-21): re-basing the tracker double-counts unrealized P&L ---
#
# An orchestrator review of the round-2 fix caught a further bug: re-basing the running
# `equity` tracker to `equity_before + value` at a flow bakes that instant's UNREALIZED P&L
# into a tracker that is supposed to be realized-only. When the underwater position later
# CLOSES, its realized fill adds the same loss/gain a second time (over- or understating
# `max_drawdown_pct`/`current_drawdown_pct`). Fixed by never re-basing the tracker — only the
# one-off UNIT count at a flow is priced off `equity_before`; the tracker stays exactly
# "realized equity + cumulative flow amounts" the way it always was, and the tracker naturally
# catches back up to the true total for free once the position's P&L actually realizes.


def test_deposit_during_unrealized_drawdown_then_close_at_that_loss_max_dd_not_overstated(
    db, monkeypatch
):
    """(a) The position that was -$700 unrealized at deposit time later closes at EXACTLY
    that loss. Re-basing the tracker would count the $700 twice (once folded into the
    deposit's re-based tracker, once again when the closing fill realizes it) and read
    ~14.7% max drawdown; the true figure — matching the single real 10% dip — must hold."""
    monkeypatch.setattr(settings, "live_trading", False)
    monkeypatch.setattr(settings, "account_equity", 7000.0)
    prices = {"ETH": 80.0}
    monkeypatch.setattr(portfolio, "get_current_prices", lambda syms: dict.fromkeys(syms, prices["ETH"]))

    pos = _position(db)
    _sell_fill(db, 0.0, _T0, side="BUY")
    _deposit_via_record(db, 7000.0, _T0 + timedelta(days=1))
    _sell_fill(db, -700.0, _T0 + timedelta(days=2))  # closes at exactly the prior unrealized loss
    pos.quantity = 0.0
    pos.total_cost = 0.0
    db.commit()

    p = portfolio.performance_view(db)
    assert p["max_drawdown_pct"] == pytest.approx(10.0, abs=0.01)
    assert p["current_drawdown_pct"] == pytest.approx(10.0, abs=0.01)


def test_current_dd_matches_true_twr_across_flows_and_fills(db, monkeypatch):
    """(b) A longer walk exercising the exact numbers from the bug report: deposit while
    -$700 underwater (NAV 0.9) -> position recovers and closes at break-even (NAV 0.947) ->
    an unrelated +$700 realized gain (NAV 0.9947) -> a NEW position ends $1,400 underwater
    (final MTM $13,300). The true time-weighted drawdown is 10% off the NAV-1.0 peak; a
    re-based tracker understates the intermediate peak and reads ~5% instead."""
    monkeypatch.setattr(settings, "live_trading", False)
    monkeypatch.setattr(settings, "account_equity", 7000.0)
    prices = {"ETH": 80.0, "SOL": 100.0}
    monkeypatch.setattr(portfolio, "get_current_prices",
                        lambda syms: {s: prices.get(s, 0.0) for s in syms})

    pos_a = _position(db, symbol="ETH", cost=3500.0, entry=100.0)
    _sell_fill(db, 0.0, _T0, side="BUY")                        # opens A, -20% while ETH=80
    _deposit_via_record(db, 7000.0, _T0 + timedelta(days=1))    # deposit while A is underwater

    prices["ETH"] = 100.0                                        # A recovers to break-even
    _sell_fill(db, 0.0, _T0 + timedelta(days=2))                 # A closes at exactly break-even
    pos_a.quantity = 0.0
    pos_a.total_cost = 0.0
    db.commit()

    _sell_fill(db, 700.0, _T0 + timedelta(days=3))               # an unrelated realized gain

    _position(db, symbol="SOL", cost=2800.0, entry=100.0)        # a new open position
    prices["SOL"] = 50.0                                          # ...now -$1,400 unrealized

    p = portfolio.performance_view(db)
    assert p["total_equity"] == pytest.approx(13_300.0, abs=0.01)
    assert p["current_drawdown_pct"] == pytest.approx(10.0, abs=0.01)


def test_withdrawal_during_unrealized_drawdown_symmetric(db, monkeypatch):
    """The withdrawal mirror of the HIGH bug: a withdrawal recorded while underwater must not
    manufacture EXTRA drawdown beyond the real unrealized loss either."""
    monkeypatch.setattr(settings, "live_trading", True)
    monkeypatch.setattr(settings, "use_exchange_balance", False)
    monkeypatch.setattr(settings, "account_equity", 7000.0)
    prices = {"ETH": 80.0}
    monkeypatch.setattr(portfolio, "get_current_prices", lambda syms: dict.fromkeys(syms, prices["ETH"]))

    _position(db)
    _sell_fill(db, 0.0, _T0, side="BUY")
    _withdrawal_via_record(db, 1000.0, _T0 + timedelta(days=1))

    assert portfolio.performance_view(db)["current_drawdown_pct"] == pytest.approx(10.0, abs=0.01)


def test_withdrawal_equity_before_migration_registered(db):
    """`withdrawals` already exists in the running DB — `equity_before` must arrive via the
    ALTER-TABLE migration list, not rely on `create_all` (which never touches an existing
    table)."""
    from sqlalchemy import inspect

    from app.db import _ADDED_COLUMNS, engine

    assert ("withdrawals", "equity_before", "FLOAT") in _ADDED_COLUMNS
    cols = {c["name"] for c in inspect(engine).get_columns("withdrawals")}
    assert "equity_before" in cols


# --- round-2 LOW: nav <= 0 is a reset, not silent corruption --------------------------------


def test_deposit_into_zero_equity_books_full_drawdown_then_resets(db, monkeypatch):
    """Starting from literally $0, the first deposit can't be priced (nav would be 0/anything)
    — treated as a wipeout: max_drawdown reads 100%, and NAV re-seeds to 1.0 from the deposit
    itself so the SUBSEQUENT loss reads its own true %, not a corrupted/undefined one."""
    monkeypatch.setattr(settings, "live_trading", False)
    monkeypatch.setattr(settings, "account_equity", 0.0)
    _deposit_via_record(db, 5000.0, _T0)
    _sell_fill(db, -500.0, _T0 + timedelta(days=1))  # 10% off the post-deposit $5,000

    p = portfolio.performance_view(db)
    assert p["max_drawdown_pct"] == pytest.approx(100.0, abs=0.01)
    assert p["current_drawdown_pct"] == pytest.approx(10.0, abs=0.01)


def test_deposit_after_negative_equity_does_not_crash(db, monkeypatch):
    """A blown-through book (realized loss deeper than starting capital) followed by a rescue
    deposit must not raise — just smoke-tested, no specific % asserted (an intentionally
    degenerate state no real risk gate would ever let the book reach)."""
    monkeypatch.setattr(settings, "live_trading", False)
    monkeypatch.setattr(settings, "account_equity", 7000.0)
    _sell_fill(db, -8000.0, _T0)
    _deposit_via_record(db, 5000.0, _T0 + timedelta(days=1))
    _sell_fill(db, 100.0, _T0 + timedelta(days=2))

    p = portfolio.performance_view(db)  # must not raise
    assert p["max_drawdown_pct"] >= 0.0


# --- round-2 LOW: chart points sorted by timestamp, period seed uses the window's own event --


def test_all_period_points_are_time_sorted_when_a_deposit_predates_the_first_fill(db):
    """A deposit dated BEFORE the first fill must not appear after the seed point in
    `equity_times` ("time goes backwards") — every point sorted chronologically."""
    _deposit_at(db, 1000.0, _T0)
    _sell_fill(db, -100.0, _T0 + timedelta(days=1))

    times = portfolio.performance_view(db)["equity_times"]
    from datetime import datetime as _dt

    parsed = [_dt.fromisoformat(t) for t in times]
    assert parsed == sorted(parsed)


def test_period_view_seed_point_is_the_first_window_event_not_the_bare_cutoff(db):
    """The period-view seed's timestamp should be the first REAL event inside the window, not
    a synthetic point stamped exactly at the cutoff boundary where nothing happened."""
    now = datetime.utcnow()
    fill_t = now - timedelta(hours=1)
    _sell_fill(db, -10.0, fill_t)

    times = portfolio.performance_view(db, period="24h")["equity_times"]
    assert times[0] == fill_t.isoformat()


# --- round-2: no-flow regression -------------------------------------------------------------


def test_noflow_book_unaffected_by_the_equity_before_snapshot_logic(db, monkeypatch):
    """A book with zero deposits/withdrawals must be completely untouched by any of the
    round-2 changes — same numbers as the round-1 no-flow regression."""
    monkeypatch.setattr(settings, "live_trading", False)
    monkeypatch.setattr(settings, "account_equity", 10_000.0)
    now = datetime.utcnow()
    _sell_fill(db, 1000.0, now - timedelta(days=2))
    _sell_fill(db, -1100.0, now - timedelta(days=1))

    p = portfolio.performance_view(db)
    old_style_dd = (11_000.0 - 9_900.0) / 11_000.0 * 100
    assert p["current_drawdown_pct"] == pytest.approx(old_style_dd, abs=0.01)
    assert p["max_drawdown_pct"] == pytest.approx(old_style_dd, abs=0.01)


# --- API: validation, auth, idempotency, rendering ------------------------------


def test_create_deposit_endpoint_happy_path(client):
    r = client.post("/api/deposits", json={"amount": 500.0, "note": "monthly top-up"})
    assert r.status_code == 200
    body = r.json()["deposit"]
    assert body["amount"] == 500.0
    assert body["note"] == "monthly top-up"

    listed = client.get("/api/deposits").json()["rows"]
    assert len(listed) == 1 and listed[0]["amount"] == 500.0


@pytest.mark.parametrize("amount", [-10.0, 0.0, 20_000_000.0])
def test_create_deposit_endpoint_rejects_invalid_amount(client, amount):
    r = client.post("/api/deposits", json={"amount": amount})
    assert r.status_code == 422


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_record_deposit_rejects_non_finite_amount(db, bad):
    # Exercised at the domain layer, not through the HTTP endpoint: Pydantic DOES reject a
    # non-finite `amount` (gt=0/le=10_000_000 already fail on nan/inf), but Starlette's default
    # JSONResponse hard-codes `allow_nan=False` (starlette/responses.py), so the validation
    # error handler itself crashes trying to echo the rejected value back — a pre-existing
    # framework landmine shared by every float field in this app (e.g. WithdrawalBody.amount
    # has the identical exposure already), not something introduced or fixed here. The
    # request never reaches record_deposit in that case; this test pins that record_deposit's
    # OWN guard rejects the same values directly, independent of the transport layer.
    with pytest.raises(ValueError):
        deposits.record_deposit(db, bad)


def test_create_deposit_endpoint_rejects_long_note(client):
    r = client.post("/api/deposits", json={"amount": 10.0, "note": "x" * 201})
    assert r.status_code == 422


def test_create_deposit_endpoint_rejects_duplicate_submit(client):
    body = {"amount": 250.0, "note": "same"}
    r1 = client.post("/api/deposits", json=body)
    assert r1.status_code == 200
    r2 = client.post("/api/deposits", json=body)
    assert r2.status_code == 400

    # A different amount right after is NOT a duplicate.
    r3 = client.post("/api/deposits", json={"amount": 251.0, "note": "same"})
    assert r3.status_code == 200


def test_create_deposit_endpoint_requires_api_key_when_auth_on(client, monkeypatch):
    monkeypatch.setattr(settings, "require_auth", True)
    monkeypatch.setattr(settings, "api_key", SecretStr("a-strong-unique-key"))

    denied = client.post("/api/deposits", json={"amount": 100.0})
    assert denied.status_code == 401

    allowed = client.post("/api/deposits", json={"amount": 100.0},
                          headers={"X-API-Key": "a-strong-unique-key"})
    assert allowed.status_code == 200


def test_deposit_note_is_escaped_in_capital_partial(client, db):
    deposits.record_deposit(db, 42.0, note="<script>alert(1)</script>")
    r = client.get("/partials/capital")
    assert r.status_code == 200
    assert "<script>alert(1)</script>" not in r.text
    assert "&lt;script&gt;" in r.text
