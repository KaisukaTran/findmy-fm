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

import collections
import logging
import re
import time
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


class RateLimited(RuntimeError):
    """The venue rate-limited or banned us; sending another request now makes it worse."""


# One client per (exchange, sandbox) instead of one per call. ccxt's rate limiter is
# per-instance — rebuilding it every call reset the throttle and paced nothing — and every
# unified method begins with load_markets(), so a cold instance spent 3 requests (weight 20)
# on EVERY order, cancel and fetch.
_CLIENTS: dict[tuple[str, bool], object] = {}
_rate_limited_until: float = 0.0


def reset_client_cache() -> None:
    """Drop the cached clients and every hold — rate-limit, order-count budget, weight budget,
    the cached venue limits, and the credential-failure/alert state (tests, and a credentials
    change)."""
    global _rate_limited_until, _weight_hold_until, _weight_limit_per_min
    global _venue_limits_cache, _venue_limits_fetched_at
    global _credentials_ok, _credential_alert_last
    _CLIENTS.clear()
    _last_clock_refresh.clear()
    _rate_limited_until = 0.0
    _weight_hold_until = 0.0
    _weight_limit_per_min = WEIGHT_LIMIT_PER_MIN
    _venue_limits_cache = None
    _venue_limits_fetched_at = 0.0
    _credentials_ok = True
    _credential_alert_last = 0.0
    reset_order_budget()


def rate_limited_until() -> float:
    """Monotonic deadline before which no request may be sent (0 = clear)."""
    return _rate_limited_until


def assert_not_rate_limited(urgent: bool = False) -> None:
    """Raise if the venue told us to back off and the pause has not expired.

    ``urgent`` exempts anything that REDUCES risk — a SELL exit, a cancel. Exits are never
    gated in this system, and a throttle must not be the exception: a stop-loss costs one
    weight unit, and holding it back to protect a rate budget trades a real position for an
    imaginary saving. The hold exists to stop NEW exposure, which is what actually escalates
    a ban.
    """
    if urgent:
        return
    remaining = _rate_limited_until - time.monotonic()
    if remaining > 0:
        raise RateLimited(f"exchange rate limit / ban active — {remaining:.0f}s remaining")


def note_rate_error(exc: Exception, retry_after: float | None = None) -> str:
    """Record a 429/418 so the NEXT call refuses instead of hammering.

    The classifier already existed; nothing called it, so a 429 arrived as a generic
    exception, was swallowed by a broad handler and retried on the next cycle — exactly how a
    two-minute Binance ban escalates toward a three-day one. Returns the action taken.
    """
    global _rate_limited_until
    action, wait = classify_rate_error(exc, retry_after)
    if action == "halt":
        pause = float(retry_after or 300.0)  # a 418 is an IP ban: stop, loudly
        logger.error("LIVE exchange BAN (418) — pausing exchange calls for %.0fs", pause)
    elif action == "retry":
        pause = float(wait or 1.0)
        logger.warning("LIVE rate limited (429) — pausing exchange calls for %.0fs", pause)
    elif action == "orders_exceeded":
        # -1015: the ACCOUNT is over its ORDERS budget. Same hold mechanism as a 429/418 —
        # which already exempts a SELL via assert_not_rate_limited(urgent=True) — sized to the
        # window the venue actually named instead of a flat 1s retry.
        pause = float(wait or ORDERS_EXCEEDED_DEFAULT_WAIT)
        logger.warning(
            "LIVE order-count budget exceeded (-1015) — pausing NEW exposure for %.0fs "
            "(exits are never held back)", pause,
        )
    else:
        return action
    _rate_limited_until = max(_rate_limited_until, time.monotonic() + pause)
    return action


# --- credential-failure detection + alerting -------------------------------------------------
#
# A dead API key (Binance deletes an un-whitelisted key after 90 days; a whitelist that no
# longer matches this machine's public IP breaks it the moment the IP changes — see
# `app.scheduler.check_ip_change`) makes EVERY signed call fail the same way: placements,
# cancels, and the reconciliation that books fills. Before this, the app had zero handling of
# that failure mode — no code anywhere recognised -2015/-2014/-1022/-2008 or
# AuthenticationError/PermissionDenied — so the failure was TOTALLY SILENT: /health still said
# "ok", no fills happened, and the owner was never told, because notify.py was only ever called
# on a fill.

_CREDENTIAL_ERROR_CODES = {-2015, -2014, -1022, -2008}


def classify_credential_error(exc: Exception) -> bool:
    """True when *exc* is a Binance authentication/permission failure — a dead, revoked, or
    IP-restricted API key.

    Matches the ccxt exception TYPE (`AuthenticationError`, whose subclass `PermissionDenied`
    is caught by the same isinstance check) and the numeric body CODE Binance actually returns,
    never a substring of the message — a signed URL embeds an orderId, a 13-digit timestamp and
    a 64-hex signature, and `classify_rate_error` above already had one bug from grepping that
    text for "418" (misread ~3% of ordinary network errors as an IP ban). The code is parsed out
    of the JSON shape ccxt embeds verbatim (`"code":-2015`), not grepped for as free text, so a
    coincidental digit run in the signed URL can't trigger it.

    -2015 "Invalid API-key, IP, or permissions for action" (dead key / wrong IP / missing
    permission — the most likely live cause), -2014 "API-key format invalid", -1022 bad
    signature (wrong secret, or clock skew corrupting it), -2008 "Invalid Api-Key ID". The
    installed ccxt (binance.py) already maps all four to AuthenticationError/PermissionDenied;
    the code check is a defense-in-depth fallback in case a bare ExchangeError ever carries the
    code without ccxt's mapping catching it.
    """
    if isinstance(exc, ccxt.AuthenticationError):  # PermissionDenied subclasses this
        return True
    match = re.search(r'"code"\s*:\s*(-?\d+)', str(exc))
    if not match:
        return False
    try:
        code = int(match.group(1))
    except ValueError:
        return False
    return code in _CREDENTIAL_ERROR_CODES


