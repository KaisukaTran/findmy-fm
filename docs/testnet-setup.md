# Binance Spot Testnet — setup runbook (live-readiness 1.8)

Validate the real-money code path against Binance's **Spot testnet** before any real key
touches it. Testnet is a separate exchange with its own accounts, its own matching engine
and fake funds — nothing here can move real money.

**What a green testnet run proves:** keys, signing and clock are good; exchange filters
(tick/step/minNotional) are respected; orders place, report status truthfully, reconcile
and cancel. **What it does not prove:** fills at production prices (see caveats below).

---

## 0. Before you start — what testnet is and isn't

- **Separate account.** No KYC, no relation to your real Binance login; production keys do
  not work on testnet and vice-versa.
- **Periodic resets.** Binance wipes testnet accounts, keys and balances from time to time.
  When previously good keys start returning `-2015`, regenerate them.
- **Fewer symbols.** Testnet lists a subset of production pairs. `scripts/testnet_check.py`
  prints the available Spot/USDT pairs when the one you asked for is missing.
- **Its own price book.** The app's price feed and scanner read **production** public data
  (`app/data/providers.py` builds a plain ccxt client — `set_sandbox_mode` is applied only
  to the *trading* client in `app/execution.py:_client`). So a price derived from production
  can rest, or be post-only rejected, against testnet's thinner book. Expect divergence; it
  is a property of the harness, not a bug in the strategy.
- **Two live models, one switch.** With **`MAKER_ORDERS=true`** the resting model (task 1.5)
  is active: every queued rung and each session's take-profit sit on the exchange in advance
  and the venue fills them, which is what actually earns the maker/spread saving. With
  **`MAKER_ORDERS=false`** the legacy model applies — the app waits for the market to reach a
  limit and then sends a marketable order. Start on the resting model; flipping the flag off
  is the way back if anything surprises you.

---

## 1. Create testnet keys

1. Open **https://testnet.binance.vision** — this is the *Spot* testnet (the futures
   testnet is a different site and is not used here).
2. Log in with the GitHub account link on that page.
3. Generate an **HMAC_SHA256** key. Tick **TRADE** (place/cancel) and **USER_DATA**
   (balance + order status) — both are required; **USER_STREAM** is optional today and
   needed when fills move to the user-data WebSocket, so tick it too. Leave the FIX_API
   boxes alone: they only apply to Ed25519 keys. Copy the **API key** and the **secret**
   immediately — the secret is shown once and cannot be recovered.
4. Optional: restrict the key to your IP. If you do, a changing home IP will surface as
   `-2015` later.
5. The new account arrives pre-funded with test assets; check them in step 3.

## 2. Wire the keys into the LIVE worktree

Keys belong to the **live** instance only — never to the paper worktree's `.env`
(see [dual-instance.md](dual-instance.md) for why the two instances are isolated).

```powershell
cd d:\FINDMY-live
copy ..\FINDMY\.env.live.example .env
```

Then edit that `.env`:

```dotenv
LIVE_TRADING=true                 # master switch (the live instance runs with it on)
LIVE_USE_TESTNET=true             # routes orders to testnet.binance.vision
LIVE_EXCHANGE=binance             # the default is kraken — testnet here is Binance-specific
LIVE_API_KEY=<testnet key>
LIVE_API_SECRET=<testnet secret>
LIVE_MAX_ORDER_NOTIONAL=25        # per-BUY cap; keep it small even on testnet
MAKER_ORDERS=true                 # resting model (§0); false = legacy wait-then-market
DATABASE_URL=sqlite:///./data/live.db
SCHEDULER_LOCK_PORT=8802
```

`.env` is git-ignored — never commit a key, testnet or not.

## 3. Preflight the keys

```powershell
python scripts/testnet_check.py                          # read-only
python scripts/testnet_check.py --symbol BTC/USDT --place  # + place & cancel one tiny order
```

The script drives the app's own helpers (`app.execution`), not raw ccxt, and reports:

| Step | What it proves |
|------|----------------|
| 1. Posture | `LIVE_EXCHANGE`, keys present, `LIVE_USE_TESTNET`, caps — read from this worktree's `.env` |
| 2. Public | the client really points at `testnet.binance.vision`; the symbol is listed; its tick/step/minNotional filters |
| 3. Private | `fetch_balance` accepted → key, signature and system clock are all good; shows the faucet balance |
| 4. `--place` | one post-only BUY 20% below market: filter rounding → placement → a resting order correctly reports `filled=0` → `fetch_order` → cancel |

The test order is cancelled in a `finally` block; if the cancel itself fails the script says
so loudly — clear it by hand on the testnet UI.

`--place` is refused unless `LIVE_USE_TESTNET=true`, so real keys can't be exercised by
accident, and refused when `--notional` exceeds `LIVE_MAX_ORDER_NOTIONAL`.

Once the preflight is green, run the whole live path end to end in one command:

```powershell
python scripts/testnet_e2e.py                                  # rung 0.2% below market, wait 90s
python scripts/testnet_e2e.py --distance-pct 0.05 --wait-sec 180
python scripts/testnet_e2e.py --symbol YB/USDT --rest-at-touch --force-match   # the fill leg
```

