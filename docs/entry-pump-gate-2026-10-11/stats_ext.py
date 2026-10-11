"""Pure statistics for the entry-pump-gate study (numpy only, no app imports).

Everything is expressed on the per-entry arrays
    day   int   entry-day index (cluster of one scan; also the unit of time)
    coin  int   coin index
    pnl   float realised $ (MTM at the last bar for truncated sessions)
    cap   float $-days of capital actually deployed
    veto  bool  the gate vetoes this entry
and the headline metric is  ppdd = 1000 * sum(pnl)/sum(cap)  (net $ per $1000 capital-day),
a RATIO OF SUMS (never a mean of ratios).

Three views of "is this gate better than chance", deliberately kept separate:
  * within-day permutation  (original tool, reused)     -> CROSS-SECTIONAL skill only
  * coin-wise circular day-shift permutation (here)      -> TIMING skill (market + within-coin)
  * cluster / day-block bootstrap CIs (here)             -> size and sign of the combined effect
"""
from __future__ import annotations

import numpy as np


def ppdd(pnl: np.ndarray, cap: np.ndarray) -> float:
    c = float(np.sum(cap))
    return 1000.0 * float(np.sum(pnl)) / c if c > 0 else float("nan")


def lifts(pnl, cap, veto) -> tuple[float, float]:
    """(passed - baseline, passed - vetoed) in ppdd units. NaN when a side is empty."""
    pas = ~veto
    p = ppdd(pnl[pas], cap[pas])
    return p - ppdd(pnl, cap), p - ppdd(pnl[veto], cap[veto])


# ---------------------------------------------------------------------------------------
# Bootstrap CIs from per-cluster aggregates
# ---------------------------------------------------------------------------------------


def _agg(idx: np.ndarray, n: int, pnl, cap, veto):
    """Per-cluster sums: passed pnl/cap, vetoed pnl/cap."""
    pas = ~veto
    Pp = np.bincount(idx[pas], weights=pnl[pas], minlength=n)
    Cp = np.bincount(idx[pas], weights=cap[pas], minlength=n)
    Pv = np.bincount(idx[veto], weights=pnl[veto], minlength=n)
    Cv = np.bincount(idx[veto], weights=cap[veto], minlength=n)
    return Pp, Cp, Pv, Cv


def _lift_from_sums(Pp, Cp, Pv, Cv):
    with np.errstate(divide="ignore", invalid="ignore"):
        p = 1000.0 * Pp / Cp
        v = 1000.0 * Pv / Cv
        a = 1000.0 * (Pp + Pv) / (Cp + Cv)
    return p - a, p - v


def _pct_ci(x: np.ndarray, level: float = 0.95):
    x = x[np.isfinite(x)]
    if len(x) < 20:
        return (float("nan"), float("nan"))
    t = (1 - level) / 2
    return (float(np.quantile(x, t)), float(np.quantile(x, 1 - t)))


def day_block_bootstrap(day, pnl, cap, veto, *, block: int = 30, rounds: int = 2000, seed: int = 1):
    """Circular moving-block bootstrap over ENTRY DAYS. Returns {'pb': (lo,hi), 'pv': (lo,hi)}:
    CI of (passed-baseline) and (passed-vetoed) ppdd. Blocks keep serial dependence (sessions last
    days to weeks) and each day carries its whole cross-section (coins are not independent)."""
    ud, inv = np.unique(day, return_inverse=True)
    n = len(ud)
    Pp, Cp, Pv, Cv = _agg(inv, n, pnl, cap, veto)
    b = max(1, min(block, n))
    nb = int(np.ceil(n / b))
    rng = np.random.default_rng(seed)
    starts = rng.integers(0, n, size=(rounds, nb))
    idx = ((starts[:, :, None] + np.arange(b)[None, None, :]) % n).reshape(rounds, nb * b)[:, :n]
    sums = [a[idx].sum(axis=1) for a in (Pp, Cp, Pv, Cv)]
    pb, pv = _lift_from_sums(*sums)
    return {"pb": _pct_ci(pb), "pv": _pct_ci(pv)}


def coin_bootstrap(coin, pnl, cap, veto, *, rounds: int = 2000, seed: int = 2):
    """Cluster-by-coin bootstrap (resample coins with replacement, each with ALL its days).
    Captures between-coin heterogeneity; does NOT capture market-wide time dependence (use the
    day-block CI for that)."""
    uc, inv = np.unique(coin, return_inverse=True)
    n = len(uc)
    Pp, Cp, Pv, Cv = _agg(inv, n, pnl, cap, veto)
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, n, size=(rounds, n))
    sums = [a[idx].sum(axis=1) for a in (Pp, Cp, Pv, Cv)]
    pb, pv = _lift_from_sums(*sums)
    return {"pb": _pct_ci(pb), "pv": _pct_ci(pv)}


# ---------------------------------------------------------------------------------------
# Westfall-Young step-down max-T on a SHARED null draw matrix
# ---------------------------------------------------------------------------------------


def westfall_young(observed: dict[str, float], null: dict[str, np.ndarray]):
    """observed[g] = |T_g|; null[g] = array of |T_g| under B shared draws (NaN ignored).
    Returns (raw p, FWER-adjusted p, family-wise detectable |T| = 95th pct of the step-down max)."""
    names = list(observed)
    B = len(next(iter(null.values())))
    raw = {n: float((np.nansum(null[n] >= observed[n]) + 1) / (B + 1)) for n in names}
    ranked = sorted(names, key=lambda n: observed[n], reverse=True)
    running = np.zeros(B)
    adj, det = {}, {}
    for n in reversed(ranked):
        running = np.fmax(running, null[n])
        adj[n] = float((np.nansum(running >= observed[n]) + 1) / (B + 1))
        det[n] = float(np.nanpercentile(running, 95.0))
    prev = 0.0
    for n in ranked:
        prev = adj[n] = max(adj[n], prev)
    return raw, adj, det