_credentials_ok: bool = True
_credential_alert_last: float = 0.0


def credentials_ok() -> bool:
    """True unless the most recent signed exchange call failed with a credential error.

    Reflects CURRENT state, not history: any later successful signed call clears it (see
    `_mark_credentials_ok`, called by `place_live_order`/`cancel_live_order`/`fetch_live_order`
    right after a call that actually succeeded) — so `/health` self-heals the moment a rotated
    key or a restored IP whitelist starts working again, instead of latching a stale failure.
    """
    return _credentials_ok


def _mark_credentials_ok() -> None:
    global _credentials_ok
    _credentials_ok = True


def note_credential_error(exc: Exception) -> bool:
    """Alert (throttled) when *exc* is a credential failure. Returns True iff it was one.

    This ONLY alerts — unlike `note_rate_error` it never gates anything. An exit must never be
    slowed by a credential problem: a SELL that fails on a dead key still needs to reach the
    venue exactly as fast as it otherwise would (or be retried next cycle); this function's only
    job is to make sure a human finds out, since nothing else in the app did before. At most one
    Telegram alert per `settings.credential_alert_cooldown_min` — a dead key fails on EVERY
    subsequent call, and without a throttle each one would fire its own message — but it fires
    again once the cooldown elapses as long as the condition still persists, so it never goes
    quiet forever the way a fire-once notice would.
    """
    global _credentials_ok, _credential_alert_last
    if not classify_credential_error(exc):
        return False
    _credentials_ok = False
    now = time.monotonic()
    cooldown = max(settings.credential_alert_cooldown_min, 0.0) * 60.0
    if cooldown > 0 and now - _credential_alert_last < cooldown:
        return True
    _credential_alert_last = now
    logger.error("LIVE credential failure — %s: %s", type(exc).__name__, exc)
    try:
        from app import notify  # local import: avoid a module-load-time cycle with app.notify

        notify.event(
            "risk",
            f"🔑 LIVE credential failure ({type(exc).__name__}): {exc}\n"
            "Every signed call (placements, cancels, fill reconciliation) is failing on this "
            "key — exits are NOT gated by this but will not confirm either. Check the API "
            "key/secret and its IP whitelist now.",
        )
    except Exception:  # a broken notifier must never mask the underlying exchange error
        logger.debug("note_credential_error: alert send failed", exc_info=True)
    return True


# How often a long-lived client re-measures the venue's clock and reloads market metadata.
# ccxt applies `adjustForTimeDifference` only during an actual market (re)load, and
# `load_markets()` returns early once markets are cached — so a client that lives for days
# measured the offset ONCE. That silently re-opens the -1021 hole the option was added to
# close, on exactly the drifting host it was meant to protect. Filters go stale the same way.
_CLOCK_REFRESH_SEC = 900.0
_last_clock_refresh: dict[int, float] = {}


def _refresh_clock(ex) -> None:
    """Re-measure the venue clock (and refresh market metadata) periodically. Best effort."""
    now = time.monotonic()
    last = _last_clock_refresh.get(id(ex), 0.0)
    if now - last < _CLOCK_REFRESH_SEC:
        return
    _last_clock_refresh[id(ex)] = now
    try:
        ex.load_markets(True)  # reload=True: re-runs the time-difference measurement too
    except Exception:  # a stale offset is better than a broken call path
        logger.debug("clock/markets refresh failed", exc_info=True)


def _client():
    key, secret = _secret(settings.live_api_key), _secret(settings.live_api_secret)
    if not (key and secret):
        raise RuntimeError("live trading enabled but exchange API key/secret missing")
    sandbox = bool(settings.live_use_testnet)
    cached = _CLIENTS.get((settings.live_exchange, sandbox))
    if cached is not None:
        _refresh_clock(cached)
        return cached
    ex = getattr(ccxt, settings.live_exchange)({
        "apiKey": key, "secret": secret, "enableRateLimit": True,
        "options": {
            # Binance rejects a timestamp more than 1s ahead of ITS clock with -1021, whatever
            # recvWindow says, and a Windows host syncs weekly. Without this every signed call
            # fails once the clock drifts — placements, cancels, and the reconciliation that
            # books fills — so exits stop with positions still open.
            "adjustForTimeDifference": True,
            "recvWindow": 10_000,
        },
    })
    # 1.8: validate the live path on the exchange TESTNET before real funds. ccxt's
    # set_sandbox_mode swaps to the sandbox/testnet base URLs (no-op on exchanges without one).
    if sandbox and hasattr(ex, "set_sandbox_mode"):
        ex.set_sandbox_mode(True)
    _CLIENTS[(settings.live_exchange, sandbox)] = ex
    return ex


