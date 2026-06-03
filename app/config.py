"""
Configuration for FINDMY-FM (lean rebuild).

Loads from environment variables and a local .env file via pydantic-settings.
Secrets use SecretStr so they are never accidentally logged or serialized.
"""

from __future__ import annotations

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings. Env vars override .env; extra vars ignored."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Security ---
    app_secret_key: SecretStr = Field(
        default=SecretStr("dev-insecure-change-me"),
        description="Secret for signing/session use. Set a strong random value in production.",
    )
    api_key: SecretStr = Field(
        default=SecretStr("dev-key"),
        description="API key required (X-API-Key header) for write/mutation endpoints.",
    )
    require_auth: bool = Field(
        default=False,
        description="If true, enforce X-API-Key on write endpoints. Off by default for local demo.",
    )
    cors_origins: list[str] = Field(
        default=["http://localhost:8000", "http://127.0.0.1:8000"],
        description="Allowed CORS origins (no wildcard with credentials).",
    )

    # --- Database (single SQLite file) ---
    database_url: str = Field(
        default="sqlite:///./data/findmy.db",
        description="SQLAlchemy database URL. Defaults to a local SQLite file.",
    )

    # --- KSS / risk / pip sizing ---
    pip_multiplier: float = Field(default=2.0, description="1 pip = pip_multiplier × minQty.")
    max_position_size_pct: float = Field(default=10.0, description="Max position as % of equity.")
    max_daily_loss_pct: float = Field(default=5.0, description="Max daily loss as % of equity.")
    demo_isolated_fund: float = Field(default=10000.0, description="Default demo isolated fund (USD).")
    account_equity: float = Field(default=10000.0, description="Notional account equity for risk checks.")

    # --- Paper execution simulation ---
    taker_fee_pct: float = Field(default=0.1, description="Taker fee % applied per fill.")
    slippage_pct: float = Field(default=0.05, description="Simulated slippage % on market fills.")

    # --- Market data ---
    price_cache_ttl: int = Field(default=60, description="Seconds to cache Binance prices.")


settings = Settings()
