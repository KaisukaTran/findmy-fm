"""
Tests for app.guardian — AI Guardian veto layer.

Network is never touched: _call_anthropic is always monkeypatched.
"""

from __future__ import annotations

from pydantic import SecretStr

from app import guardian
from app.config import settings
from app.models import PENDING, PendingOrder

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_order(db, oid: int | None = None, symbol: str = "BTC", price: float = 100.0) -> PendingOrder:
    """Persist a minimal PendingOrder and return it (id assigned by DB)."""
    order = PendingOrder(
        symbol=symbol,
        side="BUY",
        order_type="LIMIT",
        quantity=0.001,
        price=price,
        source="kss",
        status=PENDING,
    )
    db.add(order)
    db.commit()
    db.refresh(order)
    # Allow the caller to override the auto-assigned id for canned-response tests
    if oid is not None and order.id != oid:
        # Patch in-memory only — used for response-parsing tests where
        # valid_ids must match the canned veto id.
        order.id = oid
    return order


def _enable_guardian(monkeypatch) -> None:
    """Flip settings so guardian.enabled() returns True."""
    monkeypatch.setattr(settings, "guardian_enabled", True)
    monkeypatch.setattr(settings, "anthropic_api_key", SecretStr("test-key"))


# ---------------------------------------------------------------------------
# enabled()
# ---------------------------------------------------------------------------


def test_enabled_false_by_default():
    assert guardian.enabled() is False


def test_enabled_true_when_configured(monkeypatch):
    _enable_guardian(monkeypatch)
    assert guardian.enabled() is True


def test_enabled_false_when_key_missing(monkeypatch):
    monkeypatch.setattr(settings, "guardian_enabled", True)
    monkeypatch.setattr(settings, "anthropic_api_key", SecretStr(""))
    assert guardian.enabled() is False


def test_enabled_false_when_flag_off_but_key_set(monkeypatch):
    monkeypatch.setattr(settings, "guardian_enabled", False)
    monkeypatch.setattr(settings, "anthropic_api_key", SecretStr("test-key"))
    assert guardian.enabled() is False


# ---------------------------------------------------------------------------
# review([]) short-circuit
# ---------------------------------------------------------------------------


def test_review_empty_list_returns_empty_without_calling_api(monkeypatch):
    _enable_guardian(monkeypatch)
    called = []
    monkeypatch.setattr("app.guardian._call_anthropic", lambda *a, **kw: called.append(1) or "{}")
    result = guardian.review([])
    assert result == {}
    assert called == []


def test_review_returns_empty_when_disabled(db, monkeypatch):
    # guardian disabled by default — should short-circuit before even calling API
    called = []
    monkeypatch.setattr("app.guardian._call_anthropic", lambda *a, **kw: called.append(1) or "{}")
    order = _make_order(db)
    result = guardian.review([order])
    assert result == {}
    assert called == []


# ---------------------------------------------------------------------------
# review() happy-path veto
# ---------------------------------------------------------------------------


def test_review_returns_veto_for_matching_id(db, monkeypatch):
    _enable_guardian(monkeypatch)
    order = _make_order(db)
    oid = order.id

    canned = f'{{"vetoes":[{{"id":{oid},"reason":"too risky"}}]}}'
    monkeypatch.setattr("app.guardian._call_anthropic", lambda *a, **kw: canned)

    result = guardian.review([order])
    assert result == {oid: "too risky"}


def test_review_ignores_ids_not_in_input(db, monkeypatch):
    """Response may reference ids outside the batch — those must be dropped."""
    _enable_guardian(monkeypatch)
    order1 = _make_order(db, symbol="BTC")
    order2 = _make_order(db, symbol="ETH")

    # Veto order1 and a phantom id (99999)
    canned = (
        f'{{"vetoes":['
        f'{{"id":{order1.id},"reason":"bad"}},'
        f'{{"id":99999,"reason":"phantom"}}'
        f']}}'
    )
    monkeypatch.setattr("app.guardian._call_anthropic", lambda *a, **kw: canned)

    result = guardian.review([order1, order2])
    assert order1.id in result
    assert 99999 not in result
    assert order2.id not in result


def test_review_no_veto_when_all_safe(db, monkeypatch):
    _enable_guardian(monkeypatch)
    order = _make_order(db)
    monkeypatch.setattr("app.guardian._call_anthropic", lambda *a, **kw: '{"vetoes":[]}')
    result = guardian.review([order])
    assert result == {}


# ---------------------------------------------------------------------------
# Robust JSON parsing — ```json fence stripping
# ---------------------------------------------------------------------------


def test_review_parses_response_with_json_fences(db, monkeypatch):
    _enable_guardian(monkeypatch)
    order = _make_order(db)
    oid = order.id

    fenced = f'```json\n{{"vetoes":[{{"id":{oid},"reason":"fence test"}}]}}\n```'
    monkeypatch.setattr("app.guardian._call_anthropic", lambda *a, **kw: fenced)

    result = guardian.review([order])
    assert result == {oid: "fence test"}


def test_review_parses_plain_code_fence(db, monkeypatch):
    """Triple-backtick without 'json' tag should also be stripped."""
    _enable_guardian(monkeypatch)
    order = _make_order(db)
    oid = order.id

    fenced = f'```\n{{"vetoes":[{{"id":{oid},"reason":"plain fence"}}]}}\n```'
    monkeypatch.setattr("app.guardian._call_anthropic", lambda *a, **kw: fenced)

    result = guardian.review([order])
    assert result == {oid: "plain fence"}


# ---------------------------------------------------------------------------
# Fail-open / fail-closed behaviour
# ---------------------------------------------------------------------------


def _raising_call(*args, **kwargs):
    raise RuntimeError("network timeout")


def test_fail_open_returns_empty_on_error(db, monkeypatch):
    _enable_guardian(monkeypatch)
    monkeypatch.setattr(settings, "guardian_fail_open", True)
    monkeypatch.setattr("app.guardian._call_anthropic", _raising_call)

    order = _make_order(db)
    result = guardian.review([order])
    assert result == {}


def test_fail_closed_vetoes_all_on_error(db, monkeypatch):
    _enable_guardian(monkeypatch)
    monkeypatch.setattr(settings, "guardian_fail_open", False)
    monkeypatch.setattr("app.guardian._call_anthropic", _raising_call)

    order1 = _make_order(db, symbol="BTC")
    order2 = _make_order(db, symbol="ETH")
    result = guardian.review([order1, order2])

    assert order1.id in result
    assert order2.id in result
    for reason in result.values():
        assert "guardian unavailable" in reason.lower() or "fail-closed" in reason.lower()


def test_fail_closed_reason_string(db, monkeypatch):
    _enable_guardian(monkeypatch)
    monkeypatch.setattr(settings, "guardian_fail_open", False)
    monkeypatch.setattr("app.guardian._call_anthropic", _raising_call)

    order = _make_order(db)
    result = guardian.review([order])
    assert "fail-closed" in result[order.id]
