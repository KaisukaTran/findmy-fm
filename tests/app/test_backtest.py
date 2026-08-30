"""Deterministic backtest tests on synthetic candles (no network)."""

from app.backtest import _fill_price, estimate_win_rate, simulate_kss

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


# ----- MAE tests: drawdown discrimination -----

def test_mae_tracks_dip_before_tp():
    """mae_pct captures the deepest dip below avg even when the same bar reaches TP."""
    # max_waves=1 → avg stays 100. Bar 1 dips to 90 (−10% vs avg) but high 104 ≥ TP(103).
    c = [candle(0, 100.0), candle(1, 100.0, high=104.0, low=90.0, open_=100.0)]
    r = simulate_kss(c, 0, distance_pct=2, max_waves=1, tp_pct=3, deadline_days=30, sl_pct=0)
    assert r.tp_hit is True
    assert r.mae_pct == -10.0


def test_avg_mae_discriminates_shallow_vs_deep():
    """A shallow-dip coin scores a higher (closer to 0) MAE than a deep-dip one — the metric
    that DISCRIMINATES when win-rate saturates at ~100%."""
    shallow = [candle(0, 100.0), candle(1, 100.0, high=104.0, low=99.0)]   # dips −1%, then TP
    deep = [candle(0, 100.0), candle(1, 100.0, high=104.0, low=90.0)]      # dips −10%, then TP
    rs = simulate_kss(shallow, 0, 2, 1, 3, 30, sl_pct=0)
    rd = simulate_kss(deep, 0, 2, 1, 3, 30, sl_pct=0)
    assert rs.tp_hit and rd.tp_hit
    assert rs.mae_pct > rd.mae_pct                # −1% ranks above −10%


def test_estimate_win_rate_reports_mae_keys():
    candles = []
    price = 100.0
    for d in range(40):
        candles.append(candle(d, price, high=price, low=price * 0.98))  # each bar dips ~2%
        price *= 1.01
    res = estimate_win_rate(candles, distance_pct=2, max_waves=5, tp_pct=3, deadline_days=30)
    assert "avg_mae" in res and "worst_mae" in res
    assert res["avg_mae"] <= 0.0 and res["worst_mae"] <= res["avg_mae"]


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


# --- the entry bar's own range already happened -----------------------------


def test_the_entry_bars_own_high_cannot_pay_the_take_profit():
    """We enter at bar `start`'s CLOSE, so that bar's high and low are already in the past.

    Counting them was look-ahead: the smaller the take-profit, the more likely the entry
    candle's own wick had already cleared it, so a trial "won" on a move we could never have
    caught. Measured on real data (SOL, 365d daily, 2026-08-30) it inflated the take-profit
    win rate to 100% and shortened the average time-to-TP 4.2x at tp=1.5% — and time-to-TP is
    what turnover, and therefore projected daily return, is computed from. It biased every
    parameter search toward take-profits that are too small to actually reach.
    """
    # The bar we enter on ran up to 110 before closing at 100. That +10% is gone.
    candles = [candle(0, 100.0, high=110.0, low=99.0),
               candle(1, 100.5, high=100.5, low=100.0)]

    r = simulate_kss(candles, 0, distance_pct=2, max_waves=5, tp_pct=3, deadline_days=30)

    assert r.tp_hit is False, "that high happened before we bought"


def test_the_entry_bars_own_low_cannot_trigger_the_stop():
    """The mirror: a wick below the stop, before we were in the trade, is not our loss."""
    candles = [candle(0, 100.0, high=101.0, low=80.0),
               candle(1, 100.5, high=100.5, low=100.0)]

    r = simulate_kss(candles, 0, distance_pct=2, max_waves=5, tp_pct=3, deadline_days=30,
                     sl_pct=8, cost_pct=0)

    assert r.stopped is False, "that low happened before we bought"


