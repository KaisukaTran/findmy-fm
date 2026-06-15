# LIVE-readiness plan — maker execution, order-status tracking, 5m, rate-limit safety

**Created:** 2026-06-14 · **Branch:** `telegram-notify` (or a new `live-readiness`) · **Status:** PLANNED, not started.
Goal: make the live execution path (currently SHIPPED OFF — `LIVE_TRADING=false`, paper everywhere)
production-ready so flipping `LIVE_TRADING=true` is safe. The user wants KSS/OPUS orders to be
**maker** (fee/slippage optimisation) and to trade **5m candles**.

## Grounded facts (verified against the live Binance API 2026-06-14 — do NOT re-research)
- **Rate limits:** REQUEST_WEIGHT **6000/min (per IP)**; ORDERS **100/10s + 200k/day (per account)**;
  RAW_REQUESTS 300k/5min. Response header `X-MBX-USED-WEIGHT-1M` reports usage. HTTP **429** = rate
  limited (honor `Retry-After`); **418** = IP banned.
- **Order types (SOLUSDT):** LIMIT, **LIMIT_MAKER**, MARKET, STOP_LOSS, STOP_LOSS_LIMIT, TAKE_PROFIT,
  TAKE_PROFIT_LIMIT. `LIMIT_MAKER` = post-only; **REJECTED (-2010) if it would cross the book** (i.e. if
  it would be a taker). ccxt: `create_order(pair,'limit',side,qty,price,{'postOnly':True})`.
- **Filters (per symbol, from exchangeInfo):** PRICE_FILTER `tickSize` (e.g. 0.01 → round price);
  LOT_SIZE `stepSize`/`minQty` (e.g. 0.001 → round qty); NOTIONAL `minNotional` (e.g. $5);
  PERCENT_PRICE_BY_SIDE (order price must be within 0.5×–2× of the 5-min avg).
- **Fee reality:** Binance Spot **VIP 0: maker == taker == 0.10%** (equal!). Maker is cheaper only at
  **VIP 1+** or paying fees in **BNB** (−25%, applies to both). BUT maker **always saves the spread/
  slippage** (app models `slippage_pct=0.05%`) — that is the real VIP-0 win, biggest on small alts.
- **Klines:** max **1000 candles/request**. 5m = 288 bars/day; the scanner already maps timeframes
  (`5m:288`). A 365-day lookback at 5m = ~105k bars → impossible without pagination.

## Current code gaps (the "order status" problem)
- `app/execution.py:place_live_order` ASSUMES immediate fill: `filled = order.get("filled") or amount`
  → a resting maker order (`status=NEW, filled=0`) is recorded as a **phantom full fill**. Hard blocker.
- The app's model is **synchronous "wait until market reaches the limit, then place a (marketable) order"**
  (`orders.auto_fill_due_orders` fires when `market<=limit` for BUY). That is INCOMPATIBLE with maker:
  by the time market==limit, a buy at the limit crosses → LIMIT_MAKER rejected.
  → **Maker requires the inverse model: place the resting LIMIT_MAKER ON THE EXCHANGE in advance
  (below market) and let the exchange fill it when price dips, then RECONCILE the fill.** This is the
  central architectural shift for live mode (paper stays synchronous/simulated).

## Design decisions
1. **Maker only for entries + take-profit; risk exits stay taker (MARKET).** DCA buys + TP sells →
   `LIMIT_MAKER`. **Stop-loss / trailing / OPUS-close / deadline → MARKET** (speed > fee; a maker
   stop can fail to fill in a fast drop and trap a loss). Honors [[drawdown-exit-deadlock]] (never
   slow a risk exit).
2. **Async live order lifecycle.** Live KSS waves + TP are placed as resting exchange orders; a new
   reconciliation step polls/streams status and applies the Fill + Position update ONLY on real fill
   (NEW→PARTIALLY_FILLED→FILLED). Paper path unchanged (synchronous simulated fill).
3. **Testnet first.** Validate the whole live path on Binance Spot **testnet**
   (`https://testnet.binance.vision`, ccxt `set_sandbox_mode(True)`) before any real key.
