# Go-Live Runbook — real-money trading (SHIPPED OFF)

FINDMY-FM ships with **paper execution everywhere**. The live path is wired, gated, and
disabled by default. This is the operator's checklist to turn it on — deliberately.

## What "live" means
When live is active, an **approved** order is placed as a **real order** on
`LIVE_EXCHANGE` via ccxt's private endpoints. Approval still flows through the same
pending → approve gate; nothing auto-executes that wouldn't in paper mode.

## Safety gates (all enforced in code)
- **Master flag off by default.** `LIVE_TRADING=false`. Real placement runs only when the
  flag is on **and** API keys are present (`execution.live_enabled()`); otherwise paper.
- **Per-order notional cap.** A live **BUY** above `LIVE_MAX_ORDER_NOTIONAL` (quote ccy) is
  refused (`live BUY notional … exceeds cap`). Start small (default $25).
- **Circuit breaker blocks new exposure.** A frozen breaker blocks live **BUYs**.
- **Exits are never gated.** Live **SELLs** are never blocked by the breaker or the cap —
  an exit must always be able to reduce risk (the drawdown-exit-deadlock invariant).
- **Typed confirmation.** Enabling via the dashboard requires typing `LIVE-TRADING`, plus
  configured keys and an armed breaker (a tripped breaker refuses to go live).
- **No secret ever logged.** Keys are `SecretStr`; `execution.py` logs order ids/prices only.

## Turn it on
1. Put real credentials in `.env` (never commit them):
   ```
   LIVE_EXCHANGE=binance          # or your exchange's ccxt id
   LIVE_API_KEY=...
   LIVE_API_SECRET=...
   LIVE_MAX_ORDER_NOTIONAL=25     # keep tiny for the first live orders
   ```
   Leaving `LIVE_TRADING=false` here is fine — you flip it at runtime.
2. Restart. The boot log prints the go-live posture (`execution.validate_at_boot()`):
   - no message → paper;
   - `LIVE_TRADING=true but no exchange API key/secret — staying on paper`;
   - `LIVE_TRADING active on '<exchange>' (cap $X/BUY)`.
3. In the dashboard → **Chiến lược → Go-live**, click **Bật LIVE** and type `LIVE-TRADING`.
   The switch refuses if keys are missing or the breaker is frozen.

## Turn it off
Click **Tắt LIVE** (no confirmation needed) — instantly reverts to paper. Setting
`LIVE_TRADING=false` and restarting also reverts. The choice persists in `runtime_config`,
so a restart keeps your last setting; missing keys make it inert regardless.

## API
- `GET /api/live-trading` → `{live_trading, live_keys, exchange, max_notional, frozen, confirm_phrase}`
- `POST /api/live-trading` `{enabled: bool, confirm?: str}` — enabling requires
  `confirm == "LIVE-TRADING"`, keys present, breaker armed.
