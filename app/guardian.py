"""
AI Guardian — veto-only LLM layer for FINDMY-FM auto-approvals.

The Guardian may ONLY block orders it judges clearly unsafe. It can never
approve anything, and it never overrides the deterministic risk gates.
Any error/timeout is handled by the fail-open/closed policy in settings.
"""

from __future__ import annotations

import json
import logging

import httpx

from app.config import settings
from app.models import PendingOrder

log = logging.getLogger(__name__)

_ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
_ANTHROPIC_VERSION = "2023-06-01"
_TIMEOUT = 20.0  # seconds

# ---------------------------------------------------------------------------
# Static system prompt — marked ephemeral so the Anthropic cache keeps it
# across repeated calls within the same TTL window (prompt caching).
# ---------------------------------------------------------------------------
_STATIC_INSTRUCTION = (
    "You are a conservative risk guardian for a PAPER crypto DCA (dollar-cost averaging) "
    "bot called FINDMY-FM. Your ONLY power is to veto orders you judge clearly unsafe. "
    "You CANNOT approve anything — that is decided by deterministic rules elsewhere. "
    "Do NOT veto routine, in-policy orders. ONLY veto when at least one of these is true: "
    "(1) the risk_note signals a severe risk (e.g. circuit-breaker breach, extreme drawdown); "
    "(2) the notional is abnormally large compared to the other orders in the batch (>10× median); "
    "(3) the price is clearly nonsensical for the symbol (e.g. negative, or off by orders of magnitude). "
    "Reply with STRICT JSON only — no prose, no markdown fences — exactly: "
    '{"vetoes":[{"id":<int>,"reason":"<short string>"}]} '
    "where the array is empty if nothing is vetoed."
)

_SYSTEM_BLOCKS: list[dict] = [
    {
        "type": "text",
        "text": _STATIC_INSTRUCTION,
        "cache_control": {"type": "ephemeral"},
    }
]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def enabled() -> bool:
    """Return True only when the guardian is switched on AND a key is present."""
    if not settings.guardian_enabled:
        return False
    key = settings.anthropic_api_key.get_secret_value()
    return bool(key)


def review(
    orders: list[PendingOrder],
    *,
    market: dict[str, float] | None = None,
) -> dict[int, str]:
    """
    Review a batch of pending orders and return vetoed ids with reasons.

    Returns ``{order_id: veto_reason}``; empty dict means nothing vetoed.
    Never raises — failures are governed by ``settings.guardian_fail_open``.
    """
    if not enabled() or not orders:
        return {}

    valid_ids = {o.id for o in orders}

    try:
        payload = _build_payload(orders, market or {})
        raw = _call_anthropic(_SYSTEM_BLOCKS, payload)
        return _parse_response(raw, valid_ids)
    except Exception as exc:  # noqa: BLE001
        # Log only the exception type at WARNING — the full exc could, in some
        # httpx/logging setups, carry request detail. Full exc at DEBUG only.
        log.warning("Guardian review failed: %s", type(exc).__name__)
        log.debug("Guardian review error detail", exc_info=exc)
        if settings.guardian_fail_open:
            return {}
        reason = "guardian unavailable (fail-closed)"
        return {o.id: reason for o in orders}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _build_payload(orders: list[PendingOrder], market: dict[str, float]) -> str:
    """Serialise orders to a compact JSON string for the user turn."""
    rows: list[dict] = []
    for o in orders:
        price = o.price if o.price else market.get(o.symbol, 0.0)
        notional = round(o.quantity * price, 4) if price else 0.0
        rows.append(
            {
                "id": o.id,
                "symbol": o.symbol,
                "side": o.side,
                "order_type": o.order_type,
                "quantity": o.quantity,
                "price": o.price,
                "notional": notional,
                "source": o.source,
                "strategy_name": o.strategy_name,
                "risk_note": o.risk_note,
            }
        )
    return (
        "Review the following pending orders and veto any that are clearly unsafe. "
        f"Orders: {json.dumps(rows, separators=(',', ':'))}"
    )


def _parse_response(raw: str, valid_ids: set[int]) -> dict[int, str]:
    """Parse the model reply into ``{id: reason}``, robust to JSON fences."""
    # Strip optional ```json … ``` fences
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        # drop first and last fence lines
        inner = [ln for ln in lines if not ln.startswith("```")]
        text = "\n".join(inner).strip()

    data = json.loads(text)
    vetoes: dict[int, str] = {}
    for item in data.get("vetoes", []):
        oid = item.get("id")
        reason = str(item.get("reason", ""))[:200]
        if isinstance(oid, int) and oid in valid_ids and reason:
            vetoes[oid] = reason
    return vetoes


def _call_anthropic(system_blocks: list[dict], user_text: str) -> str:
    """
    POST to the Anthropic Messages API and return concatenated text content.

    Raises ``httpx.HTTPStatusError`` on non-2xx; all other httpx exceptions
    propagate to ``review`` which handles them.
    """
    key = settings.anthropic_api_key.get_secret_value()
    headers = {
        "x-api-key": key,
        "anthropic-version": _ANTHROPIC_VERSION,
        "content-type": "application/json",
        "anthropic-beta": "prompt-caching-2024-07-31",
    }
    body = {
        "model": settings.guardian_model,
        "max_tokens": settings.guardian_max_tokens,
        "system": system_blocks,
        "messages": [{"role": "user", "content": user_text}],
    }
    response = httpx.post(_ANTHROPIC_URL, headers=headers, json=body, timeout=_TIMEOUT)
    response.raise_for_status()
    blocks = response.json().get("content", [])
    return "".join(b["text"] for b in blocks if b.get("type") == "text")