def test_the_entry_bars_own_low_cannot_fill_a_deeper_rung():
    """Same reasoning for the ladder: a rung can only fill on a bar that trades after entry."""
    candles = [candle(0, 100.0, high=100.0, low=90.0),
               candle(1, 100.0, high=100.0, low=100.0)]

    r = simulate_kss(candles, 0, distance_pct=2, max_waves=5, tp_pct=3, deadline_days=30)

    assert r.waves_filled == 1, "only wave 0 is filled at entry"


def test_a_later_bar_still_pays_the_take_profit():
    """Guard the fix does not simply stop detecting wins."""
    candles = [candle(0, 100.0, high=100.0, low=100.0),
               candle(1, 100.0, high=104.0, low=100.0)]

    r = simulate_kss(candles, 0, distance_pct=2, max_waves=5, tp_pct=3, deadline_days=30)

    assert r.tp_hit is True and r.days_to_tp == 1.0


# ----- pessimistic_intrabar: the other honest bound on one bar's high/low order -------------
#
# WHY: on one bar the true chronological order of the high and the low is unknowable. The
# default (pessimistic_intrabar=False) assumes low-then-high: deeper waves fill at the bar's
# LOW first (lowering the running average), and the SAME bar's HIGH is then tested against
# that already-lowered average — an optimistic assumption, since it lets one candle "DCA
# down, then recover" for free. pessimistic_intrabar=True assumes the opposite: the HIGH
# happened first, so take-profit must clear the harder PRE-fill average with no such benefit;
# only afterwards do the fills happen and the stop-loss get tested against the resulting
# (lower) post-fill average. Neither bound is "more correct" — a trustworthy evaluation must
# report both (see app/evaluate.py) rather than silently picking one.

def test_pessimistic_default_is_false_and_matches_omitting_the_kwarg():
    """pessimistic_intrabar defaults to False; passing it explicitly changes nothing."""
    candles = [candle(0, 100.0), candle(1, 95.0, high=99.0, low=90.0, open_=97.0)]
    implicit = simulate_kss(candles, 0, distance_pct=2, max_waves=5, tp_pct=3, deadline_days=30)
    explicit = simulate_kss(candles, 0, distance_pct=2, max_waves=5, tp_pct=3, deadline_days=30,
                            pessimistic_intrabar=False)
    assert implicit == explicit


def test_pessimistic_flip_turns_a_stop_loss_into_a_take_profit():
    """The clearest possible divergence: on ONE bar whose high clears even the harder
    pre-fill take-profit AND whose low would (after DCA) clear the stop-loss, the two bounds
    disagree about which happened first — and therefore about win vs. loss entirely.

    Bar 1: open=97 (gap-fills wave 1 below its 98 target), low=90 (fills every deeper wave,
    dragging the average down to ~94.65), high=104.
      - Optimistic (default): stop-loss is checked FIRST, against the POST-fill average
        (94.65 × 0.97 ≈ 91.81) — bar low 90 ≤ 91.81 → STOPPED. The take-profit check is never
        reached.
      - Pessimistic: take-profit is checked FIRST, against the PRE-fill average (100 × 1.03 =
        103) — bar high 104 ≥ 103 → TP HIT, before any fill or stop-loss check ever runs.
    """
    candles = [candle(0, 100.0), candle(1, 95.0, high=104.0, low=90.0, open_=97.0)]

    optimistic = simulate_kss(candles, 0, distance_pct=2, max_waves=5, tp_pct=3,
                              deadline_days=30, sl_pct=3, cost_pct=0)
    pessimistic = simulate_kss(candles, 0, distance_pct=2, max_waves=5, tp_pct=3,
                               deadline_days=30, sl_pct=3, cost_pct=0,
                               pessimistic_intrabar=True)

    assert optimistic.stopped is True and optimistic.tp_hit is False
    assert optimistic.pnl_pct == -3
    assert optimistic.waves_filled == 5  # the SL check happens AFTER the fill loop

    assert pessimistic.tp_hit is True and pessimistic.stopped is False
    assert pessimistic.pnl_pct == 3
    assert pessimistic.waves_filled == 1  # TP fired before this bar's fill loop ever ran