4. **Filter compliance** at placement: round price→tickSize, qty→stepSize, enforce minNotional,
   clamp to PERCENT_PRICE_BY_SIDE — reject/skip if impossible.
5. **Rate-limit guard:** keep ccxt `enableRateLimit`; additionally read `X-MBX-USED-WEIGHT-1M`, back
   off above ~80% of 6000; on 429 sleep `Retry-After`; on 418 halt live + alert. Prefer the user-data
   **WebSocket** (`executionReport`) for fills over polling `fetch_order` (weight 4 each) to save weight.
6. **5m:** paginate klines (>1000 → multiple requests), cap lookback for intraday (e.g. 7–14 days),
   set `scan_interval_min ≤ 5`.

## Plans.md task ledger (harness)
Spec delta: this doc is the product contract for live-readiness (repo has no root spec.md).
Precedence: this spec > Plans table.

| Task | Content | DoD | Depends | Status |
|------|---------|-----|---------|--------|
| 1.1 | Fix `place_live_order` phantom-fill: return real `status`/`filled`/`average`; never invent a fill `[tdd:required]` | resting/NEW order returns filled=0, not amount | - | **cc:DONE 2026-06-15** (paper-safe) |
| 1.2 | Exchange-filter helper: round price→tickSize, qty→stepSize, enforce minNotional + PERCENT_PRICE `[tdd:required]` | unit tests vs real SOLUSDT filters pass | 1.1 | **cc:DONE 2026-06-15** (`execution.round_to_filters`) |
| 1.3 | Maker placement: `postOnly`/LIMIT_MAKER for entry+TP; MARKET for SL/trailing/close; handle -2010 REJECTED `[tdd:required]` | maker path sets LIMIT_MAKER; risk exits stay MARKET | 1.1 | **cc:DONE 2026-06-15** — `order_placement`/`is_post_only_reject`/`filters_from_market`; LIMIT→postOnly+filter-rounded, MARKET stays taker; -2010→`status='rejected'`; 8 tests. TP→maker waits on 1.5 (TP is still a MARKET order) |
| 1.4 | Async order tracking: persist exchange order id + status on KssWave/PendingOrder; a `reconcile_live_orders()` scheduler step applies Fills on FILLED/PARTIAL `[tdd:required]` | a NEW→FILLED transition creates exactly one Fill + Position update | 1.1,1.3 | **cc:DONE 2026-06-15** — columns paper-safe via `_ADDED_COLUMNS`; `reconcile_live_orders()` gated by `live_enabled()`; 8 tests |
| 1.5 | Live KSS/OPUS model shift: in live mode place resting waves/TP in advance (not "wait-then-market"); cancel+replace on avg/target change `[tdd:required]` | live wave rests on exchange; paper unchanged | 1.4 | cc:TODO |
| 1.6 | Rate-limit guard: read X-MBX-USED-WEIGHT-1M + backoff; 429 honor Retry-After; 418 → halt live + alert `[tdd:required]` | guard backs off; 418 stops live | 1.1 | **cc:DONE 2026-06-15** (`used_weight_from_headers`/`weight_backoff_seconds`/`classify_rate_error`) |
| 1.7 | 5m support: kline pagination (>1000), intraday lookback cap, scan_interval≤5 config `[tdd:required]` | 7-day 5m fetch returns >1000 bars via pagination | - | cc:TODO (touches shared candle path — deferred for paper safety) |
| 1.8 | Testnet harness: `set_sandbox_mode`, `live_use_testnet` flag; end-to-end place→fill→reconcile on testnet `[tdd:skip:manual-testnet]` | full round-trip works on testnet | 1.1-1.6 | **cc:PARTIAL** — flag + `set_sandbox_mode` wired in `_client`; manual testnet e2e pending |
| 1.9 | Config + UI knobs: `maker_orders`, `order_fill_timeout_sec`, `live_use_testnet`, BNB-fee note; Strategy-tab exposure `[tdd:skip:server-render]` | knobs load + persist | 1.3,1.7 | **cc:PARTIAL** — config fields added (load from .env); Strategy-tab UI exposure pending |
| 1.10 | Security pass on the live path (keys never logged, idempotent placement, no double-fill) `[tdd:skip:review-only]` | no findings | 1.5 | **cc:DONE 2026-06-15** (review below) |

