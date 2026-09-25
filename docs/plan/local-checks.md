# Local checks queued by cloud sessions

A cloud session has no `d:\`, no instance DB, no runtime settings and no exchange access. When it
needs a fact only the trading machine has, it queues the check here. **A local session: do these
first, report the answers, then delete the entry** (or move it to "Done" with the finding).

---

## 1. XPL sold at its fixed TP (+6.2%) and ran to +22.6% — which path sold it? (2026-09-25)

Context: branch `claude/nifty-lamport-9d90hv` adds **runner mode** (`kss_arm_at_tp`, see
`docs/kss-dynamic-tp-plan.md` rev 5). The cloud session could not tell which of three causes
closed the XPL session (avg `0.0928018`, TP `0.0985926` = tp_pct 6 + 0.24 fee buffer, wave 3/10,
status `completed`). Each cause has a different fix, so confirm it on the instance that ran it.

Run against **both** `data/findmy.db` (paper, :8000) and `data/live.db` (live, :8001):

```sql
-- the session
SELECT id, status, strategy_mode, tp_pct, avg_price, peak_price, trail_active, trail_sl_price
FROM kss_sessions WHERE symbol LIKE 'XPL%' ORDER BY id DESC LIMIT 3;

-- how it was sold (use the id above)
SELECT id, source_ref, order_type, price, quantity, status, reviewer, created_at, decided_at
FROM pending_orders WHERE source_ref LIKE 'pyramid:<ID>:%' AND side = 'SELL';

-- what the exit logic decided
SELECT created_at, action, detail FROM audit_log
WHERE entity = 'kss:<ID>'
  AND action IN ('dyn_tp_armed', 'tp_queued', 'tp_replaced', 'tp_deferred', 'stop_queued')
ORDER BY id;

-- the switches as the instance saw them (runtime overrides; absent = .env / default)
SELECT key, value FROM runtime_config
WHERE key IN ('kss:kss_dynamic_tp_enabled', 'kss:kss_arm_at_tp', 'kss:kss_tp_gap_pct',
              'kss:kss_trail_arm_pct', 'kss:kss_trail_lock_pct');
```

Also note `MAKER_ORDERS` / `LIVE_TRADING` in that worktree's `.env`.

Read the result:

| Evidence | Cause | Fixed by this branch? |
|---|---|---|
| `:tp` order is **LIMIT**, reviewer `resting-tp`/venue fill, live DB | 1.5 maker model rested a fixed TP on the exchange; it filled before any trail could run | yes — `sync_resting_tp` no longer rests a TP for a session the dynamic exit governs |
| `:tp` is **MARKET**, no `dyn_tp_armed` row | `kss_dynamic_tp_enabled` was off → frozen fixed TP | only once you turn the dynamic exit + runner mode on |
| `:tp` is **MARKET** *after* a `dyn_tp_armed` row | Ride & Trail's spike-grab ceiling `SL×(1+gap)` sat below price (high-ATR coin) and sold at ~+7% | yes — runner mode anchors the ceiling to the peak |

Then, on **paper** first: set `kss_dynamic_tp_enabled=1` and `kss_arm_at_tp=1` (Strategy tab),
restart, and watch for `dyn_tp_armed` with `mode=runner` in the log. Do not flip it on `live` until
the branch is merged there and a paper runner has closed through `trail_sl`.

Caveat for live: `_maintain_live_stop` is still a no-op, so an armed trail on live is enforced by the
90s guard with a market sell, not by a stop resting on the exchange. Binance spot's
`TAKE_PROFIT` + `trailingDelta` could do the whole "arm at TP, then trail" on the venue, but that
needs a testnet run (`scripts/testnet_check.py`) before anyone relies on it.
