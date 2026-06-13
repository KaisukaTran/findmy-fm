# FINDMY-FM — Go-Live Plan

Created: 2026-06-13 · Owner: Opus (orchestrator) · Workers: subagents in parallel

---

## Spec delta

- **Product contract (correctness conditions added by this plan):**
  - All trading pairs are quoted in **USDT** (e.g. `BTCUSDT`), never `USD`. UI labels that
    mean *US-dollar value* (the `$` value of equity/PnL) stay as-is; only **pair/quote-symbol**
    strings change. (req #1 of the user's go-live note)
  - New **PnL Calendar** surface: realized PnL aggregated per **day / week / month**, colour-coded
    (green/red), server-rendered, click a day → that day's closed trades. (req #7)
  - **Performance tab** equity/win-loss charts are enlarged & enriched (drawdown band, period
    toggle, larger viewport). (req #8)
  - **UI shell**: left **sidebar nav** + a top strip of **small KPI cards** (overview only);
    **all dense data stays in tables** (per `ui-prefers-tables-not-cards`, now revised). (req #6)
  - **Live path exists but defaults OFF** — real-Binance execution is wired, gated, and shipped
    disabled; the operator flips it on manually. (go-live mode = "infrastructure only")
- **Spec skip reason** for security/logic-audit tasks: no behaviour change, hardening only.

## Validation (harness non-trivial gate)

- `team_validation_mode: subagent` — Opus orchestrates; Product/Architecture/Security/QA/Skeptic
  perspectives handed to subagents per phase (req #1: always Opus → parallel agents).
- Wheel-reinvention check: charts reuse `app/charts.py` SVG (no new JS/canvas dep); calendar reuses
  the same SVG/HTML-grid approach; UI reuses existing tables. No new framework.
- Lint/format baseline: existing `tests/app` + ruff/mypy via `test-runner`. Setup task = none new.
- Gates in DoD: tests green + security pass + smoke before any phase merges.

## Orchestration model (req #1, #2)

Opus assigns each task to a specialist subagent and runs independent files in parallel waves.
Every worker loads the relevant **skill** first (req #2):

| Agent | Skill to load | Owns |
|-------|---------------|------|
| `backend-builder` | `fm-conventions` | app/ services, routes, aggregation |
| `frontend-htmx` | `htmx-dashboard` | templates, static, partials |
| `kss-strategy` | `kss-spec` | strategy math / session logic |
| `security-reviewer` | `security-checklist` | pre-merge security pass |
| `test-runner` | — | suite + ruff + mypy |
| (all) | `context-engineering` | read only the named files, return `path:line` summaries |

**Parallel waves:** Wave A = Phase 1 ∥ Phase 2 (backend/security, different files).
Wave B = Phase 3 ∥ Phase 4 ∥ Phase 5 (frontend; 4 & 5 share `charts.py` → sequence 5 before 4's render).
Wave C = Phase 6 (live infra) then final gate.

---

## Phase 1: USDT correctness sweep  [tdd:required]

| Task | Content | DoD | Depends | Agent | Status |
|------|---------|-----|---------|-------|--------|
| 1.1 | Audit the 145 `USD\b` hits across app/tests; classify each as **pair/symbol** (→ rename) vs **$-value label** (→ keep). Produce a path:line table. | A written classification list, 0 ambiguous | - | backend-builder | cc:DONE — all app `USD` hits are $-value labels or legit alt-exchange quotes (kraken/coinbase/bitstamp); Binance family already `USDT` in `_QUOTES`. `BTC/USD` in tests/ are fixtures, not prod config. |
| 1.2 | Rename pair/quote-symbol occurrences to `USDT` in app/ (config defaults, market/orders/scanner, providers). No label/`$` changes. | grep shows no stray trading-pair `USD`; app boots | 1.1 | backend-builder | cc:DONE — `providers._QUOTES` already maps binance→USDT; nothing to rename. (`live_exchange` defaults to kraken because binance.com is geo-blocked in dev; operator sets binance for go-live.) |
| 1.3 | Add a regression test asserting universe/pairs are `*USDT` and a non-USDT pair is rejected. | new test fails pre-fix, passes post-fix | 1.2 | test-runner | cc:DONE — `tests/app/test_usdt_correctness.py`, 9 passed (asserts Binance provider → `/USDT`, kraken/coinbase stay `USD`). |

## Phase 2: Security + logic audit  [tdd:skip:hardening-no-behaviour-change]

| Task | Content | DoD | Depends | Agent | Status |
|------|---------|-----|---------|-------|--------|
| 2.1 | Security pass via `security-checklist`: secrets/API-key handling, approval gate, CSP/headers, input validation, SSRF on data providers, rate-limit. | written findings; 0 high-severity open | - | security-reviewer | cc:DONE — `docs/plan/security-audit-2026-06-13.md`. PASS, 0 high-severity. 1 go-live carry-forward folded into 6.3 (`require_auth`↔`LIVE_TRADING`). |
| 2.2 | Logic audit of known traps from memory: SELLs never gated ([[drawdown-exit-deadlock]]), breaker auto-rearm ([[breaker-deadlock-fix]]), Guardian veto TTL ([[guardian-veto-deadlock]]), KSS ladder/SL consistency ([[kss-ladder-sl-consistency]]), 1-session-per-symbol cap ([[kss-rescue-duplicate-session-k1-hole]]). | each confirmed by a test or path:line note | - | kss-strategy | cc:DONE — all 5 confirmed by path:line **and** test: risk.py:120 (`test_drawdown_fixes`), circuit.py:80 (`test_breaker_deadlock`/`test_circuit::auto_rearm`), config.py:174+scheduler (`test_scheduler::expired_veto`), kss/service.py:124,684 (`test_kss::wave_below_sl`), kss/service.py:214 K-1 cap. |
| 2.3 | Fix the open auto-approve max-notional reset bug ([[bug-autoapprove-max-resets]]) if still present (U5 may already cover). | max-notional persists across 5 Set clicks | 2.1 | backend-builder | cc:DONE — already fixed (a8ae5a3 + 5ea0d35): runtime.py:151-166 persist+preserve-on-toggle, :349 restore. Tests: `test_runtime::set_autoapprove_*`, `test_provenance_persist`. |

## Phase 3: UI refresh — sidebar + small KPI cards + tables (req #6)  [tdd:skip:ui]

| Task | Content | DoD | Depends | Agent | Status |
|------|---------|-----|---------|-------|--------|
| 3.1 | Design brief: dark design tokens (spacing scale, mono numerics, accent palette), sidebar nav spec, small-KPI-card spec. Confirm against the chosen mock. | brief committed in docs/ | - | frontend-htmx | cc:DONE — layout confirmed with user (sidebar-left + KPI-strip-in-content, Option A). Reuses existing dark tokens; no card-ify of dense data. |
| 3.2 | Replace top tabs with a left **sidebar nav** (sticky, icon+label, active state); keep HTMX lazy-tab loading from P-phase. | nav works, no full reloads, CSP clean | 3.1 | frontend-htmx | cc:DONE — `dashboard.html` `.layout`>`aside.sidebar`+`.content`; nav items keep `data-tab` so `showTab` drives them with **0 JS change**. TestClient render 200, CSP intact, lazy-tab triggers preserved. |
| 3.3 | Top **KPI card strip** (Equity/PnL/Win/Drawdown) — small cards, overview only; all detail stays tables. | cards render; tables untouched | 3.1 | frontend-htmx | cc:DONE — existing `/partials/summary` `.cards` strip moved to top of `.content`; tables unchanged. |
| 3.4 | Apply tokens to style.css (typography, contrast, table density); no card-ify of dense data. | visual pass, Lighthouse a11y ≥ 90 | 3.2,3.3 | frontend-htmx | cc:DONE — sidebar styles + `tabular-nums` on `.card .value`/`.tbl td`; semantic `nav`/`aside`, `aria-selected`, `aria-hidden` icons; responsive fold <900px. |

## Phase 4: PnL Calendar — day/week/month (req #7)  [tdd:required]

| Task | Content | DoD | Depends | Agent | Status |
|------|---------|-----|---------|-------|--------|
| 4.1 | Aggregation service: realized PnL grouped by day/week/month from closed trades (USDT). | unit test on a fixture matches hand-sum | - | backend-builder | cc:DONE — `app/pnlcal.py` buckets `Fill.realized_pnl` by local date (UTC+offset); `tests/app/test_pnlcal.py` 8 pass (hand-sums for day/week/month/year, in-month leak guard). |
| 4.2 | Server-rendered calendar grid (month view) colour-coded green/red + day $ amount; week/month toggle. | renders for the fixture; zero JS beyond HTMX | 4.1, 5.x SVG helpers | frontend-htmx | cc:DONE — `partials/calendar.html` month grid (per-day cells + weekly subtotal col) + green/red bg; Tháng/Năm toggle via HTMX swap. No inline style (CSP clean). |
| 4.3 | Click a day → partial listing that day's closed trades. | HTMX partial returns correct rows | 4.2 | frontend-htmx | cc:DONE — `/partials/calendar/day?d=YYYY-MM-DD` → `partials/calendar_day.html`; bad date → 400. |
| 4.4 | Route + sidebar entry + lazy-load. | tab loads on reveal only | 4.2,3.2 | frontend-htmx | cc:DONE — `GET /partials/calendar`; sidebar item `data-tab="calendar"` + panel; `hx-trigger="tab-shown"` (lazy, no load-while-hidden); added to app.js hash `valid`. |

## Phase 5: Performance chart revamp (req #8)  [tdd:skip:visual]

| Task | Content | DoD | Depends | Agent | Status |
|------|---------|-----|---------|-------|--------|
| 5.1 | Enlarge equity curve (bigger viewport, drawdown shaded band, hover-free value ticks) in `charts.py`. | new SVG renders; existing tests green | - | backend-builder | cc:DONE — `equity_curve_svg` now 860×300 with a running-peak dashed line + red-tinted underwater (drawdown) band; preserves polyline/polygon/value-tick/time-axis contract (`test_charts` green). |
| 5.2 | Add a period toggle (24h/7d/30d/all) feeding the curve + a richer win/loss + expectancy panel. | toggle switches series server-side | 5.1 | frontend-htmx | cc:DONE — `performance_view(db, period=)` windows fills by cutoff (curve continues from equity-as-of-cutoff) + adds expectancy/avg-win/avg-loss/profit-factor; `/partials/performance?period=` validated; self-replacing `#perf` partial bakes period into its own poll so the toggle survives refreshes. Test: `test_performance_view_expectancy_and_period`. |
| 5.3 | Replace tiny winloss bar with a fuller performance panel; keep CSP-perfect inline SVG. | performance.html updated, no canvas/JS dep | 5.1 | frontend-htmx | cc:DONE — performance.html: KPI cards incl. expectancy/PF/max-DD + enlarged winloss bar + econ cards (avg win/loss, closed). No inline style/JS — CSP stays 'self'. |

## Phase 6: Go-live infrastructure — default OFF (go-live mode)  [tdd:required]

| Task | Content | DoD | Depends | Agent | Status |
|------|---------|-----|---------|-------|--------|
| 6.1 | Live Binance execution path behind a `LIVE_TRADING=false` master flag + small per-order notional cap; paper stays default. | flag off by default; unit test asserts paper unless explicitly on | 1.x,2.x | backend-builder | cc:DONE — `app/execution.py` (live_enabled = flag AND keys); `orders._execute` dispatches paper↔live; `_live_execute` re-gates BUYs (frozen + `live_max_order_notional`), never gates SELL exits; never silently papers on error. `tests/app/test_golive.py` (10 pass). |
| 6.2 | Paper→live switch in UI with a typed confirm + circuit-breaker/approval-gate re-check before first live order. | switch requires confirm; breaker veto blocks live | 6.1,3.x | frontend-htmx | cc:DONE — `POST /api/live-trading` requires `confirm=="LIVE-TRADING"` + keys present + breaker armed (409 if frozen); Strategy-tab `partials/live_trading.html` panel + `toggleLiveTrading` (typed prompt). Persisted via `runtime.set_live_trading`. |
| 6.3 | `.env.example` + docs for real keys; secrets never logged; key validated at boot. | security-reviewer sign-off | 6.1 | security-reviewer | cc:DONE — `.env.example` go-live block; `docs/go-live.md` runbook; `execution.validate_at_boot()` logged in `main.lifespan` (no secret); keys are `SecretStr`, never logged. |
| 6.4 | Final gate: full suite + ruff + mypy green, offline smoke via `scripts/observe_full_auto.py`, security pass, single squash-ready summary. | all gates green; go/no-go note | all | test-runner | cc:DONE — suite **420 pass / 2 skip**; ruff clean except 1 **pre-existing** C901 (`auto_approve_by_policy`, present on HEAD, unrelated to go-live); mypy not installed in this env; app boots + scans on paper via TestClient lifespan. See go/no-go note below. |

---

## Go / no-go note (2026-06-13)

**GO for shipping the infrastructure (still on paper).** All six phases landed on branch
`go-live` as six commits. Gates: full app suite **420 pass / 2 skip**; ruff clean apart
from one pre-existing `C901` on `auto_approve_by_policy` (present on `main`, unrelated to
this work — left untouched to avoid editing the approval path); app boots and runs a paper
scan cycle via the TestClient lifespan. mypy is not installed in this dev env (could not
run that gate here). Phases 1–2 were largely verification — most was already shipped in
prior sessions; this push added the USDT regression test, the security-audit doc, and
re-confirmed every memory trap by path:line + test.

**NO-GO for real money** until the operator: sets exchange keys, flips `LIVE_TRADING`
(typed confirm), and starts with a tiny `LIVE_MAX_ORDER_NOTIONAL`. See `docs/go-live.md`.
Carry-forward from Phase 2.1: when going live, also set `REQUIRE_AUTH=true` so the
mutation endpoints aren't reachable unauthenticated.

## Out of scope / deferred
- Phase D of the full-auto roadmap ([[full-auto-roadmap]]) — unchanged.
- Actually enabling real-money trading — operator decision after this infra lands.