**Sequencing:** 1.1 → (1.2,1.3,1.6,1.7 parallel) → 1.4 → 1.5 → 1.8 → 1.9 → 1.10.
Build with Karpathy discipline; commit per task at green. LIVE_TRADING stays OFF until 1.8 passes on testnet.

## Team validation (manual-pass — subagents not used)
`team_validation_mode: manual-pass`
- **Product:** delivers maker entry/TP + 5m + safe live; matches the user's "ready for LIVE" intent. ✔
- **Architecture:** the sync→async shift is the real cost; isolate it to live mode (paper untouched).
  Reuse the existing approval queue + circuit breaker + notional cap; only the execution+reconcile layer changes. ✔
- **Security:** real money — keys via SecretStr (never logged, already enforced); placement must be
  idempotent (no double-order on retry); reconciliation must not double-count a fill. Formal pass = 1.10. ✔
- **QA:** every code task `[tdd:required]`; testnet (1.8) is the integration gate before real funds. Lint=ruff. ✔
- **Skeptic:** biggest risk = the async order model + partial fills + maker rejections under live latency;
  testnet-first mitigates. Fee win at VIP0 is slippage-only — set expectations. Maker DCA may rest unfilled
  if price never dips (same as today's limit behavior). ✔

## Open questions for the user (resolve before building 1.5/1.9)
- Maker entries that never fill (price never dips to the wave): leave resting indefinitely, or a
  `order_fill_timeout` then cancel? (DCA usually wants to keep waiting.)
- TP as a resting maker placed in advance (ties up inventory, must cancel+replace as avg moves) vs a
  trigger-then-place maker? Recommend resting-in-advance with cancel+replace.
- VIP tier / paying fees in BNB? (decides whether maker actually lowers FEE vs only slippage.)
- Testnet API keys available, or validate logic-only first?

## Progress — 2026-06-15 (paper-safe subset shipped)
Executed only the tasks that cannot affect the running PAPER app (additive helpers / live-path-only
code / config). Full `tests/app/` stayed green (498 passed, 2 skipped); 11 new unit tests in
`tests/app/test_execution_live.py`.
- **1.1 done** — `place_live_order` now returns the true `status`/`filled`/`average` (no phantom
  full-fill). A resting order → `filled=0`; `_live_execute` already raises "no fill price" on that
  (safe interim until 1.4). Live-path only; paper `_paper_execute` untouched.
- **1.2 done** — `execution.round_to_filters(price, qty, filters, ref_price)` (Decimal-based tick/step
  rounding + minQty/minNotional/PERCENT_PRICE checks). Pure; not wired into paper.
- **1.6 done** — `used_weight_from_headers`, `weight_backoff_seconds`, `classify_rate_error` (429→retry
  Retry-After, 418→halt). Pure helpers; wiring into the live client loop happens with 1.4/1.5.
- **1.8/1.9 partial** — config knobs `maker_orders`, `order_fill_timeout_sec`, `live_use_testnet` added
  (default safe/off); `_client()` calls `set_sandbox_mode(True)` when `live_use_testnet`. UI exposure +
  testnet e2e pending.

**Deferred (NOT paper-safe now):** 1.3 (maker placement — low value without 1.4), 1.4 (async tracking —
needs new DB columns on KssWave/PendingOrder = a migration on the live paper DB), 1.5 (live resting
model — biggest), 1.7 (5m — touches the shared candle-fetch path; risk to the paper scanner).
LIVE_TRADING stays OFF.

## 1.10 — Live-path security review (2026-06-15, read-only)
- ✅ **Secrets never logged.** `live_api_key/secret` are `SecretStr`; `_secret()` extracts only at the
  call site; the single `logger.info` in `place_live_order` emits side/qty/pair/avg/status/exch-id —
  never the key/secret. `_client` logs nothing.
- ✅ **No silent paper fallback.** `place_live_order` raises on any exchange error; `_live_execute`
  never falls back to a paper fill — a live failure surfaces instead of being masked.
- ✅ **BUY re-gated, SELL never gated.** `_live_execute` re-checks the breaker freeze + `live_max_order_notional`
  on BUY; SELL exits are never blocked (drawdown-exit invariant).
- ✅ **Phantom double-count fixed (1.1).** Resting orders are no longer recorded as full fills.
- ⚠️ **FINDING — placement not idempotent.** A network timeout/retry after the venue accepted an order
  could double-place (no `clientOrderId`). FIX: pass a deterministic `clientOrderId` derived from the
  `PendingOrder.id` so a retry is recognised/no-op. Gate for go-live; implement with 1.4/1.5.
- ⚠️ **FINDING — no fill reconciliation yet.** With 1.1, a resting maker order raises "no fill price"
  (safe), but live maker is **not usable** until async reconcile (1.4) exists. Keep `LIVE_TRADING=false`.
- **Conclusion:** the live path is SAFE while OFF. The two ⚠️ items (idempotency, reconciliation) are
  the blockers to turning it on — tracked as 1.4/1.5; revisit 1.10 after those land.

## Progress — 2026-06-15 (1.4 async order tracking SHIPPED, paper-safe)
Built on `kss-capital-auto-sizing`; paper unaffected (everything gated by `execution.live_enabled()`).
Full `tests/app/` = **507 passed / 2 skipped** (was 498); 8 new tests in `tests/app/test_reconcile_live.py`.
- **Columns** `exchange_order_id` + `exchange_status` added to `PendingOrder` **and** `KssWave`
  (models + `to_dict`), registered in `db.py:_ADDED_COLUMNS` as nullable `VARCHAR` → idempotent
  ALTER on the existing paper DB (verified: adds on an old-schema table, second pass is a no-op,
  existing rows keep NULL). KssWave columns are for 1.5's resting model; reconcile keys off PendingOrder.
- **`execution.fetch_live_order(pair, order_id)`** — normalised `{status, filled, average, fee, raw_id}`
  (ccxt `fetch_order`); cumulative `filled`; never logs the secret.
- **`orders.reconcile_live_orders(db)`** — no-op unless `live_enabled()`. Polls every PendingOrder with
  `exchange_order_id` set and a non-terminal `exchange_status`; books the **delta** between the venue's
  cumulative `filled` and the qty already recorded as Fills (idempotency key = `sum(Fill.quantity)` for
  that order) → one NEW→FILLED = exactly one Fill + `_update_position`; partials accumulate; re-run = 0.
  Fires the KSS fill hook + `notify.fill_alert` on the booked delta; marks the order EXECUTED on a
  terminal+filled status. **Not gated by the breaker** (records a real fill, like a SELL exit — never blocked).
- **Wiring:** `_live_execute` now stamps `exchange_order_id`/`exchange_status` after placement so a
  later partial completes via reconcile; `scheduler.run_cycle` calls `reconcile_live_orders` FIRST
  (before TP/scan) so positions reflect reality; `reconciled` count added to the cycle audit + summary.
- **NOT yet wired:** placing a resting maker order without raising on filled=0 is **1.5** — until then a
  resting order still raises "no fill price" in `_live_execute` (safe). `LIVE_TRADING` stays OFF.
- **NEXT = 1.3** maker placement, then 1.5 resting model.

## Progress — 2026-06-15 (1.3 maker placement SHIPPED, paper-safe)
Execution-layer only; paper unaffected (place_live_order runs only via `_live_execute`, which is
live-gated). Full `tests/app/` = **515 passed / 2 skipped**; 8 new tests in `test_execution_live.py`.
- **`execution.order_placement(order_type, maker_orders)`** → `(ccxt_type, params)`: MARKET → taker
  market (risk exits never slowed); LIMIT → post-only `{"postOnly": True}` (Binance LIMIT_MAKER) when
  `maker_orders` on, else plain limit. Pure.
- **`execution.is_post_only_reject(exc)`** — detects Binance `-2010`/"would immediately match"/ccxt
  `OrderImmediatelyFillable`.
- **`execution.filters_from_market(market)`** (+ `_market_filters(ex, pair)` live wrapper) — extracts
  tick/step/minQty/minNotional/PERCENT from a ccxt market (raw `info.filters` first, ccxt `limits`
  fallback). Pure.
- **`place_live_order(..., maker_orders=None)`** — defaults to `settings.maker_orders`. Maker LIMIT is
  rounded through `round_to_filters` then placed `postOnly`; a `-2010` reject returns a structured
  `{status:'rejected', quantity:0, price:0}` (the 1.5 resting model cancels+replaces lower) instead of
  raising. **Maker-OFF path is byte-identical to before** (no params, no market() call) so the 1.1
  tests are untouched. `_live_execute` already passes `order.order_type` → maker flows through config,
  no caller change.
- **Scope note:** this makes **DCA entries** maker. **TP is still a MARKET order** in the strategy
  queue (`pyramid.check_tp` → order_type=MARKET); converting TP to a *resting* LIMIT_MAKER is **1.5**.
  Risk exits (SL/trailing/deadline/OPUS-close) stay MARKET by design.
- **NEXT = 1.5** live resting model (largest): place KSS waves (+ TP as resting LIMIT_MAKER) in advance,
  drop wait-then-market, cancel+replace on avg/target change. Paper stays synchronous.

## Next session — build order to a working testnet (2026-06-15)
Validation vehicle is the **live testnet instance** (`D:\FINDMY-live` @ branch `live`, port 8001) from
the dual-instance harness (`docs/dual-instance.md`). All code is built on `dev`/feature branches in the
paper worktree, unit-tested (paper unaffected — everything is gated by `execution.live_enabled()`),
merged to `live`, then run on testnet. Build order:

1. **1.4 Async order tracking (DO FIRST, foundation).** Add `exchange_order_id` + `exchange_status`
   columns to `KssWave` & `PendingOrder` — register them in `app/db.py:_ADDED_COLUMNS` (idempotent
   ALTER → paper-safe). New scheduler step `reconcile_live_orders(db)` that runs **only when
   `live_enabled()`**: poll `fetch_order` (or user-data WS later), and on `NEW→PARTIAL→FILLED` create
   **exactly one** Fill + Position update (idempotent via the exchange order id). Test: one transition =
   one Fill.
2. **1.3 Maker placement.** In `execution.place_live_order`: entry + TP → `LIMIT_MAKER` (postOnly);
   SL/trailing/close → MARKET; handle `-2010` rejected. Round price/qty through the existing
   `execution.round_to_filters` first.
3. **1.5 Live resting model** (largest). In live mode, place KSS waves + TP as resting exchange orders
   in advance (drop wait-then-market); cancel+replace on avg/target change. Paper stays synchronous.
4. **1.10 follow-ups** after 1.5: deterministic `clientOrderId` from `PendingOrder.id` (idempotent
   placement) — the open finding.
5. **1.7 5m** (optional, can come after the testnet round-trip is green): paginate klines >1000 +
   intraday lookback cap + `scan_interval_min ≤ 5`.

**Decisions needed before 1.5 (recommended defaults — confirm with user):**
- Maker entry that never fills → use `order_fill_timeout_sec` (already a config; 0 = wait forever, fits DCA). **Default: 0.**
- TP as resting maker placed in advance, cancel+replace as avg moves (vs trigger-then-place). **Default: resting + cancel/replace.**
- VIP tier / pay fees in BNB? **Default: VIP0** (maker wins slippage only, not fee).

**Prerequisite (user):** Binance Spot **testnet** HMAC keys from testnet.binance.vision → paste into
`D:\FINDMY-live\.env` (`LIVE_API_KEY`/`LIVE_API_SECRET`). Without them the live instance just runs as a
2nd paper app. Code for 1.4/1.3 can be built + unit-tested **without** keys; keys are needed only for the
1.8 end-to-end testnet validation.

**Done = testnet complete:** on testnet, the live instance runs the full round-trip — open session →
resting `LIMIT_MAKER` → exchange fills on a dip → `reconcile_live_orders` creates the Fill → Position
updates → TP/SL fire; filters + rate-limit guard clean. Green round-trip + user sign-off → only THEN
consider real funds with a tiny `live_max_order_notional`.
