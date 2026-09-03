"""D2-gap: repeated placement failures were totally silent.

`_place_resting` (the live resting-maker model) and the live placement call inside
`_execute`/`_live_execute` both used to swallow every placement exception with
``logger.exception(...); return False`` — a PERSISTENT non-auth failure (a filter error,
-1013, insufficient balance) produced total silence forever, because only
``execution.note_credential_error`` alerted, and only for credential errors.

Both paths now track CONSECUTIVE placement failures per pending-order id and fire exactly
ONE ``notify.event("risk", ...)`` on the ``settings.placement_alert_after``-th consecutive
failure (default 3), then stay quiet until a later SUCCESS for the same order clears the
streak — at which point a future run of failures can alert again.
"""

from __future__ import annotations

import pytest

from app import execution, orders
from app import notify as notify_module
from app.config import settings
from app.models import PENDING, PendingOrder


@pytest.fixture(autouse=True)
def _clean():
    orders.reset_placement_alert_state()
    yield
    orders.reset_placement_alert_state()


class _StubProvider:
    def pair(self, symbol):
        return f"{symbol}/USDT"


class _Venue:
    """Records placements and replays a canned result, or raises a canned error."""

    def __init__(self, place_result=None, place_error=None):
        self.placed: list[dict] = []
        self._result = place_result or {
            "raw_id": "X1", "status": "open", "price": 0.0, "quantity": 0.0, "fee": 0.0,
        }
        self._place_error = place_error

    def place(self, pair, side, quantity, price, order_type,
              maker_orders=None, client_order_id=None):
        self.placed.append({"pair": pair, "side": side})
        if self._place_error:
            raise self._place_error
        return dict(self._result)


def _live(monkeypatch, venue: _Venue, *, maker=True, live=True, auto_trade=True) -> _Venue:
    monkeypatch.setattr(execution, "live_enabled", lambda: live)
    monkeypatch.setattr("app.data.providers.live_provider", lambda: _StubProvider())
    monkeypatch.setattr(execution, "place_live_order", venue.place)
    settings.maker_orders = maker
    settings.auto_trade = auto_trade
    return venue


def _queued(db, **kw) -> PendingOrder:
    defaults = {
        "symbol": "SOL", "side": "BUY", "order_type": "LIMIT", "quantity": 1.0,
        "price": 10.0, "source": "kss", "source_ref": "pyramid:1:wave:2", "status": PENDING,
    }
    defaults.update(kw)
    order = PendingOrder(**defaults)
    db.add(order)
    db.commit()
    db.refresh(order)
    return order


def _capture_alerts(monkeypatch) -> list[str]:
    sent: list[str] = []
    monkeypatch.setattr(notify_module, "send", lambda text, **k: sent.append(text) or True)
    monkeypatch.setattr(settings, "telegram_notify_risk", True)
    return sent


# --- _place_resting: exception branch -------------------------------------------------


def test_three_consecutive_placement_failures_fire_exactly_one_alert(db, monkeypatch):
    _live(monkeypatch, _Venue(place_error=RuntimeError("exchange down")))
    sent = _capture_alerts(monkeypatch)
    _queued(db)
    monkeypatch.setattr(settings, "placement_alert_after", 3)

    for _ in range(3):
        orders.sync_resting_orders(db)

    assert len(sent) == 1
    assert "3 times" in sent[0]


def test_fewer_than_the_threshold_does_not_alert_yet(db, monkeypatch):
    _live(monkeypatch, _Venue(place_error=RuntimeError("exchange down")))
    sent = _capture_alerts(monkeypatch)
    _queued(db)
    monkeypatch.setattr(settings, "placement_alert_after", 3)

    orders.sync_resting_orders(db)
    orders.sync_resting_orders(db)

    assert sent == []


def test_a_fourth_and_later_failure_still_fires_only_the_one_alert(db, monkeypatch):
    _live(monkeypatch, _Venue(place_error=RuntimeError("exchange down")))
    sent = _capture_alerts(monkeypatch)
    _queued(db)
    monkeypatch.setattr(settings, "placement_alert_after", 3)

    for _ in range(6):
        orders.sync_resting_orders(db)

    assert len(sent) == 1


def test_success_then_failure_resets_the_streak_no_alert_at_two(db, monkeypatch):
    """A success in between must reset the streak counter, so 2 failures after it must NOT
    alert (threshold 3) — a naive lifetime counter would wrongly fire here."""
    venue = _Venue()
    _live(monkeypatch, venue)
    sent = _capture_alerts(monkeypatch)
    order = _queued(db)
    monkeypatch.setattr(settings, "placement_alert_after", 3)

    orders.sync_resting_orders(db)  # succeeds — order now linked, streak cleared
    db.refresh(order)
    assert order.exchange_order_id == "X1"

    # Unlink it (simulate a fresh rung that now fails) and fail twice.
    order.exchange_order_id = None
    order.exchange_status = None
    db.commit()
    venue._place_error = RuntimeError("exchange down")

    orders.sync_resting_orders(db)
    orders.sync_resting_orders(db)

    assert sent == []


