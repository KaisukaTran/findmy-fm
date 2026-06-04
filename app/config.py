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
    price_cache_ttl: int = Field(default=60, description="Seconds to cache live prices.")
    live_exchange: str = Field(default="binance", description="ccxt exchange id for live prices.")
    data_exchange: str = Field(
        default="kraken",
        description="ccxt exchange id for backtest/scan history (public, no key — e.g. kraken/coinbase).",
    )

    # --- Scanner / multi-agent decision layer ---
    watchlist: list[str] = Field(
        default=["BTC", "ETH", "SOL"], description="Symbols always evaluated by the scanner."
    )
    scan_top_n: int = Field(default=10, description="Also auto-scan the top-N symbols by volume.")
    min_quote_volume: float = Field(
        default=1_000_000.0,
        description="Scan ALL pairs whose quote volume is above this floor (liquidity filter).",
    )
    scan_max_symbols: int = Field(default=50, description="Hard cap on symbols evaluated per scan.")
    backtest_lookback_days: int = Field(default=180, description="History window for win-rate estimate.")
    backtest_timeframe: str = Field(default="1d", description="Candle timeframe for backtest.")

    min_win_rate: float = Field(default=80.0, description="Min backtested win-rate %% to qualify a pair.")
    min_confidence: float = Field(default=70.0, description="Min agent consensus %% to qualify a pair.")
    deadline_days: int = Field(default=30, description="Max days a KSS session may wait for TP.")
    auto_trade: bool = Field(
        default=False,
        description="Full-auto: auto-approve qualifying KSS orders. Off = semi-auto (human approves).",
    )
    scheduler_enabled: bool = Field(
        default=False, description="Run the background scan/manage loop. Off by default."
    )
    scan_interval_min: int = Field(default=15, description="Minutes between scheduler cycles.")

    # Default KSS parameters used when the scanner proposes a session
    scan_distance_pct: float = Field(default=2.0, description="Distance %% per wave for proposed sessions.")
    scan_tp_pct: float = Field(default=3.0, description="Take-profit %% for proposed sessions.")
    scan_max_waves: int = Field(default=10, description="Max waves for proposed sessions.")
    scan_fund: float = Field(default=1000.0, description="Isolated fund per proposed session (USD).")

    # --- Loss-minimizing / cost-aware gates (capital preservation) ---
    max_loss_rate: float = Field(default=20.0, description="Max backtested loss-rate %% to qualify.")
    min_net_edge: float = Field(default=0.5, description="Min TP%% above round-trip cost to trade (micro-trade guard).")
    walk_forward_split: float = Field(default=0.5, description="Fraction of history used in-sample; metric is out-of-sample.")
    max_concurrent_sessions: int = Field(default=5, description="Cap on simultaneously active sessions.")
    max_deployed_pct: float = Field(default=50.0, description="Cap total isolated funds as %% of equity.")
    scan_min_notional: float = Field(default=10.0, description="Skip dust micro-trades below this USD notional/wave.")


settings = Settings()
