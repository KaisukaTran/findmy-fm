# Multi-Agent Scanner + Backtested Auto-Trading

A decision layer over the KSS engine: deterministic quant **agents** evaluate
pairs, an **aggregator** turns their votes into a consensus %, and qualifying
pairs (gated by a **backtested win-rate** and a **≤30-day deadline**) open KSS
sessions — semi-auto (human approves) or full-auto (auto-approved, still
risk-checked). Every AI action is **audit-logged**.

> The win-rate is a **backtest estimate over historical candles, not a
> guarantee** of live results.

## Data (real prices, no API key)

`app/data/providers.py` wraps ccxt public endpoints. `settings.live_exchange`
(default `binance`) serves live prices; `settings.data_exchange` (default
`kraken`) serves historical OHLCV + top-symbols for scans/backtests — no key, no
new dependency. Quote asset is per-exchange (Binance→USDT, Kraken/Coinbase→USD).

## Pipeline (`app/scanner.py::run_scan`)

1. **Universe** = `settings.watchlist ∪ provider.top_symbols(scan_top_n)`.
2. **Backtest** (`app/backtest.py`): replays the KSS pyramid over history using
   the same formulas as `app/kss/pyramid.py`; `estimate_win_rate` rolls entries
   and counts a win = TP reached within `deadline_days`.
3. **Agents** (`app/agents/`, deterministic) each return `AgentVote{score, confidence, reason}`:
   `trend` · `dip` · `volatility` · `liquidity` · `backtest` (dominant safety vote).
4. **Aggregate + decide** (`app/agents/aggregator.py`): consensus % weighted by
   weight × confidence. **Trade only if ALL gates pass**:
   `consensus ≥ min_confidence` AND `win_rate ≥ min_win_rate` AND `avg_days_to_tp ≤ deadline_days`.
5. **Persist + audit**: `ScanRun`, `Candidate`, `AgentVoteRecord`, `AuditLog`.
6. **Act** on a "trade": open a KSS session (`deadline_at = start + deadline_days`).
   - semi-auto: wave 0 → pending queue (human approves).
   - full-auto (`settings.auto_trade`): wave 0 auto-approved via `orders.approve_order`
     (risk checks + isolated fund + deadline still apply); logged as `auto-trader`.

## Deadline (≤ 30 days)

`KssSession.deadline_days/deadline_at`; `kss.service.sweep_deadlines` force-closes
overdue ACTIVE sessions (queues a market SELL of any inventory through the normal
approval flow) and logs `deadline_close`. Called at the start of every scan.

## Settings (`app/config.py`, override via env/.env)

`min_win_rate=80` · `min_confidence=70` · `deadline_days=30` · `auto_trade=false`
· `watchlist` · `scan_top_n` · `data_exchange` · `backtest_lookback_days/timeframe`
· proposed-session params `scan_distance_pct/scan_tp_pct/scan_max_waves/scan_fund`.

## API / dashboard

`POST /api/scan` (key) · `GET /api/candidates` · `GET /api/agents/decisions` ·
`GET /api/audit` · `GET|POST /api/autotrade` (POST keyed). Dashboard adds an
**Agent Scanner** panel ("Run scan" + auto-trade toggle + gate display) and an
**AI Audit Log** panel.

## Run a real scan

```bash
export SCAN_TOP_N=0        # watchlist only (faster)
uvicorn app.main:app --port 8000
curl -X POST localhost:8000/api/scan        # evaluates BTC/ETH/SOL on Kraken
curl localhost:8000/api/candidates
```

Example (real Kraken daily data): BTC win 97.6%, ETH 100%, SOL 98.3% — all
**skip** because agent consensus (~60-64%) was below the 70% gate, even though
the win-rate gate passed. That multi-gate conservatism is by design.

## Test

```bash
pytest tests/app -c tests/app/pytest.ini      # incl. providers/backtest/agents/scanner/audit
ruff check app tests/app
```

## Safety

Full-auto is off by default and only toggles with the API key (UI confirms).
Even on, orders pass risk checks, the isolated-fund cap, the deadline, and the
audit trail. Live trading remains paper; exchanges are used for prices only.
