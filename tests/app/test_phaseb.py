"""Phase B: cost engine, walk-forward/loss-rate backtest, cost+loss decision gates."""

import pytest

from app import costengine
from app.agents import decide
from app.backtest import estimate_win_rate

_DAY = 86_400_000


def _series(changes, start=100.0):
    out, p = [], start
    for d, ch in enumerate(changes):
        p *= (1 + ch)
        out.append({"ts": d * _DAY, "open": p, "high": p, "low": p * 0.999,
                    "close": p, "volume": 1.0})
    return out


def test_cost_and_net_edge():
    # defaults: taker 0.1%, slippage 0.05% -> round trip 0.3%
    assert costengine.round_trip_cost_pct() == pytest.approx(0.3)
    assert costengine.net_edge_pct(3.0) == pytest.approx(2.7)
    assert costengine.covers_costs(3.0) is True
    assert costengine.covers_costs(0.3) is False  # net 0.0 < min_net_edge 0.5
    assert costengine.notional_ok(1000) is True and costengine.notional_ok(5) is False


def test_loss_rate_reported():
    loss = estimate_win_rate(_series([-0.01] * 60), 2, 5, 3, 30)
    assert loss["loss_rate"] == 100.0 and loss["win_rate"] == 0.0
    win = estimate_win_rate(_series([0.01] * 40), 2, 5, 3, 30)
    assert win["loss_rate"] == 0.0


def test_walk_forward_split_reduces_trials():
    candles = _series([0.01] * 40)
    full = estimate_win_rate(candles, 2, 5, 3, 30, split=0.0)
    oos = estimate_win_rate(candles, 2, 5, 3, 30, split=0.8)
    assert 0 < oos["trials"] < full["trials"]


def test_decide_cost_and_loss_gates():
    base = {"min_confidence": 70, "min_win_rate": 80, "deadline_days": 30,
            "max_loss_rate": 20, "min_net_edge": 0.5}
    assert decide(85, 90, 5, loss_rate=10, net_edge=2.7, **base)["decision"] == "trade"
    assert decide(85, 90, 5, loss_rate=10, net_edge=0.2, **base)["decision"] == "skip"  # cost
    assert decide(85, 90, 5, loss_rate=30, net_edge=2.7, **base)["decision"] == "skip"  # loss
