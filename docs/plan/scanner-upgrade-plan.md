# Scanner Upgrade Plan (v1) — for builder agents

> Status: **PLAN ONLY — not implemented.** Author: planning session 2026-06-12.
> Scope: the scanner pipeline — `app/scanner.py`, `app/agents/*`, `app/backtest.py`,
> `app/data/providers.py`, `app/ta/*`, `app/orchestrator/grok.py` (scanner gate only),
> `app/scheduler.py` (cycle wiring only).
>
> **Karpathy discipline for every phase** (see `.claude/skills/context-engineering`):
> each phase lists *exactly* which files/lines to read — read those, not the whole
> module tree. Builder agents return compressed summaries citing `path:line`, never
> file dumps. One phase = one small reviewable diff (1–3 files) + tests green before
> the next phase starts. Do **not** load `kss-spec`, OPUS, or dashboard context unless
> the phase says so.

## External references (patterns to borrow, not dependencies to add)

- freqtrade pairlist handlers — chained filter pipeline + TTL-cached volume pairlist
  (`refresh_period`, default 1800 s): the model for our universe builder and
  cheap-gates-first ordering.
  https://github.com/freqtrade/freqtrade/blob/develop/freqtrade/plugins/pairlist/VolumePairList.py ,
  https://www.freqtrade.io/en/stable/plugins/
- ccxt async/rate-limit guidance — batch `fetch_ohlcv` concurrently with
  `enableRateLimit`; 5+× speedups are routine for multi-pair scanners.
  https://docs.ccxt.com/ , https://github.com/ccxt/ccxt/issues/11917
- Wilson bound + walk-forward already in-house (`app/backtest.py`) — keep; the gaps
  are classification and fill realism, not statistics.

## Current pipeline (verified 2026-06-12, branch `rebuild/lean-v2`)

```
scheduler.run_cycle ─► scanner.run_scan
  _universe (watchlist + all_symbols by volume, cached fallback)   scanner.py:40
  per symbol (SEQUENTIAL, network-blocking):
    get_ohlcv ─► estimate_win_rate ─► 6 agent votes ─► aggregate ─► decide
  trade? ─► _trade_block_reason (cooldown/streak/cap/OPUS)         scanner.py:205
        ─► TA bundle ─► batched Grok endorse/veto (fail-open)      scanner.py:225
        ─► _can_open caps ─► _open_session (+ Guardian veto, auto-approve)
```

---

## Bug register (fix in Phase S1 unless marked otherwise)