def place_live_order(
    pair: str, side: str, quantity: float, price: float, order_type: str,
    maker_orders: bool | None = None, client_order_id: str | None = None,
) -> dict:
    """Place a REAL order on ``settings.live_exchange`` and return a normalised fill dict
    ``{price, quantity, fee, raw_id, status}``.

    Order kind (1.3): MARKET → taker market order (risk exits — SL/trailing/close/OPUS —
    always take, never slowed). LIMIT → a post-only ``LIMIT_MAKER`` when ``maker_orders``
    is on (saves the spread; rounded to the symbol's exchange filters first), else a plain
    limit. ``maker_orders`` defaults to ``settings.maker_orders``. A post-only order that
    would cross is rejected by Binance (-2010) — that is surfaced as a structured
    ``status='rejected'`` (the 1.5 resting model cancels+replaces lower), not an exception.

    Idempotency (1.10): when ``client_order_id`` is given it is sent as the order's
    ``clientOrderId`` (deterministic from the PendingOrder id). If a retry after a lost
    response hits a 'Duplicate order' rejection, the prior placement already succeeded — we
    recover that order by clientOrderId instead of double-placing.

    Raises on any other exchange error — the caller must NOT fall back to a paper fill (that
    would mask a real placement failure). The API secret is never logged.
    """
    ex = _client()
    side_l = side.lower()
    if maker_orders is None:
        maker_orders = settings.maker_orders
    ccxt_type, base_params = order_placement(order_type, maker_orders, side=side)
    params = dict(base_params)
    if client_order_id:
        params["clientOrderId"] = client_order_id
    qty, px = quantity, price

    # Comply with the symbol's exchange filters before sending — round price→tickSize,
    # qty→stepSize, enforce minQty/minNotional/PERCENT. This covers MARKET too: an entry sized
    # from a USD amount lands on a ragged quantity (15/48.78 = 0.3074915...) and the venue
    # answers -1013 rather than rounding for us. It used to apply to post-only limits only,
    # because MARKET meant "sell the whole position", where the quantity was already valid.
    try:
        filters = _market_filters(ex, pair)
    except Exception:  # market metadata unavailable — place unrounded rather than block
        filters = {}
    if filters:
        # With no price (a MARKET order carries none) there is nothing to value the order
        # against, so only the quantity can be made compliant — checking a notional of zero
        # would reject every market order.
        checks = dict(filters)
        if px <= 0:
            checks.pop("minNotional", None)
            checks.pop("percentUp", None)
            checks.pop("percentDown", None)
        try:
            px, qty = round_to_filters(px, qty, checks, ref_price=px if px > 0 else None)
        except ValueError:
            # An exit is NEVER gated: a dust position that no longer clears minNotional must
            # still be sellable, and the venue is the right place to refuse it. A BUY that
            # cannot comply is new exposure, so it fails here instead of at the exchange.
            if side.upper() == "BUY":
                raise
            logger.warning("live %s %s: filters reject qty %s — sending unrounded (exits are "
                           "never blocked)", side, pair, qty)

    urgent = side_l == "sell"
    assert_not_rate_limited(urgent=urgent)
    assert_order_budget_available(urgent=urgent)
    assert_weight_budget_available(urgent=urgent)
    try:
        if ccxt_type == "market" or px <= 0:
            order = ex.create_order(pair, "market", side_l, qty, None, params) if params \
                else ex.create_order(pair, "market", side_l, qty)
        elif params:
            order = ex.create_order(pair, "limit", side_l, qty, px, params)
        else:
            order = ex.create_order(pair, "limit", side_l, qty, px)
        # A request that actually reached the venue spends its ORDERS/REQUEST_WEIGHT budget
        # regardless of what happens to it next — record it here, inside the try, so a
        # duplicate-recovery (which skips this whole block, see except below) never re-counts
        # a placement that was already recorded on the lost original attempt.
        record_order_placed()
        _note_weight_usage(ex)
        _mark_credentials_ok()  # this signed call worked — the key is not the problem right now
    except Exception as exc:
        # 'Duplicate order' (also code -2010): this exact clientOrderId was already accepted
        # on a prior, lost attempt — recover it rather than place a second order.
        if client_order_id and is_duplicate_client_order(exc):
            logger.info("LIVE duplicate clientOrderId %s — recovering the existing order", client_order_id)
            order = _fetch_by_client_id(ex, pair, client_order_id)
        elif is_post_only_reject(exc):
            logger.info("LIVE post-only rejected (would cross): %s %s %s @ %s", side, qty, pair, px)
            return {"price": 0.0, "quantity": 0.0, "fee": 0.0, "raw_id": None, "status": "rejected"}
        else:
            # A 429/418 must pause the next call rather than be retried on the next cycle. A
            # credential failure (dead/revoked/IP-restricted key) is never a hold — it's an
            # ALERT only, never a gate (see note_credential_error).
            note_rate_error(exc)
            note_credential_error(exc)
            raise

    # 1.1 — report the TRUTH, never invent a fill. A resting maker order comes back
    # status='open'/filled=0; the old code fell back to `amount` and recorded a phantom
    # FULL fill (double-count blocker). Only treat as filled what the exchange actually
    # reports filled; the caller turns a real fill into a Fill, and async reconciliation
    # (live-readiness task 1.4) handles NEW→FILLED later.
    status = order.get("status")  # ccxt-normalised: 'open' | 'closed' | 'canceled'
    filled = float(order.get("filled") or 0.0)
    if str(status).lower() == "closed" and filled <= 0:
        # Fully-filled (e.g. a marketable order) but the venue omitted `filled` → trust amount.
        filled = float(order.get("amount") or qty)
    if str(status).lower() == "closed" and filled > 0:
        # A synchronous fill (every taker/MARKET order — every risk exit, and the wave-0
        # entry) frees its own ORDERS-budget slot immediately. A resting maker order stays
        # outstanding until app.orders._book_delta credits it back on the async fill it
        # discovers later (reconciliation) — see the order-count budget note near the bottom.
        record_order_filled()
    avg = float(order.get("average") or 0.0)
    if avg <= 0 and filled > 0:  # fall back to a price ONLY when something actually filled
        avg = float(order.get("price") or px or 0.0)
    quote = pair.partition("/")[2]
    fee = fee_cost(order, quote)
    logger.info(
        "LIVE order placed: %s %s/%s %s @ %s status=%s (exch id %s)",
        side, filled, qty, pair, avg, status, order.get("id"),
    )
    return {
        "price": avg, "quantity": filled, "fee": fee,
        "raw_id": order.get("id"), "status": status,
    }


