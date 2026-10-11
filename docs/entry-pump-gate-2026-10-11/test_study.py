"""pytest for the entry-pump-gate study:  .venv/Scripts/python.exe -m pytest docs/entry-pump-gate-2026-10-11 -q"""
from __future__ import annotations

import common  # noqa: F401  (throw-away DB env before any app import)

import numpy as np
import pytest

import stats_ext as S


# ----------------------------------------------------------------------------- ratio of sums
def test_ppdd_is_ratio_of_sums_not_mean_of_ratios():
    pnl = np.array([1.0, 10.0])
    cap = np.array([1.0, 100.0])           # ratios 1.0 and 0.1 -> mean 0.55, ratio of sums 11/101
    assert S.ppdd(pnl, cap) == pytest.approx(1000 * 11 / 101)


def test_lifts_signs():
    pnl = np.array([5.0, 5.0, -5.0, -5.0])
    cap = np.ones(4)
    veto = np.array([False, False, True, True])
    pb, pv = S.lifts(pnl, cap, veto)
    assert pv == pytest.approx(10000.0)     # passed 5000 - vetoed -5000
    assert pb == pytest.approx(5000.0)      # passed 5000 - baseline 0


# ----------------------------------------------------------------------------- bootstraps
def _world(n_days=400, n_coins=30, edge=0.0, seed=0):
    rng = np.random.default_rng(seed)
    day = np.repeat(np.arange(n_days), n_coins)
    coin = np.tile(np.arange(n_coins), n_days)
    veto = rng.random(len(day)) < 0.3
    pnl = rng.normal(0.5, 3.0, len(day)) - edge * veto          # vetoed entries earn `edge` less
    cap = np.full(len(day), 100.0)
    return day, coin, pnl, cap, veto


def test_bootstraps_cover_zero_under_null_and_exclude_it_under_edge():
    d, c, p, k, v = _world(edge=0.0)
    for ci in (S.day_block_bootstrap(d, p, k, v, block=10, rounds=500),
               S.coin_bootstrap(c, p, k, v, rounds=500)):
        assert ci["pv"][0] < 0 < ci["pv"][1]
    d, c, p, k, v = _world(edge=3.0)
    for ci in (S.day_block_bootstrap(d, p, k, v, block=10, rounds=500),
               S.coin_bootstrap(c, p, k, v, rounds=500)):
        assert ci["pv"][0] > 0


def test_bootstrap_deterministic_for_seed():
    d, c, p, k, v = _world()
    a = S.day_block_bootstrap(d, p, k, v, block=7, rounds=200, seed=5)
    b = S.day_block_bootstrap(d, p, k, v, block=7, rounds=200, seed=5)
    assert a == b


# ----------------------------------------------------------------------------- Westfall-Young
def test_westfall_young_monotone_and_ge_raw():
    rng = np.random.default_rng(1)
    null = {g: np.abs(rng.normal(size=2000)) for g in "abc"}
    obs = {"a": 3.5, "b": 1.0, "c": 0.2}
    raw, adj, det = S.westfall_young(obs, null)
    assert all(adj[g] >= raw[g] - 1e-12 for g in obs)
    assert adj["a"] <= adj["b"] <= adj["c"]
    assert raw["a"] < 0.01 and adj["c"] > 0.5


# ----------------------------------------------------------------------------- timing permutation
def test_day_shift_perm_detects_day_level_timing_but_not_noise():
    """A day-level label that really predicts outcomes (a persistent regime) must be significant;
    an unrelated persistent regime must not."""
    rng = np.random.default_rng(2)
    nd, nc = 500, 20
    regime = np.cumsum(rng.normal(size=nd))
    regime = (regime > np.median(regime))                       # persistent day-level regime (bool)
    other = np.cumsum(rng.normal(size=nd)) > 0                  # unrelated persistent regime
    day = np.repeat(np.arange(nd), nc)
    coin = np.tile(np.arange(nc), nd)
    pnl = rng.normal(0, 3, len(day)) + 2.0 * regime[day]
    cap = np.full(len(day), 100.0)
    vetoes = {"timing": ~regime[day], "noise": other[day]}      # veto when regime is bad
    raw, adj, det, n = S.day_shift_permutation(coin, day, pnl, cap, vetoes, min_shift=30)
    assert raw["timing"] < 0.01
    assert raw["noise"] > 0.05
    assert n == nd - 60 + 1


