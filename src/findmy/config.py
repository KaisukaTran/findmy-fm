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

    live_trading: bool = Field(
        default=False,
        description="Enable live trading on real exchange (default: paper trading)"
    )

    class Config:
        """Pydantic settings configuration."""
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = False
        extra = "ignore"  # Ignore extra environment variables


# Global settings instance
settings = Settings()