def cancel_live_order(pair: str, order_id: str) -> None:
    """Cancel a resting exchange order (live-readiness 1.5 cancel+replace).

    Raises on any exchange error so the caller keeps the local link to the order and
    retries next cycle — silently dropping ``exchange_order_id`` after a failed cancel
    would orphan a live order nothing tracks any more. Never logs the secret.
    """
    ex = _client()
    try:
        ex.cancel_order(order_id, pair)
    except Exception as exc:
        note_rate_error(exc)  # a 429/418 here must pause the next call, not be retried blindly
        note_credential_error(exc)  # alert only — never gates the cancel/its retry
        raise
    _mark_credentials_ok()
    logger.info("LIVE cancelled resting order %s on %s", order_id, pair)


def _fee_entries(order: dict) -> list[dict]:
    """Every fee record ccxt exposes for an order: the top-level one, the `fees` list, and the
    per-trade fees of a fill that crossed several price levels."""
    out: list[dict] = []
    top = order.get("fee")
    if isinstance(top, dict):
        out.append(top)
    for f in order.get("fees") or []:
        if isinstance(f, dict):
            out.append(f)
    for t in order.get("trades") or []:
        f = (t or {}).get("fee")
        if isinstance(f, dict):
            out.append(f)
    return out


def fee_cost(order: dict, quote: str) -> float:
    """Commission actually charged in the QUOTE currency (0.0 when there is none).

    Binance spot returns no top-level `fee` on the order endpoints — the real commissions come
    back in `fees` / `trades`. ccxt still builds `order['fee'] = {'cost': None}`, and because
    that dict is not None it does not promote the per-trade commissions into it. Reading only
    `order['fee']['cost']` therefore recorded 0.0 on EVERY live fill, which quietly understates
    cost basis, overstates realised P&L, and erodes the K-2 floor.

    A commission taken in the BASE asset (the spot-BUY default) is deliberately NOT counted
    here — it is not a quote amount, and adding 0.0003 BTC to a USD cost is meaningless. See
    `fee_base_qty` for that side of it.
    """
    total = 0.0
    for f in _fee_entries(order):
        cost = f.get("cost")
        if cost in (None, ""):
            continue
        currency = (f.get("currency") or quote or "").upper()
        if currency and currency != (quote or "").upper():
            continue
        try:
            total += float(cost)
        except (TypeError, ValueError):
            continue
    return total


def fee_base_qty(order: dict, base: str) -> float:
    """Commission charged in the BASE asset (0.0 when there is none).

    On a spot BUY without BNB fee payment, Binance deducts the commission from the asset you
    bought: the wallet receives LESS than `filled` reports. Selling the full booked quantity
    later is then rejected with -2010 'insufficient balance', which is why this has to be
    visible rather than folded into a quote figure.
    """
    if not base:
        return 0.0
    total = 0.0
    for f in _fee_entries(order):
        cost = f.get("cost")
        if cost in (None, "") or (f.get("currency") or "").upper() != base.upper():
            continue
        try:
            total += float(cost)
        except (TypeError, ValueError):
            continue
    return total


def order_is_gone(exc: Exception) -> bool:
    """True when the venue refused a cancel because it no longer holds that order.

    Binance answers -2011 / ``OrderNotFound`` for an order that already filled or was already
    cancelled. That is not a failed cancel — it is precisely the case where a fill may still
    be waiting to be booked, so the caller must read the final status instead of retrying.
    """
    return isinstance(exc, ccxt.OrderNotFound) or "-2011" in str(exc)


def fetch_live_order(pair: str, order_id: str) -> dict:
    """Fetch the live status of a resting order and return a normalised dict
    ``{status, filled, average, fee, raw_id}``.

    Used by ``app.orders.reconcile_live_orders`` to book a maker order's fill
    asynchronously (live-readiness task 1.4). ``filled`` is the venue's *cumulative*
    filled quantity (ccxt-normalised); ``status`` is ccxt's 'open'|'closed'|'canceled'.
    Raises on any exchange error (the caller logs + skips); never logs the secret.
    """
    ex = _client()
    try:
        order = ex.fetch_order(order_id, pair)
    except Exception as exc:
        note_rate_error(exc)
        note_credential_error(exc)  # alert only — the reconciliation pass must still be retried
        raise
    _mark_credentials_ok()
    _note_weight_usage(ex)
    status = order.get("status")
    filled = float(order.get("filled") or 0.0)
    avg = float(order.get("average") or 0.0)
    if avg <= 0 and filled > 0:  # some venues report price, not average, on a fill
        avg = float(order.get("price") or 0.0)
    fee = fee_cost(order, pair.partition("/")[2])
    return {"status": status, "filled": filled, "average": avg, "fee": fee, "raw_id": order.get("id")}


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


