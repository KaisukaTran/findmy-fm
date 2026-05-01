"""Base class for AI consultant agents."""

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class ConsultantVote:
    vote: str  # AGREE | DISAGREE | ABSTAIN
    confidence: float
    reasoning: str


class ConsultantAgent(ABC):
    """Abstract base for consultant agents that vote on proposed signals."""

    def __init__(self, name: str, config: dict | None = None):
        self.name = name
        self.config = config or {}

    @abstractmethod
    def vote(self, symbol: str, signal) -> ConsultantVote:
        """Given a symbol and proposed TradingSignal, return a vote."""
        ...
