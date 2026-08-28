"""
Binance Spot TESTNET preflight - live-readiness task 1.8.

Answers one question before the live instance is started: *can this worktree's keys
actually reach Binance testnet, and does the app's own live path place, read and cancel
an order there?* It drives ``app.execution`` (not raw ccxt), so a green run exercises the
real plumbing - sandbox client, exchange filters, post-only placement, order status,
cancel - rather than proving that ccxt works.

Read-only by default. With ``--place`` it rests exactly ONE post-only BUY far below market
(default 20% below), polls it, then cancels it; the order is never meant to fill. Placement
is refused unless ``LIVE_USE_TESTNET=true`` so real keys can never be exercised by mistake.
The API secret is never printed.

Run from the worktree whose ``.env`` holds the testnet keys (see docs/testnet-setup.md):

    python scripts/testnet_check.py
    python scripts/testnet_check.py --symbol BTC/USDT --place
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import execution  # noqa: E402
from app.config import settings  # noqa: E402

OK, WARN, BAD = "[ok]", "[--]", "[!!]"


def _say(mark: str, msg: str) -> None:
    print(f" {mark} {msg}")


def check_posture() -> bool:
    """Print the configured live posture. False when the config cannot reach testnet."""
    print("\n1. Posture (from .env / environment)")
    fatal = False

    if settings.live_exchange == "binance":
        _say(OK, "LIVE_EXCHANGE=binance")
    else:
        _say(BAD, f"LIVE_EXCHANGE={settings.live_exchange} - testnet here is Binance-specific; "
                  "set LIVE_EXCHANGE=binance")
        fatal = True

    if execution.live_key_present():
        _say(OK, "LIVE_API_KEY / LIVE_API_SECRET are set (values never printed)")
    else:
        _say(BAD, "LIVE_API_KEY / LIVE_API_SECRET missing - generate HMAC keys at "
                  "https://testnet.binance.vision and put them in this worktree's .env")
        fatal = True

    if settings.live_use_testnet:
        _say(OK, "LIVE_USE_TESTNET=true - orders route to the sandbox")
    else:
        _say(BAD, "LIVE_USE_TESTNET=false - these keys would hit PRODUCTION Binance; "
                  "--place is refused")

    # Informational: the app itself also needs the master switch, this script does not.
    _say(OK if settings.live_trading else WARN,
         f"LIVE_TRADING={str(settings.live_trading).lower()} "
         f"(the app places orders only when true; this preflight does not need it)")
    _say(WARN, f"MAKER_ORDERS={str(settings.maker_orders).lower()} | "
               f"LIVE_MAX_ORDER_NOTIONAL=${settings.live_max_order_notional:.2f} | "
               f"ORDER_FILL_TIMEOUT_SEC={settings.order_fill_timeout_sec}")
    return not fatal


def check_public(ex, symbol: str) -> tuple[float, dict]:
    """Load markets, confirm the sandbox URL, and return (last_price, filters)."""
    print("\n2. Public endpoints (sandbox)")
    url = str(ex.urls.get("api", {}).get("public", ""))
    _say(OK if "testnet" in url else BAD, f"base URL {url}")

    markets = ex.load_markets()
    _say(OK, f"{len(markets)} markets loaded")
    if symbol not in markets:
        usdt = sorted(m for m, v in markets.items() if v.get("spot") and v.get("quote") == "USDT")
        _say(BAD, f"{symbol} is not listed on testnet. Spot/USDT pairs available: "
                  f"{', '.join(usdt[:25])}{' ...' if len(usdt) > 25 else ''}")
        raise SystemExit(1)

    last = float(ex.fetch_ticker(symbol)["last"])
    _say(OK, f"{symbol} last={last:g} (testnet's own book - it drifts from production)")

    filters = execution.filters_from_market(ex.market(symbol))
    _say(OK, f"filters {filters}")
    return last, filters


def check_private(ex, symbol: str) -> dict:
    """Confirm the keys are accepted and report the balances that matter for *symbol*.

    The faucet funds dozens of currencies; sorting by raw amount buries the two that
    decide whether an order can be placed at all, so report those first and explicitly.
    """
    print("\n3. Private endpoints (signed)")
    balance = ex.fetch_balance()
    free = {k: float(v) for k, v in (balance.get("free") or {}).items() if v}
    _say(OK, "fetch_balance accepted - key, signature and clock are good")

    base, _, quote = symbol.partition("/")
    for ccy, need in ((quote, "to BUY"), (base, "to SELL")):
        amount = free.get(ccy, 0.0)
        _say(OK if amount > 0 else WARN,
             f"{ccy} free = {amount:g}" + ("" if amount > 0 else f"  (needed {need} {symbol})"))

    others = sorted((kv for kv in free.items() if kv[0] not in (base, quote)),
                    key=lambda kv: -kv[1])[:6]
    if others:
        _say(WARN, "also funded: " + ", ".join(f"{k}={v:g}" for k, v in others))
    elif not free:
        _say(WARN, "no free balance - testnet resets wipe funds; regenerate keys to get a "
                   "freshly funded account")
    return free


def check_place(ex, symbol: str, last: float, filters: dict, free: dict,
                notional: float, distance_pct: float) -> None:
    """Rest one post-only BUY far below market through the app's own path, then cancel it."""
    print("\n4. Place -> read -> cancel (post-only BUY, far below market)")
    quote = symbol.partition("/")[2]
    have = free.get(quote, 0.0)
    if have < notional:  # the venue locks the quote funds when the order rests
        _say(BAD, f"{quote} free = {have:g}, need ~{notional:g} to rest this BUY - fund the "
                  f"account, lower --notional, or probe a pair whose quote you hold")
        raise SystemExit(1)
    price = last * (1.0 - distance_pct / 100.0)
    try:
        px, qty = execution.round_to_filters(price, notional / price, filters, ref_price=last)
    except ValueError as exc:
        _say(BAD, f"cannot build a compliant order at ${notional:g}: {exc} - raise --notional")
        raise SystemExit(1) from exc
    _say(OK, f"rounded to filters: qty={qty:g} @ {px:g} (notional ${px * qty:.2f})")

    cid = f"fm-preflight-{int(time.time())}"
    order_id = None
    try:
        res = execution.place_live_order(symbol, "buy", qty, px, "LIMIT",
                                         maker_orders=True, client_order_id=cid)
        order_id = res.get("raw_id")
        if res.get("status") == "rejected":
            _say(BAD, "post-only rejected - the order would have crossed the book; "
                      "raise --distance-pct")
            return
        _say(OK, f"placed id={order_id} status={res.get('status')} filled={res.get('quantity'):g} "
                 "(a resting order MUST report filled=0 - task 1.1)")

        state = execution.fetch_live_order(symbol, str(order_id))
        _say(OK, f"fetch_order -> status={state['status']} filled={state['filled']:g} "
                 f"avg={state['average']:g}")

        open_ids = [o.get("id") for o in ex.fetch_open_orders(symbol)]
        _say(OK if order_id in open_ids else WARN,
             f"open orders on {symbol}: {len(open_ids)}")
    finally:
        if order_id:
            try:
                ex.cancel_order(order_id, symbol)
                _say(OK, f"cancelled {order_id} - nothing left resting")
            except Exception as exc:  # never leave a live order behind silently
                _say(BAD, f"CANCEL FAILED for {order_id}: {type(exc).__name__} {exc} - "
                          "cancel it by hand on testnet.binance.vision")


