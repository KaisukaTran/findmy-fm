"""TP-anchor variant of the portfolio engine (docs/tp-anchor-2026-10-11).

Wraps scripts/capital_portfolio_study.py WITHOUT editing it: `install()` monkey-patches
`cps.step_session` with a copy that has one extra hook, `tp_price(state, cfg, avg, k)`.
Everything else (cash ledger, reserve gate, sessions/day, deep-lock, warmup, delist, reporting)
is the existing engine untouched.

tp_anchor:
  'avg'   : TP = avg   * (1 + X_k/100)            (current live behaviour)  -> byte-identical to cps
  'entry' : TP = entry * (1 + X_k/100)            (wave-0 fill price)
  'max'   : TP = max(avg * (1 + X_k/100), entry)  (avg TP, never below the wave-0 price)
X_k = tp% + tp_fee_buffer + step * (k-1)   (cps._eff_tp; k = rungs filled, wave 0 counts)

Everything below `tp_price` is a line-for-line copy of cps.step_session with `avg * _tp_factor`
replaced by `tp_price(...)` and the realized pnl % of a TP computed from the actual TP price.
The cash-floor-release / backstop / policy-B branches are dropped (all OFF in this study).
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
class TPConfig(cps.Config):
    tp_anchor: str = "avg"


def tp_price(state, cfg, avg: float, k: int) -> float:
    a = getattr(cfg, "tp_anchor", "avg")
    f = _tp_factor(cfg, k)
    if a in ("avg", "avgcopy"):    # 'avgcopy' = avg through the COPIED code path (self-test only)
        return avg * f
    if a == "entry":
        return state.entry * f
    if a == "max":
        return max(avg * f, state.entry)
    raise ValueError(a)


def tp_pnl_pct(cfg, avg: float, tp: float, k: int) -> float:
    if getattr(cfg, "tp_anchor", "avg") == "avg":
        return round(_eff_tp(cfg, k) - cfg.cost_pct, 4)   # byte-identical to the original
    return round((tp / avg - 1) * 100.0 - cfg.cost_pct, 4)


def step_session(state, bar, idx, cfg, ledger):
    """cps.step_session with the TP price hook. Only the take-profit lines differ."""
    if getattr(cfg, "tp_anchor", "avg") == "avg":
        return ORIG_STEP(state, bar, idx, cfg, ledger)   # exact original for the baseline
    assert not (cfg.floor_release_trigger or cfg.floor_release_manual_delay_days > 0 or cfg.backstop)
    days = (bar["ts"] - state.entry_ts) / _MS_PER_DAY
    if ledger.halted and cfg.brake_suspend_deadline:
        state.halt_credit_days += _bar_len_days(state.candles, idx)
    days -= state.halt_credit_days
    bar_open = bar.get("open", bar["close"])
    just_armed = False
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

    if cfg.pessimistic:
        pre_avg = _avg_price(state)
        if pre_avg > 0:
            dd = (bar["low"] - pre_avg) / pre_avg * 100.0
            state.mae_pct = min(state.mae_pct, dd)
            k = _waves_touched(state)
            tp = tp_price(state, cfg, pre_avg, k)
            if not state.armed and bar["high"] >= tp:
                if cfg.trail_after_tp_pct <= 0:
                    closed = close("tp", tp, tp_pnl_pct(cfg, pre_avg, tp, k))
                    return {"closed": closed, "event": event, "event_usd": event_usd}
                state.armed = True
                just_armed = True
                state.floor = tp
                state.peak = bar["high"]
                state.eff_tp_at_arm = _eff_tp(cfg, k)

    # ---- rung fills: identical to cps (floor/backstop/release/policy-B branches are OFF) ----
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

    if cfg.pessimistic:
        if state.armed and not just_armed:
            stop = max(state.floor, state.peak * (1 - cfg.trail_after_tp_pct / 100))
            if bar["low"] <= stop:
                gross = (stop / avg - 1) * 100.0
                closed = close("trail", stop, round(gross - cfg.cost_pct, 4))
                return {"closed": closed, "event": event, "event_usd": event_usd}
            state.peak = max(state.peak, bar["high"])
        elif not state.armed:
            if cfg.sl_pct > 0 and bar["low"] <= avg * (1 - cfg.sl_pct / 100):
                closed = close("sl", avg * (1 - cfg.sl_pct / 100), round(-cfg.sl_pct - cfg.cost_pct, 4))
                return {"closed": closed, "event": event, "event_usd": event_usd}
        if not just_armed and days >= cfg.deadline_days:
            last = bar["close"]
            closed = close("deadline", last, round((last - avg) / avg * 100.0 - cfg.cost_pct, 4))
            return {"closed": closed, "event": event, "event_usd": event_usd}
    else:
        if state.armed and not just_armed:
            state.peak = max(state.peak, bar["high"])
            stop = max(state.floor, state.peak * (1 - cfg.trail_after_tp_pct / 100))
            if bar["low"] <= stop:
                gross = (stop / avg - 1) * 100.0
                closed = close("trail", stop, round(gross - cfg.cost_pct, 4))
                return {"closed": closed, "event": event, "event_usd": event_usd}
        elif not state.armed:
            if cfg.sl_pct > 0 and bar["low"] <= avg * (1 - cfg.sl_pct / 100):
                closed = close("sl", avg * (1 - cfg.sl_pct / 100), round(-cfg.sl_pct - cfg.cost_pct, 4))
                return {"closed": closed, "event": event, "event_usd": event_usd}
            k = _waves_touched(state)
            tp = tp_price(state, cfg, avg, k)
            if bar["high"] >= tp:
                if cfg.trail_after_tp_pct <= 0:
                    closed = close("tp", tp, tp_pnl_pct(cfg, avg, tp, k))
                    return {"closed": closed, "event": event, "event_usd": event_usd}
                state.armed = True
                just_armed = True
                state.floor = tp
                state.peak = bar["high"]
                state.eff_tp_at_arm = _eff_tp(cfg, k)
        if not just_armed and days >= cfg.deadline_days:
            last = bar["close"]
            closed = close("deadline", last, round((last - avg) / avg * 100.0 - cfg.cost_pct, 4))
            return {"closed": closed, "event": event, "event_usd": event_usd}

    return {"closed": None, "event": event, "event_usd": event_usd}


def install() -> None:
    cps.step_session = step_session
