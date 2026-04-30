"""LLM-based consultant agent — uses Claude to vote on proposed signals."""

import json
import logging
from .base import ConsultantAgent, ConsultantVote
from services.ai.prompts import CONSULTANT_SYSTEM_PROMPT

logger = logging.getLogger(__name__)


class LLMConsultant(ConsultantAgent):
    """
    Uses a configured Claude model to vote on a proposed signal.
    Cheaper/faster model (haiku) by default.
    """

    def __init__(self, name: str, config: dict | None = None):
        super().__init__(name, config)
        self._client = None

    def _get_client(self):
        if self._client is None:
            import anthropic
            from src.findmy.config import settings
            key = settings.anthropic_api_key
            if key is None:
                raise RuntimeError("ANTHROPIC_API_KEY not set")
            api_key = key.get_secret_value() if hasattr(key, "get_secret_value") else str(key)
            self._client = anthropic.Anthropic(api_key=api_key)
        return self._client

    def vote(self, symbol: str, signal) -> ConsultantVote:
        from src.findmy.config import settings
        model = self.config.get("model", settings.ai_consultant_model)
        extra_prompt = self.config.get("system_prompt_extra", "")

        system = CONSULTANT_SYSTEM_PROMPT
        if extra_prompt:
            system += f"\n\nAdditional focus: {extra_prompt}"

        user_msg = (
            f"Symbol: {symbol}\n"
            f"Proposed signal: {signal.signal}\n"
            f"Confidence: {signal.confidence}\n"
            f"Reasoning: {signal.reasoning}\n"
            f"Risk note: {signal.risk_note}\n\n"
            f"Should this trade proceed? Vote AGREE, DISAGREE, or ABSTAIN."
        )

        try:
            client = self._get_client()
            response = client.messages.create(
                model=model,
                max_tokens=256,
                system=system,
                messages=[{"role": "user", "content": user_msg}],
            )
            text = response.content[0].text if response.content else ""
            start = text.find("{")
            end = text.rfind("}") + 1
            if start >= 0 and end > start:
                data = json.loads(text[start:end])
                return ConsultantVote(
                    vote=data.get("vote", "ABSTAIN"),
                    confidence=float(data.get("confidence", 0.5)),
                    reasoning=data.get("reasoning", ""),
                )
        except Exception as e:
            logger.warning(f"LLMConsultant {self.name} error: {e}")

        return ConsultantVote("ABSTAIN", 0.0, "Error or no response")
