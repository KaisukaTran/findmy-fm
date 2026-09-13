"""Decimal places a KSS price is rounded to — ONE rule for wave targets, dynamic exits and
Pyramid-UP triggers.

Why this exists: the rule used to be a three-bucket ladder (BTC-like 2, ETH-like 4, everything
else 6) copied into three modules. Six decimals is fine for a $0.07 coin and catastrophic for a
sub-cent one: PEPE at 3.47e-06 became 3e-06 — a wave-0 limit 13.5% under the market, DCA rungs
at −13% instead of −4%, and on paper a fill at a price no venue ever printed (2026-09-13,
session 23: +$9.99 that did not exist). SHIB lost 4.7%, BONK 6.8% the same way.

The rule now keeps at least five significant digits below $100 (six decimals stays the floor,
so every price ≥ $0.01 rounds exactly as before) and the two large-price buckets unchanged.
"""

from __future__ import annotations

import math


def price_precision(reference_price: float) -> int:
    """Decimal places for *reference_price* (BTC-like 2, ETH-like 4, ≥ five significant digits
    below $100 with a floor of 6)."""
    if reference_price >= 10_000:
        return 2
    if reference_price >= 100:
        return 4
    if reference_price <= 0:
        return 6
    # 4 − floor(log10 p) decimals keeps five significant digits: 0.0679 → 6, 3.47e-06 → 10.
    return max(6, 4 - math.floor(math.log10(reference_price)))
