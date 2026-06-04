"""
Integration tests: AI Guardian veto propagation through the scheduler cycle
and orders domain functions.

All external calls (market prices, Anthropic API, Telegram, scanner) are
monkeypatched so these tests are deterministic and network-free.
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from app import orders, scanner, scheduler
from app.config import settings
from app.kss import service
from app.models import PENDING, PendingOrder

_DAY = 86_400_000

# ---------------------------------------------------------------------------
# Shared market / scanner fakes  (mirrors test_scheduler.py)
# ---------------------------------------------------------------------------


def _uptrend(n: int = 60, start: float = 100.0, vol: float = 1e6) -> list[dict]:
    out, price = [], start
    for d in range(n):
        out.append({"ts": d * _DAY, "open": price, "high": price,
                    "low": price * 0.999, "close": price, "volume": vol})
        price *= 1.01
    return out


class _FakeProvider:
    def get_ohlcv(self, symbol, timeframe="1d", limit=200):
        return _uptrend() if symbol == "BTC" else []

    def all_symbols(self, min_quote_volume=0.0):
        return ["BTC"]

    def top_symbols(self, n=10):
        return ["BTC"]

    def get_prices(self, symbols):
        return dict.fromkeys(symbols, 1.0)

    def get_exchange_info(self, symbol):
        return {"minQty": 0.00001, "stepSize": 0.00001, "maxQty": 10000.0}


@pytest.fixture
def env(monkeypatch):
    """Full market + scanner fake (mirrors test_scheduler.py env fixture)."""
    monkeypatch.setattr(scanner, "data_provider", lambda: _FakeProvider())
    monkeypatch.setattr("app.kss.pyramid.get_exchange_info",
                        lambda s: {"minQty": 0.00001, "stepSize": 0.00001, "maxQty": 10000.0})
    monkeypatch.setattr("app.kss.pyramid.get_current_prices", lambda syms: dict.fromkeys(syms, 1.0))
    monkeypatch.setattr("app.orders.get_current_prices", lambda syms: dict.fromkeys(syms, 1.0))
    monkeypatch.setattr("app.market.get_current_prices", lambda syms: dict.fromkeys(syms, 1.0))
    monkeypatch.setattr(settings, "watchlist", ["BTC"])
    monkeypatch.setattr(settings, "min_confidence", 0.0)
    monkeypatch.setattr(settings, "min_win_rate", 0.0)
    monkeypatch.setattr(settings, "max_loss_rate", 100.0)
    monkeypatch.setattr(settings, "auto_trade", True)


def _new_session(db):
    """Create and start a minimal KSS session that queues wave-0 immediately."""
    row = service.create_session(
        db, symbol="BTC", entry_price=100.0, distance_pct=2, max_waves=3,
        isolated_fund=100000, tp_pct=3, timeout_x_min=999999.0, gap_y_min=0.0,
    )
    service.start_session(db, row.id)
    return row


def _enable_guardian(monkeypatch) -> None:
    monkeypatch.setattr(settings, "guardian_enabled", True)
    monkeypatch.setattr(settings, "anthropic_api_key", SecretStr("test-key"))


def _noop_send(monkeypatch) -> None:
    monkeypatch.setattr("app.notify.send", lambda text: False)


# ---------------------------------------------------------------------------
# Guardian veto sets auto_veto flag on the order
# ---------------------------------------------------------------------------


def test_run_cycle_guardian_vetoes_order(db, env, monkeypatch):
    """
    run_cycle with guardian enabled should mark the targeted order auto_veto=True
    and increment guardian_vetoes in the summary.
    """
    _enable_guardian(monkeypatch)
    _noop_send(monkeypatch)

    _new_session(db)

    # Find the pending order id that the session just queued
    pend = db.query(PendingOrder).filter(PendingOrder.status == PENDING).all()
    assert pend, "expected at least one pending order from session start"
    target_id = pend[0].id

    canned = f'{{"vetoes":[{{"id":{target_id},"reason":"test veto reason"}}]}}'
    monkeypatch.setattr("app.guardian._call_anthropic", lambda *a, **kw: canned)

    summary = scheduler.run_cycle(db)

    assert summary["guardian_vetoes"] >= 1

    db.expire_all()
    order = db.get(PendingOrder, target_id)
    assert order.auto_veto is True
    assert order.auto_veto_reason == "test veto reason"


# ---------------------------------------------------------------------------
# auto_fill_due_orders skips vetoed orders
# ---------------------------------------------------------------------------


def test_auto_fill_skips_vetoed_order(db, env, monkeypatch):
    """auto_fill_due_orders must not fill an order that has auto_veto=True."""
    _new_session(db)

    pend = db.query(PendingOrder).filter(PendingOrder.status == PENDING).all()
    assert pend
    order = pend[0]
    order.auto_veto = True
    order.auto_veto_reason = "test"
    db.commit()

    filled = orders.auto_fill_due_orders(db)
    assert order.id not in filled


# ---------------------------------------------------------------------------
# auto_approve_by_policy skips vetoed orders
# ---------------------------------------------------------------------------


def test_auto_approve_by_policy_skips_vetoed_order(db, monkeypatch):
    """auto_approve_by_policy must not approve an order flagged auto_veto."""
    monkeypatch.setattr(settings, "autoapprove_enabled", True)
    monkeypatch.setattr(settings, "autoapprove_sources", ["kss"])
    monkeypatch.setattr(settings, "autoapprove_max_notional", 1_000_000.0)
    monkeypatch.setattr(settings, "autoapprove_require_no_risk", False)
    monkeypatch.setattr("app.orders.get_current_prices", lambda syms: dict.fromkeys(syms, 1.0))
    monkeypatch.setattr("app.market.get_current_prices", lambda syms: dict.fromkeys(syms, 1.0))

    # Queue an order manually (no KSS session needed)
    order = PendingOrder(
        symbol="BTC",
        side="BUY",
        order_type="LIMIT",
        quantity=0.01,
        price=1.0,
        source="kss",
        status=PENDING,
        auto_veto=True,
        auto_veto_reason="policy blocked",
    )
    db.add(order)
    db.commit()
    db.refresh(order)

    approved = orders.auto_approve_by_policy(db)
    assert order.id not in approved


# ---------------------------------------------------------------------------
# Vetoed order is still approvable by a human reviewer
# ---------------------------------------------------------------------------


def test_vetoed_order_approvable_by_human(db, monkeypatch):
    """
    A vetoed order stays PENDING so a human (dashboard) can override.
    approve_order(reviewer='dashboard') must succeed even when auto_veto=True.
    """
    monkeypatch.setattr("app.orders.get_current_prices", lambda syms: dict.fromkeys(syms, 100.0))
    monkeypatch.setattr("app.market.get_current_prices", lambda syms: dict.fromkeys(syms, 100.0))

    order = PendingOrder(
        symbol="BTC",
        side="BUY",
        order_type="LIMIT",
        quantity=0.001,
        price=100.0,
        source="manual",
        status=PENDING,
        auto_veto=True,
        auto_veto_reason="guardian test",
    )
    db.add(order)
    db.commit()
    db.refresh(order)

    fill = orders.approve_order(db, order.id, reviewer="dashboard")
    assert fill is not None
    assert fill.symbol == "BTC"


# ---------------------------------------------------------------------------
# run_cycle guardian summary key present even when guardian disabled
# ---------------------------------------------------------------------------


def test_run_cycle_guardian_vetoes_zero_when_disabled(db, env, monkeypatch):
    """guardian_vetoes key is present and 0 when guardian is off."""
    _noop_send(monkeypatch)
    # guardian disabled by default
    _new_session(db)
    summary = scheduler.run_cycle(db)
    assert "guardian_vetoes" in summary
    assert summary["guardian_vetoes"] == 0


# ---------------------------------------------------------------------------
# Guardian fail-open in cycle: network error still allows auto-fill
# ---------------------------------------------------------------------------


def test_run_cycle_guardian_fail_open_allows_fill(db, env, monkeypatch):
    """
    When _call_anthropic raises and fail_open=True, the cycle must NOT veto
    any orders — auto_fill should proceed normally.
    """
    _enable_guardian(monkeypatch)
    _noop_send(monkeypatch)
    monkeypatch.setattr(settings, "guardian_fail_open", True)
    monkeypatch.setattr("app.guardian._call_anthropic",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("timeout")))

    _new_session(db)
    summary = scheduler.run_cycle(db)

    assert summary["guardian_vetoes"] == 0
    # price=1.0 ≤ order price=100.0 → BUY is due → auto_fill fires
    assert len(summary["auto_filled"]) >= 1
