"""Deterministic backtest tests on synthetic candles (no network)."""

from app.backtest import estimate_win_rate, simulate_kss, _fill_price

_DAY = 86_400_000


def candle(day, close, high=None, low=None, open_=None):
    o = open_ if open_ is not None else close
    return {"ts": day * _DAY, "open": o, "high": high or close,
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
    # Entry 100, next bar opens ABOVE wave-1 target (open=99 > target=98) so no gap-fill
    # improvement, then bar craters (low=80).  Wave 1 fills at target=98;
    # avg=(100*1+98*2+...all waves fill from low 80)/weights.
    # With open=99: waves fill at min(target, 99) = target for waves 1-4 (all targets < 99).
    # avg(all 5 waves) = (100+98*2+96.04*3+94.12*4+92.24*5)/(1+2+3+4+5)=95.6/15...
    # SL at 13% fires because bar low=80 is far below any avg.
    candles = [candle(0, 100.0), candle(1, 82.0, high=99.0, low=80.0, open_=99.0)]
    r = simulate_kss(candles, 0, distance_pct=2, max_waves=5, tp_pct=3, deadline_days=30,
                     sl_pct=13, cost_pct=0.3)
    assert r.tp_hit is False and r.stopped is True
    assert r.pnl_pct == round(-13 - 0.3, 4)


def test_stop_loss_turns_a_recovering_dip_into_a_loss():
    # Bar 1 opens at 99 (above wave-1 target 98 → no gap-fill), trades deep (low=80 → SL
    # fires) but also trades high (high=105 → TP would have fired without SL).
    # Without SL: wave0 avg=100, high=105>103 → TP on bar 1.
    # With SL=13%: wave fills at target prices (open=99 > all targets), avg rises, SL line
    # stays above 80 → SL cuts the trade before TP.
    candles = [candle(0, 100.0),
               candle(1, 90.0, high=105.0, low=80.0, open_=99.0),
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


# ----- B3 tests: 3-way outcome classification -----

def test_b3_deadline_exit_positive_pnl_is_flat_not_loss():
    """B3: a deadline exit with pnl >= 0 must be classified as flat, not loss."""
    # Entry=100.  Price sits at 102 for 35 days (above avg=100, below TP=103).
    # Deadline fires at day 30: close=102, pnl=(102-100)/100=2% > 0 → flat.
    candles = [candle(0, 100.0)]
    for d in range(1, 35):
        candles.append(candle(d, 102.0, high=102.0, low=99.0))
    r = simulate_kss(candles, 0, distance_pct=2, max_waves=5, tp_pct=3, deadline_days=30)
    assert r.hit_deadline is True
    assert r.pnl_pct > 0, "deadline exit pnl should be positive in this fixture"

    # estimate_win_rate should count it as a flat, not a loss.
    res = estimate_win_rate(
        [candle(0, 100.0)] + [candle(d, 102.0, high=102.0, low=99.0) for d in range(1, 35)],
        distance_pct=2, max_waves=5, tp_pct=3, deadline_days=30,
    )
    assert res["flats"] >= 1, "at least one flat expected"
    assert res["losses"] == 0 or res["flats"] > 0, "profitable deadline must not be a loss"
    # When every trial exits flat, loss_rate=0 and flat_rate>0.
    if res["trials"] > 0 and res["wins"] == 0:
        assert res["loss_rate"] == 0.0 or res["flat_rate"] > 0.0


def test_b3_rates_sum_to_100():
    """B3 acceptance: win_rate + loss_rate + flat_rate == 100 on a mixed fixture."""
    # Build a fixture that produces all three outcome types:
    #   - bar-sequence A: TP hit quickly  (win)
    #   - bar-sequence B: steady decline  (loss at deadline)
    #   - bar-sequence C: mild rise, deadline (flat)
    # Concatenate 3 × 35-bar segments so walk-forward produces at least one of each.
    bars = []
    day = 0

    # Segment 1: strong uptrend → TP wins
    price = 100.0
    for _ in range(35):
        bars.append(candle(day, price, high=price * 1.005, low=price * 0.999))
        price *= 1.005
        day += 1

    # Segment 2: steady decline → deadline losses
    price = bars[-1]["close"]
    for _ in range(35):
        bars.append(candle(day, price, high=price * 1.001, low=price * 0.997))
        price *= 0.995
        day += 1

    # Segment 3: mild drift above entry so late entries exit flat at deadline
    price = bars[-1]["close"]
    for _ in range(35):
        bars.append(candle(day, price, high=price * 1.001, low=price * 0.999))
        price *= 1.001
        day += 1

    res = estimate_win_rate(bars, distance_pct=2, max_waves=5, tp_pct=3, deadline_days=30)
    assert res["trials"] > 0
    total = res["wins"] + res["losses"] + res["flats"]
    assert total == res["trials"], f"wins+losses+flats={total} != trials={res['trials']}"
    rate_sum = round(res["win_rate"] + res["loss_rate"] + res["flat_rate"], 1)
    assert rate_sum == 100.0, f"win+loss+flat rates = {rate_sum}, expected 100"


def test_b3_new_keys_present():
    """B3: estimate_win_rate must always return flats and flat_rate keys."""
    candles = [candle(d, 100.0) for d in range(5)]
    res = estimate_win_rate(candles, distance_pct=2, max_waves=3, tp_pct=3, deadline_days=30)
    assert "flats" in res
    assert "flat_rate" in res


# ----- B4 tests: gap-below fill price -----

def test_b4_fill_price_helper_gap_below():
    """B4: _fill_price returns bar open when it is below the target (gap-down)."""
    assert _fill_price(98.0, 95.0) == 95.0   # open < target → fill at open
    assert _fill_price(98.0, 100.0) == 98.0  # open > target → fill at target (no gap)
    assert _fill_price(98.0, 98.0) == 98.0   # open == target → fill at target


def test_b4_gap_below_fills_at_open_not_target():
    """B4: when a bar opens below a wave's target, the simulated fill is at the open."""
    # Entry 100, wave-1 target = 100*(1-0.02)=98.
    # Bar 1: open=95 (below 98 → gap-fill at 95), low=93, high=95.5.
    # Old (pre-B4): fill wave 1 at 98 → avg=(100*1+98*2)/3=98.67
    # New (B4):    fill wave 1 at 95 → avg=(100*1+95*2)/3=96.67
    # Both: waves 2+ fill at min(target, 95). After simulation, avg is lower with B4.
    c = [candle(0, 100.0), candle(1, 95.0, high=95.5, low=93.0, open_=95.0)]
    r = simulate_kss(c, 0, distance_pct=2, max_waves=3, tp_pct=3, deadline_days=30)
    # With gap fill, avg is pushed down (better entry); TP threshold is lower.
    # Verify the sim runs without error and filled >1 wave.
    assert r.waves_filled >= 2

    # Directly verify the fill price logic: bar opens at 95 < target 98 → fill at 95.
    # If bar opened at 99 (above target), fill would be at 98.
    # The two sims differ only in bar_open; gap-fill gives a lower (better) avg.
    # We cannot inspect avg directly, but we can verify: with a subsequent TP bar,
    # the gap-fill version hits TP at a lower price.
    # Build: entry 100, gap bar (2 versions), then a recovery bar at 96.7.
    # Gap-fill avg ≈ 96, TP ≈ 98.88; no-gap avg ≈ 98.67, TP ≈ 101.63.
    recovery_bar = candle(2, 99.0, high=99.0, low=96.0)
    c_gap_tp = [candle(0, 100.0), candle(1, 95.0, high=95.5, low=93.0, open_=95.0), recovery_bar]
    c_nogap_tp = [candle(0, 100.0), candle(1, 95.0, high=95.5, low=93.0, open_=99.0), recovery_bar]
    r_gap_tp = simulate_kss(c_gap_tp, 0, distance_pct=2, max_waves=3, tp_pct=3, deadline_days=30)
    r_nogap_tp = simulate_kss(c_nogap_tp, 0, distance_pct=2, max_waves=3, tp_pct=3, deadline_days=30)
    # Gap-fill lowers avg so TP fires at a lower market price → more likely TP on bar 2.
    assert r_gap_tp.tp_hit is True, "gap-fill avg is lower → TP at 99 should fire"
    assert r_nogap_tp.tp_hit is False, "no-gap avg is higher → TP at 99 should not fire"
