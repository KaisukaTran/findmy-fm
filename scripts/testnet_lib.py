"""Shared pieces of the Binance-testnet harnesses (live-readiness 1.8).

Both harnesses (``testnet_e2e.py``, ``testnet_session_e2e.py``) drive the app's OWN live
resting path against Binance Spot testnet on a throwaway database. What they share lives
here: shaping the environment before ``app.config`` is imported, refusing to run anywhere
but testnet, and supplying the counter side of a trade so the venue actually fills a
resting rung.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

OK, WARN, BAD = "[ok]", "[--]", "[!!]"

# The paper and live books. A harness wipes its database on every run, so pointing one at
# either of these would destroy a real book — refuse by name, not by convention. Compared
# case-INSENSITIVELY: Windows opens `data/Live.db` as the very same file, so a name-cased
# argument would otherwise walk straight past this guard and delete the live book.
PROTECTED_DB = {"findmy.db", "live.db"}


def say(mark: str, msg: str) -> None:
    print(f" {mark} {msg}")


def prepare_env(db_path: str, **extra: str) -> None:
    """Shape the environment for a harness run — must run BEFORE ``app.config`` is imported.

    Starts from an empty book every run: a previous run that died mid-way leaves its rung
    queued, and the next run would rest that one too and only clean up its own.
    """
    if Path(db_path).name.lower() in PROTECTED_DB:
        raise SystemExit(f"{BAD} refusing to run against {db_path} — use a throwaway database")
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    Path(db_path).unlink(missing_ok=True)
    os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"
    os.environ["MAKER_ORDERS"] = "true"  # the resting model is what these harnesses exercise
    os.environ["AUTO_TRADE"] = "true"    # sync_resting_orders places only when auto-trade is on
    os.environ.update(extra)


def require_testnet(execution: Any, settings: Any) -> None:
    """Refuse to trade unless this is the live path pointed at TESTNET."""
    if not execution.live_enabled():
        raise SystemExit(f" {BAD} live is off (needs LIVE_TRADING=true + keys) — nothing to test")
    if not settings.live_use_testnet:
        raise SystemExit(f" {BAD} LIVE_USE_TESTNET=false — refusing to trade with real keys")


def bid_queue_above(ex: Any, pair: str, price: float, depth: int = 50) -> float:
    """Total base quantity bid at or above *price* — the queue the venue must consume
    before it reaches a rung resting there (price-time priority)."""
    book = ex.fetch_order_book(pair, depth)
    return sum(qty for lvl_price, qty in book["bids"] if lvl_price >= price - 1e-12)


def cross_fill(
    ex: Any, pair: str, price: float, *, qty: float | None = None,
    max_cross_usd: float = 60.0, depth: int = 50,
) -> dict:
    """Make the venue fill OUR resting BUY rung by supplying the counter side.

    A testnet book is simulated and deep: a passive rung below the touch is never reached,
    so waiting cannot exercise the fill leg (two runs, 90s and 300s, produced no fill even
    0.03% below the last price). This sells INTO our own rung from the same testnet account
    — the match is still Binance's, against the real resting order the app placed; only the
    liquidity on the other side is ours.

    Sells the whole bid queue at or above *price*, so anything resting ahead of our rung is
    consumed and the venue reaches it. IOC so no part of the counter order is left on the
    book, and ``selfTradePreventionMode=NONE`` because both sides are this account (the
    account default, EXPIRE_MAKER, would expire our rung instead of filling it — the taker
    order's mode decides).

    *qty* sells only that much — a PARTIAL fill of a rung we are alone at that price with
    (the caller checks that; the queue is still read so a rung that is not on the book at all
    fails loudly rather than dumping into someone else's bid).

    Raises when the queue is deeper than *max_cross_usd*: crossing it would mean dumping an
    unbounded amount into the book, which is never what a test wants.
    """
    resting = bid_queue_above(ex, pair, price, depth)
    if resting <= 0:
        raise RuntimeError(f"nothing bid at or above {price:g} — the rung is not on the book")
    qty = resting if qty is None else qty
    notional = qty * price
    if notional > max_cross_usd:
        raise RuntimeError(
            f"bid queue at/above {price:g} is {qty:g} (${notional:,.2f}) — deeper than the "
            f"${max_cross_usd:,.2f} cross cap; rest the rung where it is the best bid instead"
        )
    res = ex.create_order(
        pair, "limit", "sell", qty, price,
        {"timeInForce": "IOC", "selfTradePreventionMode": "NONE"},
    )
    return {"quantity": qty, "notional": notional, "filled": float(res.get("filled") or 0.0),
            "id": res.get("id"), "status": res.get("status")}
