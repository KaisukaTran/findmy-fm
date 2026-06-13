# Cost-tracking plan (phí vận hành: trade / rút / VAT / AI)

**Created:** 2026-06-14 · **Branch:** `telegram-notify` (or a new `cost-tracking`)

## Goal
A single "Chi phí" view that totals every operating cost and can be sliced by
**tuần / tháng / năm**:

1. **Phí giao dịch** — taker fee paid per fill. *Already recorded* in `Fill.fee`; just aggregate.
2. **Phí rút sàn** — only booked when a withdrawal actually happens. Fee = Binance %-rate
   **+ 0.05% dung sai**, on the withdrawn amount.
3. **VAT 10%** — `0.10 × số tiền rút`, booked per withdrawal.
4. **Phí AI (Claude + Grok)** — **metered actuals** from `OpusCostLedger.billed_cost`, with a
   **fallback estimate** of **$25/tháng Claude + $20/tháng Grok** when a period has no metered data.

## Resolved decisions (from the user)
- VAT base = **số tiền rút** (not the fee).
- Withdrawal fee = **(`withdrawal_fee_pct` + 0.05% tolerance) × amount** ("theo quy định Binance theo %").
- AI cost = **both**: metered ledger first, prorated estimate as fallback.

## Formulas (canonical)
```
withdrawal_fee   = amount × (withdrawal_fee_pct + withdrawal_fee_tolerance_pct) / 100
vat              = amount × vat_pct / 100                      # vat_pct = 10
withdrawal_cost  = withdrawal_fee + vat                        # booked once, at withdrawal time

trade_fees(P)    = Σ Fill.fee            for fills with executed_at ∈ P
withdraw_cost(P) = Σ (fee + vat)         for withdrawals with created_at ∈ P
ai_metered(P)    = Σ OpusCostLedger.billed_cost for rows with ts ∈ P   # split Claude vs Grok by `purpose`
ai_estimate(P)   = (claude_monthly + grok_monthly) × (len(P) / 30.44 days)
ai_cost(P)       = ai_metered(P) if ai_metered(P) > 0 else ai_estimate(P)

total(P)         = trade_fees(P) + withdraw_cost(P) + ai_cost(P)
net_after_cost(P)= realized_pnl(P) − total(P)                  # optional, nice-to-have
```
Period P ∈ {ISO week, calendar month, calendar year}, in display TZ (GMT+7). Storage stays UTC.

## Data model — new `Withdrawal` (app/models.py)
| col | type | note |
|---|---|---|
| id | int pk | |
| amount | float | số tiền rút (USD) |
| fee | float | computed at insert |
| vat | float | computed at insert |
| exchange | str | default "binance" |
| note | str? | optional |
| created_at | datetime | UTC, indexed |

`fee`/`vat` are frozen at insert (snapshot of the rates) so later config changes don't rewrite history.

## Config — new knobs (app/config.py / .env)
- `withdrawal_fee_pct: float = 0.0` — Binance withdrawal fee % (operator sets per usage).
- `withdrawal_fee_tolerance_pct: float = 0.05` — the dung-sai buffer added on top.
- `vat_pct: float = 10.0` — VAT on withdrawal amount.
- `ai_monthly_claude_usd: float = 25.0` — fallback estimate.
- `ai_monthly_grok_usd: float = 20.0` — fallback estimate.

## Service — new `app/costs.py`
- `record_withdrawal(db, amount, note=None) -> Withdrawal` — validates amount>0, computes
  fee + vat from config, persists, returns the row.
- `cost_summary(db, period: str, buckets: int = 12) -> dict` — returns the current bucket
  totals + a series of the last `buckets` periods, each with
  `{trade_fees, withdrawal_fee, vat, ai_claude, ai_grok, ai_total, total}`.
- Helpers: `_period_bounds(period, n)`, `_ai_split(rows)` (Claude vs Grok via `purpose`).

## API — new endpoints (app/routes.py)
- `POST /api/withdrawals` (auth) — body `{amount, note?}` → records + returns the row.
- `GET  /api/withdrawals` — recent withdrawals (for the table).
- `GET  /api/costs?period=week|month|year` — `cost_summary` JSON.
- `GET  /partials/costs?period=…` — HTML partial for the dashboard tab.

