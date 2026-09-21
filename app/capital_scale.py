"""Percentage-of-equity capital sizing, resolved at READ time.

WHY. Every dollar-denominated knob in this app — ``kss_first_wave_usd``, ``cash_floor_usd``,
``max_session_deploy_usd``, ``live_max_order_notional``, ``autoapprove_max_notional`` — is a
static number a human typed for one equity level. Kai tops the account up, or profit
compounds, and none of them follow: a book sized for $200,000 (``cash_floor_usd=$40,000``)
refuses every BUY the moment the account is $5,000, and a book sized for $5,000 badly
under-deploys at $200,000. ``app/capital.py`` already has the sizing math (``recommend_*``)
but is called by nothing, on purpose — it must never write settings, so nobody ever wired it.

The fix here is smaller and safer than wiring ``capital.py``: a *percentage* knob resolved
against ``portfolio.equity()`` at the moment a caller needs the number, rather than a dollar
knob typed once. ``equity() == capital_anchor + realized_pnl + unrealized_pnl`` already rides
compounding profit for free, so a percentage naturally rescales with it.

WHAT THIS MODULE DELIBERATELY DOES NOT DO.

* It never writes a setting. Every public function here returns a :class:`Scaled` —  a
  recommendation naming the number and whether it was scaled — and the CALLER decides whether
  to use it. The one exception is bookkeeping, not settings: ``anchored_equity()`` persists a
  cached equity snapshot to ``runtime_config`` under the literal key ``capital_scale_anchor``
  so repeated reads within the deadband agree with each other. That key is not a ``Settings``
  field and is never restored into ``settings`` on boot; nothing else reads it but this module.
* It never takes or exposes a SHAPE parameter. ``docs/capital-scaling-policy.md`` §1 draws the
  line precisely: equity may decide SIZE (session count, wave size, notional caps) and never
  SHAPE (take-profit %, stop-loss %, DCA spacing, wave count, deadlines, any filter threshold).
  ``tests/app/test_capital_scaling.py`` enforces this on the public API *surface* — signatures
  and names, not source text — for both ``app.capital`` and this module.
* Phase 1 landed this module inert: nothing called the five helpers below, so
  ``capital_scale_enabled`` (default False) was the ONLY thing that mattered. Phase 2 wires
  every call site listed in ``docs/capital-scaling-policy.md`` (session open, the cash-cap gate,
  the live/resting notional caps, the auto-approve ceiling, the dashboard displays) — the master
  switch is still the guarantee: with it off, every wired call site is byte-identical to before
  this module existed, and ``_resolve_lazy`` below makes that true even for equity itself (never
  read, never anchored, when the switch is off).

THE TRAP THIS MODULE EXISTS TO AVOID: THE DEADBAND. ``portfolio.equity()`` is
mark-to-market — every open position's unrealized P&L moves it on every price tick. Without a
deadband, a percentage-of-equity knob would jitter on every read: two KSS sessions opened a
minute apart, with no deposit or withdrawal between them, would be sized differently for no
reason a human could explain, and a session's wave-0 size would not even be stable across the
few seconds between a scan deciding to open it and the order actually placing. ``anchored_equity()``
freezes the anchor and only moves it once live equity has drifted by ``capital_scale_deadband_pct``
(default 10%) from the last anchor — deliberately coarse, and logged via ``audit.log`` every
time it moves so a sizing change is traceable to a specific anchor jump rather than looking
like unexplained variance.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app import audit, risk, runtime
from app.config import settings
from app.models import AuditLog


@dataclass(frozen=True)
class Scaled:
    """The result of resolving one dollar-denominated knob against equity.

    ``value`` is what a caller should actually use. When scaling is off (or the knob's own
    percentage is 0, meaning "off" the same way the absolute knob's 0 usually does), ``value``
    is exactly ``absolute`` and ``enabled`` is False — the two are the same object identity for
    floats, but the flag says so explicitly rather than making a caller compare them.
    """

    value: float
    pct: float
    equity: float
    absolute: float
    floored: bool
    enabled: bool


def resolve(*, absolute: float, pct: float, equity: float, floor: float = 0.0,
            enabled: bool) -> Scaled:
    """The one pure function every helper below goes through.

    ``enabled=False`` or ``pct<=0`` returns ``absolute`` untouched — today's behaviour, exactly.
    Otherwise ``value = max(floor, equity * pct / 100)``, so a small account is never sized
    below the floor (e.g. the exchange's own minimum notional) even though the percentage alone
    would compute dust.
    """
    if not enabled or pct <= 0:
        return Scaled(value=absolute, pct=pct, equity=equity, absolute=absolute,
                      floored=False, enabled=False)
    raw = equity * pct / 100.0
    value = max(floor, raw)
    return Scaled(value=value, pct=pct, equity=equity, absolute=absolute,
                  floored=raw < floor, enabled=True)


def _resolve_lazy(db: Session, *, absolute: float, pct: float, floor: float = 0.0) -> Scaled:
    """``resolve()``, but only touches equity when scaling is actually on.

    Phase 2 wiring finding: every public helper below is now on a hot path
    (``_apply_cash_cap`` runs on every BUY approval). Passing ``anchored_equity(db)`` as a plain
    argument evaluates it unconditionally, even with ``capital_scale_enabled=False`` — two extra
    queries (``risk.account_equity`` -> ``portfolio.equity`` -> a Position scan + a realized-PnL
    sum) on every call, and worse, ``anchored_equity``'s first-ever call WRITES the anchor to
    ``runtime_config`` regardless of the master switch. That is not the no-op this module
    promises. Guarding here — before equity is ever read — makes "off" truly free; ``equity=0.0``
    in the returned ``Scaled`` when disabled is a placeholder, not a real reading (callers never
    look at it in that case: ``value == absolute``, ``enabled`` is False).
    """
    if not settings.capital_scale_enabled:
        return Scaled(value=absolute, pct=pct, equity=0.0, absolute=absolute,
                      floored=False, enabled=False)
    return resolve(absolute=absolute, pct=pct, equity=anchored_equity(db), floor=floor,
                   enabled=True)


def _audit_floored(db: Session, knob: str, scaled: Scaled) -> None:
    """Surface a floored read — RULE 3: a knob silently pinned at the exchange minimum is
    exactly the kind of control that reports the SETTING instead of the EFFECT, which this
    project has paid for before. Logged at most once per distinct (knob, raw, floor) —
    mirrors the dedupe ``app.kss.service._audit_insufficient_fund`` already uses — so a caller
    on a hot path (every BUY approval) never floods the trail re-discovering the same shortfall.
    """
    if not scaled.floored:
        return
    raw = scaled.equity * scaled.pct / 100.0
    existing = (
        db.query(AuditLog)
        .filter(AuditLog.action == "capital_scale_floored", AuditLog.entity == knob)
        .all()
    )
    for row in existing:
        try:
            detail = json.loads(row.detail or "{}")
        except (TypeError, ValueError):
            continue
        if abs(detail.get("raw", 0.0) - raw) < 1e-6 and abs(detail.get("floor", 0.0) - scaled.value) < 1e-6:
            return  # already audited this exact shortfall
    audit.log(db, "capital_scale", "capital_scale_floored", entity=knob,
              raw=round(raw, 4), floor=round(scaled.value, 4))
    db.commit()


def anchored_equity(db: Session) -> float:
    """Live equity, deadbanded against a stored anchor — see the module docstring's TRAP.

    No anchor stored yet: store the live reading and return it. Otherwise, only move the
    anchor (and audit-log the move) once ``|live/anchor - 1| * 100 >= capital_scale_deadband_pct``;
    below that, return the stored anchor unchanged so repeated reads agree with each other
    between deposits/withdrawals-sized moves in equity.
    """
    live = risk.account_equity(db)
    raw = runtime.get(db, runtime.KEY_CAPITAL_SCALE_ANCHOR)
    if raw is None:
        runtime.set(db, runtime.KEY_CAPITAL_SCALE_ANCHOR, live)
        return live

    try:
        anchor = float(raw)
    except (TypeError, ValueError):
        anchor = 0.0
    if anchor <= 0:
        # Corrupt/zero stored value — cannot compute a drift ratio against it. Re-anchor
        # rather than divide by zero or silently trust a broken number.
        runtime.set(db, runtime.KEY_CAPITAL_SCALE_ANCHOR, live)
        return live

    drift_pct = abs(live / anchor - 1.0) * 100.0
    if drift_pct >= settings.capital_scale_deadband_pct:
        runtime.set(db, runtime.KEY_CAPITAL_SCALE_ANCHOR, live)
        audit.log(db, "capital_scale", "anchor_moved", old=round(anchor, 2),
                  new=round(live, 2), drift_pct=round(drift_pct, 4))
        db.commit()
        return live
    return anchor


def first_wave_usd(db: Session) -> Scaled:
    """``kss_first_wave_usd``, scaled by ``first_wave_pct`` of anchored equity.

    Floored at ``scan_min_notional`` — a wave the venue would refuse to place is worse than a
    small one, the same reasoning ``capital.recommend_sessions`` already applies.
    """
    scaled = _resolve_lazy(db, absolute=settings.kss_first_wave_usd, pct=settings.first_wave_pct,
                            floor=settings.scan_min_notional)
    _audit_floored(db, "first_wave_usd", scaled)
    return scaled


def cash_floor_usd(db: Session) -> Scaled:
    """``cash_floor_usd``, scaled by ``cash_floor_pct`` of anchored equity. No hard floor of its
    own — 0 is a legitimate answer (never refuse a BUY on cash grounds alone)."""
    scaled = _resolve_lazy(db, absolute=settings.cash_floor_usd, pct=settings.cash_floor_pct,
                            floor=0.0)
    _audit_floored(db, "cash_floor_usd", scaled)
    return scaled


def session_deploy_cap_usd(db: Session) -> Scaled:
    """``max_session_deploy_usd``, scaled by ``max_session_deploy_pct`` of anchored equity.
    0 (the default for both the absolute and the percentage knob) means "no cap", not "$0"."""
    scaled = _resolve_lazy(db, absolute=settings.max_session_deploy_usd,
                            pct=settings.max_session_deploy_pct, floor=0.0)
    _audit_floored(db, "session_deploy_cap_usd", scaled)
    return scaled


def live_order_notional_cap_usd(db: Session) -> Scaled:
    """``live_max_order_notional``, scaled by ``live_max_order_notional_pct`` of anchored
    equity. Floored at ``scan_min_notional`` for the same reason as ``first_wave_usd``."""
    scaled = _resolve_lazy(db, absolute=settings.live_max_order_notional,
                            pct=settings.live_max_order_notional_pct,
                            floor=settings.scan_min_notional)
    _audit_floored(db, "live_order_notional_cap_usd", scaled)
    return scaled


def autoapprove_notional_cap_usd(db: Session) -> Scaled:
    """``autoapprove_max_notional``, scaled by ``autoapprove_max_notional_pct`` of anchored
    equity. No hard floor: a tiny auto-approve ceiling on a tiny account is not a bug."""
    scaled = _resolve_lazy(db, absolute=settings.autoapprove_max_notional,
                            pct=settings.autoapprove_max_notional_pct, floor=0.0)
    _audit_floored(db, "autoapprove_notional_cap_usd", scaled)
    return scaled