# --- 1.3: maker placement (post-only entries/TP; risk exits stay taker) -----------------

# Binance forbids self-trading as market manipulation, and this strategy meets itself
# routinely: an ACTIVE session rests DCA rung BUYs below the market AND a take-profit SELL
# above it, so a taker order of ours can cross a resting order of ours. Production Binance
# rejects `NONE` outright (testnet allows it — that is how the 1.8 harness crosses its own
# rung, see scripts/testnet_lib.py, testnet-gated), so the match itself cannot be opted out
# of. What IS ours to choose is which side dies, and the venue reads that from the TAKER's
# mode; inheriting the account default would leave it to a setting we do not control.
#
# So the mode is per-side, and both halves protect the same thing:
#   SELL taker (a stop sweeping down through our rungs) → EXPIRE_MAKER: the rung gives way.
#   BUY  taker (wave-0 entry, pyramid_up add) → EXPIRE_TAKER: the BUY gives way, because the
#     maker it would otherwise kill is our own resting take-profit — and we would not even
#     notice, since that row keeps `exchange_status='open'` until the slow reconcile pass.
# A cancelled entry is retried next cycle. A cancelled exit is the one unforgivable bug.
SELF_TRADE_PREVENTION = {"SELL": "EXPIRE_MAKER", "BUY": "EXPIRE_TAKER"}


def order_placement(order_type: str, maker_orders: bool, side: str = "SELL") -> tuple[str, dict]:
    """Map an order to its ccxt ``(type, params)``.

    MARKET → ``("market", …)`` — risk exits (SL/trailing/close/OPUS) always take, never
    slowed (see the drawdown-exit invariant). LIMIT → a post-only ``("limit", {postOnly})``
    when ``maker_orders`` is on (Binance routes post-only limits as ``LIMIT_MAKER``), else a
    plain limit. Carries the side's ``SELF_TRADE_PREVENTION`` mode, but only on Binance — the
    param is Binance-only and an unknown one would be rejected on every order, exits included.
    ``side`` defaults to the exit-safe mode. Pure.
    """
    params: dict = {}
    if settings.live_exchange == "binance":
        params["selfTradePreventionMode"] = SELF_TRADE_PREVENTION.get(
            side.upper(), SELF_TRADE_PREVENTION["SELL"])
    if order_type.upper() == "MARKET":
        return "market", params
    if maker_orders:
        return "limit", {**params, "postOnly": True}
    return "limit", params


def is_post_only_reject(exc: Exception) -> bool:
    """True when an exchange error is a post-only/LIMIT_MAKER rejection — Binance ``-2010``
    'Order would immediately match and take'. ccxt may also raise ``OrderImmediatelyFillable``.
    Such a maker order would have crossed the book; the caller treats it as a non-fill.

    Matches the post-only *message* (not the bare ``-2010`` code, which Binance also returns
    for duplicate-clientOrderId and insufficient-balance — those must NOT be read as a cross)."""
    text = str(exc).lower()
    if "immediately match" in text or "post only" in text or "post-only" in text:
        return True
    return isinstance(exc, getattr(ccxt, "OrderImmediatelyFillable", ()))


def client_order_id(order_id: int) -> str:
    """Deterministic Binance ``clientOrderId`` from a PendingOrder id (1.10). A retry after a
    lost response re-sends the SAME id, so the venue rejects the duplicate / we recover it
    instead of double-placing. Binance allows ``[A-Za-z0-9-_.:/]``, max 36 chars."""
    return f"fm-{order_id}"


def is_duplicate_client_order(exc: Exception) -> bool:
    """True when an exchange error means this exact ``clientOrderId`` was already accepted
    (Binance 'Duplicate order sent.') — the prior placement succeeded; recover it, don't replace."""
    text = str(exc).lower()
    return "duplicate order" in text or "duplicate clientorderid" in text


def _fetch_by_client_id(ex, pair: str, cid: str) -> dict:
    """Live wrapper: fetch an order by its ``clientOrderId`` (idempotent recovery, 1.10)."""
    return ex.fetch_order(cid, pair, {"clientOrderId": cid})


def filters_from_market(market: dict) -> dict:
    """Build a ``round_to_filters`` input dict from a ccxt market structure.

    Prefers the raw Binance ``info.filters`` (exact ``tickSize``/``stepSize``/``minQty``/
    ``minNotional``/PERCENT_PRICE_BY_SIDE), falling back to ccxt's normalised ``limits``.
    Pure — unit-testable with a fake market dict.
    """
    out: dict = {}
    info = market.get("info") or {}
    for f in info.get("filters", []) or []:
        ftype = f.get("filterType")
        if ftype == "PRICE_FILTER":
            out["tickSize"] = float(f.get("tickSize") or 0.0)
        elif ftype == "LOT_SIZE":
            out["stepSize"] = float(f.get("stepSize") or 0.0)
            out["minQty"] = float(f.get("minQty") or 0.0)
        elif ftype in ("NOTIONAL", "MIN_NOTIONAL"):
            out["minNotional"] = float(f.get("minNotional") or f.get("notional") or 0.0)
        elif ftype == "PERCENT_PRICE_BY_SIDE":
            up = f.get("bidMultiplierUp") or f.get("multiplierUp")
            down = f.get("bidMultiplierDown") or f.get("multiplierDown")
            if up is not None:
                out["percentUp"] = float(up)
            if down is not None:
                out["percentDown"] = float(down)
        elif ftype == "PERCENT_PRICE":
            if f.get("multiplierUp") is not None:
                out["percentUp"] = float(f["multiplierUp"])
            if f.get("multiplierDown") is not None:
                out["percentDown"] = float(f["multiplierDown"])
    limits = market.get("limits") or {}
    if "minQty" not in out:
        out["minQty"] = float((limits.get("amount") or {}).get("min") or 0.0)
    if "minNotional" not in out:
        out["minNotional"] = float((limits.get("cost") or {}).get("min") or 0.0)
    return out


