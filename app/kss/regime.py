"""
Pure regime router: classify a scanner candidate into a KSS strategy mode.

See docs/pyramid-up-plan.md Phase 0. A single strategy mode (DCA-down,
buy-the-dip) under-deploys capital on a coin that rips straight up after entry
— the WLFI session-26 case (entry 0.05793 rallied to +5.5% without ever
touching the wave-1 dip target, so only $80 of $1,597 reserved was deployed).
This router tags a candidate ``'pyramid_up'`` (scale into strength) when its
TA already looks like a strong uptrend outperforming BTC, and falls back to
the existing ``'dca_down'`` (buy-the-dip) for everything else, including a
dip/downtrend candidate where averaging down is the right shape.

FROZEN-safe / pure: no DB, no network, no `settings` import — every tunable is
an argument so the service layer (and the scanner) can call this with whatever
the runtime knobs currently are, and so it stays trivially unit-testable.
"""

from __future__ import annotations


def classify_mode(
    *,
    enabled: bool,
    htf_trend: str | None,
    st_trend: str | None,
    adx: float,
    rel_strength: float,
    macdh: float,
    min_rel_strength: float,
    min_adx: float,
) -> str:
    """Return ``'pyramid_up'`` or ``'dca_down'`` for this candidate.

    ``'dca_down'`` is always the safe default/fallback:
      - the router is OFF (``enabled=False``) → zero behavior change, always
        ``'dca_down'`` regardless of any signal;
      - any signal is missing/None or fails a threshold → ``'dca_down'``.

    ``'pyramid_up'`` requires ALL of:
      1. uptrend on either timeframe: ``htf_trend == 'up' OR st_trend == 'up'``
         (trend strings may be 'up'/'down'/'flat'/None; None is just "no
         signal", never treated as 'up');
      2. ``rel_strength > min_rel_strength`` — the coin is outperforming BTC,
         not just rising because the whole market is up;
      3. ``macdh > 0`` — momentum confirms (MACD histogram positive);
      4. ``adx >= min_adx`` — the trend is strong enough to be worth riding,
         not noise.
    """
    if not enabled:
        return "dca_down"

    is_uptrend = htf_trend == "up" or st_trend == "up"
    if not is_uptrend:
        return "dca_down"

    if rel_strength <= min_rel_strength:
        return "dca_down"

    if macdh <= 0:
        return "dca_down"

    if adx < min_adx:
        return "dca_down"

    return "pyramid_up"