# ---------------------------------------------------------------------------------------
# Timing-aware permutation: coin-wise circular day shift of the VETO labels
# ---------------------------------------------------------------------------------------


def _grid(coin, day, vals, ncoin, nday, dtype=float):
    g = np.zeros((ncoin, nday), dtype=dtype)
    g[coin, day] = vals
    return g


def day_shift_null(coin, day, pnl, cap, vetoes: dict[str, np.ndarray], *,
                   min_shift: int = 30, max_shifts: int | None = None, seed: int = 3):
    """Null for market-timing / within-coin-timing skill.

    Each coin's veto label series (indexed by entry day) is circularly shifted by the SAME s days
    for every coin and every gate (so cross-coin and cross-gate dependence is preserved), while
    outcomes stay put. A label that lands on a (coin, day) without an entry is dropped from that
    draw. |shift| >= `min_shift` days so the shifted labels are not just yesterday's labels (labels
    and outcomes are strongly autocorrelated). Statistic: |ppdd(passed) - ppdd(vetoed)|.
    Observed = shift 0. Returns (observed {g: |T|}, null {g: array}, n_shifts)."""
    uc, ci = np.unique(coin, return_inverse=True)
    ud, di = np.unique(day, return_inverse=True)
    nc, nd = len(uc), len(ud)
    pres = _grid(ci, di, True, nc, nd, bool)
    P = _grid(ci, di, pnl, nc, nd)
    C = _grid(ci, di, cap, nc, nd)
    labs = {g: _grid(ci, di, v, nc, nd, bool) for g, v in vetoes.items()}
    shifts = np.arange(min_shift, nd - min_shift + 1)
    if max_shifts and len(shifts) > max_shifts:
        shifts = np.sort(np.random.default_rng(seed).choice(shifts, size=max_shifts, replace=False))
    Pf, Cf = P.ravel(), C.ravel()

    def stat(valid, lab):
        v = (valid & lab).ravel().astype(np.float64)
        p = (valid & ~lab).ravel().astype(np.float64)
        cv, cp = v @ Cf, p @ Cf
        if cv <= 0 or cp <= 0:
            return np.nan
        return abs(1000.0 * (p @ Pf) / cp - 1000.0 * (v @ Pf) / cv)

    observed = {g: stat(pres, lab) for g, lab in labs.items()}
    null = {g: np.empty(len(shifts)) for g in labs}
    for k, s in enumerate(shifts):
        pres_s = np.roll(pres, s, axis=1)
        valid = pres & pres_s
        for g, lab in labs.items():
            null[g][k] = stat(valid, np.roll(lab, s, axis=1))
    return observed, null, len(shifts)


def day_shift_permutation(coin, day, pnl, cap, vetoes, **kw):
    """Raw-unit Westfall-Young on the day-shift null. Returns (raw, adj, detectable, n_shifts)."""
    obs, null, n = day_shift_null(coin, day, pnl, cap, vetoes, **kw)
    raw, adj, det = westfall_young(obs, null)
    return raw, adj, det, n


def within_day_null(day, pnl, cap, vetoes: dict[str, np.ndarray], *, rounds: int = 3000, seed: int = 4):
    """Null for CROSS-SECTIONAL skill: outcomes shuffled WITHIN each entry day (same scheme as
    scripts/measure_entry_gates.cluster_permutation_pvalues, but returns the null draws so they can
    be studentized). Day-level gates (G6) have a constant null by construction -> zero power."""
    order = np.argsort(day, kind="stable")
    d = day[order]
    P, C = pnl[order].astype(float), cap[order].astype(float)
    masks = {g: v[order].astype(np.float64) for g, v in vetoes.items()}
    inv = {g: 1.0 - m for g, m in masks.items()}

    def stat(g, p, c):
        cv, cp = masks[g] @ c, inv[g] @ c
        if cv <= 0 or cp <= 0:
            return np.nan
        return abs(1000.0 * (inv[g] @ p) / cp - 1000.0 * (masks[g] @ p) / cv)

    observed = {g: stat(g, P, C) for g in masks}
    null = {g: np.empty(rounds) for g in masks}
    rng = np.random.default_rng(seed)
    for r in range(rounds):
        perm = np.lexsort((rng.random(len(d)), d))
        p, c = P[perm], C[perm]
        for g in masks:
            null[g][r] = stat(g, p, c)
    return observed, null


def studentized_wy(observed: dict[str, float], null: dict[str, np.ndarray], members: list[str] | None = None):
    """Westfall-Young step-down on z = |T| / sd(null_g): puts gates with very different sampling
    noise on one scale (the raw-unit max-T is dominated by the noisiest gate -- a day-level gate
    such as G6 can by itself set the whole family's threshold). Gates whose null has ~zero spread
    (no power under this scheme) are dropped from the family and reported as None.
    Returns (raw p, FWER p, detectable lift in the gate's own ppdd units, dropped list)."""
    names = [g for g in (members or list(observed)) if np.isfinite(observed[g])]
    sd = {g: float(np.nanstd(null[g])) for g in names}
    keep = [g for g in names if sd[g] > 1e-9 * max(1.0, abs(observed[g]))]
    dropped = [g for g in (members or list(observed)) if g not in keep]
    if not keep:
        return {}, {}, {}, dropped
    zo = {g: observed[g] / sd[g] for g in keep}
    zn = {g: null[g] / sd[g] for g in keep}
    raw, adj, det = westfall_young(zo, zn)
    return raw, adj, {g: det[g] * sd[g] for g in keep}, dropped