# --- _place_resting: post-only-reject branch --------------------------------------------


def test_post_only_rejects_do_not_feed_the_streak(db, monkeypatch):
    """Fix round A / item 6(a) — SPEC CHANGE, not a weakening: a rung priced at/through the book
    is post-only-rejected EVERY cycle by design (comment right above the reject branch: "the
    next cycle retries once the book moves away") — that is a normal, expected market
    condition, not a persistent problem. Feeding it into the same streak as a real placement
    failure turned any rung sitting near the touch into Telegram risk spam at the default
    threshold (3). This replaces the old
    `test_post_only_rejects_also_count_toward_the_streak`, which enshrined the opposite."""
    _live(monkeypatch, _Venue(place_result={
        "raw_id": None, "status": "rejected", "price": 0.0, "quantity": 0.0, "fee": 0.0,
    }))
    sent = _capture_alerts(monkeypatch)
    _queued(db)
    monkeypatch.setattr(settings, "placement_alert_after", 3)

    for _ in range(10):  # far past the threshold — a post-only reject must never alert
        orders.sync_resting_orders(db)

    assert sent == []


# --- the streak tracker's own memory is bounded ---------------------------------------


def test_the_streak_tracker_evicts_the_oldest_half_past_the_cap(db, monkeypatch):
    """Fix round A / item 6(b): only a SUCCESS clears a tracked order id
    (`_note_placement_success`) — an id that simply stops being retried (approved, rejected,
    its session closed, ...) never leaves the dict on its own, so a long-running instance would
    grow it forever. Past `_PLACEMENT_STREAK_MAX` tracked ids, the oldest half must be evicted."""
    monkeypatch.setattr(settings, "placement_alert_after", 1_000_000)  # never actually alert

    class _Order:
        def __init__(self, oid):
            self.id = oid
            self.symbol = "AAA"
            self.side = "BUY"

    for i in range(orders._PLACEMENT_STREAK_MAX + 1):
        orders._note_placement_failure(_Order(i), "boom")

    assert len(orders._placement_fail_streak) <= orders._PLACEMENT_STREAK_MAX
    assert 0 not in orders._placement_fail_streak, "the oldest tracked id must be evicted"
    assert orders._PLACEMENT_STREAK_MAX in orders._placement_fail_streak, \
        "the most recent tracked id must survive the eviction"


# --- the alert never raises out, never gates -----------------------------------------


def test_a_broken_notifier_never_breaks_placement(db, monkeypatch):
    _live(monkeypatch, _Venue(place_error=RuntimeError("exchange down")))
    monkeypatch.setattr(notify_module, "send", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("notify down")))
    monkeypatch.setattr(settings, "telegram_notify_risk", True)
    _queued(db)
    monkeypatch.setattr(settings, "placement_alert_after", 1)

    result = orders.sync_resting_orders(db)  # must not raise

    assert result["placed"] == 0


# --- the live MARKET placement call inside _execute/_live_execute (wave-0 entry) --------


def _executed_order(db, **kw) -> PendingOrder:
    from app.models import APPROVED

    defaults = {
        "symbol": "SOL", "side": "BUY", "order_type": "MARKET", "quantity": 1.0,
        "price": 10.0, "source": "kss", "source_ref": "pyramid:1:wave:0", "status": APPROVED,
    }
    defaults.update(kw)
    order = PendingOrder(**defaults)
    db.add(order)
    db.commit()
    db.refresh(order)
    return order


def test_live_execute_placement_failures_also_alert_after_the_threshold(db, monkeypatch):
    monkeypatch.setattr(execution, "live_enabled", lambda: True)
    monkeypatch.setattr("app.data.providers.live_provider", lambda: _StubProvider())
    monkeypatch.setattr("app.orders.get_current_prices", lambda syms: dict.fromkeys(syms, 10.0))
    monkeypatch.setattr(execution, "fetch_order_by_client_id", lambda *a, **k: None)
    sent = _capture_alerts(monkeypatch)
    monkeypatch.setattr(settings, "placement_alert_after", 3)
    monkeypatch.setattr(settings, "live_trading", True)

    def _boom(*a, **k):
        raise RuntimeError("exchange down")

    monkeypatch.setattr(execution, "place_live_order", _boom)
    order = _executed_order(db)

    for _ in range(3):
        with pytest.raises(RuntimeError):
            orders._execute(db, order)

    assert len(sent) == 1
