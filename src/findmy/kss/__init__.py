"""
KSS (Kai Strategy Service) - Automated DCA strategies.

This module provides:
- PyramidSession: Pyramid DCA strategy with wave-based entries
- KSSManager: Manages multiple concurrent sessions

v0.10.0: Initial implementation with Pyramid DCA.
"""

from src.findmy.kss.pyramid import PyramidSession, PyramidSessionStatus, WaveInfo
from src.findmy.kss.manager import KSSManager

__all__ = [
    "PyramidSession",
    "PyramidSessionStatus",
    "WaveInfo",
    "KSSManager",
]