It queues a KSS rung, lets `sync_resting_orders` place it as a resting `LIMIT_MAKER`, polls
`reconcile_live_orders` until the venue fills it (or the window ends), and cancels whatever is
left. The exchange side is real; only the database is disposable (`data/testnet_e2e.db`), so
neither the paper nor the live book is touched.

**Waiting does not exercise the fill leg.** Testnet's book is simulated and deep: runs at 0.2%
and then 0.03% below the last price waited 90s and 300s, and the venue never reached the rung —
the price prints through a level without consuming what rests under it. Two flags make the fill
happen instead of hoping for it:

- `--rest-at-touch` prices the rung one tick above the best bid (still post-only) so nothing is
  queued ahead of it. Needs a pair whose spread is wider than one tick — `YB/USDT` is one;
  `BTC/USDT` on testnet is usually one tick wide with thousands of units at the touch.
- `--force-match` then sells into it from this same testnet account
  (`selfTradePreventionMode=NONE` — the account default `EXPIRE_MAKER` would kill our own rung
  instead of filling it — IOC, capped by `--max-cross-usd`). The match is still the venue's,
  against the real order the app placed; only the liquidity on the other side is ours.

`--prove-cancel-books-fill` is a different mode of the same harness: it half-fills the rung and
then cancels it, checking that the filled half is booked BEFORE the exchange link is dropped
(the race that used to lose it). Use it with `--rest-at-touch` so our rung is alone at its price:

```powershell
python scripts/testnet_e2e.py --symbol YB/USDT --rest-at-touch --notional 14 --prove-cancel-books-fill
```

For the session-level proof — a real KSS session whose take-profit rests in advance and follows
the position — run the second harness:

```powershell
python scripts/testnet_session_e2e.py --first-wave-usd 11 --cross-timeout-sec 420
```

It starts a session at the touch, fills wave 0 (partially first, when minNotional allows),
watches `sync_resting_tp` put the exit on the book above market and at/above the K-2 floor,
checks that the exit is cancelled and re-placed as the position grows and the average moves,
and finally that stopping the session takes it off the book. Every step re-checks the venue
first, so a rung the market reaches on its own is filled by the market, not by us.

## 4. Start the live instance

```powershell
cd d:\FINDMY-live ; pwsh -File scripts/run_live.ps1     # http://127.0.0.1:8001
```

Confirm two things:

- the boot log prints `LIVE_TRADING active on 'binance' (cap $25.00/BUY)`
  (`execution.validate_at_boot`). Anything else — no line, or "staying on paper" — means
  the flag or the keys are not being read;
- the dashboard header shows the **LIVE · TESTNET** badge (green). **LIVE · REAL** (red)
  means `LIVE_USE_TESTNET` is false — stop and fix it.

## 5. First round trip

1. Open a small KSS session on a pair testnet actually lists.
2. Approve the first BUY in the pending queue (approval still gates everything).
3. Check that a **Fill** was recorded, the **Position** updated, and the order row carries
   `exchange_order_id` / `exchange_status`.
4. Check the cycle audit's `reconciled` counter — `orders.reconcile_live_orders` runs first
   in every scheduler cycle and books partials of resting orders.
5. Verify the exit. With the resting model on, the take-profit sits on the book as a LIMIT
   SELL and is re-priced whenever a fill moves the average; SL, trailing and the deadline
   stay **MARKET** by design (risk exits are never slowed, never blocked by the breaker or
   the notional cap).

## 6. Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `-2015 Invalid API-key, IP, or permissions` | key wiped by a testnet reset, wrong key, or IP restriction | regenerate at testnet.binance.vision; re-check the IP allow-list |
| `-1021 Timestamp ... outside of the recvWindow` | system clock drift | sync the clock (`w32tm /resync` on Windows) |
| `-1121 Invalid symbol` / symbol missing | pair not listed on testnet | pick one from the list the preflight prints |
| `-2010 ... insufficient balance` | faucet empty after a reset | regenerate keys for a freshly funded account |
| post-only rejected (`would immediately match`) | the limit crossed the book | expected for a maker order at/above market; rest it lower |
| `NetworkError` on `exchangeInfo` | testnet host unreachable (firewall, VPN, corporate proxy) | check connectivity to `testnet.binance.vision:443` |
| `429` / `418` | rate limit / IP ban | back off; the guard lives in `execution.used_weight_from_headers` + `classify_rate_error` |
| App runs but never places | `LIVE_TRADING=false` or missing keys → `execution.live_enabled()` false | read the boot log line; it names which one |
| "no fill price" on a live limit | a limit rested under the legacy model | turn `MAKER_ORDERS=true` so rungs rest by design (§0) |
| a rung never rests, log says post-only rejected | the book already reached that price, so a maker order cannot rest there | expected; it retries next cycle, or rest it further below |

## 7. Before switching to real funds

1. Get a green end-to-end round trip on testnet (`scripts/testnet_e2e.py`) — the resting
   model (**1.5**) is in, so this is the remaining definition of done for **1.8** in
   [plan/live-readiness-plan.md](plan/live-readiness-plan.md).
2. Re-run the **1.10** security pass on the live path.
3. Only then: real HMAC keys, `LIVE_USE_TESTNET=false`, and a deliberately tiny
   `LIVE_MAX_ORDER_NOTIONAL`. The dashboard badge turns **LIVE · REAL** — see
   [go-live.md](go-live.md) for the arming procedure.
