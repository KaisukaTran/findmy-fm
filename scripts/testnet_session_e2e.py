"""
A real KSS session against Binance testnet — the live-readiness 1.8 definition of done.

``testnet_e2e.py`` proves one rung: placed, filled, booked. This proves the SESSION the
resting model exists for — that the take-profit rests on the exchange in advance and
follows the position as it changes:

    start a session   -> wave 0 rests on the venue as a post-only LIMIT_MAKER
    cross half of it  -> reconcile books the partial, the session goes ACTIVE
    sync_resting_tp   -> a take-profit SELL rests on the venue, above market, at/above K-2
    cross the rest    -> the position grew, so the resting TP is cancelled and re-placed
    cross wave 1      -> the average dropped, so the TP is re-priced to follow it
    stop the session  -> the exit comes off the book, nothing is left resting

Every step re-checks the venue first, so a rung the market reaches on its own is filled by the
market. What it does not reach is crossed by a counter order from this same testnet account
(``testnet_lib.cross_fill``) — the match is still the venue's, against the real orders the app
placed; a simulated book routinely never reaches a passive rung at all. Everything else —
pricing, placing, cancelling, booking, the ladder — is the app's own code, and only the
database is disposable.

Crossing is capped (``--max-cross-usd``), so a rung that has sunk under a wall of simulated
depth waits for the market instead of dumping into it — the run then reports which leg it
could not prove rather than pretending. Pick a pair with a thin top of book and a spread wider
than one tick.

Needs the live worktree's ``.env`` (testnet keys + LIVE_TRADING + LIVE_USE_TESTNET).

    python scripts/testnet_session_e2e.py
    python scripts/testnet_session_e2e.py --symbol YB/USDT --first-wave-usd 11 --max-cross-usd 5000
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
    ap = argparse.ArgumentParser(description="A real KSS session on Binance testnet (1.8 DoD).")
    ap.add_argument("--symbol", default="YB/USDT",
                    help="pair to trade — needs a spread wider than one tick and a thin top of "
                         "book, so the session's rungs can rest where nothing is queued ahead "
                         "of them (default YB/USDT)")
    ap.add_argument("--first-wave-usd", type=float, default=8.0,
                    help="wave-0 size in USD (default 8; wave 1 is twice that, and both must "
                         "clear minNotional and stay under live_max_order_notional)")
    ap.add_argument("--distance-pct", type=float, default=0.15,
                    help="wave step %% (default 0.15; must be more than one tick so wave 1 "
                         "prices BELOW wave 0 and the average actually moves)")
    ap.add_argument("--tp-pct", type=float, default=1.0, help="session take-profit %% (default 1)")
    ap.add_argument("--fund-usd", type=float, default=40.0,
                    help="session isolated_fund (default 40)")
    ap.add_argument("--max-cross-usd", type=float, default=300.0,
                    help="refuse to cross a bid queue deeper than this (default $300)")
    ap.add_argument("--cross-timeout-sec", type=int, default=180,
                    help="how long to keep retrying a cross whose queue is too deep — the "
                         "simulated book thins out and refills (default 180)")
    ap.add_argument("--db", default="data/testnet_session_e2e.db",
                    help="throwaway SQLite file for this run (never the paper or live book)")
    return ap.parse_args()


args = _parse()
# Settings are read at import time, so the environment must be shaped BEFORE app.config loads.
prepare_env(args.db, KSS_FIRST_WAVE_USD=str(args.first_wave_usd))

from app import execution, models, orders  # noqa: E402
from app.config import settings  # noqa: E402
from app.data.providers import live_provider  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.kss import service as kss  # noqa: E402

PAIR = args.symbol
BASE = PAIR.partition("/")[0]


def _venue_price(ex, order) -> float:
    """The price the order actually rests at — the local row is pre-filter rounding."""
    res = ex.fetch_order(str(order.exchange_order_id), PAIR)
    return float(res["price"])


def _fill(db, ex, order, *, qty: float | None = None, label: str = "") -> None:
    """Get *order* filled: book what the venue already did, then cross what is still resting.

    The market moves on its own — a rung the price comes back to fills without any help, and
    that is the better proof when it happens, so every pass re-checks the venue before trying
    to cross. Crossing is capped, so a rung that has sunk under a wall of simulated depth
    waits for the market instead of dumping into it.
    """
    deadline = time.time() + args.cross_timeout_sec
    warned = ""
    while True:
        orders.reconcile_live_orders(db)
        res = ex.fetch_order(str(order.exchange_order_id), PAIR)
        price, left = float(res["price"]), float(res["amount"]) - float(res["filled"])
        if left <= 0:
            say(OK, f"{label}: the venue filled it on its own "
                    f"({float(res['filled']):g} @ {price:g})")
            return
        # A partial only reaches OUR order when nothing else is bid at that price — otherwise
        # the counter SELL fills the other bid and our rung never moves (seen: wave 1 stayed
        # resting while $21.95 of the cross went to someone else's bid). Selling the WHOLE
        # queue always reaches ours; a partial is only safe when the queue is ours alone.
        want: float | None = None
        if qty is not None:
            queue = bid_queue_above(ex, PAIR, price)
            if queue <= left * 1.001:
                want = min(qty, left)
            elif warned != "partial":
                warned = "partial"
                say(WARN, f"{queue:g} bid at {price:g} but our rung is {left:g} — a partial "
                          "would fill someone else's order; crossing the whole queue instead")
        try:
            out = cross_fill(ex, PAIR, price, qty=want, max_cross_usd=args.max_cross_usd)
            say(OK, f"crossed {label}: sold {out['quantity']:g} @ {price:g} "
                    f"(${out['notional']:.2f}, filled={out['filled']:g})")
            return
        except RuntimeError as exc:
            if time.time() >= deadline:
                raise
            if warned != "deep":
                warned = "deep"
                say(WARN, f"{exc}\n      -> waiting for the market to come back to {price:g} "
                          f"instead (up to {args.cross_timeout_sec}s)")
            time.sleep(10)


def _resting_tp(db):
    return next((o for o in kss._resting_tp_rows(db)), None)


def _sync(db) -> tuple[dict, dict]:
    """One cycle of the two halves of the resting model, in the order the scheduler runs them."""
    tp_counts = kss.sync_resting_tp(db)
    order_counts = orders.sync_resting_orders(db)
    return tp_counts, order_counts


def _open_ids(ex) -> set[str]:
    return {str(o["id"]) for o in ex.fetch_open_orders(PAIR)}


def _start(db, ex) -> tuple[models.KssSession, models.PendingOrder]:
    print("\n2. Start a KSS session whose wave 0 rests at the top of the book")
    tick = execution.filters_from_market(ex.market(PAIR))["tickSize"]
    # As HIGH in the spread as post-only allows, not just above the bid: wave 1 prices a step
    # BELOW wave 0, and on a wide spread that lands it inside the empty gap too. Anchored to
    # the bid instead, wave 1 falls into the wall of simulated depth under the touch (seen:
    # $6,726 queued ahead of it) and can only be filled by dumping into that wall.
    # A one-tick spread leaves nowhere to rest at all — that comes and goes on a simulated
    # book, so wait for it to open rather than making the operator re-run.
    deadline = time.time() + args.cross_timeout_sec
    while True:
        book = ex.fetch_order_book(PAIR, 5)
        best_bid, best_ask = book["bids"][0][0], book["asks"][0][0]
        entry = best_ask - tick
        if entry <= best_bid:
            entry = best_bid + tick
        if best_bid < entry < best_ask:
            break
        if time.time() >= deadline:
            raise SystemExit(f" {BAD} {PAIR} spread stayed one tick (bid {best_bid:g} / ask "
                             f"{best_ask:g}) — nothing to rest into; try another pair")
        say(WARN, f"spread is one tick (bid {best_bid:g} / ask {best_ask:g}) — waiting for it "
                  "to open")
        time.sleep(10)
    row = kss.create_session(
        db, symbol=BASE, entry_price=entry, distance_pct=args.distance_pct,
        max_waves=3, isolated_fund=args.fund_usd, tp_pct=args.tp_pct,
        timeout_x_min=60.0, gap_y_min=1.0, note="live-readiness 1.8 session e2e",
    )
    started = kss.start_session(db, row.id)
    say(OK, f"session {row.id} {BASE} entry={entry:g} (best bid {best_bid:g}, ask {best_ask:g}), "
            f"step={args.distance_pct:g}%, tp={row.tp_pct:g}%")
    order = db.get(models.PendingOrder, started["pending_order_id"])
    say(OK, f"wave 0 queued: {order.quantity:g} @ {order.price:g} (${order.quantity * order.price:.2f})")

    print("\n3. The wave rests on the exchange in advance (1.5)")
    counts = orders.sync_resting_orders(db)
    db.refresh(order)
    if not order.exchange_order_id:
        raise SystemExit(f" {BAD} wave 0 was not placed ({counts}) — post-only reject means the "
                         "book already moved through the rung; re-run")
    say(OK, f"wave 0 rests as exchange order {order.exchange_order_id} "
            f"@ {_venue_price(ex, order):g} (local row still {order.status})")
    return row, order


def _partial_qty(ex, order, price: float) -> float | None:
    """Half of the rung, or None when a half (or what it leaves) would breach minNotional —
    the venue rejects a counter order under it, and a remainder under it could never fill."""
    filters = execution.filters_from_market(ex.market(PAIR))
    floor = float(filters.get("minNotional") or 0.0) * 1.05  # a margin for the step rounding
    step = float(filters.get("stepSize") or 0.0)
    half = order.quantity / 2
    if step > 0:
        half = round(half / step) * step
    if half * price < floor or (order.quantity - half) * price < floor:
        return None
    return round(half, 8)


def _partial(db, ex, row, order) -> bool:
    """Fill part of wave 0. Returns True when a remainder is still resting for step 6."""
    print("\n4. Cross HALF of wave 0 — a partial fill the venue really made")
    price = _venue_price(ex, order)
    queue = bid_queue_above(ex, PAIR, price)
    qty = _partial_qty(ex, order, price)
    if queue > order.quantity * 1.001:
        say(WARN, f"{queue:g} bid at our price but our rung is only {order.quantity:g} — "
                  "someone is queued with us, so a partial would fill THEIR order first; "
                  "crossing the whole queue instead")
        qty = None
    elif qty is None:
        say(WARN, f"a half of {order.quantity:g} @ {price:g} would sit under minNotional — "
                  "filling wave 0 whole (raise --first-wave-usd to exercise the partial)")
    _fill(db, ex, order, qty=qty, label="wave 0 (half)" if qty else "wave 0 (whole)")

    booked = orders.reconcile_live_orders(db)
    db.refresh(row)
    py = kss._to_pyramid(row)
    say(OK, f"reconcile booked {booked} -> session status={row.status}, "
            f"filled={py.total_filled_qty:g}, avg={py.avg_price:g}")
    return qty is not None


def _tp_rests(db, ex) -> models.PendingOrder:
    print("\n5. The take-profit rests on the exchange, above market, at/above the K-2 floor")
    tp_counts, order_counts = _sync(db)
    tp = _resting_tp(db)
    if tp is None or not tp.exchange_order_id:
        raise SystemExit(f" {BAD} no resting TP ({tp_counts}, {order_counts})")
    venue_price = _venue_price(ex, tp)
    last = float(ex.fetch_ticker(PAIR)["last"])
    floor = kss._k2_floor_price(db, BASE)
    say(OK, f"TP {tp.quantity:g} @ {venue_price:g} rests as exchange order {tp.exchange_order_id} "
            f"(market {last:g}, K-2 floor {floor:g})")
    if venue_price <= last:
        say(BAD, "TP is at or below the market — a post-only exit there would have been rejected")
    if floor > 0 and venue_price < floor - 1e-12:
        say(BAD, f"TP {venue_price:g} sits BELOW the K-2 floor {floor:g} — invariant broken")
    return tp


def _tp_follows_size(db, ex, row, tp, order) -> models.PendingOrder:
    print("\n6. Fill the rest of wave 0 — the position grew, so the resting TP must be re-placed")
    old_id, old_qty = str(tp.exchange_order_id), tp.quantity
    _fill(db, ex, order, label="wave 0 (remainder)")
    orders.reconcile_live_orders(db)
    _sync(db)
    tp = _resting_tp(db)
    if tp is None or not tp.exchange_order_id:
        raise SystemExit(f" {BAD} the TP did not come back after the position grew")
    db.refresh(row)
    py = kss._to_pyramid(row)
    new_id = str(tp.exchange_order_id)
    say(OK, f"position now {py.total_filled_qty:g} @ avg {py.avg_price:g}; TP {old_qty:g}->"
            f"{tp.quantity:g}, exchange order {old_id}->{new_id}")
    if abs(tp.quantity - old_qty) < 1e-9:
        say(OK, "the position did not actually grow (the venue had already filled the rung "
                "before the TP was placed), so the exit correctly stayed put")
    elif new_id == old_id:
        say(BAD, "the TP size changed but the exchange order did not — the venue still holds "
                 "an exit for the old size")
    elif old_id in _open_ids(ex):
        say(BAD, f"the replaced TP {old_id} is STILL open on the venue — a duplicate exit")
    else:
        say(OK, f"the replaced TP {old_id} is off the book (cancel+replace, not two exits)")
    return tp


def _tp_follows_avg(db, ex, row, tp) -> models.PendingOrder:
    print("\n7. Fill wave 1 lower — the average drops, so the TP must follow it down")
    wave1 = (
        db.query(models.PendingOrder)
        .filter(models.PendingOrder.source == "kss", models.PendingOrder.side == "BUY",
                models.PendingOrder.status == models.PENDING)
        .order_by(models.PendingOrder.id.desc())
        .first()
    )
    if wave1 is None:
        say(WARN, "no wave 1 was queued — the ladder did not chain; skipping the avg leg")
        return tp
    if not wave1.exchange_order_id:
        orders.sync_resting_orders(db)
        db.refresh(wave1)
    if not wave1.exchange_order_id:
        say(WARN, f"wave 1 ({wave1.quantity:g} @ {wave1.price:g}) is not on the venue — "
                  "skipping the avg leg")
        return tp

    old_id, old_price = str(tp.exchange_order_id), _venue_price(ex, tp)
    py_before = kss._to_pyramid(row)
    say(OK, f"wave 1 rests as {wave1.exchange_order_id} @ {_venue_price(ex, wave1):g} "
            f"(avg before {py_before.avg_price:g})")
    _fill(db, ex, wave1, label="wave 1")
    orders.reconcile_live_orders(db)
    _sync(db)
    db.refresh(row)
    py = kss._to_pyramid(row)
    tp = _resting_tp(db)
    if tp is None or not tp.exchange_order_id:
        raise SystemExit(f" {BAD} the TP did not come back after the average moved")
    new_id, new_price = str(tp.exchange_order_id), _venue_price(ex, tp)
    say(OK, f"avg {py_before.avg_price:g} -> {py.avg_price:g}; TP {old_price:g} -> {new_price:g}, "
            f"exchange order {old_id} -> {new_id}")
    if py.avg_price >= py_before.avg_price:
        say(WARN, "the average did not drop — wave 1 did not book a lower fill")
    elif new_price >= old_price:
        say(BAD, "the average dropped but the TP did not follow it down")
    elif new_id == old_id:
        say(BAD, "the TP price changed but the exchange order did not — the venue still holds "
                 "the old exit")
    elif old_id in _open_ids(ex):
        say(BAD, f"the replaced TP {old_id} is STILL open on the venue")
    else:
        say(OK, f"the old exit {old_id} is off the book and {new_id} rests at the new target")
    return tp


def _stop(db, ex, row, tp) -> None:
    print("\n8. Stop the session — the exit must come off the book")
    tp_id = str(tp.exchange_order_id) if tp is not None else None
    kss.stop_session(db, row.id, reason="testnet e2e complete")
    _sync(db)
    still_open = _open_ids(ex)
    if tp_id and tp_id in still_open:
        say(BAD, f"TP {tp_id} is still resting after the session stopped")
    else:
        say(OK, f"TP {tp_id} is off the book; open orders left on {PAIR}: {len(still_open)}")


def _cleanup(db, ex) -> None:
    print("\n9. Cleanup")
    left = [o for o in db.query(models.PendingOrder)
            .filter(models.PendingOrder.exchange_order_id.isnot(None)).all()
            if str(o.exchange_order_id) in _open_ids(ex)]
    for order in left:
        try:
            execution.cancel_live_order(live_provider().pair(order.symbol), order.exchange_order_id)
            say(OK, f"cancelled {order.exchange_order_id}")
        except Exception as exc:  # never leave a live order behind silently
            say(BAD, f"CANCEL FAILED for {order.exchange_order_id}: {type(exc).__name__} {exc} "
                     "— cancel it by hand on testnet.binance.vision")
    if not left:
        say(OK, "nothing of ours left on the book")


def main() -> int:
    print(f"Testnet KSS session e2e | {PAIR} | wave0=${args.first_wave_usd:g} | "
          f"step={args.distance_pct:g}% | tp={args.tp_pct:g}%")
    print("\n1. Posture")
    require_testnet(execution, settings)
    say(OK, f"live on {settings.live_exchange} TESTNET, maker={settings.maker_orders}, "
            f"cap ${settings.live_max_order_notional:.2f}/BUY")
    say(OK, f"throwaway database {args.db} (paper and live books untouched)")

    init_db()  # the app's own bootstrap: KSS *and* the OPUS tables the fill hook reads
    db = SessionLocal()
    ex = execution._client()
    ex.load_markets()  # ccxt's .market() raises until something has loaded them
    try:
        row, order = _start(db, ex)
        has_remainder = _partial(db, ex, row, order)
        tp = _tp_rests(db, ex)
        if has_remainder:
            tp = _tp_follows_size(db, ex, row, tp, order)
        else:
            print("\n6. (skipped — wave 0 filled whole, so there is no remainder to grow into)")
        tp = _tp_follows_avg(db, ex, row, tp)
        _stop(db, ex, row, tp)
    except Exception as exc:
        print(f"\n {BAD} {type(exc).__name__}: {exc}")
        return 1
    finally:
        _cleanup(db, ex)
        db.close()

    print("\nDONE — a real KSS session ran on testnet with its take-profit resting in advance.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