def main() -> int:
    ap = argparse.ArgumentParser(description="Binance Spot testnet preflight for the live path.")
    ap.add_argument("--symbol", default="BTC/USDT", help="pair to probe (default BTC/USDT)")
    ap.add_argument("--place", action="store_true",
                    help="also place and cancel one tiny post-only BUY (testnet only)")
    ap.add_argument("--notional", type=float, default=15.0,
                    help="test-order notional in quote ccy (default 15; must clear minNotional)")
    ap.add_argument("--distance-pct", type=float, default=20.0,
                    help="how far below market to rest the test order (default 20%%)")
    args = ap.parse_args()

    print(f"Binance testnet preflight | symbol={args.symbol} | place={args.place}")
    if not check_posture():
        print("\nFAILED - fix the posture above first.")
        return 1
    if args.place and not settings.live_use_testnet:
        print("\nFAILED - refusing to place with LIVE_USE_TESTNET=false (those are real keys).")
        return 1
    if args.place and args.notional > settings.live_max_order_notional:
        print(f"\nFAILED - --notional ${args.notional:g} exceeds LIVE_MAX_ORDER_NOTIONAL "
              f"${settings.live_max_order_notional:g}; the app would refuse this BUY too.")
        return 1

    try:
        ex = execution._client()
        last, filters = check_public(ex, args.symbol)
        free = check_private(ex, args.symbol)
        if args.place:
            check_place(ex, args.symbol, last, filters, free,
                        args.notional, args.distance_pct)
        else:
            print("\n4. Place -> read -> cancel: skipped (add --place to exercise it)")
    except SystemExit:
        raise
    except Exception as exc:
        print(f"\n {BAD} {type(exc).__name__}: {exc}")
        print("\nFAILED - see the troubleshooting table in docs/testnet-setup.md.")
        return 1

    print("\nPASSED - the live path can talk to Binance testnet.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
