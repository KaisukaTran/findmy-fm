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
    kss_first_wave_usd: float = Field(
        default=0.0,
        ge=0,
        description="Target notional (USD) of a KSS session's FIRST wave. >0 sizes pip_size = "
        "first_wave_usd/entry so wave-0 ≈ this value (later waves keep the (n+1)× pyramid shape); "
        "0 = legacy pip_multiplier×minQty sizing. Raise to deploy more idle capital per session.",
    )
    max_position_size_pct: float = Field(default=10.0, description="Max position as % of equity.")
    max_daily_loss_pct: float = Field(default=5.0, description="Max daily loss as % of equity.")
    demo_isolated_fund: float = Field(default=10000.0, description="Default demo isolated fund (USD).")
    account_equity: float = Field(default=10000.0, description="Notional account equity for risk checks.")

    # --- Go-live execution (SHIPPED OFF — operator flips it) ---
    live_trading: bool = Field(
        default=False,
        description="Master switch for REAL-money order placement. Off = paper everywhere. "
        "When on AND exchange API keys are set, approved orders place real orders on "
        "`live_exchange`. New-exposure BUYs are still gated by the circuit breaker and "
        "`live_max_order_notional`; SELL exits are never gated.",
    )
    live_max_order_notional: float = Field(
        default=25.0,
        gt=0,
        description="Per-order notional cap (quote ccy) for live BUYs — a real BUY above this "
        "is refused. A small default keeps the first live orders tiny.",
    )
    live_api_key: SecretStr = Field(
        default=SecretStr(""),
        description="API key for the live exchange (private trading endpoints). Empty = live off.",
    )
    live_api_secret: SecretStr = Field(
        default=SecretStr(""),
        description="API secret for the live exchange. Empty = live off. Never logged.",
    )

    # --- Paper execution simulation ---
    taker_fee_pct: float = Field(default=0.1, description="Taker fee % applied per fill.")
    slippage_pct: float = Field(default=0.05, description="Simulated slippage % on market fills.")
    binance_max_fee_pct: float = Field(
        default=0.1,
        description="Binance's highest standard spot taker fee %. The take-profit floor is "
        "2x this — a session's tp_pct is raised to it so TP never fires on a gain that "
        "wouldn't even clear a round-trip's worth of the highest fee.",
    )

    # --- Market data ---
    price_cache_ttl: int = Field(default=60, description="Seconds to cache live prices.")
    tz_offset_hours: int = Field(
        default=7,
        description="Display timezone offset from UTC (Vietnam = +7). Storage stays UTC; this "
        "only shifts timestamps shown in the dashboard/charts.",
    )
    live_exchange: str = Field(default="kraken", description="ccxt exchange id for live prices (public, no key — e.g. binance/kraken/gateio/okx/bybit). NOTE: binance.com is reachable from this machine (re-verified 2026-06-13); override via .env LIVE_EXCHANGE.")
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
    backtest_lookback_days: int = Field(default=365, description="History window for win-rate estimate (longer = more regimes, less single-trend bias).")
    backtest_timeframe: str = Field(default="1d", description="Candle timeframe for backtest.")
    backtest_trial_spacing_days: float = Field(default=7.0, description="Min days between backtest entry points — decorrelates overlapping trials so the win-rate isn't inflated by one regime (0 = every bar).")
    min_trials: int = Field(default=8, description="Min completed backtest trials for a trustworthy win-rate; below this a pair is skipped (a 100%% from 3 trials is noise).")

    min_win_rate: float = Field(default=60.0, description="Min backtested win-rate %% (Wilson lower bound) to qualify. Paired with min_expectancy_pct as the primary trade rule: a pair trades when E ≥ min_expectancy_pct AND win-rate ≥ this.")
    min_confidence: float = Field(default=45.0, description="Min agent consensus %% to qualify a pair. S4: default lowered from 70 to 45 because the consensus is now a pure market-context score from {{trend,dip,volatility,liquidity,ml}} (backtest weight=0); the hard gates (E, win_lb) own the backtest evidence.")
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
    min_expectancy_pct: float = Field(default=3.0, description="PRIMARY gate: min mean net expected PnL %% per backtested trade (after stop-loss + round-trip cost). Paired with min_win_rate as the trade rule: trade when E ≥ this AND win-rate ≥ min_win_rate.")
    max_loss_rate: float = Field(default=20.0, description="Max backtested loss-rate %% to qualify.")
    min_net_edge: float = Field(default=0.5, description="Min TP%% above round-trip cost to trade (micro-trade guard).")
    walk_forward_split: float = Field(default=0.5, description="Fraction of history used in-sample; metric is out-of-sample.")
    max_concurrent_sessions: int = Field(default=10, description="Cap on simultaneously active sessions.")
    max_deployed_pct: float = Field(default=50.0, description="Cap total isolated funds as %% of equity.")
    scan_min_notional: float = Field(default=10.0, description="Skip dust micro-trades below this USD notional/wave.")

    # --- Loss-streak block: skip re-trading a pair on a recent losing streak ---
    loss_block_enabled: bool = Field(default=True, description="Block new KSS sessions on a pair with a recent consecutive-loss streak.")
    loss_streak_block_k: int = Field(default=2, description="Block a pair after this many consecutive losing closes (a win breaks the streak).")
    loss_streak_window_days: int = Field(default=14, description="Only count closes within this sliding window (days) — the block auto-decays as old losses age out.")

    # --- Grok scanner gate: a Grok (xAI) endorse/veto pass over qualified candidates ---
    grok_scanner_enabled: bool = Field(default=False, description="Have Grok review scanner candidates that passed every deterministic gate (one batched call/scan). Needs xai_api_key. Off = no cost, deterministic behaviour unchanged.")
    grok_scanner_fail_mode: str = Field(
        default="open",
        description=(
            "Grok scanner failure posture. 'open' (default): a symbol absent from the Grok "
            "verdict map (parse failure / timeout / not returned / batch-cap drop) is treated "
            "as endorsed — a Grok outage never blocks a deterministically-approved trade. "
            "'closed': a symbol WITHOUT an explicit endorse verdict must NOT open this scan "
            "(capital-preservation posture for full-auto). Allowed: 'open', 'closed'."
        ),
    )

    # --- TA evidence bundle: enrich the Grok gate with technical indicators ---
    # Tier 1 (pure-Python indicators) is ALWAYS on and feeds the bundle; these two flags only
    # toggle the optional overlays. Both default OFF, both fail-open back to Tier 1.
    ta_lib_enabled: bool = Field(default=False, description="Tier 2: overlay a few indicators via pandas-ta for extra precision. Requires `pip install pandas-ta` (pulls numpy+pandas); falls back to pure-Python if the import fails.")
    ta_external_enabled: bool = Field(default=False, description="Tier 3: merge external TA signals (taapi.io) into the bundle. Needs taapi_api_key and network. STUB until a provider is wired; fail-open.")
    taapi_api_key: SecretStr = Field(default=SecretStr(""), description="API key for the external TA source (taapi.io). Empty = Tier 3 off.")

    # --- Full-auto master switch + circuit breaker (Phase A) ---
    full_auto: bool = Field(
        default=False,
        description="Master switch: when on, scheduler + auto_trade + autoapprove run as one. Persisted via runtime_config.",
    )
    sl_pct: float = Field(default=8.0, description="KSS session stop-loss %% below avg price (0 = disabled).")
    trailing_pct: float = Field(default=3.0, description="KSS trailing-stop %% below peak once in profit (0 = disabled).")
    max_sessions_per_symbol: int = Field(
        default=1,
        description="Cap concurrent ACTIVE KSS sessions per symbol. 1 (K-1) keeps one owner "
        "per coin so the session avg == the symbol Position avg (no blended cost basis → no "
        "'take-profit that realizes a loss'). 0 = unlimited.",
    )
    stop_cooldown_min: float = Field(
        default=240.0,
        description="Minutes after a stop-loss/trailing exit before the scanner may re-open "
        "the same symbol (avoids immediate re-entry into a falling market). 0 = disabled.",
    )
    max_drawdown_pct: float = Field(default=15.0, description="Circuit breaker: freeze auto when equity drawdown %% exceeds this.")
    daily_loss_hard_pct: float = Field(default=5.0, description="Circuit breaker: freeze auto when today's realized loss %% exceeds this.")
    max_consecutive_losses: int = Field(default=4, description="Circuit breaker: freeze auto after this many losing SELL fills in a row.")
    breaker_cooldown_min: int = Field(default=60, description="Minutes the breaker stays frozen before it may auto-rearm.")

    # --- AI Guardian (Phase B): LLM veto layer over auto-approvals ---
    guardian_enabled: bool = Field(default=False, description="Run the Claude veto layer before auto-approving. Needs anthropic_api_key.")
    anthropic_api_key: SecretStr = Field(default=SecretStr(""), description="Anthropic API key for the AI Guardian. Empty = guardian no-op.")
    guardian_model: str = Field(default="claude-haiku-4-5-20251001", description="Claude model id used by the Guardian (cheap by default).")
    guardian_max_tokens: int = Field(default=1024, description="Max output tokens per Guardian review call.")
    guardian_fail_open: bool = Field(default=True, description="On Guardian error/timeout, allow auto-approval (fail-open) rather than block.")
    guardian_veto_ttl_min: int = Field(default=30, description="Minutes a Guardian veto holds before it expires and the order is re-reviewed. Prevents a transient veto from permanently deadlocking a KSS DCA wave. 0 = vetoes never expire.")

    # --- Telegram remote-kill (Phase B): alerts + /pause /resume /status /freeze /reset ---
    telegram_enabled: bool = Field(default=False, description="Enable the Telegram notifier + command poller. Needs token + chat id.")
    telegram_bot_token: SecretStr = Field(default=SecretStr(""), description="Telegram bot token (from @BotFather).")
    telegram_chat_id: str = Field(default="", description="Only this chat id may send commands and receive alerts.")
    telegram_poll_interval: int = Field(default=5, description="Seconds between Telegram getUpdates polls.")
    telegram_notify_trades: bool = Field(default=True, description="Push a Telegram alert on each fill (trade). Kill switch for trade alerts.")
    telegram_notify_risk: bool = Field(default=True, description="Push Telegram alerts on risk events (SL/trailing exits, breaker freeze, guardian veto).")
    telegram_digest_hours: int = Field(default=0, ge=0, description="Hours between periodic Telegram digest pushes (equity + today's P&L + open counts). 0 = off.")

    # --- Discord notifier (alternative to Telegram; works where TG is SNI/DPI-blocked) ---
    # Push alerts via a channel webhook (no bot needed); optional 2-way commands via a bot
    # gateway (NAT-friendly outbound WebSocket, like Telegram long-poll). Shares the
    # telegram_notify_* kill switches + telegram_digest_hours (channel-agnostic categories).
    discord_enabled: bool = Field(default=False, description="Enable the Discord notifier. Needs discord_webhook_url (alerts) and/or discord_bot_token+discord_channel_id (commands).")
    discord_webhook_url: SecretStr = Field(default=SecretStr(""), description="Discord channel webhook URL for pushing alerts. Empty = no Discord push. Never logged.")
    discord_bot_token: SecretStr = Field(default=SecretStr(""), description="Discord bot token for the 2-way command gateway. Requires the privileged MESSAGE CONTENT intent enabled in the Developer Portal. Empty = no inbound commands.")
    discord_channel_id: str = Field(default="", description="Only messages in this Discord channel id are accepted as commands (auth boundary). Empty = commands off.")

    # --- Phase C: per-pair hyperopt + ML win-rate (off by default) ---
    hyperopt_enabled: bool = Field(default=False, description="Tune KSS params per pair (grid search; falls back to global scan_* when off).")
    hyperopt_trials: int = Field(default=50, description="Param combinations evaluated per pair (out-of-sample objective).")
    hyperopt_interval_hours: int = Field(default=24, description="Hours between background hyperopt runs.")
    ml_enabled: bool = Field(default=False, description="Augment agent votes with a learned win-rate model.")
    ml_min_samples: int = Field(default=200, description="Min training samples before the ML model is used.")
    ml_weight: float = Field(default=0.25, description="Aggregator weight for the ML agent vote.")
    ml_retrain_hours: int = Field(default=24, description="Hours between background ML retrains.")

    # --- Pending-queue auto-approval policy (AI clears safe orders) ---
    autoapprove_enabled: bool = Field(default=False, description="Let the AI auto-approve pending orders matching the rule.")
    autoapprove_max_notional: float = Field(default=50.0, description="Auto-approve only orders with notional ≤ this USD value.")
    autoapprove_sources: list[str] = Field(default=["kss"], description="Only auto-approve orders from these sources.")
    autoapprove_require_no_risk: bool = Field(default=False, description="If true, never auto-approve orders carrying a risk note.")

    # --- OPUS orchestrator mode (advanced, independent full-auto; see docs/opus-orchestrator-plan.md) ---
    # OFF by default. Opus is advisory inside a deterministic sandbox; every order still
    # flows through the approval queue + circuit breaker + kill switch (paper-only).
    opus_mode: bool = Field(default=False, description="Master switch for OPUS orchestrator mode (persisted in runtime_config).")
    opus_allocation_usd: float = Field(default=2000.0, description="Capital envelope carved out for OPUS mode. KPI denominator; rule-based mode sees equity minus this.")
    opus_interval_min: int = Field(default=5, description="Minutes between OPUS decision ticks.")
    opus_daily_cost_cap_usd: float = Field(default=5.0, description="Hard ceiling on Opus API spend per day; exceeded → OPUS pauses new decisions.")
    opus_kpi_target_pct: float = Field(default=1.0, description="Net-profit KPI target on invested capital per rolling 24h (%).")
    opus_model: str = Field(default="claude-opus-4-8", description="Claude model id for the OPUS orchestrator brain.")
    opus_max_tokens: int = Field(default=2048, description="Max output tokens per OPUS decision call.")
    opus_price_in_per_mtok: float = Field(default=15.0, description="Opus input price (USD per million tokens) for cost metering.")
    opus_price_out_per_mtok: float = Field(default=75.0, description="Opus output price (USD per million tokens) for cost metering.")
    opus_cost_multiplier: float = Field(default=2.0, description="Multiplier applied to raw Opus cost before counting it against net profit (requirement #5).")
    opus_ride_hard_sl_pct: float = Field(default=10.0, description="Hard stop-loss for a 'ride' position so a reversing winner can't become an unbounded loss.")
    opus_max_trade_notional: float = Field(default=200.0, description="Per-trade notional cap for OPUS discretionary orders.")
    opus_shadow: bool = Field(default=True, description="Shadow mode: Opus intents are logged but NOT executed. Flip off to let the sandbox route orders.")

    # --- Grok co-pilot (xAI) for consensus decisions with OPUS (off by default) ---
    # When enabled WITH a key, OPUS (Claude) and Grok (xAI) each decide on the SAME snapshot;
    # consensus = OPEN only if BOTH agree, CLOSE if EITHER wants out (fast risk, slow entry).
    grok_enabled: bool = Field(default=False, description="Add Grok (xAI) as a second decision agent alongside OPUS (needs xai_api_key).")
    xai_api_key: SecretStr = Field(default=SecretStr(""), description="xAI API key for the Grok co-pilot. Empty = Grok off.")
    grok_model: str = Field(default="grok-3", description="xAI Grok model id (OpenAI-compatible chat API).")
    grok_max_tokens: int = Field(default=2048, description="Max output tokens per Grok decision call.")
    grok_price_in_per_mtok: float = Field(default=3.0, description="Grok input price (USD per million tokens) for cost metering.")
    grok_price_out_per_mtok: float = Field(default=15.0, description="Grok output price (USD per million tokens).")
    grok_role: str = Field(default="risk", description="Grok's mandate: 'risk' (skeptical second opinion) or 'peer' (equal alpha agent).")

    # --- Operating-cost tracking (trade fee + withdrawal fee + VAT + AI cost) ---
    # Withdrawal fee = (withdrawal_fee_pct + withdrawal_fee_tolerance_pct) × amount; VAT =
    # vat_pct × amount. Both are booked ONLY when a withdrawal is recorded. AI cost reads the
    # metered OpusCostLedger first and falls back to these monthly estimates for empty periods.
    withdrawal_fee_pct: float = Field(default=0.0, ge=0, description="Exchange (Binance) withdrawal fee, %% of the withdrawn amount. Operator sets the real rate.")
    withdrawal_fee_tolerance_pct: float = Field(default=0.05, ge=0, description="Safety buffer %% added on top of withdrawal_fee_pct (dung sai).")
    vat_pct: float = Field(default=10.0, ge=0, description="VAT %% charged on the withdrawn amount, per withdrawal.")
    ai_monthly_claude_usd: float = Field(default=25.0, ge=0, description="Fallback estimate of Claude (Anthropic) API spend per month, used when a period has no metered cost.")
    ai_monthly_grok_usd: float = Field(default=20.0, ge=0, description="Fallback estimate of Grok (xAI) API spend per month, used when a period has no metered cost.")


settings = Settings()
