"""Deep-ladder-tail controls (docs/deep-ladder-tail-2026-10-11).

Wraps scripts/capital_portfolio_study.py WITHOUT editing it, same pattern as
docs/tp-anchor-2026-10-11/tp_anchor_engine.py: `install()` swaps `cps.step_session` for a copy
with two hooks. When neither hook is active the ORIGINAL function is called (byte-identical
baseline; D1/D4/D5 are pure config changes and also go through the original).

Hooks (fills k = _waves_touched = rungs filled INCLUDING wave 0; "wave index >= 4" <=> k >= 5):
  D2  deep_tp_pct >= 0 and k >= deep_k : TP = avg * (1 + (tp_fee_buffer + deep_tp_pct)/100)
                                          (default 0.24% + 1.0%); net pnl = that - cost_pct (0.30)
                                          K-2 asserted: trigger >= avg*(1+cost_pct/100).
  D3  deep_exit_days > 0 and k >= deep_k and age >= deep_exit_days : exit at bar CLOSE, the very
      same convention as the 60-day deadline (checked after fills and the TP check, both bounds),
      reason "deadline_deep".
k is the post-fill count where the engine already uses post-fill (optimistic TP check, deadline) and
the pre-fill count where it uses pre-fill (pessimistic TP check) -- exactly like the original.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(r"D:\FINDMY")
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scripts.capital_portfolio_study as cps  # noqa: E402
from scripts.capital_portfolio_study import (  # noqa: E402
    _MS_PER_DAY, _avg_price, _bar_len_days, _eff_tp, _fill_price, _tp_factor, _waves_touched,
)

ORIG_STEP = cps.step_session


@dataclass
class DeepConfig(cps.Config):
    deep_k: int = 5                 # fills (incl. wave 0) at which the deep rules start
    deep_tp_pct: float = -1.0       # D2: >=0 enables; TP% over the fee buffer for k >= deep_k
    deep_exit_days: float = 0.0     # D3: >0 enables; market exit at age >= this when k >= deep_k
    force_copy: bool = False        # self-test only: run the COPIED path with hooks off


def _hooks_on(cfg) -> bool:
    return (getattr(cfg, "deep_tp_pct", -1.0) >= 0.0 or getattr(cfg, "deep_exit_days", 0.0) > 0.0
            or getattr(cfg, "force_copy", False))


def trigger_pct(cfg, k: int) -> float:
    """TP trigger in % over avg (incl. fee buffer)."""
    if cfg.deep_tp_pct >= 0.0 and k >= cfg.deep_k:
        t = cfg.tp_fee_buffer_pct + cfg.deep_tp_pct
        assert t >= cfg.cost_pct, "K-2 violated: deep TP below cost basis + fees"
        return t
    return _eff_tp(cfg, k)


def tp_px(cfg, avg: float, k: int) -> float:
    if cfg.deep_tp_pct >= 0.0 and k >= cfg.deep_k:
        return avg * (1 + trigger_pct(cfg, k) / 100)
    return avg * _tp_factor(cfg, k)


def step_session(state, bar, idx, cfg, ledger):
    if not _hooks_on(cfg):
        return ORIG_STEP(state, bar, idx, cfg, ledger)
    assert not (cfg.floor_release_trigger or cfg.floor_release_manual_delay_days > 0 or cfg.backstop)
    assert cfg.trail_after_tp_pct <= 0 and cfg.sl_pct <= 0
    days = (bar["ts"] - state.entry_ts) / _MS_PER_DAY
    if ledger.halted and cfg.brake_suspend_deadline:
        state.halt_credit_days += _bar_len_days(state.candles, idx)
    days -= state.halt_credit_days
    bar_open = bar.get("open", bar["close"])
    event = None
    event_usd = 0.0

    state.capital_days_acc += state.deployed_usd * (bar["ts"] - state.prev_ts) / _MS_PER_DAY
    state.prev_ts = bar["ts"]
    state.last_close = bar["close"]

    def close(reason, exit_price, pnl_pct):
        pnl_usd = round(state.deployed_usd * pnl_pct / 100.0, 6)
        capital_days = state.capital_days_acc + state.deployed_usd * _bar_len_days(state.candles, idx)
        return {
            "reason": reason, "tp_hit": reason in ("tp", "trail"), "pnl_pct": pnl_pct,
            "pnl_usd": pnl_usd, "exit_price": exit_price, "days": round(days, 2),
            "waves_filled": _waves_touched(state), "deployed_usd": round(state.deployed_usd, 6),
            "capital_days": round(capital_days, 6),
        }

    def tp_close(avg, k):
        px = tp_px(cfg, avg, k)
        assert px >= avg * (1 + cfg.cost_pct / 100), "K-2"
        pct = round(trigger_pct(cfg, k) - cfg.cost_pct, 4)
        return {"closed": close("tp", px, pct), "event": event, "event_usd": event_usd}

    if cfg.pessimistic:
        pre_avg = _avg_price(state)
        if pre_avg > 0:
            dd = (bar["low"] - pre_avg) / pre_avg * 100.0
            state.mae_pct = min(state.mae_pct, dd)
            k = _waves_touched(state)
            if bar["high"] >= tp_px(cfg, pre_avg, k):
                return tp_close(pre_avg, k)

    while (not state.armed and state.next_rung < cfg.max_waves
           and bar["low"] <= state.targets[state.next_rung]):
        n = state.next_rung
        price = _fill_price(state.targets[n], bar_open)
        full_qty = (n + 1) * state.unit_qty
        full_cost = full_qty * price
        if ledger.halted:
            ledger.rungs_blocked += 1
            ledger.blocked_usd += full_cost
            event = "blocked"
            event_usd = round(full_cost, 6)
            break
        avail = ledger.cash - ledger.floor
        if avail >= full_cost:
            ledger.cash -= full_cost
            state.fill_qty[n] = full_qty
            state.fill_prices[n] = price
            state.deployed_usd += full_cost
            state.next_rung += 1
        elif cfg.partial_last_rung and avail > 0 and avail >= min(cfg.wave0_floor, full_cost):
            spent = avail
            got_qty = spent / price
            ledger.cash -= spent
            state.fill_qty[n] = got_qty
            state.fill_prices[n] = price
            state.deployed_usd += spent
            state.next_rung += 1
            state.rungs_partial += 1
            event = "partial"
            event_usd = round(full_cost - spent, 6)
            break
        else:
            state.rungs_starved += 1
            event = "starved"
            event_usd = round(full_cost, 6)
            if state.last_starved_rung != n:
                state.last_starved_rung = n
                ledger.starved_rungs_distinct += 1
                ledger.starved_distinct_usd += full_cost
            break

    avg = _avg_price(state)
    if avg > 0:
        dd = (bar["low"] - avg) / avg * 100.0
        state.mae_pct = min(state.mae_pct, dd)

    k = _waves_touched(state)
    if not cfg.pessimistic:
        if bar["high"] >= tp_px(cfg, avg, k):
            return tp_close(avg, k)
    deep_out = cfg.deep_exit_days > 0 and k >= cfg.deep_k and days >= cfg.deep_exit_days
    if days >= cfg.deadline_days or deep_out:
        last = bar["close"]
        reason = "deadline" if days >= cfg.deadline_days else "deadline_deep"
        closed = close(reason, last, round((last - avg) / avg * 100.0 - cfg.cost_pct, 4))
        return {"closed": closed, "event": event, "event_usd": event_usd}
    return {"closed": None, "event": event, "event_usd": event_usd}


def install() -> None:
    cps.step_session = step_session


# variant name -> Config overrides
VARIANTS = {
    "A0": {},
    "D1": dict(tp_step_pct=0.0),
    "D2": dict(deep_tp_pct=1.0),
    "D3": dict(deep_exit_days=30.0),
    "D4": dict(max_waves=7),
    "D5": dict(warmup=180),
}
