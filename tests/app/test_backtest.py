"""Deterministic backtest tests on synthetic candles (no network)."""

from app.backtest import estimate_win_rate, simulate_kss

_DAY = 86_400_000


def candle(day, close, high=None, low=None):
    return {"ts": day * _DAY, "open": close, "high": high or close,
            "low": low if low is not None else close, "close": close, "volume": 1.0}


def test_simulate_tp_hit():
    candles = [candle(0, 100.0), candle(1, 104.0, high=104.0, low=100.0)]
    r = simulate_kss(candles, 0, distance_pct=2, max_waves=5, tp_pct=3, deadline_days=30)
    assert r.tp_hit is True
    assert r.days_to_tp == 1.0
    assert r.pnl_pct == 3.0


def test_simulate_deadline_miss():
    # Entry 100, then 40 days oscillating 95-99 — never returns to avg*1.03.
    candles = [candle(0, 100.0, high=100.0, low=100.0)]
    candles += [candle(d, 97.0, high=99.0, low=95.0) for d in range(1, 41)]
    r = simulate_kss(candles, 0, distance_pct=2, max_waves=5, tp_pct=3, deadline_days=30)
    assert r.tp_hit is False
    assert r.hit_deadline is True
    assert r.waves_filled >= 2  # deeper waves filled as price dipped


def test_estimate_win_rate_all_win():
    # +1%/day uptrend: every completed entry reaches +3% well within 30 days.
    candles = []
    price = 100.0
    for d in range(40):
        candles.append(candle(d, price, high=price, low=price * 0.999))
        price *= 1.01
    res = estimate_win_rate(candles, distance_pct=2, max_waves=5, tp_pct=3, deadline_days=30)
    assert res["trials"] > 0
    assert res["win_rate"] == 100.0
    assert res["avg_days_to_tp"] is not None


def test_simulate_stop_loss_hit():
    # Entry 100, next bar craters (low 80) — hard stop at avg×0.87 fires → realized loss.
    candles = [candle(0, 100.0), candle(1, 82.0, high=85.0, low=80.0)]
    r = simulate_kss(candles, 0, distance_pct=2, max_waves=5, tp_pct=3, deadline_days=30,
                     sl_pct=13, cost_pct=0.3)
    assert r.tp_hit is False and r.stopped is True
    assert r.pnl_pct == round(-13 - 0.3, 4)


def test_stop_loss_turns_a_recovering_dip_into_a_loss():
    # A deep dip (would hit SL) that later recovers above TP: the realism fix.
    candles = [candle(0, 100.0),
               candle(1, 90.0, high=101.0, low=80.0),   # same bar touches deep low AND >TP
               candle(2, 110.0, high=110.0, low=108.0)]
    no_sl = simulate_kss(candles, 0, 2, 5, 3, 30, sl_pct=0, cost_pct=0)
    assert no_sl.tp_hit is True              # legacy: rode the dip all the way to TP
    with_sl = simulate_kss(candles, 0, 2, 5, 3, 30, sl_pct=13, cost_pct=0)
    assert with_sl.tp_hit is False and with_sl.stopped is True  # SL cut it first → loss


def test_estimate_win_rate_reports_expectancy_and_wilson_lb():
    candles = []
    price = 100.0
    for d in range(40):
        candles.append(candle(d, price, high=price, low=price * 0.999))
        price *= 1.01
    res = estimate_win_rate(candles, distance_pct=2, max_waves=5, tp_pct=3, deadline_days=30,
                            cost_pct=0.3)
    assert "expectancy" in res and "win_rate_lb" in res and "trials" in res
    assert res["win_rate_lb"] <= res["win_rate"]          # lower bound never above point est
    assert res["expectancy"] == round(3 - 0.3, 4)         # all wins, net of cost


def test_estimate_win_rate_all_loss():
    # -1%/day decline over 60 days: entries hit the 30-day deadline without TP.
    candles = []
    price = 100.0
    for d in range(60):
        candles.append(candle(d, price, high=price, low=price * 0.999))
        price *= 0.99
    res = estimate_win_rate(candles, distance_pct=2, max_waves=5, tp_pct=3, deadline_days=30)
    assert res["trials"] > 0
    assert res["win_rate"] == 0.0