def test_day_shift_perm_null_is_roughly_uniform():
    ps = []
    for seed in range(12):
        rng = np.random.default_rng(100 + seed)
        nd, nc = 300, 10
        day = np.repeat(np.arange(nd), nc)
        coin = np.tile(np.arange(nc), nd)
        lab = (np.cumsum(rng.normal(size=nd)) > 0)[day]
        pnl = rng.normal(0, 3, len(day))
        raw, *_ = S.day_shift_permutation(coin, day, pnl, np.full(len(day), 100.0), {"g": lab})
        ps.append(raw["g"])
    assert np.mean(np.array(ps) < 0.05) < 0.35                  # not wildly anti-conservative


# ----------------------------------------------------------------------------- look-ahead guard
def _fake_sym(n_hours=24 * 60, seed=0, start_ts=None):
    import entry_study as E
    from common import H, WINDOW_START_TS

    rng = np.random.default_rng(seed)
    S_ = E.Sym.__new__(E.Sym)
    S_.name = "FAKE"
    S_.ts = [WINDOW_START_TS + i * H for i in range(n_hours)]
    px = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, n_hours)))
    S_.c = px.tolist()
    S_.o = np.r_[px[0], px[:-1]].tolist()
    S_.h = (px * 1.004).tolist()
    S_.l = (px * 0.996).tolist()
    S_.v = [10.0] * n_hours
    days = n_hours // 24
    S_.d_ts = [WINDOW_START_TS + k * 86_400_000 for k in range(days)]
    S_.d = []
    for k in range(days):
        sl = slice(k * 24, k * 24 + 24)
        S_.d.append(dict(ts=S_.d_ts[k], open=S_.o[sl][0], high=max(S_.h[sl]), low=min(S_.l[sl]),
                         close=S_.c[sl][-1], volume=240.0))
    S_._candles = None
    return S_


class _Snap:
    def __init__(self, w):
        self.downtrend = w[-1]["close"] < w[-5]["close"]
        self.falling_knife = w[-1]["close"] < w[-2]["close"]
        self.rel_strength = False
        self.backtest_evidence = False
        self.consensus = False


def test_entry_features_are_immune_to_the_future():
    """Corrupt EVERYTHING after the entry bar; features and flags must not move."""
    import entry_study as E
    from common import H, WINDOW_START_TS

    a, btc = _fake_sym(seed=1), _fake_sym(seed=2)
    start = 24 * 45 + 7                                         # day 45, hour 7
    gate = lambda w, bw: _Snap(w)                               # noqa: E731
    rsi = lambda v: float(np.mean(v[-14:]) / np.mean(v) * 50)   # noqa: E731  (any causal fn)
    f0, mx0 = E.entry_features(a, btc, start, gate, rsi)
    assert mx0 <= a.ts[start]

    b, btc2 = _fake_sym(seed=1), _fake_sym(seed=2)
    for X in (b, btc2):
        for i in range(start + 1, len(X.ts)):
            X.c[i] *= 7.0; X.h[i] *= 9.0; X.l[i] *= 0.1; X.o[i] *= 3.0; X.v[i] = 1e9
        k0 = start // 24                                         # day of entry and later daily bars
        for k in range(k0, len(X.d)):
            X.d[k] = dict(X.d[k], close=X.d[k]["close"] * 7.0, high=1e9, low=1e-9)
    # the entry day's own daily bar (index start//24) is NOT supposed to be read as closed data
    f1, mx1 = E.entry_features(b, btc2, start, gate, rsi)
    for k in f0:
        v0, v1 = f0[k], f1[k]
        assert (v0 == v1) or (isinstance(v0, float) and np.isnan(v0) and np.isnan(v1)), k
    assert mx1 == mx0


