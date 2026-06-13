"""Tier 2 — optional pandas-ta adapter.

Lazily imports pandas/pandas-ta (NOT in the default install — they pull numpy+pandas into
an otherwise dependency-light project). Enable with `ta_lib_enabled=true` AND
`pip install pandas-ta`. Any import or per-indicator failure raises, and `bundle.build`
catches it and falls back to the pure-Python Tier 1 — so enabling this can only add
precision, never break a scan.

Only overlays a few keys where a vetted library implementation is worth more than the
hand-rolled one; the rest of the bundle stays Tier 1.
"""

from __future__ import annotations

from app.data.providers import Candle


def enrich(candles: list[Candle]) -> dict:
    """Return a dict of bundle keys computed via pandas-ta. Raises if the library is
    missing or a computation fails (caller falls back to Tier 1)."""
    import pandas as pd  # lazy: only imported when ta_lib_enabled
    import pandas_ta as pta

    if len(candles) < 35:
        return {}

    df = pd.DataFrame(candles)
    out: dict = {}

    rsi = pta.rsi(df["close"], length=14)
    if rsi is not None and len(rsi.dropna()):
        out["rsi"] = round(float(rsi.dropna().iloc[-1]), 1)

    macd = pta.macd(df["close"], fast=12, slow=26, signal=9)
    if macd is not None and len(macd.dropna()):
        hist_col = [c for c in macd.columns if c.startswith("MACDh")]
        if hist_col:
            price = float(df["close"].iloc[-1]) or 1.0
            out["macd_h"] = round(float(macd[hist_col[0]].dropna().iloc[-1]) / price * 100, 2)

    adx = pta.adx(df["high"], df["low"], df["close"], length=14)
    if adx is not None and len(adx.dropna()):
        adx_col = [c for c in adx.columns if c.startswith("ADX")]
        if adx_col:
            out["adx"] = round(float(adx[adx_col[0]].dropna().iloc[-1]), 1)

    return out
