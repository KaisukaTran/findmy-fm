"""Phase 2: wiring `app/capital_scale.py`'s helpers into their real call sites.

Companion to `tests/app/test_capital_scale.py` (Phase 1: the pure resolve-at-read-time math,
called by nothing yet). These tests exercise the ACTUAL call sites this phase wires — session
open (`kss.service.create_session`), the cash-cap gate (`orders._apply_cash_cap`), the live/
resting notional caps, and the auto-approve ceiling — proving the acceptance criterion: switch
OFF is a byte-identical no-op, switch ON at the equity this was calibrated against ($200k)
reproduces today's numbers exactly, and switch ON at a much smaller equity actually changes
behaviour (a BUY the old flat $40,000 cash floor would have refused now succeeds).
"""

from __future__ import annotations

import pytest

from app import capital_scale, execution, models, orders
from app.config import settings
from app.kss import service as kss_service

# The live configuration these percentage defaults were calibrated against: at exactly this
# equity every *_pct knob resolves to today's absolute knob (docs/capital-scaling-policy.md).
LIVE_EQUITY = 200_000.0
LIVE_KNOBS = {
    "kss_first_wave_usd": 28.0,
    "cash_floor_usd": 40_000.0,
    "live_max_order_notional": 500.0,
    "autoapprove_max_notional": 5_000.0,
}


