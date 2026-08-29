"""
Binance testnet end-to-end for the live resting model — live-readiness 1.8.

One command drives the app's OWN live path against Binance Spot testnet:

    queue a KSS rung -> orders.sync_resting_orders places it as a resting LIMIT_MAKER
    -> poll orders.reconcile_live_orders -> book the Fill + Position if the venue filled it
    -> cancel whatever is still resting before exiting.

``--force-match`` supplies the counter side so the venue really fills the rung. Waiting does
not: the testnet book is simulated and deep, and two runs (90s, then 300s at 0.03% below the
last price) never had it reach a passive rung. Rest the rung where it IS the best bid — a
pair with a wide spread and a thin top — and cross it: ``--symbol YB/USDT --distance-pct 0.1
--force-match``. See ``testnet_lib.cross_fill``.

Nothing is simulated: the order really rests on testnet and the app's own functions place,
read and book it. Only the database is disposable — the harness runs on a THROWAWAY SQLite
file (``data/testnet_e2e.db``), so the paper and live books are never touched.

Needs the live worktree's ``.env`` (keys + LIVE_TRADING + LIVE_USE_TESTNET); MAKER_ORDERS and
AUTO_TRADE are forced on because the resting model is exactly what is under test. Run
``scripts/testnet_check.py`` first if the keys have never been used. See docs/testnet-setup.md.

    python scripts/testnet_e2e.py
    python scripts/testnet_e2e.py --symbol ETH/USDT --distance-pct 0.05 --wait-sec 180
    python scripts/testnet_e2e.py --symbol YB/USDT --distance-pct 0.1 --force-match
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from testnet_lib import (  # noqa: E402
    BAD,
    OK,
    WARN,
    bid_queue_above,
    cross_fill,
    prepare_env,
    require_testnet,
    say,
)


def _parse() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Binance testnet end-to-end for the resting model.")
    ap.add_argument("--symbol", default="BTC/USDT", help="pair to trade (default BTC/USDT)")
    ap.add_argument("--notional", type=float, default=15.0,
                    help="rung size in quote ccy (default 15; must clear minNotional)")
    ap.add_argument("--distance-pct", type=float, default=0.2,
                    help="how far below market to rest the rung (default 0.2%%; smaller = more "
                         "likely to fill inside the wait window)")
    ap.add_argument("--wait-sec", type=int, default=90,
                    help="how long to wait for the venue to fill it (default 90)")
    ap.add_argument("--db", default="data/testnet_e2e.db",
                    help="throwaway SQLite file for this run (never the paper or live book)")
    ap.add_argument("--force-match", action="store_true",
                    help="cross the rung with a counter SELL from this same testnet account so "
                         "the venue fills it - the only way to exercise the fill leg on a book "
                         "whose depth never reaches a passive rung (testnet only)")
    ap.add_argument("--max-cross-usd", type=float, default=60.0,
                    help="refuse to cross a bid queue deeper than this (default $60)")
    ap.add_argument("--prove-cancel-books-fill", action="store_true",
                    help="different mode: half-fill the rung and then cancel it, checking that "
                         "the filled half is booked BEFORE the exchange link is dropped (use "
                         "with --rest-at-touch so our rung is alone at its price)")
    ap.add_argument("--rest-at-touch", action="store_true",
                    help="price the rung one tick above the best bid instead of --distance-pct "
                         "below the last trade, so nothing rests ahead of it (use with "
                         "--force-match on a pair whose spread is wider than one tick)")
    return ap.parse_args()


args = _parse()
# Settings are read at import time, so the environment must be shaped BEFORE app.config loads.
prepare_env(args.db)

from app import execution, models, orders  # noqa: E402
from app.config import settings  # noqa: E402
from app.data.providers import live_provider  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402


def _posture() -> None:
    print("\n1. Posture")
    require_testnet(execution, settings)
    say(OK, f"live on {settings.live_exchange} TESTNET, maker={settings.maker_orders}, "
             f"cap ${settings.live_max_order_notional:.2f}/BUY")
    say(OK, f"throwaway database {args.db} (paper and live books untouched)")


def _rung(ex, pair: str) -> tuple[float, float, float]:
    """Return (last, price, qty) for a compliant rung below the TESTNET book.

    ``--rest-at-touch`` prices it one tick above the best bid instead of a % below the last
    trade: still post-only (it stays under the ask), but with nothing queued ahead of it, so
    a counter SELL reaches OUR order rather than 100k units of simulated depth.
    """
    print("\n2. Build the rung from testnet's own book")
    last = float(ex.fetch_ticker(pair)["last"])
    filters = execution.filters_from_market(ex.market(pair))
    if args.rest_at_touch:
        book = ex.fetch_order_book(pair, 5)
        best_bid, best_ask = book["bids"][0][0], book["asks"][0][0]
        raw_price = best_bid + filters["tickSize"]
        if raw_price >= best_ask:  # one-tick spread: nothing to step into, join the queue
            raw_price = best_bid
        where = f"one tick above best bid {best_bid:g} (ask {best_ask:g})"
    else:
        raw_price = last * (1.0 - args.distance_pct / 100.0)
        where = f"{args.distance_pct:g}% below last"
    price, qty = execution.round_to_filters(raw_price, args.notional / raw_price, filters,
                                            ref_price=last)
    say(OK, f"{pair} last={last:g} -> rung {qty:g} @ {price:g} (${price * qty:.2f}, {where})")
    return last, price, qty


def _queue(db, base: str, price: float, qty: float):
    print("\n3. Queue it as a KSS rung (through the approval queue, like the strategy does)")
    order, risk_note = orders.queue_order(
        db, symbol=base, side="BUY", quantity=qty, price=price, order_type="LIMIT",
        source="kss", source_ref="pyramid:0:wave:0", strategy_name="testnet-e2e",
        note="live-readiness 1.8 end-to-end",
    )
    say(OK, f"pending order {order.id} queued" + (f" (risk: {risk_note})" if risk_note else ""))
    return order


def _rest(db, ex, order) -> bool:
    print("\n4. Place it on the exchange in advance (the 1.5 resting model)")
    counts = orders.sync_resting_orders(db)
    db.refresh(order)
    if not order.exchange_order_id:
        say(BAD, f"not placed ({counts}) — a post-only reject means the book already reached "
                  "the rung; raise --distance-pct and retry")
        return False
    say(OK, f"resting as exchange order {order.exchange_order_id} "
             f"(status={order.exchange_status}, local status still {order.status})")
    book = ex.fetch_order_book(args.symbol, 5)
    best_bid = book["bids"][0] if book["bids"] else (0.0, 0.0)
    say(OK, f"book now: best bid {best_bid[0]:g} x {best_bid[1]:g}"
             + (" (that IS our rung — nothing queued ahead of it)"
                if abs(best_bid[0] - order.price) < 1e-12 else
                f", our rung sits at {order.price:g}"))
    return True


def _cross(ex, order) -> None:
    """Supply the counter side so the venue fills the rung (--force-match)."""
    print("\n4b. Cross the rung from this same testnet account (test-only liquidity)")
    out = cross_fill(ex, args.symbol, order.price, max_cross_usd=args.max_cross_usd)
    say(OK, f"counter SELL {out['quantity']:g} @ {order.price:g} (${out['notional']:.2f}) "
             f"-> id={out['id']} status={out['status']} filled={out['filled']:g}")


def _cancel_books_the_partial(db, ex, order) -> int:
    """Half-fill the rung, then CANCEL it — the cancel must still book what already filled.

    A cancel races the venue. `orders._cancel_resting` drops the exchange link, and
    `reconcile_live_orders` only looks at rows that still have one, so booking has to happen
    before the link goes — otherwise the quantity the venue really filled is lost and the app
    keeps (or misses) a position it does not hold. Returns the exit code for this mode.
    """
    print("\n4b. Half-fill the rung, then cancel it — the filled half must still be booked")
    price = float(ex.fetch_order(str(order.exchange_order_id), args.symbol)["price"])
    queue = bid_queue_above(ex, args.symbol, price)
    if queue > order.quantity * 1.001:
        say(BAD, f"{queue:g} bid at {price:g} but our rung is {order.quantity:g} — someone is "
                 "queued with us, so a partial would fill THEIR order; re-run")
        return 1
    half = round(order.quantity / 2, 8)
    out = cross_fill(ex, args.symbol, price, qty=half, max_cross_usd=args.max_cross_usd)
    # What the venue actually matched, not what we asked for: the counter order is rounded to
    # the lot step, so asking for 70.45 fills 70.4 and only that is owed a Fill.
    matched = float(out["filled"])
    say(OK, f"counter SELL {out['quantity']:g} @ {price:g} filled={matched:g} — the rung is now "
            "PARTLY filled and still resting")

    say(OK, "calling orders._cancel_resting directly (no reconcile first — that is the race)")
    cancelled = orders._cancel_resting(db, order)
    db.commit()
    fills = db.query(models.Fill).filter(models.Fill.pending_order_id == order.id).all()
    booked = sum(f.quantity for f in fills)
    pos = db.query(models.Position).filter(models.Position.symbol == order.symbol).one_or_none()
    say(OK, f"_cancel_resting -> {cancelled}; Fills booked for this order: "
            f"{[(f.quantity, f.price) for f in fills]}")
    ok = booked >= matched - 1e-9
    if not ok:
        say(BAD, f"the cancel LOST the fill: {booked:g} booked, {matched:g} was filled — the app "
                 "would keep cash it no longer has")
    else:
        say(OK, f"booked {booked:g} before unlinking; Position {order.symbol} qty="
                f"{pos.quantity if pos else 0:g}")
    db.refresh(order)
    if order.exchange_order_id is not None:
        say(BAD, f"still linked to {order.exchange_order_id} after a successful cancel")
        ok = False
    return 0 if ok else 1


def _poll(db, order) -> bool:
    print(f"\n5. Wait up to {args.wait_sec}s for the venue to fill it, booking via reconcile")
    deadline = time.time() + args.wait_sec
    while time.time() < deadline:
        booked = orders.reconcile_live_orders(db)
        if booked:
            fills = db.query(models.Fill).filter(models.Fill.pending_order_id == order.id).all()
            pos = db.query(models.Position).filter(
                models.Position.symbol == order.symbol).one_or_none()
            for f in fills:
                say(OK, f"Fill booked: {f.quantity:g} @ {f.price:g} (fee {f.fee:g})")
            say(OK, f"Position {order.symbol}: qty={pos.quantity:g} avg={pos.avg_entry_price:g}"
                 if pos else f"{BAD} no Position row — reconcile booked a fill without one")
            db.refresh(order)
            say(OK, f"order status={order.status} exchange_status={order.exchange_status}")
            _idempotent(db, order, len(fills))
            return True
        time.sleep(5)
    say(WARN, "no fill inside the window — expected when the price never dipped to the rung. "
               "The placement, status and cancel paths are still proven above.")
    return False


def _idempotent(db, order, fills_after_booking: int) -> None:
    """A second reconcile pass must book nothing: one venue transition = exactly one Fill."""
    print("\n5b. Re-run reconcile — booking a real fill has to be idempotent")
    again = orders.reconcile_live_orders(db)
    total = db.query(models.Fill).filter(models.Fill.pending_order_id == order.id).count()
    if again or total != fills_after_booking:
        say(BAD, f"NOT idempotent: second pass booked {again}, fills {fills_after_booking}"
                  f"->{total} — the same venue fill was recorded twice")
        return
    say(OK, f"second pass booked nothing, still {total} Fill(s) for order {order.id}")


def _cleanup(db) -> None:
    """Cancel every order this run left on the book — not just the one we tracked."""
    print("\n6. Cleanup")
    resting = [
        o for o in db.query(models.PendingOrder)
        .filter(models.PendingOrder.exchange_order_id.isnot(None)).all()
        # A filled order is off the book already; cancelling it earns -2011, which would read
        # as "a live order was left behind" when nothing was.
        if str(o.exchange_status or "").lower() not in orders._TERMINAL_EXCHANGE_STATUS
    ]
    if not resting:
        say(OK, "nothing left resting")
        return
    for order in resting:
        try:
            execution.cancel_live_order(live_provider().pair(order.symbol),
                                        order.exchange_order_id)
            say(OK, f"cancelled {order.exchange_order_id}")
        except Exception as exc:  # never leave a live order behind silently
            say(BAD, f"CANCEL FAILED for {order.exchange_order_id}: {type(exc).__name__} {exc} "
                      "— cancel it by hand on testnet.binance.vision")


def main() -> int:
    print(f"Testnet e2e | symbol={args.symbol} | notional=${args.notional:g} | "
          f"distance={args.distance_pct:g}% | wait={args.wait_sec}s")
    _posture()

    init_db()  # the app's own bootstrap: every table, incl. the ones the KSS hook reads
    db = SessionLocal()
    base = args.symbol.partition("/")[0]
    order = None
    try:
        ex = execution._client()
        ex.load_markets()  # ccxt's .market() raises until something has loaded them
        _, price, qty = _rung(ex, args.symbol)
        order = _queue(db, base, price, qty)
        if not _rest(db, ex, order):
            return 1
        if args.prove_cancel_books_fill:
            return _cancel_books_the_partial(db, ex, order)
        if args.force_match:
            _cross(ex, order)
        _poll(db, order)
    except Exception as exc:
        print(f"\n {BAD} {type(exc).__name__}: {exc}")
        return 1
    finally:
        _cleanup(db)
        db.close()

    print("\nDONE — the live resting path ran end to end against testnet.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