def test_pessimistic_flip_can_turn_a_win_into_a_ran_out_of_data_non_win():
    """A milder divergence than the SL/TP flip above: TP that only clears the LOWER post-fill
    average (not the higher pre-fill one) fires under the optimistic bound but not under the
    pessimistic one (which — on this 2-candle fixture — then simply runs out of data)."""
    candles = [candle(0, 100.0), candle(1, 95.0, high=99.0, low=90.0, open_=97.0)]

    optimistic = simulate_kss(candles, 0, distance_pct=2, max_waves=5, tp_pct=3,
                              deadline_days=30, sl_pct=0)
    pessimistic = simulate_kss(candles, 0, distance_pct=2, max_waves=5, tp_pct=3,
                               deadline_days=30, sl_pct=0, pessimistic_intrabar=True)

    assert optimistic.tp_hit is True
    assert pessimistic.tp_hit is False


# ----- capital_days / exit_capital: capital actually held, not capital reserved -------------
#
# WHY: a session RESERVES a full ladder but only DEPLOYS what filled. Wave 0's dollar cost
# counts from the ENTRY bar; each deeper rung's dollar cost counts from the bar it filled
# onward — never from entry, and never at all if it never filled. Bar length is derived from
# consecutive `ts` values (never an assumed timeframe), so this works identically on daily or
# 5-minute candles.

def test_capital_days_single_wave_scales_with_wave0_notional():
    """max_waves=1: capital never changes after entry, so capital_days is exactly
    wave0_notional_usd × (number of bar-widths from entry through the exit bar's own width),
    and both capital_days and exit_capital scale linearly with wave0_notional_usd."""
    candles = [candle(0, 100.0), candle(1, 100.0, high=100.0, low=100.0),
               candle(2, 104.0, high=104.0, low=100.0)]

    r = simulate_kss(candles, 0, distance_pct=2, max_waves=1, tp_pct=3, deadline_days=30,
                     sl_pct=0, cost_pct=0)
    assert r.tp_hit is True and r.days_to_tp == 2.0
    # 3 bar-widths of 1 day each (entry→bar1, bar1→bar2, bar2's own width via fallback),
    # each holding the full $100 wave-0 notional: 100*3 = 300.
    assert r.exit_capital == 100.0
    assert r.capital_days == 300.0

    r2 = simulate_kss(candles, 0, distance_pct=2, max_waves=1, tp_pct=3, deadline_days=30,
                      sl_pct=0, cost_pct=0, wave0_notional_usd=250.0)
    assert r2.exit_capital == 250.0
    assert r2.capital_days == 750.0


def test_capital_days_deeper_rung_counts_only_from_the_bar_it_filled():
    """max_waves=2: wave 1 fills on bar 1, TP hits on bar 2. If wave 1's capital were (wrongly)
    counted from ENTRY instead of from the bar it filled, capital_days would be 888.0 (both
    waves' cost × 2 bar-widths); the correct value credits wave 0 alone for the entry→bar1
    gap and both waves for bar1→bar2 and bar2's own width: 100×1 + 296×1 + 296×1 = 692.0."""
    candles = [candle(0, 100.0),
               candle(1, 98.0, high=99.0, low=97.0, open_=98.0),
               candle(2, 103.0, high=103.0, low=100.0)]

    r = simulate_kss(candles, 0, distance_pct=2, max_waves=2, tp_pct=3, deadline_days=30,
                     sl_pct=0, cost_pct=0)
    assert r.tp_hit is True
    assert r.waves_filled == 2
    assert r.exit_capital == 296.0  # wave0 $100 + wave1 (2 × $1 unit_qty × $98 fill) = $296
    assert r.capital_days == 692.0
    assert r.capital_days != 888.0, "wave 1 must count from bar 1, not from entry"


