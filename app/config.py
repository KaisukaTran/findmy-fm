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
        default=True,
        description="If true, enforce X-API-Key on write/mutation endpoints. ON by default — the "
        "dashboard prompts for the key once and stores it in sessionStorage. The app refuses to boot "
        "if this is on while api_key is still a shipped default, and live trading always requires it.",
    )
    cors_origins: list[str] = Field(
        default=["http://localhost:8000", "http://127.0.0.1:8000"],
        description="Allowed CORS origins (no wildcard with credentials).",
    )
    auth_max_failures: int = Field(
        default=10,
        description="Failed X-API-Key attempts from one IP within auth_lockout_window_sec "
        "before that IP is locked out.",
    )
    auth_lockout_window_sec: int = Field(
        default=60,
        description="Sliding window (seconds) over which failed-auth attempts are counted.",
    )
    auth_lockout_sec: int = Field(
        default=60,
        description="How long (seconds) an IP stays locked out after tripping "
        "auth_max_failures. 429 with Retry-After during lockout.",
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
    # --- Live-readiness knobs (additive; inert until the live maker/async path is built) ---
    maker_orders: bool = Field(
        default=False,
        description="LIVE only: place entries + take-profit as post-only LIMIT_MAKER (saves the "
        "spread/slippage; at VIP0 maker==taker fee). Risk exits (SL/trailing/close) stay MARKET. "
        "No effect on paper.",
    )
    order_fill_timeout_sec: int = Field(
        default=0,
        ge=0,
        description="LIVE only: cancel a resting maker order if unfilled after this many seconds "
        "(0 = wait indefinitely, the usual DCA behaviour). No effect on paper.",
    )
    live_use_testnet: bool = Field(
        default=False,
        description="LIVE only: route real orders to the exchange TESTNET (ccxt set_sandbox_mode) "
        "instead of production — validate the live path before using real keys. No effect on paper.",
    )
    scheduler_lock_port: int = Field(
        default=8801,
        description="Localhost port for the single-process scheduler mutex. Give each parallel "
        "instance (e.g. paper vs live) a DISTINCT port so both schedulers run; same port across two "
        "processes = only one scheduler (the cross-process lock behind the concurrency-cap fix).",
    )

    # --- Execution capital guard ---
    cash_floor_usd: float = Field(
        default=0.0, ge=0,
        description="HARD floor: account cash may never drop below this. Every BUY is "
        "partial-filled down to the cash that's actually available (or rejected if even a "
        "min-notional slice won't fit), so cash can never go negative. 0 = cash ≥ 0 always. "
        "SELL exits are never gated. Applies to paper AND live.",
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
    tp_fee_coverage: float = Field(
        default=1.2,
        ge=0,
        description="Every take-profit target adds this multiple of the round-trip fee "
        "(buy + sell = 2x binance_max_fee_pct) on top of the session's tp_pct, so a TP always "
        "clears its fees with a margin. 1.2 = +120% of the total fee. Applies to paper AND live.",
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
    live_ws_prices: bool = Field(
        default=True,
        description="Live instance only: stream real-time prices from the Binance public "
        "WebSocket (!miniTicker@arr) to keep the price cache warm so trailing-stop/crash-detect "
        "react sub-second instead of every kss_exit_check_sec. Gated by live_trading — paper "
        "never starts it. REST stays the fallback if the socket drops.",
    )
    ws_stale_sec: int = Field(
        default=10,
        description="Live WS price feed: if no ticker message has arrived within this many "
        "seconds the socket is treated as down and get_current_prices(force=True) falls back "
        "to a REST fetch.",
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
    min_trials: int = Field(default=15, description="Min completed backtest trials for a trustworthy win-rate; below this a pair is skipped (a 100%% from a handful of trials is noise). Raised 8→15 to cut thin-sample false positives.")
    block_downtrend_adx: float = Field(default=25.0, description="Hard entry gate: veto a 'trade' candidate when the higher-timeframe trend AND Supertrend are BOTH down with ADX ≥ this (a confirmed downtrend — avoid catching a falling knife). 0 = off. Deterministic mirror of Grok's most common veto, so it works even when Grok is off.")
    entry_momentum_gate: bool = Field(default=True, description="Tighter entry-timing gate than block_downtrend_adx: veto a 'trade' candidate whose SHORT-TERM momentum is falling (Supertrend down AND MACD histogram < 0) so a DCA ladder is not opened into a coin actively dropping (it would just sit red until a bounce). Catches mild/short-term drops the downtrend gate (which needs HTF+ST+ADX all confirming) lets through. False = off.")
    rel_strength_enabled: bool = Field(default=False, description="Entry gate (relative strength vs BTC): only open a coin whose recent return keeps pace with BTC — skip if its N-bar return < BTC's N-bar return − margin. Blocks 'alts bleeding vs BTC' (the pattern behind our open-book losses) WITHOUT a blanket BTC-down block: a strong alt while BTC falls still passes (it outperforms). N = rel_strength_lookback_bars, margin = rel_strength_margin_pct. False = off.")
    rel_strength_lookback_bars: int = Field(default=7, description="Lookback (daily bars) for the relative-strength-vs-BTC entry gate.")
    rel_strength_margin_pct: float = Field(default=2.0, description="Allowed underperformance vs BTC before the relative-strength gate blocks a coin (2 = may lag BTC by up to 2%%; 0 = must match BTC).")
    mae_quartile_gate_enabled: bool = Field(default=False, description="Relative drawdown gate: among the candidates that pass every other gate in a scan, drop the worst quartile by backtested worst_mae (deepest single dip below avg). RELATIVE (per-scan), so it never nukes the universe the way an absolute worst_mae cutoff does (−15%% would reject ~81%%). Needs ≥4 candidates. False = off.")
    overextension_penalty_enabled: bool = Field(default=False, description="SOFT rank-penalty (NOT a gate, docs/loss-cases.md Phase 3): losing sessions tended to open when the coin had already run up hard, so when the scanner can't open every candidate, de-prioritize (never block) hot entries by subtracting a penalty from the open-ranking's leading consensus term, proportional to the coin's recent run-up (overextension_lookback_bars N-bar %% return, positive-only — a flat/falling coin is never rewarded). False = off (byte-identical ranking to today).")
    overextension_penalty_weight: float = Field(default=0.5, description="Consensus-points subtracted per 1%% of positive N-bar run-up when overextension_penalty_enabled. E.g. weight=0.5 and a +13%% run-up → −6.5 points off that candidate's OPEN-RANKING key only (does not touch the persisted consensus_pct).")
    overextension_lookback_bars: int = Field(default=20, description="Lookback (bars) for the overextension rank-penalty's recent-run-up measurement.")

    strategy_router_enabled: bool = Field(default=False, description="Master switch for the regime router (docs/pyramid-up-plan.md): when on, the scanner classifies each candidate 'pyramid_up' (scale into strength) vs 'dca_down' (buy-the-dip, the existing path) from its TA signals before opening. False = every candidate stays 'dca_down' — zero behaviour change.")
    pyramid_up_min_rel_strength: float = Field(default=0.0, description="Pyramid-UP regime gate: min relative-strength-vs-BTC return (%%) a candidate must exceed to route to 'pyramid_up' instead of 'dca_down'. Higher = only the clearest BTC-outperformers get the scale-into-strength mode.")
    pyramid_up_min_adx: float = Field(default=20.0, description="Pyramid-UP regime gate: min ADX (trend strength) a candidate must have to route to 'pyramid_up'. Below this the uptrend is too weak/noisy to ride; falls back to 'dca_down'.")
    pyramid_up_step_pct: float = Field(default=2.0, description="Pyramid-UP ladder: %% above entry between each add-on trigger (add n triggers at entry×(1+this%%)^n). Smaller = adds trigger sooner/closer together; larger = waits for a stronger confirmed move before scaling in.")
    pyramid_up_size_ratio: float = Field(default=0.7, description="Pyramid-UP ladder: each add-on quantity = this ratio × the previous wave's quantity (anti-martingale — strictly decreasing). Clamped to (0, 1) exclusive; e.g. 0.7 = each add is 70%% the size of the one before.")
    pyramid_up_max_adds: int = Field(default=2, description="Pyramid-UP ladder: max number of add-on waves above the base entry (capped at 3 regardless of this value — pyramid_up.MAX_ADDS_CAP). 0 = base wave only, no scaling in.")
    pyramid_up_lock_pct: float = Field(default=1.0, description="Pyramid-UP risk management: after each add fills, the stop moves to break-even-plus = avg×(1+this%%) (floored at the fee break-even) — every add is a free-roll, net risk never exceeds the risk taken on the base wave.")

    min_win_rate: float = Field(default=60.0, description="Min backtested win-rate %% (Wilson lower bound) to qualify. Paired with min_expectancy_pct as the primary trade rule: a pair trades when E ≥ min_expectancy_pct AND win-rate ≥ this.")
    min_confidence: float = Field(default=45.0, description="Min agent consensus %% to qualify a pair. S4: default lowered from 70 to 45 because the consensus is now a pure market-context score from {{trend,dip,volatility,liquidity}} (backtest weight=0); the hard gates (E, win_lb) own the backtest evidence.")
    deadline_days: int = Field(default=30, description="Max days a KSS session may wait for TP.")
    auto_trade: bool = Field(
        default=False,
        description="Full-auto: auto-approve qualifying KSS orders. Off = semi-auto (human approves).",
    )
    scheduler_enabled: bool = Field(
        default=False, description="Run the background scan/manage loop. Off by default."
    )
    scan_interval_min: int = Field(default=15, description="Minutes between scheduler cycles. On a daily timeframe the backtest barely changes within an hour, so a longer interval cuts CPU + exchange weight + Grok cost without losing accuracy.")
    scan_fetch_workers: int = Field(default=2, ge=1, le=8, description="Parallel OHLCV fetch threads in a cold-cache scan. Each thread owns its own ccxt client with an INDEPENDENT rate limiter, so N workers burst at ~N×20 req/s — keep low to stay under the exchange's per-IP weight limit (esp. with a long lookback and/or paper+live sharing one IP).")

    # Default KSS parameters used when the scanner proposes a session
    scan_distance_pct: float = Field(default=2.0, description="Distance %% per wave for proposed sessions.")
    scan_tp_pct: float = Field(default=3.0, description="Take-profit %% for proposed sessions.")
    scan_max_waves: int = Field(default=10, description="Max waves for proposed sessions.")
    scan_fund: float = Field(default=1000.0, description="Isolated fund per proposed session (USD).")

    # --- Loss-minimizing / cost-aware gates (capital preservation) ---
    min_expectancy_pct: float = Field(default=3.0, description="PRIMARY gate: min mean net expected PnL %% per backtested trade (after stop-loss + round-trip cost). Paired with min_win_rate as the trade rule: trade when E ≥ this AND win-rate ≥ min_win_rate.")
    max_loss_rate: float = Field(default=20.0, description="Max backtested loss-rate %% to qualify.")
    max_avg_mae_pct: float = Field(default=0.0, description="Drawdown gate: skip a candidate whose backtested mean max-adverse-excursion (avg_mae — the typical deepest unrealized dip below the running avg before exit) is DEEPER than this %% (e.g. 12 = reject coins that historically plunge >12%% before recovering). 0 = off (still used for ranking: shallower drawdown ranks higher). Discriminates coins the saturated ~100%% win-rate cannot.")
    min_net_edge: float = Field(default=0.5, description="Min TP%% above round-trip cost to trade (micro-trade guard).")
    walk_forward_split: float = Field(default=0.5, description="Fraction of history used in-sample; metric is out-of-sample.")
    max_concurrent_sessions: int = Field(default=10, description="Cap on simultaneously active sessions.")
    max_new_sessions_per_scan: int = Field(default=5, ge=0, description="Cap on NEW KSS sessions opened in a single scan (0 = no limit). Ramps exposure gradually instead of opening the whole concurrent budget at once; within the cap the highest win-rate-lower-bound / most-tried candidates open first.")
    equity_backup_pct: float = Field(default=25.0, description="Reserve %% of LIVE equity the bot never deploys (backup). KSS open-gate budget = (100 − this)%% × equity.")
    scan_min_notional: float = Field(default=10.0, description="Skip dust micro-trades below this USD notional/wave.")

    # --- Loss-streak block: skip re-trading a pair on a recent losing streak ---
    loss_block_enabled: bool = Field(default=True, description="Block new KSS sessions on a pair with a recent consecutive-loss streak.")
    loss_streak_block_k: int = Field(default=2, description="Block a pair after this many consecutive losing closes (a win breaks the streak).")
    loss_streak_window_days: int = Field(default=14, description="Only count closes within this sliding window (days) — the block auto-decays as old losses age out.")

    # --- Loss re-entry escalation: space out re-opening a coin that got STOPPED OUT (hard-SL) ---
    # A "large loss" = a hard-SL close (source_ref …:sl); trailing exits (…:trail_sl) do NOT count.
    # The block escalates by how many times the coin has been stopped out, then blacklists it.
    loss_reentry_enabled: bool = Field(default=True, description="Block re-opening a coin after a hard stop-loss, escalating by how many times it was stopped out (weeks → months → blacklist). Counts every hard-SL close (…:sl); trailing exits don't count.")
    loss_reentry_weeks_1: int = Field(default=2, description="Weeks to block a coin after its 1st hard-SL stop-out.")
    loss_reentry_weeks_2: int = Field(default=8, description="Weeks to block after the 2nd hard-SL (≈2 months). The block auto-decays once this many weeks pass since the last stop-out.")
    loss_reentry_blacklist_after: int = Field(default=3, description="Hard-SL count at which a coin is blacklisted (blocked indefinitely). Clear a coin via loss_reentry_pardon.")
    loss_reentry_pardon: str = Field(default="", description="Comma-separated symbols exempt from the loss re-entry block (manual pardon / un-blacklist). e.g. 'C, MIRA'.")

    # --- Per-session deploy cap (capital-preservation wall, Phase 0 live-readiness) ---
    max_session_deploy_usd: float = Field(default=0.0, description="Hard ceiling on the total USD a SINGLE session may deploy (filled + still-pending waves). Any wave — auto-chain, manual DCA+, or the Telegram '➕' button — that would breach it is refused. 0 = off. The missing 'top fix': the C disaster deployed $162k unbounded. Set this for live (e.g. ~15-20% of equity).")

    # --- Grok scanner gate: a Grok (xAI) endorse/veto pass over qualified candidates ---
    grok_scanner_enabled: bool = Field(default=False, description="Have Grok review scanner candidates that passed every deterministic gate (one batched call/scan). Needs xai_api_key. Off = no cost, deterministic behaviour unchanged.")
    grok_scanner_batch_max: int = Field(default=60, ge=1, le=300, description="Max candidates Grok reviews per scan (single batched call). Set high enough to cover EVERY 'trade' candidate so none opens unreviewed; the batch is sorted by expectancy so the strongest are kept if it ever truncates. Larger = more tokens/call.")
    grok_live_search: bool = Field(default=False, description="Route the Grok scanner gate through the xAI Agent-Tools API (/v1/responses) with server-side web_search + x_search, so Grok weighs real-time trending/sentiment/major catalysts, not just the numeric TA bundle. Grok decides when to search; adds search cost per scan. Needs grok_scanner_enabled. (The old chat 'Live Search' params were retired by xAI — 410.)")
    grok_search_max_results: int = Field(default=8, ge=1, le=30, description="Max web/x search results Grok may pull per scan call (caps search cost).")
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

    # --- Dynamic trailing TP/SL (docs/kss-dynamic-tp-plan.md) — service-layer, OFF by default ---
    kss_dynamic_tp_enabled: bool = Field(default=False, description="Master toggle for the dynamic trailing channel: once a session clears avg*(1+distance%%) it cancels its DCA ladder and rides a volatility-aware trailing SL with a floating TP, instead of the fixed tp_pct. OFF = today's behaviour unchanged.")
    kss_tp_gap_pct: float = Field(default=5.0, description="Spike-grab TP ceiling: this %% above the ratcheted SL. A high value effectively disables the spike-grab so the trailing SL is the sole exit (pure chandelier).")
    kss_exit_fee_mult: float = Field(default=3.0, description="Fee-safe floor multiplier: both the dynamic TP and SL are floored at avg*(1 + this x round_trip_cost%%), so NO automatic trailing exit ever books a loss — not even a fee loss. >=1; 3 = comfortably above round-trip cost.")
    kss_trail_atr_mult: float = Field(default=1.0, description="Dynamic trailing-stop distance = this x the coin's daily ATR%% (volatility-aware: rides the coin's normal range, exits only on a genuine reversal).")
    kss_trail_min_pct: float = Field(default=3.0, description="Floor for the trailing distance — the SL never trails closer than this %% below the peak (so a calm coin still gets room and noise never stops it out). Also the fallback when ATR is unavailable.")
    kss_trail_arm_pct: float = Field(default=5.0, description="Ride & Trail: a profitable session RIDES (no fixed-TP cap, protected only by the hard SL — full room to run) until peak ≥ avg*(1+this%%), at which point the trailing stop ARMS. Higher = run further before locking, but bigger giveback risk (a reversal before arming falls to the hard SL).")
    kss_trail_lock_pct: float = Field(default=2.0, description="Ride & Trail: once armed, the trailing SL never locks LESS than this %% profit (floor = max(fee_floor, avg*(1+this%%))) — so a wide ATR trail can't pin the stop back at break-even. Should be < kss_trail_arm_pct (the gap is the room given at arming).")
    kss_exit_check_sec: int = Field(default=90, description="Interval (seconds) of the lightweight position-guard loop that checks OPEN-session exits using cached tickers, decoupled from the 30-min full scan. Lower = smaller gap window, more ticker calls. Should be > price_cache_ttl is NOT required — the guard forces a fresh price.")
    kss_crash_drop_pct: float = Field(default=12.0, description="Crash-detect: if a guard check sees price drop more than this %% since the last observation AND price is at/below the SL, exit at market immediately (caps further bleed on a gap). 0 = off.")
    kss_live_stop_orders: bool = Field(default=False, description="LIVE only: maintain a resting STOP-MARKET on the exchange at the current SL for server-side (ms) gap protection. Inert on paper / when live automation is off.")
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
    daily_loss_hard_usd: float = Field(default=0.0, description="Absolute USD realized-loss cap for one UTC day. 0 = off (use daily_loss_hard_pct). Trips the breaker when today's realized loss ≥ this many USD — binds regardless of equity size, unlike the % rule.")
    weekly_loss_hard_pct: float = Field(default=0.0, description="Rolling 7-day realized loss as % of equity. 0 = off. Catches a slow multi-day bleed that never breaches the daily rule.")
    weekly_loss_hard_usd: float = Field(default=0.0, description="Rolling 7-day realized loss absolute USD cap. 0 = off.")
    max_drawdown_usd: float = Field(default=0.0, description="Absolute USD equity-drawdown cap. 0 = off (use max_drawdown_pct). Only wired if portfolio exposes peak equity; otherwise documented as reserved.")

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
    telegram_api_base: str = Field(
        default="https://api.telegram.org",
        description="Base URL for the Telegram Bot API. Point at a reverse-proxy you control "
        "(e.g. a Cloudflare Worker) to bypass an SNI/DPI block on api.telegram.org — this "
        "network blocks the direct host. Token is appended as /bot<token>. No trailing slash.",
    )
    telegram_chat_id: str = Field(default="", description="Only this chat id may send commands and receive alerts.")
    telegram_poll_interval: int = Field(default=5, description="Seconds between Telegram getUpdates polls.")
    telegram_poll_commands: bool = Field(default=True, description="Run the getUpdates command poller on THIS instance. When paper + live share one bot, only ONE may poll (Telegram delivers each update once); set false on the secondary (e.g. live) so it sends labelled alerts but never steals the command stream.")
    telegram_sibling_url: str = Field(default="", description="Base URL of the sibling instance (e.g. http://127.0.0.1:8001) used to route targeted commands like '/pause live'. Empty = no cross-instance routing (targeted commands reply that it is unconfigured).")
    telegram_push_enabled: bool = Field(default=False, description="MASTER switch for PROACTIVE pushes (trade/risk fills + periodic digest). Default False = the bot only REPLIES to commands you send, never pushes unsolicited messages. The per-category telegram_notify_trades/_risk + digest knobs only matter when this is True. Command replies and the manual /api/telegram/test are never gated by this.")
    telegram_notify_trades: bool = Field(default=True, description="Push a Telegram alert on each fill (trade). Kill switch for trade alerts (only applies when telegram_push_enabled=True).")
    telegram_notify_risk: bool = Field(default=True, description="Push Telegram alerts on risk events (SL/trailing exits, breaker freeze, guardian veto).")
    telegram_notify_maxdca: bool = Field(default=True, description="Show the full-ladder (max-DCA) section in /summary + the 'Liệt kê' button. Its own kill switch.")
    maxdca_allow_add: bool = Field(default=False, description="Show the '➕ Thêm DCA' button on the Liệt kê cards. OFF by default = the list is INFORMATIONAL (only shows exhausted ladders + a 'Bỏ qua' mute) so you can't accidentally mass-add DCA into deep losers. The deliberate /dca_add <id> command always works.")
    maxdca_max_underwater_pct: float = Field(default=8.0, description="Exclude a full-ladder session from the Liệt kê list once the coin has FALLEN more than this %% from ENTRY (the ladder anchor, not avg — DCA keeps uPnL-vs-avg near zero) — don't invite averaging into a knife that already dropped far. 0 = no depth filter.")
    telegram_digest_hours: int = Field(default=0, ge=0, description="Hours between periodic Telegram digest pushes (equity + today's P&L + open counts). 0 = off.")

    # --- Discord notifier (alternative to Telegram; works where TG is SNI/DPI-blocked) ---
    # Push alerts via a channel webhook (no bot needed); optional 2-way commands via a bot
    # gateway (NAT-friendly outbound WebSocket, like Telegram long-poll). Shares the
    # telegram_notify_* kill switches + telegram_digest_hours (channel-agnostic categories).
    discord_enabled: bool = Field(default=False, description="Enable the Discord notifier. Needs discord_webhook_url (alerts) and/or discord_bot_token+discord_channel_id (commands).")
    discord_webhook_url: SecretStr = Field(default=SecretStr(""), description="Discord channel webhook URL for pushing alerts. Empty = no Discord push. Never logged.")
    discord_bot_token: SecretStr = Field(default=SecretStr(""), description="Discord bot token for the 2-way command gateway. Requires the privileged MESSAGE CONTENT intent enabled in the Developer Portal. Empty = no inbound commands.")
    discord_channel_id: str = Field(default="", description="Only messages in this Discord channel id are accepted as commands (auth boundary). Empty = commands off.")

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
    # --- God-mode scaffolding (Phase O-FIX; wiring deferred to O-COPY/O-LEARN/O-LIVE) ---
    opus_copy_mode: bool = Field(default=False, description="Bias OPUS toward symbols the rule-based engine endorsed this scan.")
    opus_solo_open: bool = Field(default=False, description="Allow OPUS to open without Grok's vote when conviction clears the floor (wiring deferred to a later phase).")
    opus_solo_min_consensus: float = Field(default=70.0, description="Deterministic-consensus floor required for a solo open.")
    opus_lessons_max: int = Field(default=8, description="Max distilled lessons injected into the system prompt.")
    opus_history_n: int = Field(default=20, description="Closed positions shown in the self-history block.")

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

    # --- Market-wide BTC regime gate (SHADOW-FIRST; see app/regime.py) ---
    # No per-coin veto can stop a CORRELATED selloff across the whole book (17/18 historical
    # loss cases were SL cascades in a broad market dump). This classifies BTC's daily trend
    # (50d/200d SMA with hysteresis) into risk_on/risk_off/neutral/unknown and, only when
    # explicitly flipped to enforcing, blocks NEW session opens (never DCA waves, never exits).
    regime_gate_enabled: bool = Field(
        default=False,
        description="Master switch for the BTC regime gate. Off = the gate does nothing at "
        "all — no classification, no audit, zero cost. On = evaluate every scan and audit the "
        "state (shadow observability), regardless of regime_gate_enforcing.",
    )
    regime_gate_enforcing: bool = Field(
        default=False,
        description="When regime_gate_enabled AND this is True AND the state is risk_off: "
        "BLOCK all NEW session opens this scan (never touches existing sessions/waves/exits). "
        "False (default) = SHADOW mode — the gate still classifies + audits 'would have "
        "blocked N opens' but opens proceed normally. Flip on only after reviewing shadow data.",
    )
    regime_sma_fast: int = Field(default=50, gt=0, description="Fast SMA period (days) for the BTC regime classifier.")
    regime_sma_slow: int = Field(default=200, gt=0, description="Slow SMA period (days) for the BTC regime classifier — the '200d line'.")
    regime_hysteresis_pct: float = Field(
        default=2.0, ge=0,
        description="Band (%) around the slow SMA required to FLIP the regime state — prevents "
        "whipsaw around the 200d line. A close just inside the band holds the previous state.",
    )


settings = Settings()
