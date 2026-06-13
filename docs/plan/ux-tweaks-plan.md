# FINDMY-FM — UX tweaks + capital/scanner plan

Created: 2026-06-13 · Opus plans + owns verification tests; specialists implement
(see [[opus-delegates-simple-tasks]]). Karpathy: each agent gets a scoped `path:line` brief.

---

## Diagnostics (answers to the user's "why")

- **#1 ARMED** = the circuit breaker is *armed* (i.e. NOT frozen — the healthy/normal state).
  It only matters when it flips to `FROZEN`. → Hide the ARMED badge; show only FROZEN.
  Source: `app/templates/partials/status.html` (`breaker-armed` / `breaker-frozen`).
- **#2 first-order / 15% capital**: KSS wave qty = `(wave_num+1) × pip_size`, and
  `pip_size = pip_multiplier(2.0) × minQty` (`app/kss/pyramid.py:188-199`, `config.pip_multiplier`).
  minQty is tiny, so the first wave (and total deployed) is far below `scan_fund`/`isolated_fund`
  (which is only a *ceiling*). That's why ~15% of equity is deployed. Fix: let the operator set
  the **first-wave notional (USD)**; size pip_size from it.
- **#3 ~30 coins**: universe = watchlist ∪ pairs above `min_quote_volume` ($1,000,000 default),
  capped at `scan_max_symbols` (50). The binding limit is the liquidity floor + `data_exchange`
  = `kraken` (fewer USD pairs; binance is geo-blocked here). Source: `app/scanner.py:142-191`,
  `config.min_quote_volume`, `config.scan_max_symbols`. Fix: expose both in the Strategy tab.
- **#6 log "shows N recent" but empty + only time**: trade/positions/etc. partials render
  whatever the view returns; the audit feed also defaults to the `af-important` filter which
  hides `cat-system` rows (can look empty). Timestamps use `| hms` (time only). Fix: paginate
  (20/page, ≤10 recent pages) + switch timestamps to `| localdt` (date + time).

## Spec delta
- New setting **kss_first_wave_usd** (USD): target notional of the first KSS wave; 0 = legacy
  pip-based sizing. Surfaced in the Strategy tab.
- `scan_max_symbols` + `min_quote_volume` become Strategy-tab editable (persisted).
- Data panels (trades, pending, positions, KSS, audit) paginate: page 1 = 20 newest, up to 10
  pages; timestamps show **date + time**.

## Validation
- `team_validation_mode: subagent` — Opus delegates per [[opus-delegates-simple-tasks]].
- Opus owns the verification tests (below); specialists implement to pass them.
- Gates: tests + ruff green; offline-safe (no network in tests).

---

## Phase 1: quick UI + scanner exposure  [tdd:required for logic]

| Task | Content | DoD / verify test | Agent | Status |
|------|---------|-------------------|-------|--------|
| 1.1 | Hide the ARMED badge in `status.html` (both mobile + desktop strips); keep FROZEN visible. | render: status partial has no "ARMED" when not frozen; shows "FROZEN" when frozen | frontend-htmx | cc:TODO |
| 1.2 | Expose `scan_max_symbols` + `min_quote_volume` as Strategy-tab fields (persisted via `KSS_SETTING_FIELDS` + kss-settings save route). | POST kss-settings persists both; `runtime.kss_settings` returns them | backend-builder | cc:TODO |
| 1.3 | Add the two fields to `kss_settings.html` with tooltips ("tăng để quét nhiều coin hơn"). | fields render with current values | frontend-htmx | cc:TODO |

## Phase 2: KSS first-wave notional (req #2)  [tdd:required]

| Task | Content | DoD / verify test | Agent | Status |
|------|---------|-------------------|-------|--------|
| 2.1 | Add `kss_first_wave_usd` config (default 0 = legacy). When >0, pyramid sizes `pip_size = first_wave_usd / entry_price` so wave-0 notional ≈ the value; later waves keep the `(n+1)×` pyramid shape. | unit test: with first_wave_usd=100, entry=50000 → wave-0 notional ≈ $100 (±1 step); =0 → legacy pip sizing unchanged | kss-strategy | cc:TODO |
| 2.2 | Persist `kss_first_wave_usd` (`KSS_SETTING_FIELDS`) + Strategy-tab field. | POST persists; new sessions use it | backend-builder (persist) + frontend-htmx (field) | cc:TODO |

## Phase 3: pagination + date/time across data panels (req #6)  [tdd:required]

| Task | Content | DoD / verify test | Agent | Status |
|------|---------|-------------------|-------|--------|
| 3.1 | Paginate the views: `trades_view(limit, offset)`, `list_pending(limit, offset)`, audit, KSS, positions — page size 20, expose total/has-more; routes accept `?page=` (1..10, clamp). | unit test: page=1 returns ≤20 newest; page=2 returns the next 20; page>10 clamps | backend-builder | cc:TODO |
| 3.2 | Page controls (‹ Prev / page n / Next ›, ≤10 pages) in the trades/pending/positions/kss/audit partials; HTMX swap; preserves the panel. | render: page nav present; clicking Next requests `?page=2` | frontend-htmx | cc:TODO |
| 3.3 | Timestamps show **date + time**: swap `| hms` → `| localdt` in trades/audit (and any time-only column). | render: a row shows `YYYY-MM-DD HH:MM:SS` | frontend-htmx | cc:TODO |

## Status — DONE 2026-06-13 (delegated per [[opus-delegates-simple-tasks]])
All phases shipped. Wave A: kss-strategy (#2 pip_size opt-in override, frozen invariants intact)
∥ backend-builder (#1.2/#2.2 persisted via KSS_SETTING_FIELDS; #3.1 pagination on
trades/pending/positions/kss/audit + offset on views). Wave B: frontend-htmx (#1.1 ARMED hidden;
#1.3/#2.2 Strategy-tab fields; #3.2 self-replacing-root page nav, ≤10 pages; #3.3 `localdt`
date+time). Opus wrote `test_kss_first_wave.py`; backend wrote `test_pagination.py`. Full suite
**450 pass / 2 skip**; render-verified. Pre-existing C901s (render/_universe/_review_and_open/
auto_approve_by_policy) left untouched.

## Out of scope
- Switching `data_exchange` off kraken (binance geo-blocked here) — operator/infra decision.
- Changing the pyramid `(n+1)×` shape — only the *base* (first-wave) size becomes settable.