| # | Where | Bug | Severity |
|---|-------|-----|----------|
| B1 | `app/scanner.py:352-360` | `_open_session` keyword defaults bind `settings.scan_*` **at import time** — runtime Strategy-tab edits never reach the defaults. Today's caller passes all three explicitly, so it's latent, but it's a loaded trap. Replace defaults with `None` → resolve inside. | Med (latent) |
| B2 | `app/scanner.py:117-118` | `settings.backtest_lookback_days` is passed as the **bar `limit`** to `get_ohlcv`. Correct only while `backtest_timeframe="1d"`. Set 4h and the "365-day" window silently becomes 365 bars ≈ 61 days. Convert days→bars from the timeframe. | High (config-triggered) |
| B3 | `app/backtest.py:168-185` | Deadline exits with **positive** pnl are counted in `losses`/`loss_rate`. The expectancy is right, but `loss_rate` is inflated and the `max_loss_rate` gate punishes profitable deadline exits. Introduce a 3-way outcome: win(TP) / loss(SL or negative deadline) / flat(positive-or-zero deadline). Decide & document whether `win_rate` should include profitable deadline exits (recommend: no — keep TP-only — but loss_rate must exclude them). | High (gate correctness) |
| B4 | `app/backtest.py:89-91` | Wave fills assume execution exactly at the ladder target even when the bar **gaps below** it (open < target). Live fills would be at the gap price (cheaper avg) or the wave order may have been queued differently. Use `min(target, bar open)` when the bar opens below the target. Keep the change behind a single function so kss-strategy can verify parity with live `app/kss/pyramid.py` math. | Med (optimistic backtest) |
| B5 | `app/agents/backtest_agent.py:14-21` | Consensus votes use the **point** `win_rate` while the hard gates use `win_rate_lb`. On thin data the consensus is optimistic relative to the gates. Pass `win_rate_lb` into ctx and score from it (confidence already scales by trials). | Med |
| B6 | `app/scanner.py:119` | A symbol whose fetch fails or returns <30 candles is skipped **silently** — no audit, no candidate row. Add one compact audit per scan (count + list of skipped symbols), not one row per symbol. | Low |
| B7 | `app/scanner.py:55-67` | `scanner_last_universe` cache has **no timestamp/TTL** — a days-old universe is reused silently forever; also rewritten every scan (DB churn). Store `{ts, symbols}`; reuse only within a TTL (e.g. 24 h); write only when changed. | Med |
| B8 | `app/scanner.py:225-258` | Grok review is called even when `_can_open` is already at the concurrent/deployed cap — pure token burn. Pre-check the caps once before building the review batch; if nothing can open, skip the LLM call entirely (audit `skipped_capped_batch`). | Med (cost) |
| B9 | `app/data/providers.py:76-83` | `get_prices` does one `fetch_ticker` per symbol; ccxt `fetch_tickers(symbols)` batches in one call on most exchanges. | Low |
| B10 | `app/ta/indicators.py:67-81` & `app/agents/base.py:53` | RSI is Cutler's (simple average), pandas-ta Tier 2 is Wilder — the two tiers report **different RSI values for the same candles** in the same Grok bundle. Make Tier 1 Wilder-smoothed (matches Tier 2), keep the neutral-50 fallback. | Med (LLM input consistency) |
| B11 | scanner/scheduler various | `datetime.utcnow()` is deprecated (3.12+). Mechanical sweep to `datetime.now(timezone.utc)` **in scanner/scheduler only** (do not touch kss/ in this plan). Beware naive-vs-aware comparisons with stored ISO strings. | Low |
| B12 | `app/agents/liquidity.py:19-21` | Confidence is 1.0 whenever any volume exists, even with 3 candles. Scale by window fill: `conf = len(window)/20 * (1.0 if any vol else 0.3)`. | Low |
| B13 | `app/scheduler.py:61-65` | Hyperopt tunes **watchlist symbols only**, but `_effective_params` applies tuned rows to any symbol — universe symbols never get tuned yet run with global params forever. Either tune recent `trade` candidates too (bounded, e.g. top 10 by recency) or document watchlist-only-tuning as intended. | Low (decide, then 5-line fix or doc) |

**Explicit non-bugs** (checked, leave alone): SL-before-TP intrabar ordering (deliberately
pessimistic, `backtest.py:95-100`); Grok fail-open contract (deliberate availability
choice, `grok.py:109-117` — revisit only in S5); Guardian SELL-exemption
(`scheduler.py:126-128` — the drawdown-deadlock fix, do not "clean up").

---

## Phases

### Phase S1 — Correctness: fix the bug register
- **Agent**: `backend-builder` (B1–B2, B5–B13), then `kss-strategy` reviews **only** B3+B4
  (backtest outcome semantics must match live exits in `app/kss/`); `test-runner` to verify.
- **Read first**: `app/scanner.py`, `app/backtest.py`, `app/agents/backtest_agent.py`,
  `app/agents/aggregator.py:34-92`, plus the table above. Nothing else.