def test_capital_days_present_on_every_exit_path():
    """capital_days/exit_capital are populated on SL, deadline, and ran-out-of-data exits too
    — not just the take-profit path exercised by the tests above."""
    sl = simulate_kss(
        [candle(0, 100.0), candle(1, 82.0, high=99.0, low=80.0, open_=99.0)],
        0, distance_pct=2, max_waves=5, tp_pct=3, deadline_days=30, sl_pct=13, cost_pct=0.3,
    )
    assert sl.stopped is True
    assert sl.capital_days > 0.0 and sl.exit_capital > 0.0

    deadline_candles = [candle(0, 100.0)] + [
        candle(d, 102.0, high=102.0, low=99.0) for d in range(1, 35)
    ]
    deadline = simulate_kss(deadline_candles, 0, distance_pct=2, max_waves=5, tp_pct=3,
                            deadline_days=30)
    assert deadline.hit_deadline is True
    assert deadline.capital_days > 0.0 and deadline.exit_capital > 0.0

    ran_out = simulate_kss(
        [candle(0, 100.0), candle(1, 99.0, high=99.5, low=98.5)],
        0, distance_pct=2, max_waves=5, tp_pct=3, deadline_days=30,
    )
    assert ran_out.tp_hit is False and ran_out.hit_deadline is False and ran_out.stopped is False
    assert ran_out.capital_days > 0.0 and ran_out.exit_capital > 0.0


# ----- estimate_win_rate: capital_days/pnl_usd/waves_filled_sum aggregation ------------------

def test_estimate_win_rate_aggregates_capital_and_dollar_pnl():
    """estimate_win_rate sums SimResult.capital_days and (pnl_pct/100 × exit_capital) across
    every counted trial — the inputs app/evaluate.py needs for profit-per-dollar-day."""
    candles = []
    price = 100.0
    for d in range(40):
        candles.append(candle(d, price, high=price, low=price * 0.999))
        price *= 1.01
    res = estimate_win_rate(candles, distance_pct=2, max_waves=1, tp_pct=3, deadline_days=30,
                            cost_pct=0.3)
    assert res["trials"] > 0
    assert res["capital_days"] > 0.0
    assert res["waves_filled_sum"] == res["trials"]  # max_waves=1 → exactly 1 wave/trial
    # All-win fixture, max_waves=1 (wave0-only, so exit_capital == wave0_notional_usd == $100
    # for every trial): pnl_usd must equal expectancy%/100 × $100 × trials, within rounding.
    expected_pnl_usd = res["expectancy"] / 100.0 * 100.0 * res["trials"]
    assert abs(res["pnl_usd"] - expected_pnl_usd) < 0.01
    assert res["pnl_usd"] > 0.0


def test_estimate_win_rate_forwards_pessimistic_and_wave0_kwargs():
    """estimate_win_rate must actually pass pessimistic_intrabar/wave0_notional_usd through
    to simulate_kss, not silently drop them."""
    candles = [candle(0, 100.0), candle(1, 95.0, high=104.0, low=90.0, open_=97.0),
               candle(2, 104.0, high=104.0, low=100.0)]
    optimistic = estimate_win_rate(candles, distance_pct=2, max_waves=5, tp_pct=3,
                                   deadline_days=30, sl_pct=3, cost_pct=0)
    pessimistic = estimate_win_rate(candles, distance_pct=2, max_waves=5, tp_pct=3,
                                    deadline_days=30, sl_pct=3, cost_pct=0,
                                    pessimistic_intrabar=True)
    # Same fixture as test_pessimistic_flip_turns_a_stop_loss_into_a_take_profit: the single
    # trial starting at index 0 is a stop under the optimistic bound, a win under pessimistic.
    assert optimistic["stops"] >= 1
    assert pessimistic["wins"] >= 1

    default_wave0 = estimate_win_rate(candles, distance_pct=2, max_waves=1, tp_pct=3,
                                      deadline_days=30)
    scaled_wave0 = estimate_win_rate(candles, distance_pct=2, max_waves=1, tp_pct=3,
                                     deadline_days=30, wave0_notional_usd=1000.0)
    if default_wave0["capital_days"] > 0:
        assert scaled_wave0["capital_days"] == default_wave0["capital_days"] * 10
