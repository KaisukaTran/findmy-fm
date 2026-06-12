"""Tier 1 — pure-Python technical indicators (no numpy/pandas/ta-lib).

Deterministic: given the same candles, each function returns the same value. Every
indicator degrades to a neutral value when there isn't enough data, so the scanner can
always build a bundle. Candle shape: {ts, open, high, low, close, volume}.

Kept self-contained (no dependency on app.agents) so the TA layer is a leaf module.
"""

from __future__ import annotations

import statistics

from app.data.providers import Candle

# --- small helpers ------------------------------------------------------------


def _closes(candles: list[Candle]) -> list[float]:
    return [c["close"] for c in candles]


def sma(values: list[float], n: int) -> float:
    """Simple moving average of the last n values (or all, if fewer)."""
    if not values:
        return 0.0
    window = values[-n:]
    return sum(window) / len(window)


def _ema_series(values: list[float], n: int) -> list[float]:
    """EMA series, seeded with the first value (standard MACD convention)."""
    if not values:
        return []
    k = 2.0 / (n + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def _true_ranges(candles: list[Candle]) -> list[float]:
    """True range per candle (index 0 = 0.0, no prior close)."""
    trs = [0.0]
    for i in range(1, len(candles)):
        h, low = candles[i]["high"], candles[i]["low"]
        pc = candles[i - 1]["close"]
        trs.append(max(h - low, abs(h - pc), abs(low - pc)))
    return trs


def _wilder_atr_series(candles: list[Candle], n: int) -> list[float]:
    """Wilder-smoothed ATR aligned per candle; values valid from index n onward."""
    trs = _true_ranges(candles)
    atr = [0.0] * len(candles)
    if len(candles) <= n:
        return atr
    atr[n] = sum(trs[1 : n + 1]) / n
    for i in range(n + 1, len(candles)):
        atr[i] = (atr[i - 1] * (n - 1) + trs[i]) / n
    return atr


# --- momentum / oscillators ---------------------------------------------------


def rsi(values: list[float], n: int = 14) -> float:
    """Classic RSI; 50 (neutral) when there isn't enough data."""
    if len(values) <= n:
        return 50.0
    gains, losses = 0.0, 0.0
    for i in range(len(values) - n, len(values)):
        delta = values[i] - values[i - 1]
        if delta >= 0:
            gains += delta
        else:
            losses -= delta
    if losses == 0:
        return 100.0
    rs = (gains / n) / (losses / n)
    return 100 - 100 / (1 + rs)


def macd(candles: list[Candle], fast: int = 12, slow: int = 26, signal: int = 9) -> dict:
    """MACD line/signal/histogram. `hist_pct` normalizes the histogram by price so it's
    comparable across pairs of different absolute price."""
    cs = _closes(candles)
    if len(cs) < slow + signal:
        return {"line": 0.0, "signal": 0.0, "hist": 0.0, "hist_pct": 0.0}
    ef, es = _ema_series(cs, fast), _ema_series(cs, slow)
    line = [a - b for a, b in zip(ef, es, strict=True)]
    sig = _ema_series(line, signal)
    last_line, last_sig = line[-1], sig[-1]
    price = cs[-1] or 1.0
    return {
        "line": last_line,
        "signal": last_sig,
        "hist": last_line - last_sig,
        "hist_pct": (last_line - last_sig) / price * 100,
    }


def bollinger(candles: list[Candle], n: int = 20, k: float = 2.0) -> dict:
    """Bollinger %B (price position in the band, 0..1) and bandwidth (% of the mid)."""
    cs = _closes(candles)
    if len(cs) < n:
        return {"pct_b": 0.5, "bandwidth": 0.0}
    window = cs[-n:]
    mid = statistics.fmean(window)
    sd = statistics.pstdev(window)
    upper, lower = mid + k * sd, mid - k * sd
    width = upper - lower
    pct_b = (cs[-1] - lower) / width if width else 0.5
    return {"pct_b": pct_b, "bandwidth": (width / mid * 100) if mid else 0.0}


# --- trend / volatility -------------------------------------------------------


def atr_pct(candles: list[Candle], n: int = 14) -> float:
    """ATR as a percentage of the last close (volatility regime, price-agnostic)."""
    if len(candles) <= n:
        return 0.0
    atr = _wilder_atr_series(candles, n)[-1]
    price = candles[-1]["close"] or 1.0
    return atr / price * 100


def adx(candles: list[Candle], n: int = 14) -> dict:
    """Wilder ADX with +DI/-DI. ADX gauges trend STRENGTH (not direction); +DI>-DI = up.
    Returns neutral (adx 0, di 0) when data is too short."""
    if len(candles) < 2 * n + 1:
        return {"adx": 0.0, "plus_di": 0.0, "minus_di": 0.0}
    trs = _true_ranges(candles)
    plus_dm, minus_dm = [0.0], [0.0]
    for i in range(1, len(candles)):
        up = candles[i]["high"] - candles[i - 1]["high"]
        down = candles[i - 1]["low"] - candles[i]["low"]
        plus_dm.append(up if (up > down and up > 0) else 0.0)
        minus_dm.append(down if (down > up and down > 0) else 0.0)

    def _wilder(series: list[float]) -> list[float]:
        sm = [0.0] * len(series)
        sm[n] = sum(series[1 : n + 1])
        for i in range(n + 1, len(series)):
            sm[i] = sm[i - 1] - sm[i - 1] / n + series[i]
        return sm

    str_, pdm, mdm = _wilder(trs), _wilder(plus_dm), _wilder(minus_dm)
    dx: list[float] = []
    for i in range(n, len(candles)):
        if str_[i] == 0:
            dx.append(0.0)
            continue
        pdi = 100 * pdm[i] / str_[i]
        mdi = 100 * mdm[i] / str_[i]
        denom = pdi + mdi
        dx.append(100 * abs(pdi - mdi) / denom if denom else 0.0)
    if len(dx) < n:
        return {"adx": 0.0, "plus_di": 0.0, "minus_di": 0.0}
    adx_val = sum(dx[:n]) / n
    for d in dx[n:]:
        adx_val = (adx_val * (n - 1) + d) / n
    last_str = str_[-1] or 1.0
    return {
        "adx": adx_val,
        "plus_di": 100 * pdm[-1] / last_str,
        "minus_di": 100 * mdm[-1] / last_str,
    }


def supertrend(candles: list[Candle], n: int = 10, mult: float = 3.0) -> str:
    """Supertrend direction at the last candle: 'up', 'down', or 'flat' (insufficient data)."""
    length = len(candles)
    if length < n + 2:
        return "flat"
    atr = _wilder_atr_series(candles, n)
    f_upper = [0.0] * length
    f_lower = [0.0] * length
    direction = [1] * length  # 1 = up, -1 = down
    for i in range(n, length):
        c = candles[i]
        hl2 = (c["high"] + c["low"]) / 2
        bu, bl = hl2 + mult * atr[i], hl2 - mult * atr[i]
        if i == n:
            f_upper[i], f_lower[i] = bu, bl
            direction[i] = 1 if c["close"] >= hl2 else -1
            continue
        prev_close = candles[i - 1]["close"]
        f_upper[i] = bu if (bu < f_upper[i - 1] or prev_close > f_upper[i - 1]) else f_upper[i - 1]
        f_lower[i] = bl if (bl > f_lower[i - 1] or prev_close < f_lower[i - 1]) else f_lower[i - 1]
        if direction[i - 1] == 1:
            direction[i] = -1 if c["close"] < f_lower[i] else 1
        else:
            direction[i] = 1 if c["close"] > f_upper[i] else -1
    return "up" if direction[-1] == 1 else "down"


# --- structure / volume -------------------------------------------------------


def support_resistance(candles: list[Candle], lookback: int = 60) -> dict:
    """Distance (% of price) to the nearest recent resistance above and support below.
    Uses the recent high/low envelope as a robust proxy for the levels."""
    if len(candles) < 5:
        return {"res_dist_pct": 0.0, "sup_dist_pct": 0.0}
    window = candles[-lookback:]
    price = candles[-1]["close"] or 1.0
    highs = [c["high"] for c in window]
    lows = [c["low"] for c in window]
    above = [h for h in highs if h > price]
    below = [low for low in lows if low < price]
    res = min(above) if above else max(highs)
    sup = max(below) if below else min(lows)
    return {
        "res_dist_pct": (res - price) / price * 100,
        "sup_dist_pct": (price - sup) / price * 100,
    }


def volume_trend(candles: list[Candle], n: int = 20) -> dict:
    """Last-volume vs its SMA (a spike ratio) and OBV slope sign over the window."""
    if len(candles) < n + 1:
        return {"vol_ratio": 1.0, "obv_up": False}
    vols = [c["volume"] for c in candles]
    avg = sma(vols, n) or 1.0
    obv = [0.0]
    for i in range(1, len(candles)):
        c, pc = candles[i]["close"], candles[i - 1]["close"]
        obv.append(obv[-1] + (vols[i] if c > pc else (-vols[i] if c < pc else 0.0)))
    return {"vol_ratio": vols[-1] / avg, "obv_up": (obv[-1] - obv[-n]) > 0}


def htf_trend(candles: list[Candle], factor: int = 7, n: int = 20) -> str:
    """Higher-timeframe trend: down-sample closes by `factor`, compare last vs SMA.
    Returns 'up', 'down', or 'flat'. (factor=7 on 1d candles ≈ a weekly view.)"""
    cs = _closes(candles)
    grouped = [cs[i] for i in range(factor - 1, len(cs), factor)]
    if len(grouped) < 5:
        return "flat"
    ref = sma(grouped, min(n, len(grouped)))
    return "up" if grouped[-1] >= ref else "down"
