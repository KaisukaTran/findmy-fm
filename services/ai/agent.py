"""Core AI trading agent using Claude via Anthropic SDK with tool use."""

import json
import logging
from typing import Optional

from src.findmy.config import settings
from .prompts import TRADING_SYSTEM_PROMPT
from .tools import TOOL_DEFINITIONS, execute_tool
from .decision_log import log_decision

logger = logging.getLogger(__name__)


class TradingSignal:
    def __init__(self, symbol: str, signal: str, confidence: float,
                 reasoning: str, suggested_price: Optional[float] = None,
                 suggested_quantity_usdt: Optional[float] = None,
                 risk_note: str = ""):
        self.symbol = symbol
        self.signal = signal  # BUY | SELL | HOLD
        self.confidence = confidence
        self.reasoning = reasoning
        self.suggested_price = suggested_price
        self.suggested_quantity_usdt = suggested_quantity_usdt or settings.ai_max_spend_usdt
        self.risk_note = risk_note

    def should_trade(self) -> bool:
        return self.signal in ("BUY", "SELL") and self.confidence >= settings.ai_confidence_threshold


class AITradingAgent:
    """
    Autonomous trading agent. Uses Claude with tool use to analyze markets
    and generate trading signals. Does NOT directly place orders — that's
    done by the runner via submit_ai_order().
    """

    def __init__(self):
        self._client = None

    def _get_client(self):
        if self._client is None:
            try:
                import anthropic
            except ImportError:
                raise RuntimeError("anthropic package not installed. Run: pip install anthropic")
            key = settings.anthropic_api_key
            if key is None:
                raise RuntimeError("ANTHROPIC_API_KEY not configured")
            api_key = key.get_secret_value() if hasattr(key, "get_secret_value") else str(key)
            self._client = anthropic.Anthropic(api_key=api_key)
        return self._client

    def analyze(self, symbol: str, extra_context: str = "") -> TradingSignal:
        """
        Run full agentic loop for one symbol:
        Claude reasons → calls tools → gets data → produces final signal.
        """
        client = self._get_client()
        messages = [
            {
                "role": "user",
                "content": (
                    f"Analyze {symbol} and decide whether to BUY, SELL, or HOLD. "
                    f"Use the available tools to fetch current price and recent OHLCV data. "
                    f"Also check current positions and daily P&L. "
                    f"{extra_context}"
                    f"Respond with a JSON signal in the specified format."
                )
            }
        ]

        max_rounds = 5
        last_response = None
        hit_max = False
        for round_idx in range(max_rounds):
            last_response = client.messages.create(
                model=settings.ai_model,
                max_tokens=1024,
                system=TRADING_SYSTEM_PROMPT,
                tools=TOOL_DEFINITIONS,
                messages=messages,
            )

            if last_response.stop_reason != "tool_use":
                break

            tool_uses = [b for b in last_response.content if getattr(b, "type", None) == "tool_use"]
            if not tool_uses:
                break

            if round_idx == max_rounds - 1:
                hit_max = True
                logger.warning(f"Max agent rounds reached for {symbol}; tool calls remained")
                break

            messages.append({"role": "assistant", "content": last_response.content})
            tool_results = []
            for tu in tool_uses:
                result = execute_tool(tu.name, tu.input)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tu.id,
                    "content": json.dumps(result),
                })
            messages.append({"role": "user", "content": tool_results})

        if last_response is None:
            return TradingSignal(symbol=symbol, signal="HOLD", confidence=0.0,
                                 reasoning="No response from model")

        if hit_max:
            return TradingSignal(symbol=symbol, signal="HOLD", confidence=0.0,
                                 reasoning="Max agent rounds reached without final signal")

        return self._parse_signal(symbol, last_response)

    def _parse_signal(self, symbol: str, response) -> TradingSignal:
        """Extract structured signal from Claude's final response."""
        text = ""
        for block in response.content:
            if hasattr(block, "text"):
                text += block.text

        try:
            # Find JSON block in response
            start = text.find("{")
            end = text.rfind("}") + 1
            if start >= 0 and end > start:
                data = json.loads(text[start:end])
                sig = data.get("signal", "HOLD")
                conf = float(data.get("confidence", 0.0))
                return TradingSignal(
                    symbol=symbol,
                    signal=sig,
                    confidence=conf,
                    reasoning=data.get("reasoning", ""),
                    suggested_price=data.get("suggested_price"),
                    suggested_quantity_usdt=data.get("suggested_quantity_usdt"),
                    risk_note=data.get("risk_note", ""),
                )
        except Exception as e:
            logger.warning(f"Signal parse error for {symbol}: {e} | text={text[:200]}")

        return TradingSignal(symbol=symbol, signal="HOLD", confidence=0.0,
                             reasoning="Parse error — defaulting to HOLD")


def submit_ai_order(signal: TradingSignal, consultant_votes: Optional[dict] = None) -> Optional[int]:
    """
    Submit an AI-trusted order through the pending orders service.
    The order bypasses manual approval if within ai_max_spend_usdt.
    Returns pending_order_id or None if blocked.
    """
    from services.sot.pending_orders_service import queue_ai_order

    if not signal.should_trade():
        log_decision(
            symbol=signal.symbol, signal=signal.signal,
            confidence=signal.confidence, reasoning=signal.reasoning,
            action="SKIPPED", consultant_votes=consultant_votes,
        )
        return None

    try:
        order, violation = queue_ai_order(
            symbol=signal.symbol,
            side=signal.signal,  # BUY or SELL
            quantity_usdt=signal.suggested_quantity_usdt,
            price=signal.suggested_price,
            confidence=signal.confidence,
            reasoning=signal.reasoning,
        )
        action = "ORDER_SUBMITTED" if order else f"BLOCKED:{violation}"
        order_id = order.id if order else None
        log_decision(
            symbol=signal.symbol, signal=signal.signal,
            confidence=signal.confidence, reasoning=signal.reasoning,
            action=action, pending_order_id=order_id,
            consultant_votes=consultant_votes,
        )
        return order_id
    except Exception as e:
        logger.error(f"submit_ai_order error: {e}")
        log_decision(
            symbol=signal.symbol, signal=signal.signal,
            confidence=signal.confidence, reasoning=signal.reasoning,
            action=f"ERROR:{e}", consultant_votes=consultant_votes,
        )
        return None


# Module-level singleton
_agent: Optional[AITradingAgent] = None


def get_agent() -> AITradingAgent:
    global _agent
    if _agent is None:
        _agent = AITradingAgent()
    return _agent