- **Order**: B3 first (it changes `estimate_win_rate`'s return shape: add `flats` count;
  keep existing keys so `Candidate`/UI don't break), then B2, B5, then the rest. B4 last,
  isolated commit (it shifts every backtest number; capture before/after expectancy on
  2–3 fixture symbols in the commit message).
- **Tests**: extend `tests/` for: deadline-exit-with-gain is not a loss (B3); gap-below
  fill price (B4); lookback→bars conversion at 4h (B2); `_open_session` resolves
  settings at call time (B1).
- **Acceptance**: full suite green; a paper scan produces candidates whose
  `loss_rate + win_rate + flat_rate = 100` (new `flats` surfaced in `Candidate.reason`).

### Phase S2 — Data layer: OHLCV cache + parallel fetch (biggest speed win)
- **Agent**: `backend-builder`. **Design sign-off** (one short consult): `kss-architect`
  on where the cache lives (recommendation below).
- **Problem**: every 15-min cycle re-downloads ~50 × 365 daily candles sequentially
  through one rate-limited ccxt client, while 1d candles change once per day.
- **Plan** (freqtrade `refresh_period` pattern):
  1. New module `app/data/candle_cache.py`: in-process dict keyed
     `(exchange_id, symbol, timeframe)` → `{fetched_at, candles}`. TTL = one bar period
     (1d → refetch only the head; minimum TTL 15 min). On expiry fetch only the tail
     (`since=` last cached ts) and merge — not the whole history.
  2. Parallel fetch: `concurrent.futures.ThreadPoolExecutor(max_workers=4)` over the
     cache-miss symbols only. ccxt sync client is not thread-safe per instance — use
     `enableRateLimit=True` and **one client per worker** (provider factory change), or
     a semaphore around a shared client; builder picks after a 10-line spike, documents why.
     (ccxt async_support is the cleaner end-state but pulls the scan into async — out of
     scope for this phase; note it as S2-follow-up.)
  3. `run_scan` takes candles from the cache; `_universe`'s `fetch_tickers` result gets
     the same TTL treatment (B7 work lands here if not already in S1).
- **Constraint**: cache is **read-through and crash-safe** — a cold start with no network
  must degrade exactly like today (empty list → symbol skipped, audited via B6).
- **Acceptance**: second scan within a bar period does **zero** OHLCV network calls
  (assert via a counter on the provider); scan wall-time on a 50-symbol universe drops
  from O(minutes) to O(seconds) warm. Add `scan_duration_ms` + `cache_hits/misses` to the
  `scan_start`/`cycle` audit payloads.

### Phase S3 — Pipeline ordering: cheap gates first
- **Agent**: `backend-builder`; `kss-architect` only if the filter-chain shape is contested.
- **Problem**: `_trade_block_reason` (cooldown / loss-streak / per-symbol cap / OPUS-owned —
  all cheap DB checks) runs **after** fetch + backtest + 6 votes. Blocked symbols pay full compute.
- **Plan**: evaluate the four blocks **before** the heavy work. Keep the audit trail:
  still create a `Candidate` row with `decision="skip"` and the block reason, but skip
  fetch/backtest/votes for it (win-rate fields null/0 + reason tag `pre-blocked`).
  Frontend check: `app/templates/partials/scanner.html` must render null win-rate rows
  (small `frontend-htmx` touch if not).
- **Also**: when the scheduler breaker is `frozen`, decide explicitly whether scanning
  should open *semi* sessions at all (today it does; auto-approve is gated but sessions
  still spawn — `scanner.py:381-401`). Recommend: when frozen, scan-and-record but do not
  open sessions; audit `skipped_frozen`. Confirm with the user before changing (one
  AskUserQuestion at build time).
- **Acceptance**: a cooldown-blocked symbol triggers zero OHLCV fetches (counter assert);
  candidates table still shows the row with its block reason.

### Phase S4 — Decision quality: de-correlate consensus from the gates
- **Agent**: `kss-strategy` leads (this is strategy math), `backend-builder` assists.
- **Problems**:
  1. Backtest feeds both the consensus (weight 0.40, the dominant vote) **and** four
     hard gates (E, win_lb, loss_rate, days) — the "multi-agent consensus" is largely
     the backtest agreeing with itself. The 5 cheap signal agents can almost never veto.
  2. `DEFAULT_WEIGHTS` are frozen constants (`aggregator.py:12-19`); the docstring's
     claim and the `ml: 0.25` weight were never re-validated.
- **Plan**:
  1. Split roles: gates own the backtest evidence; consensus becomes a pure
     *market-context* score from {trend, dip, volatility, liquidity, ml} (drop the
     backtest vote from `aggregate`, or set its weight to 0 — keep recording the vote
     row for audit). Re-tune `min_confidence` accordingly (it will drop; propose a new
     default from a paper-data sweep, e.g. replay recent ScanRuns).
  2. Make weights runtime-editable (`runtime_config` JSON, Strategy tab) with the
     current values as defaults — same pattern as `min_expectancy_pct` (commit 9d2b039).
  3. Validation harness, not vibes: a small script `scripts/replay_scans.py` that
     replays stored `ScanRun`/`Candidate` rows under old vs new consensus and reports
     decision flips + would-be PnL of flipped trades. Builder runs it and pastes the
     summary table into the PR.
- **Acceptance**: replay report attached; no gate regression (E & win_lb gates unchanged);
  weights editable + persisted; suite green.

### Phase S5 — Grok gate: cost & failure-mode hardening
- **Agent**: `backend-builder`; `security-reviewer` does a pass (LLM boundary = injection surface).
- **Plan**:
  1. B8 (skip review when capped) if not landed in S1.
  2. Cap the review batch (e.g. top 8 by expectancy) so one fat scan can't blow the
     token budget; audit the truncation.
  3. Add `grok_scanner_fail_mode: open|closed` setting (default `open`, preserving
     today's contract). `closed` = parse-failure/timeout means **no** opens this scan
     (capital-preservation posture for full-auto). Surface in Strategy tab.
  4. Per-day Grok spend ceiling for the scanner purpose (`ledger` already meters cost —
     add a daily cap check before the call, audit `skipped_budget`).
- **Read first**: `app/orchestrator/grok.py:55-145`, `app/orchestrator/ledger.py` (cost
  rows), `app/scanner.py:225-258`. Skill: `security-checklist` for the reviewer.
- **Acceptance**: unit tests for both fail modes + budget skip; reviewer signs off on
  prompt-injection posture (TA bundle is numeric-only — keep it that way; no free-text
  fields from market data may enter the prompt).

### Phase S6 — Observability & regression net
- **Agent**: `backend-builder` + `test-runner`; small `frontend-htmx` touch for the panel.
- **Plan**:
  1. Per-stage timings in the `cycle` audit: universe / fetch / backtest / votes / grok /
     open (ms each) + cache hit-rate (from S2).
  2. Scanner panel footer: last scan duration, symbols evaluated/skipped/pre-blocked,
     cache hit-rate — one HTMX partial, table not cards (user preference, memory
     `ui-prefers-tables-not-cards`).
  3. Property tests for `estimate_win_rate` invariants: rates sum to 100; expectancy
     bounded by [−sl−cost, tp−cost] when sl>0; spacing reduces trials monotonically.
- **Acceptance**: timings visible in Nhật ký feed + panel; property tests green.

## Sequencing & ownership summary

| Phase | Depends on | Lead agent | Diff size guess |
|-------|-----------|------------|-----------------|
| S1 bugfixes | — | backend-builder (+kss-strategy on B3/B4) | ~6 files, mostly small |
| S2 candle cache | S1 (B2,B6,B7) | backend-builder | 1 new module + provider/scanner edits |
| S3 cheap-gates-first | S1 | backend-builder | scanner.py + template touch |
| S4 consensus split | S1 (B3,B5) | kss-strategy | aggregator/scanner + replay script |
| S5 grok hardening | S1 (B8) | backend-builder + security-reviewer | grok.py/scanner.py/config |
| S6 observability | S2,S3 | backend-builder + frontend-htmx | audit payloads + 1 partial |

Run `test-runner` at the end of **every** phase; commit per phase at green
(`fm-conventions` commit format). S2 and S3 are independent after S1 and may be built
in parallel worktrees if desired.

## Open questions for the user (ask at build time, not now)
1. S3: should a frozen breaker also stop *semi* session creation from scans? (Recommended: yes.)
2. S4: drop the backtest vote from consensus entirely, or keep at reduced weight?
3. S5: default Grok daily budget for the scanner gate (USD)?
4. B13: hyperopt for non-watchlist symbols — extend or document as intended?
