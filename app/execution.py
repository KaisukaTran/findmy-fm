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
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

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
    ex = getattr(ccxt, settings.live_exchange)(
        {"apiKey": key, "secret": secret, "enableRateLimit": True}
    )
    # 1.8: validate the live path on the exchange TESTNET before real funds. ccxt's
    # set_sandbox_mode swaps to the sandbox/testnet base URLs (no-op on exchanges without one).
    if settings.live_use_testnet and hasattr(ex, "set_sandbox_mode"):
        ex.set_sandbox_mode(True)
    return ex


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

    # 1.1 — report the TRUTH, never invent a fill. A resting maker order comes back
    # status='open'/filled=0; the old code fell back to `amount` and recorded a phantom
    # FULL fill (double-count blocker). Only treat as filled what the exchange actually
    # reports filled; the caller turns a real fill into a Fill, and async reconciliation
    # (live-readiness task 1.4) handles NEW→FILLED later.
    status = order.get("status")  # ccxt-normalised: 'open' | 'closed' | 'canceled'
    filled = float(order.get("filled") or 0.0)
    if str(status).lower() == "closed" and filled <= 0:
        # Fully-filled (e.g. a marketable order) but the venue omitted `filled` → trust amount.
        filled = float(order.get("amount") or quantity)
    avg = float(order.get("average") or 0.0)
    if avg <= 0 and filled > 0:  # fall back to a price ONLY when something actually filled
        avg = float(order.get("price") or price or 0.0)
    fee_obj = order.get("fee") or {}
    fee = float(fee_obj.get("cost") or 0.0) if isinstance(fee_obj, dict) else 0.0
    logger.info(
        "LIVE order placed: %s %s/%s %s @ %s status=%s (exch id %s)",
        side, filled, quantity, pair, avg, status, order.get("id"),
    )
    return {
        "price": avg, "quantity": filled, "fee": fee,
        "raw_id": order.get("id"), "status": status,
    }


# --- 1.2: exchange-filter compliance (pure; live placement rounds through this) ---------


def _quantize(value: float, step: float, rounding: str) -> float:
    """Round *value* to a multiple of *step* using Decimal (no binary-float drift)."""
    if step <= 0:
        return value
    d = (Decimal(str(value)) / Decimal(str(step))).quantize(Decimal("1"), rounding=rounding)
    return float(d * Decimal(str(step)))


def round_to_filters(price: float, qty: float, filters: dict, ref_price: float | None = None):
    """Make a (price, qty) order compliant with a symbol's Binance exchange filters.

    `filters` keys (all optional): ``tickSize`` (price step), ``stepSize`` (qty step),
    ``minQty``, ``minNotional`` ($ floor), ``percentUp``/``percentDown`` (PERCENT_PRICE_BY_SIDE
    multipliers of ``ref_price``, e.g. 2.0 / 0.5). Price is rounded to the tick; qty is rounded
    DOWN to the step (never buy more than intended). Raises ``ValueError`` when the order cannot
    satisfy a hard filter (below minQty / minNotional, or price outside the PERCENT band).

    Pure and side-effect-free — unit-tested against real SOLUSDT-style filters. Live-only;
    paper execution never calls this.
    """
    tick = float(filters.get("tickSize") or 0.0)
    step = float(filters.get("stepSize") or 0.0)
    min_qty = float(filters.get("minQty") or 0.0)
    min_notional = float(filters.get("minNotional") or 0.0)

    adj_price = _quantize(price, tick, ROUND_HALF_UP) if tick > 0 else price
    adj_qty = _quantize(qty, step, ROUND_DOWN) if step > 0 else qty

    if adj_qty <= 0 or (min_qty > 0 and adj_qty < min_qty):
        raise ValueError(f"qty {adj_qty} below minQty {min_qty}")
    if min_notional > 0 and adj_price * adj_qty < min_notional:
        raise ValueError(f"notional {adj_price * adj_qty:.4f} below minNotional {min_notional}")
    if ref_price and ref_price > 0:
        up = filters.get("percentUp")
        down = filters.get("percentDown")
        if up is not None and adj_price > float(up) * ref_price:
            raise ValueError(f"price {adj_price} above PERCENT_PRICE cap {float(up) * ref_price}")
        if down is not None and adj_price < float(down) * ref_price:
            raise ValueError(f"price {adj_price} below PERCENT_PRICE floor {float(down) * ref_price}")
    return adj_price, adj_qty


# --- 1.6: rate-limit guard (Binance REQUEST_WEIGHT / 429 / 418) -------------------------

# Binance spot REQUEST_WEIGHT budget is 6000/min per IP; back off before exhausting it.
WEIGHT_LIMIT_PER_MIN = 6000


def used_weight_from_headers(headers: dict | None) -> int | None:
    """Extract ``X-MBX-USED-WEIGHT-1M`` (case-insensitive) from response headers, or None."""
    if not headers:
        return None
    for k, v in headers.items():
        if str(k).lower() == "x-mbx-used-weight-1m":
            try:
                return int(v)
            except (TypeError, ValueError):
                return None
    return None


def weight_backoff_seconds(
    used_weight: int | None, limit: int = WEIGHT_LIMIT_PER_MIN, soft_pct: float = 80.0,
    base: float = 1.0,
) -> float:
    """Seconds to pause given current REQUEST_WEIGHT usage. 0 below ``soft_pct`` of the limit;
    grows toward ``base`` as usage approaches the limit; a hard ``base*5`` once at/over it."""
    if not used_weight or limit <= 0:
        return 0.0
    soft = soft_pct / 100.0 * limit
    if used_weight < soft:
        return 0.0
    if used_weight >= limit:
        return round(base * 5, 3)
    # linear ramp from 0 (at soft) to base (at limit)
    return round(base * (used_weight - soft) / (limit - soft), 3)


def classify_rate_error(exc: Exception, retry_after: float | None = None) -> tuple[str, float | None]:
    """Map an exchange error to an action: ``('retry', seconds)`` for HTTP 429 (rate limited —
    honour Retry-After), ``('halt', None)`` for HTTP 418 (IP banned — stop live + alert), or
    ``('raise', None)`` for anything else (the caller re-raises). Pure; no sleeping/IO here."""
    text = str(exc)
    if "418" in text:
        return "halt", None
    if "429" in text or isinstance(exc, getattr(ccxt, "DDoSProtection", ())):
        return "retry", (retry_after if retry_after is not None else 1.0)
    return "raise", None