## UI — new "Chi phí" tab (dashboard.html + partials/costs.html)
- Sidebar tab "Chi phí" (icon e.g. ₫/💸).
- Period toggle **Tuần / Tháng / Năm** (re-fetches `/partials/costs?period=…`).
- Breakdown table per bucket: Trade fee · Phí rút · VAT · AI Claude · AI Grok · **Tổng**
  (+ optional Net sau phí). Totals row.
- Small "Ghi nhận lệnh rút" form: amount + note → `POST /api/withdrawals` → refreshes.
- Reuse existing filters: `money`, `localdt`; tables not cards ([[ui-prefers-tables-not-cards]]).

## Tests (tests/app/test_costs.py)
- withdrawal fee + VAT math (incl. the 0.05% tolerance, frozen-at-insert).
- aggregation by week/month/year buckets (fills + withdrawals land in the right bucket, TZ-aware).
- AI cost: metered used when >0; estimate fallback when a bucket is empty; Claude/Grok split.
- API: record withdrawal, summary shape; `require_auth` gate on POST.

## Plans.md task ledger (harness)
Spec delta: this doc IS the product contract for the cost feature (repo has no root
spec.md; follows the existing `docs/plan/` convention). Precedence: this spec > Plans table.

| Task | Content | DoD | Depends | Status |
|------|---------|-----|---------|--------|
| 1.1 | `Withdrawal` model + `init_db` table + 5 config knobs (fee%, 0.05% tol, VAT 10%, $25/$20) `[tdd:required]` | table created on boot; `settings` load the 5 knobs | - | cc:TODO |
| 1.2 | `app/costs.py`: `record_withdrawal` (fee+VAT frozen at insert) + `cost_summary(period,buckets)` `[tdd:required]` | unit math exact; buckets TZ-aware; AI metered→estimate fallback | 1.1 | cc:TODO |
| 1.3 | API: `POST/GET /api/withdrawals`, `GET /api/costs`, `GET /partials/costs` `[tdd:required]` | endpoints 200; bad amount → 422; auth-gated POST | 1.2 | cc:TODO |
| 1.4 | UI: "Chi phí" tab + Tuần/Tháng/Năm toggle + breakdown table + withdrawal form `[tdd:skip:server-rendered-partial]` | tab renders; toggle re-fetches; form posts | 1.3 | cc:TODO |
| 1.5 | `tests/app/test_costs.py` (math, bucketing, fallback, API shape) `[tdd:required]` | suite green | 1.2 | cc:TODO |
| 1.6 | Security pass on `POST /api/withdrawals` (amount validation, auth, no injection) `[tdd:skip:review-only]` | no findings | 1.3 | cc:TODO |

**Sequencing:** 1.1→1.2→1.3 (backend chain); 1.4 + 1.5 after 1.3/1.2; 1.6 last.
Built inline by Opus (user did not request subagents) with Karpathy discipline; commit per task at green.

## Team validation (manual-pass — subagents not used, per user)
`team_validation_mode: manual-pass`
- **Product:** matches the 3 confirmed decisions; weekly/monthly/yearly slices delivered. ✔
- **Architecture:** reuses `Fill.fee` + `OpusCostLedger`; only `Withdrawal` is new; `app/costs.py`
  mirrors `app/pnlcal.py` (period rollup) — consistent with the codebase. ✔
- **Security:** POST writes financial records → validate `amount>0`, gate on `require_auth`,
  no raw SQL (ORM). Does NOT read secrets. ✔ (formal pass = 1.6)
- **QA:** 1.1/1.2/1.3/1.5 carry `[tdd:required]`; DoD are Yes/No. Lint/format baseline = ruff
  (already in repo); no setup task needed. ✔
- **Skeptic:** wheel-check done — no existing withdrawal/cost-summary code (grep: only
  `costengine.py` = %-gating, not actuals). Fallback-vs-metered AI rule is the one fuzzy
  edge; pinned in the formula block. ✔
- Wheel-reinvention check: `Fill.fee`, `OpusCostLedger.billed_cost`, `pnlcal` period helpers reused.

## Open questions / notes
- `withdrawal_fee_pct` default is 0.0 — operator must set the actual Binance rate per coin/usage
  (Binance real withdrawal fees are fixed network fees per coin; modeling as % per the user's choice).
- AI metered split needs Grok calls tagged `purpose="grok"` in `meter_cost`; verify/添加 if missing.
- Scanner Grok-gate cost is NOT currently metered into `OpusCostLedger` — fallback estimate covers it.
- Stretch: "Net sau phí" per period; export CSV; cost trend chart.
```
