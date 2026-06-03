"""Shared types and indicator helpers for the quant agents.

Agents are deterministic: given the same candles they return the same vote.
Each vote carries a score in [0,1] (how favorable), a confidence in [0,1]
(how sure, e.g. enough data), and a human-readable reason for the audit trail.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Protocol

from app.data.providers import Candle


@dataclass
class AgentVote:
    name: str
    score: float       # 0..1, higher = more favorable for a KSS entry
    confidence: float  # 0..1, higher = more data / more certain
    reason: str

    def to_dict(self) -> dict:
        return {"name": self.name, "score": round(self.score, 4),
                "confidence": round(self.confidence, 4), "reason": self.reason}


class Agent(Protocol):
    name: str

    def evaluate(self, symbol: str, candles: list[Candle], ctx: dict) -> AgentVote: ...


# --- helpers ------------------------------------------------------------


def clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def closes(candles: list[Candle]) -> list[float]:
    return [c["close"] for c in candles]


def sma(values: list[float], n: int) -> float:
    if not values:
        return 0.0
    window = values[-n:]
    return sum(window) / len(window)


def rsi(values: list[float], n: int = 14) -> float:
    """Classic RSI; returns 50 (neutral) when there isn't enough data."""
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


def returns(values: list[float]) -> list[float]:
    out = []
    for i in range(1, len(values)):
        if values[i - 1]:
            out.append(values[i] / values[i - 1] - 1)
    return out


def realized_vol_pct(values: list[float], n: int = 30) -> float:
    """Stdev of recent returns, as a percentage."""
    r = returns(values[-(n + 1):])
    if len(r) < 2:
        return 0.0
    return statistics.pstdev(r) * 100


def triangular(x: float, lo: float, peak: float, hi: float) -> float:
    """Score 1.0 at `peak`, linearly falling to 0 at `lo`/`hi`. Outside -> 0."""
    if x <= lo or x >= hi:
        return 0.0
    if x == peak:
        return 1.0
    if x < peak:
        return (x - lo) / (peak - lo)
    return (hi - x) / (hi - peak)
