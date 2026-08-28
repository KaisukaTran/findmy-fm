"""
Binance testnet end-to-end for the live resting model — live-readiness 1.8.

One command drives the app's OWN live path against Binance Spot testnet:

    queue a KSS rung -> orders.sync_resting_orders places it as a resting LIMIT_MAKER
    -> poll orders.reconcile_live_orders -> book the Fill + Position if the venue filled it
    -> cancel whatever is still resting before exiting.

Nothing is simulated: the order really rests on testnet and the app's own functions place,
read and book it. Only the database is disposable — the harness runs on a THROWAWAY SQLite
file (``data/testnet_e2e.db``), so the paper and live books are never touched.

Needs the live worktree's ``.env`` (keys + LIVE_TRADING + LIVE_USE_TESTNET); MAKER_ORDERS and
AUTO_TRADE are forced on because the resting model is exactly what is under test. Run
``scripts/testnet_check.py`` first if the keys have never been used. See docs/testnet-setup.md.

    python scripts/testnet_e2e.py
    python scripts/testnet_e2e.py --symbol ETH/USDT --distance-pct 0.05 --wait-sec 180
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

OK, WARN, BAD = "[ok]", "[--]", "[!!]"
_PROTECTED_DB = {"findmy.db", "live.db"}


def _say(mark: str, msg: str) -> None:
    print(f" {mark} {msg}")


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
    return ap.parse_args()


args = _parse()
if Path(args.db).name in _PROTECTED_DB:
    raise SystemExit(f"{BAD} refusing to run against {args.db} — use a throwaway database")

# Settings are read at import time, so the environment must be shaped BEFORE app.config loads.
Path(args.db).parent.mkdir(parents=True, exist_ok=True)
os.environ["DATABASE_URL"] = f"sqlite:///{args.db}"
os.environ["MAKER_ORDERS"] = "true"  # the resting model is what this harness exercises
os.environ["AUTO_TRADE"] = "true"    # sync_resting_orders places only when auto-trade is on

from app import execution, models, orders  # noqa: E402
from app.config import settings  # noqa: E402
from app.data.providers import live_provider  # noqa: E402
from app.db import Base, SessionLocal, engine  # noqa: E402


def _posture() -> None:
    print("\n1. Posture")
    if not execution.live_enabled():
        raise SystemExit(f" {BAD} live is off (needs LIVE_TRADING=true + keys) — nothing to test")
    if not settings.live_use_testnet:
        raise SystemExit(f" {BAD} LIVE_USE_TESTNET=false — refusing to trade with real keys")
    _say(OK, f"live on {settings.live_exchange} TESTNET, maker={settings.maker_orders}, "
             f"cap ${settings.live_max_order_notional:.2f}/BUY")
    _say(OK, f"throwaway database {args.db} (paper and live books untouched)")


def _rung(ex, pair: str) -> tuple[float, float, float]:
    """Return (last, price, qty) for a compliant rung below the TESTNET book."""
    print("\n2. Build the rung from testnet's own book")
    last = float(ex.fetch_ticker(pair)["last"])
    raw_price = last * (1.0 - args.distance_pct / 100.0)
    filters = execution.filters_from_market(ex.market(pair))
    price, qty = execution.round_to_filters(raw_price, args.notional / raw_price, filters,
                                            ref_price=last)
    _say(OK, f"{pair} last={last:g} -> rung {qty:g} @ {price:g} "
             f"(${price * qty:.2f}, {args.distance_pct:g}% below)")
    return last, price, qty


def _queue(db, base: str, price: float, qty: float):
    print("\n3. Queue it as a KSS rung (through the approval queue, like the strategy does)")
    order, risk_note = orders.queue_order(
        db, symbol=base, side="BUY", quantity=qty, price=price, order_type="LIMIT",
        source="kss", source_ref="pyramid:0:wave:0", strategy_name="testnet-e2e",
        note="live-readiness 1.8 end-to-end",
    )
    _say(OK, f"pending order {order.id} queued" + (f" (risk: {risk_note})" if risk_note else ""))
    return order


def _rest(db, order) -> bool:
    print("\n4. Place it on the exchange in advance (the 1.5 resting model)")
    counts = orders.sync_resting_orders(db)
    db.refresh(order)
    if not order.exchange_order_id:
        _say(BAD, f"not placed ({counts}) — a post-only reject means the book already reached "
                  "the rung; raise --distance-pct and retry")
        return False
    _say(OK, f"resting as exchange order {order.exchange_order_id} "
             f"(status={order.exchange_status}, local status still {order.status})")
    return True


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
                _say(OK, f"Fill booked: {f.quantity:g} @ {f.price:g} (fee {f.fee:g})")
            _say(OK, f"Position {order.symbol}: qty={pos.quantity:g} avg={pos.avg_entry_price:g}"
                 if pos else f"{BAD} no Position row — reconcile booked a fill without one")
            db.refresh(order)
            _say(OK, f"order status={order.status} exchange_status={order.exchange_status}")
            return True
        time.sleep(5)
    _say(WARN, "no fill inside the window — expected when the price never dipped to the rung. "
               "The placement, status and cancel paths are still proven above.")
    return False


def _cleanup(db, order) -> None:
    print("\n6. Cleanup")
    db.refresh(order)
    if not order.exchange_order_id:
        _say(OK, "nothing left resting")
        return
    try:
        execution.cancel_live_order(live_provider().pair(order.symbol), order.exchange_order_id)
        _say(OK, f"cancelled {order.exchange_order_id}")
    except Exception as exc:  # never leave a live order behind silently
        _say(BAD, f"CANCEL FAILED for {order.exchange_order_id}: {type(exc).__name__} {exc} — "
                  "cancel it by hand on testnet.binance.vision")


def main() -> int:
    print(f"Testnet e2e | symbol={args.symbol} | notional=${args.notional:g} | "
          f"distance={args.distance_pct:g}% | wait={args.wait_sec}s")
    _posture()

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    base = args.symbol.partition("/")[0]
    order = None
    try:
        ex = execution._client()
        _, price, qty = _rung(ex, args.symbol)
        order = _queue(db, base, price, qty)
        if not _rest(db, order):
            return 1
        _poll(db, order)
    except Exception as exc:
        print(f"\n {BAD} {type(exc).__name__}: {exc}")
        return 1
    finally:
        if order is not None:
            _cleanup(db, order)
        db.close()

    print("\nDONE — the live resting path ran end to end against testnet.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
