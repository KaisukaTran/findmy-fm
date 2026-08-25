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
- **Maker resting orders are not wired yet.** Live-readiness **1.5** (place waves/TP as
  resting `LIMIT_MAKER` in advance) is still open, so a live limit that does not fill
  immediately still raises the safe "no fill price" path in `app/orders.py:_live_execute`.
  For a first end-to-end round trip keep **`MAKER_ORDERS=false`** — the synchronous
  wait-then-place model then sends a marketable order that actually fills.

---

## 1. Create testnet keys

1. Open **https://testnet.binance.vision** — this is the *Spot* testnet (the futures
   testnet is a different site and is not used here).
2. Log in with the GitHub account link on that page.
3. Generate an **HMAC_SHA256** key. Copy the **API key** and the **secret** immediately —
   the secret is shown once and cannot be recovered.
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
MAKER_ORDERS=false                # see §0 — flip to true only after task 1.5 lands
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
5. Verify an exit: TP/SL/trailing leave as **MARKET** by design (risk exits are never
   slowed, never blocked by the breaker or the notional cap).

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
| "no fill price" on a live limit | the order rested instead of filling — task 1.5 is still open | set `MAKER_ORDERS=false` (§0) |

## 7. Before switching to real funds

1. Finish live-readiness **1.5** (resting maker model) and get a green end-to-end round trip
   on testnet — that is the definition of done for **1.8** in
   [plan/live-readiness-plan.md](plan/live-readiness-plan.md).
2. Re-run the **1.10** security pass on the live path.
3. Only then: real HMAC keys, `LIVE_USE_TESTNET=false`, and a deliberately tiny
   `LIVE_MAX_ORDER_NOTIONAL`. The dashboard badge turns **LIVE · REAL** — see
   [go-live.md](go-live.md) for the arming procedure.
