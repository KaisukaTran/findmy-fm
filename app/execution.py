"""
Live order execution (Phase 6) — SHIPPED OFF.

Paper execution stays the default everywhere (`app.orders._paper_execute`). Real-money
placement runs ONLY when ``settings.live_trading`` is True **and** the live-exchange API
key/secret are configured. Even then the caller (`app.orders._live_execute`) re-gates each
order: the circuit breaker and the per-order notional cap restrict new-exposure BUYs, while
SELL exits are never gated (exits reduce risk — see the drawdown-exit-deadlock invariant).

This module only owns the exchange I/O and the on/off predicate. It never logs the secret
and never falls back to paper on error — a live failure must surface, not be hidden.
"""

from __future__ import annotations

import logging

import ccxt

from app.config import settings

logger = logging.getLogger(__name__)


def _secret(value) -> str:
    return value.get_secret_value() if hasattr(value, "get_secret_value") else str(value or "")


def live_key_present() -> bool:
    """True when both the live API key and secret are configured (non-empty)."""
    return bool(_secret(settings.live_api_key).strip() and _secret(settings.live_api_secret).strip())


def live_enabled() -> bool:
    """Real-money placement is active ONLY with the master flag set AND keys present."""
    return bool(settings.live_trading) and live_key_present()


def validate_at_boot() -> str | None:
    """Boot-time sanity check. Returns a human message describing the live state, or a
    warning string if live_trading is on but unusable. Never raises; never logs secrets."""
    if not settings.live_trading:
        return None
    if not live_key_present():
        return "LIVE_TRADING=true but no exchange API key/secret — staying on paper."
    return f"LIVE_TRADING active on '{settings.live_exchange}' (cap ${settings.live_max_order_notional:.2f}/BUY)."


def _client():
    key, secret = _secret(settings.live_api_key), _secret(settings.live_api_secret)
    if not (key and secret):
        raise RuntimeError("live trading enabled but exchange API key/secret missing")
    return getattr(ccxt, settings.live_exchange)(
        {"apiKey": key, "secret": secret, "enableRateLimit": True}
    )


def place_live_order(
    pair: str, side: str, quantity: float, price: float, order_type: str
) -> dict:
    """Place a REAL order on ``settings.live_exchange`` and return a normalised fill dict
    ``{price, quantity, fee, raw_id}``.

    Raises on any exchange error — the caller must NOT fall back to a paper fill (that would
    mask a real placement failure). The API secret is never logged.
    """
    ex = _client()
    side_l = side.lower()
    if order_type.upper() == "MARKET" or price <= 0:
        order = ex.create_order(pair, "market", side_l, quantity)
    else:
        order = ex.create_order(pair, "limit", side_l, quantity, price)

    filled = float(order.get("filled") or order.get("amount") or quantity)
    avg = float(order.get("average") or order.get("price") or price or 0.0)
    fee_obj = order.get("fee") or {}
    fee = float(fee_obj.get("cost") or 0.0) if isinstance(fee_obj, dict) else 0.0
    logger.info(
        "LIVE order placed: %s %s %s @ %s (exch id %s)", side, filled, pair, avg, order.get("id")
    )
    return {"price": avg, "quantity": filled, "fee": fee, "raw_id": order.get("id")}