def _market_filters(ex, pair: str) -> dict:
    """Live wrapper: load a symbol's exchange filters for ``round_to_filters``.

    ccxt's ``.market()`` does NOT auto-load — it raises "markets not loaded" until something
    else has warmed the cache. The caller treats a failure here as "no filters" and places the
    order unrounded, which is how a ragged quantity still reached the venue and came back
    -1013 despite the rounding. Load them explicitly and retry once.
    """
    try:
        return filters_from_market(ex.market(pair))
    except Exception:
        ex.load_markets()
        return filters_from_market(ex.market(pair))


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
    honour Retry-After), ``('halt', None)`` for HTTP 418 (IP banned — stop live + alert),
    ``('orders_exceeded', seconds)`` for Binance ``-1015`` (the ACCOUNT is over its ORDERS
    budget — narrower than a generic 429), or ``('raise', None)`` for anything else (the
    caller re-raises). Pure; no sleeping/IO here."""
    # Match on the HTTP STATUS, never on a substring of the message. ccxt puts the request URL
    # in the exception text, and a Binance signed URL carries orderId, a 13-digit timestamp and
    # a 64-hex signature — so about 3% of ordinary network errors contain "418" somewhere and
    # were being read as an IP ban, which then blocked trading for five minutes.
    status = getattr(exc, "http_status_code", None) or getattr(exc, "code", None)
    if status == 418:
        return "halt", None
    text = str(exc)
    if _is_orders_budget_exceeded(text):
        # -1015 arrives as an ordinary HTTP 429 — ccxt raises the very same DDoSProtection it
        # uses for an IP-level throttle (see handle_errors in ccxt's binance.py: it branches on
        # HTTP status before ever looking at the body's own error code) — but it means something
        # narrower: the ACCOUNT is over its ORDERS budget (100/10s or 200000/day; queried
        # 2026-08-30: developers.binance.com/docs/binance-spot-api-docs/faqs/order_count_decrement),
        # not the IP over REQUEST_WEIGHT. Caught here, before the generic 429/DDoSProtection
        # branches below, so it is never swallowed as an anonymous 1s retry — a pause too short
        # for the window that actually caused it.
        return "orders_exceeded", _orders_exceeded_wait(text, retry_after)
    if status == 429:
        return "retry", (retry_after if retry_after is not None else 1.0)
    if isinstance(exc, getattr(ccxt, "DDoSProtection", ())):
        # ccxt maps both 418 and 429 here; without a status, read the leading status token the
        # venue puts at the START of the message rather than anything further in.
        head = str(exc)[:64]
        if "418" in head:
            return "halt", None
        return "retry", (retry_after if retry_after is not None else 1.0)
    return "raise", None


ORDERS_EXCEEDED_DEFAULT_WAIT = 10.0  # the smallest real ORDERS window (10s), when unparseable
_WINDOW_UNIT_SECONDS = {"second": 1.0, "minute": 60.0, "hour": 3600.0, "day": 86400.0}


def _is_orders_budget_exceeded(text: str) -> bool:
    """True for Binance ``-1015`` "Too many new orders" (queried 2026-08-30:
    developers.binance.com/docs/binance-spot-api-docs/faqs/order_count_decrement) — an
    ACCOUNT-level ORDERS-budget breach, not the IP-level REQUEST_WEIGHT throttle a bare 429
    usually means. Matches the raw JSON code (ccxt embeds the response body verbatim) and the
    human message, so either shape is recognised."""
    return '"code":-1015' in text.replace(" ", "") or "too many new orders" in text.lower()


def _orders_exceeded_wait(text: str, retry_after: float | None) -> float:
    """Pause for an ORDERS-budget breach. Binance's own message names the window that was
    breached ("... per 10 seconds."/"... per 1 DAY.") — honour that instead of a generic 1s
    retry, since a breach of the DAY bucket is not cleared by a one-second nap. Falls back to
    Retry-After, then a conservative default sized to the smallest real ORDERS window."""
    if retry_after is not None:
        return max(float(retry_after), 0.0) or ORDERS_EXCEEDED_DEFAULT_WAIT
    match = re.search(r"(\d+)\s*(second|minute|hour|day)s?", text, re.IGNORECASE)
    if match:
        return float(match.group(1)) * _WINDOW_UNIT_SECONDS.get(match.group(2).lower(), 1.0)
    return ORDERS_EXCEEDED_DEFAULT_WAIT


# --- order-count budget (Binance ORDERS: an unfilled-order COUNT, not a raw per-window tally) -
#
# Binance's own FAQ (queried 2026-08-30: developers.binance.com/docs/binance-spot-api-docs/
# faqs/order_count_decrement) says the 100/10s and 200000/1d ORDERS limits track an UNFILLED
# order count, shared account-wide across every IP, API key and API (rotating keys does not
# evade it): a successful placement adds 1; a FILL credits some of it back (more for an
# efficient maker fill — Binance rewards resting liquidity); a CANCEL or an EXPIRY credits back
# NOTHING. Exceeding it is HTTP 429 with body code -1015 (see classify_rate_error above).
#
# That makes the 1.5 resting model's cancel+replace cycle (`_cancel_resting`/
# `sync_resting_orders` in app/orders.py) the pattern most likely to exhaust it here — every
# re-rest spends a fresh unit with no refund for the one it replaced — and it means our own
# EXPIRE_MAKER self-trade-prevention mode (an expiry, not a fill, when our own stop sweeps our
# own resting rung) is small extra pressure in the same direction.
#
# The tracker below is a deliberately CONSERVATIVE approximation: every placement is
# timestamped and prunes itself out of a window (10s / 1 day) the same way REQUEST_WEIGHT
# would, so the placement side alone can only OVER-count real usage — the safe failure
# direction for an account-ban-avoidance system, since over-counting merely throttles us early
# while under-counting sends the order that earns the -1015.
# `record_order_filled()` credits back 1 unit per confirmed fill (never more: we do not know
# Binance's real maker-fill multiplier, and assuming a bigger number than reality is the unsafe
# direction). Wired in `place_live_order` for a synchronous taker fill (every risk exit, and
# the wave-0 market entry) and in `app.orders._book_delta` for an async maker fill discovered by
# reconciliation. A cancel never calls it — see the note above.
# The credit is the one place this can drift the UNSAFE way, so it is capped at one per ORDER:
# a maker order in a thin book books several partial deltas, and `_book_delta` credits only the
# first of them. (`reset_client_cache` also clears the deque outright — a test-only path.)

ORDER_LIMIT_10S_FALLBACK = 100
ORDER_LIMIT_DAY_FALLBACK = 200_000
ORDER_BUDGET_SOFT_PCT = 80.0  # slow new exposure BEFORE the venue answers -1015, not after


def _default_order_limits() -> dict[str, tuple[float, int]]:
    """The real Binance ORDERS limits, queried from exchangeInfo on 2026-08-30 — the FALLBACK
    used until `refresh_venue_limits` succeeds, and whenever it can't reach the venue."""
    return {
        "10s": (10.0, ORDER_LIMIT_10S_FALLBACK),
        "86400s": (86400.0, ORDER_LIMIT_DAY_FALLBACK),
    }


