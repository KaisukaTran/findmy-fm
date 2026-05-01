"""Technical analysis consultant — uses RSI + MA crossover, no LLM cost."""

import logging
from .base import ConsultantAgent, ConsultantVote

logger = logging.getLogger(__name__)


class TechnicalConsultant(ConsultantAgent):
    """
    Votes based on RSI (14) and fast/slow MA crossover from recent OHLCV.
    AGREE if technicals confirm the proposed signal direction.
    """

    def __init__(self, name: str = "technical", config: dict | None = None):
        super().__init__(name, config)
        self.rsi_period = int((config or {}).get("rsi_period", 14))
        self.fast_ma = int((config or {}).get("fast_ma", 9))
        self.slow_ma = int((config or {}).get("slow_ma", 21))

    def vote(self, symbol: str, signal) -> ConsultantVote:
        try:
            closes = self._get_closes(symbol)
            if len(closes) < self.slow_ma + 1:
                return ConsultantVote("ABSTAIN", 0.5, "Insufficient data")

            rsi = self._rsi(closes, self.rsi_period)
            fast = sum(closes[-self.fast_ma:]) / self.fast_ma
            slow = sum(closes[-self.slow_ma:]) / self.slow_ma
            bullish = fast > slow and rsi < 70
            bearish = fast < slow and rsi > 30

            if signal.signal == "BUY":
                if bullish:
                    return ConsultantVote("AGREE", 0.75, f"MA bullish, RSI={rsi:.1f}")
                if bearish:
                    return ConsultantVote("DISAGREE", 0.7, f"MA bearish, RSI={rsi:.1f}")
                return ConsultantVote("ABSTAIN", 0.5, "Neutral technicals")

            if signal.signal == "SELL":
                if bearish:
                    return ConsultantVote("AGREE", 0.75, f"MA bearish, RSI={rsi:.1f}")
                if bullish:
                    return ConsultantVote("DISAGREE", 0.7, f"MA bullish, RSI={rsi:.1f}")
                return ConsultantVote("ABSTAIN", 0.5, "Neutral technicals")

            return ConsultantVote("ABSTAIN", 0.5, "HOLD signal — no vote")

        except Exception as e:
            logger.warning(f"TechnicalConsultant error for {symbol}: {e}")
            return ConsultantVote("ABSTAIN", 0.0, f"Error: {e}")

    def _get_closes(self, symbol: str) -> list[float]:
        from src.findmy.services.market_data import get_historical_ohlcv
        candles = get_historical_ohlcv(symbol, "1h", self.slow_ma + self.rsi_period + 5)
        return [c[4] for c in candles]

    def _rsi(self, closes: list[float], period: int) -> float:
        deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
        gains = [d for d in deltas[-period:] if d > 0]
        losses = [-d for d in deltas[-period:] if d < 0]
        avg_gain = sum(gains) / period if gains else 0.0
        avg_loss = sum(losses) / period if losses else 0.0
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))
