"""
Configuration management for FINDMY trading bot.

Uses pydantic-settings to load configuration from environment variables and .env files.
All sensitive credentials (API keys, secrets) are marked as SecretStr to prevent
accidental logging or display.

Environment variables will override .env file settings.
For local development, use .env file (never commit real secrets).
For production/cloud, set environment variables directly.
"""

from pydantic_settings import BaseSettings
from pydantic import SecretStr, Field
from typing import Optional


class Settings(BaseSettings):
    """
    Application settings loaded from environment variables and .env files.
    
    Attributes:
        broker_api_key: API key for broker/exchange (future live trading use)
        broker_api_secret: API secret for broker/exchange (future live trading use)
        broker_base_url: Base URL for broker API
        app_secret_key: Secret key for JWT signing, session encryption (required)
        database_url: Database connection URL (optional, defaults to SQLite)
    """

    # Broker / Live trading API keys (future use)
    broker_api_key: Optional[str] = Field(
        default=None,
        description="API key for broker/exchange. Required for live trading adapters (v2.0+)"
    )
    broker_api_secret: Optional[SecretStr] = Field(
        default=None,
        description="API secret for broker/exchange. Required for live trading adapters (v2.0+)"
    )
    broker_base_url: Optional[str] = Field(
        default=None,
        description="Base URL for broker API. Required for live trading adapters (v2.0+)"
    )

    # Application secrets
    app_secret_key: SecretStr = Field(
        description="Secret key for JWT signing, session encryption, and other security features. "
                    "Must be a strong random string in production."
    )

    # Database configuration
    database_url: Optional[str] = Field(
        default=None,
        description="Database connection URL. Defaults to SQLite in data/ directory if not set."
    )

    # Risk Management & Pip Sizing (v0.6.0)
    pip_multiplier: float = Field(
        default=2.0,
        description="Pip multiplier: 1 pip = pip_multiplier × minQty. Default: 2.0"
    )
    max_position_size_pct: float = Field(
        default=10.0,
        description="Maximum position size as % of account equity. Default: 10%"
    )
    max_daily_loss_pct: float = Field(
        default=5.0,
        description="Maximum daily loss as % of account equity. Default: 5%"
    )

    # Paper Trading Fund (v1.1.0)
    initial_fund: float = Field(
        default=10000.0,
        description="Initial demo fund in USD. Default: $10,000"
    )

    # Transaction Cost Simulation (v1.1.0)
    maker_fee_rate: float = Field(
        default=0.0,
        description="Maker fee rate (e.g. 0.001 = 0.1%). Default: 0 (disabled)"
    )
    taker_fee_rate: float = Field(
        default=0.0,
        description="Taker fee rate (e.g. 0.001 = 0.1%). Default: 0 (disabled)"
    )
    slippage_pct: float = Field(
        default=0.0,
        description="Max slippage as fraction of price (e.g. 0.0005 = 0.05%). Default: 0 (disabled)"
    )
    fill_pct: float = Field(
        default=1.0,
        description="Order fill percentage (1.0 = full fill, 0.5 = 50% partial). Default: 1.0"
    )

    live_trading: bool = Field(
        default=False,
        description="Enable live trading on real exchange (default: paper trading)"
    )
    live_trading_dry_run: bool = Field(
        default=True,
        description="When True, orders go to Binance testnet only. "
                    "Set False only after end-to-end testnet validation."
    )
    max_orders_per_minute: int = Field(
        default=10,
        description="Maximum orders per minute for circuit breaker"
    )

    # AI Agent settings
    anthropic_api_key: Optional[SecretStr] = Field(
        default=None,
        description="Anthropic API key for Claude AI agent"
    )
    ai_model: str = Field(
        default="claude-sonnet-4-6",
        description="Claude model for main trading decisions"
    )
    ai_consultant_model: str = Field(
        default="claude-haiku-4-5-20251001",
        description="Claude model for consultant agents (faster/cheaper)"
    )
    ai_confidence_threshold: float = Field(
        default=0.7,
        description="Minimum AI confidence (0-1) to submit an order"
    )
    ai_max_spend_usdt: float = Field(
        default=500.0,
        description="Maximum USDT value per AI-submitted order"
    )
    ai_daily_target_pct: float = Field(
        default=0.5,
        description="Daily profit target percentage (0.5 = 0.5%)"
    )
    ai_loop_interval_seconds: int = Field(
        default=300,
        description="Seconds between AI agent analysis loops (default: 5 min)"
    )
    ai_paper_min_days: int = Field(
        default=7,
        description="Minimum paper trading days before promotion to live"
    )
    ai_paper_min_win_rate: float = Field(
        default=0.5,
        description="Minimum win rate required for paper→live promotion"
    )
    ai_paper_max_drawdown_pct: float = Field(
        default=3.0,
        description="Max allowed drawdown % in paper before promotion blocked"
    )
    ai_max_symbols: int = Field(
        default=5,
        description="Max number of symbols to analyze per loop"
    )
    ai_exchange_order_limit_pct: float = Field(
        default=0.75,
        description="Use at most this fraction of exchange rate limit (0.75 = 75%)"
    )

    class Config:
        """Pydantic settings configuration."""
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = False
        extra = "ignore"  # Ignore extra environment variables


# Global settings instance
settings = Settings()