_order_limits: dict[str, tuple[float, int]] = _default_order_limits()
_order_events: collections.deque = collections.deque()


def reset_order_budget() -> None:
    """Clear tracked order-count events and restore the fallback limits (tests, and a
    credentials change — a tracked count from a different client is meaningless)."""
    global _order_limits
    _order_events.clear()
    _order_limits = _default_order_limits()


def set_order_limits(limits: dict[str, tuple[float, int]]) -> None:
    """Replace the tracked ``{label: (window_seconds, cap)}`` pairs. Labels are opaque — only
    the values are read. Used by `refresh_venue_limits` and directly by tests."""
    global _order_limits
    _order_limits = dict(limits)


def order_limits() -> dict[str, tuple[float, int]]:
    """The ``(window_seconds, cap)`` pairs currently enforced."""
    return dict(_order_limits)


def _prune_order_events() -> None:
    if not _order_events:
        return
    longest = max((w for w, _ in _order_limits.values()), default=0.0)
    cutoff = time.monotonic() - longest
    while _order_events and _order_events[0] < cutoff:
        _order_events.popleft()


def record_order_placed(now: float | None = None) -> None:
    """Record one accepted new-order placement (+1 to the outstanding count).

    Call ONLY after the exchange actually accepted the order — a rejected post-only
    (would-cross) or a recovered duplicate must not double-count (see `place_live_order`),
    and neither does a cancel (see the module note above).
    """
    _order_events.append(now if now is not None else time.monotonic())
    _prune_order_events()


def record_order_filled(credit: int = 1) -> int:
    """Credit back up to ``credit`` outstanding placements on a confirmed fill.

    A cancel or an expiry must NEVER call this — Binance refunds neither (see the module note
    above). Returns how many were actually credited (may be fewer than ``credit`` if the
    tracked count is already lower, e.g. right after a restart)."""
    _prune_order_events()
    n = 0
    while n < credit and _order_events:
        _order_events.pop()  # any entry — the budget cares about the COUNT, not which one
        n += 1
    return n


def outstanding_order_count() -> int:
    """The current tracked outstanding-order count (for logs/dashboard visibility — the
    cancel+replace pattern above is the one expected to run this up)."""
    _prune_order_events()
    return len(_order_events)


def order_count_in_window(seconds: float) -> int:
    """How many tracked events are still within the last ``seconds``."""
    _prune_order_events()
    cutoff = time.monotonic() - seconds
    return sum(1 for t in _order_events if t >= cutoff)


def assert_order_budget_available(urgent: bool = False) -> None:
    """Refuse NEW EXPOSURE once any tracked ORDERS window is at/over its soft threshold.

    ``urgent`` exempts an exit exactly like ``assert_not_rate_limited`` — the ONE unforgivable
    bug in this project is a gated exit, and the order-count budget must not become that bug:
    a stop-loss is one order, and refusing to send it to protect a count budget trades a real
    position for an imaginary saving.
    """
    if urgent:
        return
    for label, (window_sec, cap) in _order_limits.items():
        if cap <= 0:
            continue
        used = order_count_in_window(window_sec)
        soft_cap = cap * (ORDER_BUDGET_SOFT_PCT / 100.0)
        if used >= soft_cap:
            raise RateLimited(
                f"order-count budget near cap ({label}): {used}/{cap} outstanding — "
                "pausing new exposure"
            )