def test_forming_bar_stops_at_entry_bar():
    import entry_study as E
    from common import WINDOW_START_TS

    s = _fake_sym(seed=3)
    day_ts = WINDOW_START_TS + 10 * 86_400_000
    start = 10 * 24 + 5
    fb = E.forming_bar(s, day_ts, start)
    assert fb["close"] == s.c[start]
    assert fb["high"] == max(s.h[10 * 24:start + 1])
    assert fb["volume"] == 6 * 10.0


def test_rung2_within_uses_timestamps():
    import entry_study as E

    s = _fake_sym(seed=4)
    start = 100
    entry = s.c[start]
    for j in range(start + 1, start + 200):
        s.l[j] = entry * 0.999
    s.l[start + 48] = entry * 0.92           # exactly 48h later: inside the window
    assert E.rung2_within(s, start, entry, 0.93, 48)
    s.l[start + 48] = entry * 0.999
    s.l[start + 49] = entry * 0.92           # 49h: outside
    assert not E.rung2_within(s, start, entry, 0.93, 48)


# ----------------------------------------------------------------------------- within-day + studentized
def test_within_day_null_has_zero_power_for_day_level_gate_and_studentized_drops_it():
    rng = np.random.default_rng(7)
    nd, nc = 200, 25
    day = np.repeat(np.arange(nd), nc)
    pnl = rng.normal(0, 3, len(day))
    cap = np.full(len(day), 100.0)
    day_gate = (rng.random(nd) < 0.3)[day]                    # same label for every coin of a day
    coin_gate = rng.random(len(day)) < 0.3
    obs, null = S.within_day_null(day, pnl, cap, {"day": day_gate, "coin": coin_gate}, rounds=300)
    assert np.nanstd(null["day"]) < 1e-9                       # permuting inside a day cannot move it
    assert np.nanstd(null["coin"]) > 0
    raw, adj, det, dropped = S.studentized_wy(obs, null)
    assert dropped == ["day"] and "coin" in adj


def test_within_day_null_matches_original_tool_p_value():
    import measure_entry_gates as M

    rng = np.random.default_rng(11)
    nd, nc = 120, 20
    day = np.repeat(np.arange(nd), nc)
    coin = np.tile(np.arange(nc), nd)
    veto = rng.random(len(day)) < 0.3
    pnl = rng.normal(0, 3, len(day)) - 1.0 * veto
    cap = np.full(len(day), 100.0)
    keys = [(f"c{c}", int(d)) for c, d in zip(coin, day)]
    outs = {k: M.EntryOutcome(symbol=k[0], index=k[1], ts=int(d), pnl_usd=float(p), capital_days=float(c),
                              pnl_pct=0.0, stopped=False, tp_hit=False)
            for k, d, p, c in zip(keys, day, pnl, cap)}
    vet = {k: {"g": bool(v)} for k, v in zip(keys, veto)}
    raw0, *_ = M.cluster_permutation_pvalues(keys, outs, vet, ["g"], rounds=1500, seed=1)
    obs, null = S.within_day_null(day, pnl, cap, {"g": veto}, rounds=1500, seed=2)
    raw1, _, _ = S.westfall_young(obs, null)
    assert abs(raw0["g"] - raw1["g"]) < 0.02 or (raw0["g"] < 0.01 and raw1["g"] < 0.01)


def test_studentized_fwer_is_not_dominated_by_a_noisy_gate():
    rng = np.random.default_rng(5)
    B = 2000
    null = {"loud": np.abs(rng.normal(0, 5, B)), "quiet": np.abs(rng.normal(0, 0.2, B))}
    obs = {"loud": 6.0, "quiet": 0.7}                           # quiet is 3.5 sd out; loud only 1.2 sd
    _, adj_raw, _ = S.westfall_young(obs, null)
    _, adj_stud, _, _ = S.studentized_wy(obs, null)
    assert adj_raw["quiet"] > 0.2                               # raw units: swamped by the loud gate
    assert adj_stud["quiet"] < 0.05
