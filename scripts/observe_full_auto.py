"""
Offline full-auto observation harness (paper, no network).

Why: the machine can't reach Binance (ccxt NetworkError), so a live scan idles.
This drives the *real* scheduler cycle against a deterministic synthetic provider
so we can watch the whole full-auto pipeline end-to-end:

    scan -> candidate -> KSS session -> auto-approve (auto-trader + policy)
          -> auto-fill -> guardian veto -> hyperopt/ML retrain -> breaker freeze

It mutates nothing real: a throwaway SQLite DB under the system temp dir.

Run:  python scripts/observe_full_auto.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# --- point the app at a throwaway paper DB BEFORE importing any app module ----
_TMP = tempfile.mkdtemp(prefix="findmy_observe_")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/observe.db"
os.environ["REQUIRE_AUTH"] = "false"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import market, orders, runtime, scanner  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import Base, SessionLocal, engine  # noqa: E402
from app import models  # noqa: E402,F401  (register tables on Base)

_DAY = 86_400_000


# --- synthetic offline market (mirrors tests/app _FakeProvider shape) --------
def _uptrend(n=80, start=100.0, vol=2e6):
    out, price = [], start
    for d in range(n):
        out.append({"ts": d * _DAY, "open": price, "high": price * 1.01,
                    "low": price * 0.99, "close": price, "volume": vol})
        price *= 1.012
    return out


_UNIVERSE = {"BTC": _uptrend(start=60000.0), "ETH": _uptrend(start=3000.0),
             "SOL": _uptrend(start=150.0)}


class _FakeProvider:
    def get_ohlcv(self, symbol, timeframe="1d", limit=200):
        return _UNIVERSE.get(symbol, [])

    def top_symbols(self, n=10):
        return []

    def all_symbols(self, min_quote_volume=0.0):
        return list(_UNIVERSE)

    def get_prices(self, symbols):
        return {s: _UNIVERSE[s][-1]["close"] for s in symbols if s in _UNIVERSE}

    def get_exchange_info(self, symbol):
        return {"minQty": 0.00001, "stepSize": 0.00001, "maxQty": 1e6}


def _fake_prices(symbols):
    return {s: _UNIVERSE[s][-1]["close"] for s in symbols if s in _UNIVERSE}


def _patch_offline():
    """Replace every network seam with the synthetic universe."""
    scanner.data_provider = lambda: _FakeProvider()
    # get_current_prices is imported by-name into orders, so patch both bindings.
    market.get_current_prices = _fake_prices
    orders.get_current_prices = _fake_prices


def _enable_full_auto():
    """Flip the safe-by-default toggles ON for the observation (paper only)."""
    settings.full_auto = True
    settings.auto_trade = True              # auto mode: sessions self-approve wave 0
    settings.autoapprove_enabled = True     # policy approves remaining KSS orders
    settings.autoapprove_sources = ["kss"]
    settings.guardian_enabled = False       # no API key / no network -> keep off
    settings.hyperopt_enabled = True
    settings.ml_enabled = True
    settings.ml_min_samples = 1
    settings.hyperopt_interval_hours = 0    # force a retrain every cycle
    settings.ml_retrain_hours = 0
    # make the gate permissive so the synthetic uptrend yields trades
    settings.watchlist = list(_UNIVERSE)
    settings.scan_top_n = 0
    settings.min_confidence = 0.0
    settings.min_win_rate = 0.0
    settings.max_loss_rate = 100.0
    settings.min_net_edge = -100.0


def _fmt(s: dict) -> str:
    L = lambda v: len(v) if isinstance(v, list) else v  # noqa: E731
    return (f"frozen={str(s['frozen']):5}  "
            f"auto_approved={L(s['auto_approved']):>2}  "
            f"auto_filled={L(s['auto_filled']):>2}  "
            f"tp_queued={L(s['tp_queued']):>2}  "
            f"deadlines_closed={L(s['deadlines_closed']):>2}  "
            f"guardian_vetoes={s['guardian_vetoes']:>2}  "
            f"hyperopt_runs={s['hyperopt_runs']:>2}  "
            f"ml_trained={str(s['ml_trained'])}")


def main() -> None:
    Base.metadata.create_all(bind=engine)
    _patch_offline()
    _enable_full_auto()

    from app import scheduler  # imported after patching so it sees the seams

    db = SessionLocal()
    try:
        runtime.full_auto_on(db)
        db.commit()
        print(f"DB: {os.environ['DATABASE_URL']}")
        print(f"Universe: {list(_UNIVERSE)}  |  full_auto ON, guardian OFF (no key)\n")

        print("== Normal cycles (breaker armed) ==")
        for i in range(1, 4):
            s = scheduler.run_cycle(db)
            print(f"cycle {i}: {_fmt(s)}")

        active = db.query(models.KssSession).filter(
            models.KssSession.status == "active").count()
        pend = db.query(models.PendingOrder).filter(
            models.PendingOrder.status == "pending").count()
        print(f"\nstate: active KSS sessions={active}, pending orders={pend}")

        print("\n== Breaker FROZEN (manual trip) -> auto must halt ==")
        runtime.freeze(db, "observe: manual trip")
        db.commit()
        s = scheduler.run_cycle(db)
        print(f"frozen cycle: {_fmt(s)}")
        assert s["frozen"] and not s["auto_approved"], "auto-approve must be 0 when frozen"

        print("\n== Breaker reset -> auto resumes ==")
        runtime.unfreeze(db)
        db.commit()
        s = scheduler.run_cycle(db)
        print(f"resumed cycle: {_fmt(s)}")

        print("\nOK: full-auto pipeline observed; breaker gates auto-approval as designed.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