# --- weight-based backoff (wires the previously-dead used_weight_from_headers/ ---------------
# --- weight_backoff_seconds into a real caller) -----------------------------------------------

_weight_limit_per_min: int = WEIGHT_LIMIT_PER_MIN
_weight_hold_until: float = 0.0


def current_weight_limit() -> int:
    """The REQUEST_WEIGHT/1m cap currently in effect — venue-sourced once `refresh_venue_limits`
    has succeeded, else the fallback constant."""
    return _weight_limit_per_min


def weight_hold_until() -> float:
    """Monotonic deadline before which NEW EXPOSURE should pause for REQUEST_WEIGHT (0 = clear)."""
    return _weight_hold_until


def assert_weight_budget_available(urgent: bool = False) -> None:
    """Pause NEW EXPOSURE while the last-seen REQUEST_WEIGHT usage is near the cap.

    ``urgent`` exempts an exit — same contract as the other two guards. Populated by
    `_note_weight_usage`, which reads ``ex.last_response_headers`` (confirmed present on the
    installed ccxt 4.0.5: ``Exchange.enableLastResponseHeaders`` defaults True and every REST
    call in ``Exchange.fetch()`` assigns it from the real response headers).
    """
    if urgent:
        return
    remaining = _weight_hold_until - time.monotonic()
    if remaining > 0:
        raise RateLimited(f"REQUEST_WEIGHT budget near cap — pausing new exposure for {remaining:.0f}s")


def _note_weight_usage(ex) -> None:
    """After an exchange call, read the USED-WEIGHT header ccxt cached on the client and
    schedule a pause for NEW EXPOSURE if usage is approaching the REQUEST_WEIGHT budget.
    Best-effort: a missing/malformed header just means no pause is scheduled — never raises."""
    global _weight_hold_until
    try:
        used = used_weight_from_headers(getattr(ex, "last_response_headers", None))
    except Exception:
        return
    if used is None:
        return
    pause = weight_backoff_seconds(used, limit=current_weight_limit())
    if pause > 0:
        _weight_hold_until = max(_weight_hold_until, time.monotonic() + pause)


# --- venue-sourced limits (exchangeInfo['rateLimits'], cached; fallback constants above) ------

_RATE_LIMIT_INTERVAL_SECONDS = {"SECOND": 1.0, "MINUTE": 60.0, "HOUR": 3600.0, "DAY": 86400.0}
_VENUE_LIMITS_TTL_SEC = 6 * 3600.0  # limits rarely change; a few refreshes/day is plenty
_venue_limits_cache: dict[str, tuple[float, int]] | None = None
_venue_limits_fetched_at: float = 0.0


def parse_venue_rate_limits(raw: list[dict]) -> tuple[dict[str, tuple[float, int]], int | None]:
    """Pure parse of Binance ``exchangeInfo()['rateLimits']`` into (order-count limits,
    REQUEST_WEIGHT/1m). Unknown/malformed entries are skipped, never raised — a partial or
    reordered response must not crash the caller, it just keeps fewer limits."""
    orders: dict[str, tuple[float, int]] = {}
    weight_per_min: int | None = None
    for entry in raw or []:
        try:
            kind = str(entry.get("rateLimitType") or "").upper()
            interval = str(entry.get("interval") or "").upper()
            num = float(entry.get("intervalNum") or 1)
            limit = int(entry.get("limit") or 0)
        except (TypeError, ValueError, AttributeError):
            continue
        unit = _RATE_LIMIT_INTERVAL_SECONDS.get(interval)
        if unit is None or limit <= 0:
            continue
        window_sec = unit * num
        if kind == "ORDERS":
            orders[f"{window_sec:.0f}s"] = (window_sec, limit)
        elif kind == "REQUEST_WEIGHT" and interval == "MINUTE" and num == 1:
            weight_per_min = limit
    return orders, weight_per_min


def refresh_venue_limits(force: bool = False) -> dict[str, tuple[float, int]]:
    """Pull the REAL ORDERS/REQUEST_WEIGHT limits from Binance ``exchangeInfo`` and cache them.

    Falls back to the hardcoded constants (queried from the real venue 2026-08-30) when the
    call fails, live trading is not configured, or the app is offline — a cold/offline app
    must still enforce SOME budget rather than none. Cached for ``_VENUE_LIMITS_TTL_SEC``; call
    sites must NOT call this per order.
    """
    global _venue_limits_cache, _venue_limits_fetched_at, _weight_limit_per_min
    now = time.monotonic()
    if not force and _venue_limits_cache is not None \
            and now - _venue_limits_fetched_at < _VENUE_LIMITS_TTL_SEC:
        return dict(_venue_limits_cache)
    try:
        ex = _client()
        info = ex.publicGetExchangeInfo()
        parsed_orders, parsed_weight = parse_venue_rate_limits(info.get("rateLimits") or [])
    except Exception:
        logger.debug("venue rateLimits refresh failed — keeping the fallback", exc_info=True)
        parsed_orders, parsed_weight = {}, None
    if parsed_orders:
        _venue_limits_cache = parsed_orders
        set_order_limits(parsed_orders)
    elif _venue_limits_cache is None:
        _venue_limits_cache = _default_order_limits()
        set_order_limits(_venue_limits_cache)
    if parsed_weight:
        _weight_limit_per_min = parsed_weight
    _venue_limits_fetched_at = now
    return dict(_venue_limits_cache or {})