def _set_live_knobs(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in LIVE_KNOBS.items():
        monkeypatch.setattr(settings, name, value)


def _stub_exchange_info(monkeypatch: pytest.MonkeyPatch) -> None:
    """`create_session` -> `PyramidSession.__post_init__` calls the real (network) exchange-info
    lookup unless stubbed — same pattern as tests/app/test_ladder.py."""
    monkeypatch.setattr(
        "app.kss.pyramid.get_exchange_info",
        lambda s: {"minQty": 0.00001, "stepSize": 0.00001, "maxQty": 1e6},
    )


def _open_session(db, symbol: str = "BTC") -> models.KssSession:
    return kss_service.create_session(
        db, symbol=symbol, entry_price=100.0, distance_pct=2.0, max_waves=5,
        isolated_fund=1000.0, tp_pct=3.0, timeout_x_min=9999.0, gap_y_min=0.0,
    )


# --- 1. off is a no-op --------------------------------------------------------


def test_off_session_open_uses_the_raw_absolute_first_wave(db, monkeypatch):
    _stub_exchange_info(monkeypatch)
    monkeypatch.setattr(settings, "capital_scale_enabled", False)
    _set_live_knobs(monkeypatch)
    monkeypatch.setattr(settings, "account_equity", LIVE_EQUITY)

    row = _open_session(db)

    assert row.first_wave_usd == settings.kss_first_wave_usd


def test_off_cash_cap_caps_at_exactly_the_raw_setting(db, monkeypatch):
    """A BUY bigger than free cash partial-fills to exactly what the RAW cash_floor_usd allows
    — the same formula `_apply_cash_cap` always used, byte-for-byte, with the switch off."""
    monkeypatch.setattr(settings, "capital_scale_enabled", False)
    _set_live_knobs(monkeypatch)
    monkeypatch.setattr(settings, "account_equity", 100_000.0)
    o, _ = orders.queue_order(db, symbol="BTC", side="BUY", quantity=2.0, price=50_000.0)  # $100k

    fill = orders.approve_order(db, o.id)

    free = settings.account_equity - settings.cash_floor_usd  # 60,000 — the raw knob, untouched
    unit_cost = 50_000.0 * (1 + settings.slippage_pct / 100.0) * (1 + settings.taker_fee_pct / 100.0)
    expected_qty = (free / unit_cost) * (1 - 1e-9)
    assert fill.quantity == pytest.approx(expected_qty, rel=1e-9)


# --- 2. on at $200,000 is also a no-op ----------------------------------------


def test_on_at_200k_equity_reproduces_todays_first_wave(db, monkeypatch):
    _stub_exchange_info(monkeypatch)
    _set_live_knobs(monkeypatch)
    monkeypatch.setattr(settings, "capital_scale_enabled", True)
    monkeypatch.setattr(settings, "account_equity", LIVE_EQUITY)

    row = _open_session(db)

    assert row.first_wave_usd == pytest.approx(28.0)


def test_on_at_200k_equity_reproduces_todays_cash_cap(db, monkeypatch):
    _set_live_knobs(monkeypatch)
    monkeypatch.setattr(settings, "capital_scale_enabled", True)
    monkeypatch.setattr(settings, "account_equity", LIVE_EQUITY)
    o, _ = orders.queue_order(db, symbol="BTC", side="BUY", quantity=10.0, price=50_000.0)  # $500k

    fill = orders.approve_order(db, o.id)

    free = LIVE_EQUITY - 40_000.0  # raw and scaled agree exactly at this equity
    unit_cost = 50_000.0 * (1 + settings.slippage_pct / 100.0) * (1 + settings.taker_fee_pct / 100.0)
    expected_qty = (free / unit_cost) * (1 - 1e-9)
    assert fill.quantity == pytest.approx(expected_qty, rel=1e-9)


# --- 3. on at $7,000 actually changes the numbers -----------------------------


def test_on_at_7k_equity_floors_the_first_wave(db, monkeypatch):
    """7,000 x 0.014%% = $0.98 — dust the venue would reject; the exchange-min floor wins,
    not the flat $28 sized for a $200k book."""
    _stub_exchange_info(monkeypatch)
    _set_live_knobs(monkeypatch)
    monkeypatch.setattr(settings, "capital_scale_enabled", True)
    monkeypatch.setattr(settings, "account_equity", 7_000.0)

    row = _open_session(db)

    assert row.first_wave_usd == pytest.approx(settings.scan_min_notional)
    assert row.first_wave_usd != pytest.approx(28.0)


def test_cash_floor_scaling_unlocks_a_previously_refused_buy(db, monkeypatch):
    """The whole point of the feature: a $40,000 floor sized for a $200k book refuses every BUY
    on a $7,000 book; scaled to 20%% of equity ($1,400) the SAME order succeeds."""
    _set_live_knobs(monkeypatch)  # cash_floor_usd = $40,000 — sized for a $200k book
    monkeypatch.setattr(settings, "account_equity", 7_000.0)
    monkeypatch.setattr(settings, "capital_scale_enabled", False)
    o, _ = orders.queue_order(db, symbol="ETH", side="BUY", quantity=1.0, price=3_000.0)  # $3,000

    with pytest.raises(orders.InsufficientCashError):
        orders.approve_order(db, o.id)
    db.refresh(o)
    assert o.status == models.PENDING  # untouched — retried once cash frees / config changes

    monkeypatch.setattr(settings, "capital_scale_enabled", True)
    got = capital_scale.cash_floor_usd(db)
    assert got.value == pytest.approx(1_400.0)  # 7,000 x 20%% — not $40,000

    fill = orders.approve_order(db, o.id)
    assert fill.quantity == pytest.approx(1.0)  # now fits: $3,000 of $7,000 − $1,400 free


def test_live_notional_cap_scales_down_with_equity(db, monkeypatch):
    monkeypatch.setattr(execution, "live_enabled", lambda: True)
    monkeypatch.setattr(
        execution, "place_live_order",
        lambda *a, **k: pytest.fail("must be blocked before placement"),
    )
    _set_live_knobs(monkeypatch)  # live_max_order_notional = $500 — sized for a $200k book
    monkeypatch.setattr(settings, "capital_scale_enabled", True)
    monkeypatch.setattr(settings, "account_equity", 7_000.0)  # -> cap resolves to ~$17.50
    b, _ = orders.queue_order(db, symbol="BTC", side="BUY", quantity=1.0, price=100.0)  # $100

    with pytest.raises(ValueError, match="notional"):
        orders.approve_order(db, b.id)


def test_autoapprove_cap_scales_down_with_equity(db, monkeypatch):
    _set_live_knobs(monkeypatch)  # autoapprove_max_notional = $5,000 — sized for a $200k book
    monkeypatch.setattr(settings, "capital_scale_enabled", True)
    monkeypatch.setattr(settings, "autoapprove_enabled", True)
    monkeypatch.setattr(settings, "autoapprove_sources", ["manual"])

    monkeypatch.setattr(settings, "account_equity", 7_000.0)  # cap ~$175
    o1, _ = orders.queue_order(db, symbol="BTC", side="BUY", quantity=1.0, price=100.0,
                               order_type="MARKET", source="manual")
    assert orders.auto_approve_by_policy(db) == [o1.id]

    monkeypatch.setattr(settings, "account_equity", 1_000.0)  # cap ~$25 — a $100 order no longer fits
    o2, _ = orders.queue_order(db, symbol="ETH", side="BUY", quantity=1.0, price=100.0,
                               order_type="MARKET", source="manual")
    assert orders.auto_approve_by_policy(db) == []


# --- 4. an open session is never resized --------------------------------------


def test_an_open_session_is_never_resized_by_a_later_equity_move(db, monkeypatch):
    _stub_exchange_info(monkeypatch)
    _set_live_knobs(monkeypatch)
    monkeypatch.setattr(settings, "capital_scale_enabled", True)
    monkeypatch.setattr(settings, "account_equity", LIVE_EQUITY)

    old = _open_session(db, symbol="BTC")
    assert old.first_wave_usd == pytest.approx(28.0)

    monkeypatch.setattr(settings, "account_equity", 7_000.0)  # a huge drop — well past any deadband
    new = _open_session(db, symbol="ETH")

    db.refresh(old)
    assert old.first_wave_usd == pytest.approx(28.0)  # frozen at open — never re-priced
    assert new.first_wave_usd == pytest.approx(settings.scan_min_notional)  # new session, new equity


# --- 5. floored is audited, once ----------------------------------------------


def test_floored_read_is_audited_once_not_once_per_call(db, monkeypatch):
    monkeypatch.setattr(settings, "capital_scale_enabled", True)
    monkeypatch.setattr(settings, "kss_first_wave_usd", 28.0)
    monkeypatch.setattr(settings, "account_equity", 7_000.0)  # dust -> floored every read

    for _ in range(5):
        capital_scale.first_wave_usd(db)

    assert db.query(models.AuditLog).filter_by(action="capital_scale_floored").count() == 1


# --- rule 2: exits are never gated, on or off ---------------------------------


def test_sell_still_never_gated_when_capital_scale_is_on(db, monkeypatch):
    monkeypatch.setattr(settings, "account_equity", 100_000.0)
    b, _ = orders.queue_order(db, symbol="ETH", side="BUY", quantity=1.0, price=1000.0)
    orders.approve_order(db, b.id)

    # An absurd scaled cash floor — no BUY could ever pass — must still never touch a SELL.
    monkeypatch.setattr(settings, "capital_scale_enabled", True)
    monkeypatch.setattr(settings, "cash_floor_pct", 500.0)
    monkeypatch.setattr(settings, "account_equity", 0.0)  # cash exhausted vs invested
    s, _ = orders.queue_order(db, symbol="ETH", side="SELL", quantity=1.0, price=1100.0)

    fill = orders.approve_order(db, s.id)

    assert fill.quantity == pytest.approx(1.0)  # full exit, not gated
