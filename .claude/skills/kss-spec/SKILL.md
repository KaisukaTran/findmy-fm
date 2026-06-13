---
name: kss-spec
description: Canonical specification of the KSS Pyramid DCA strategy — wave formulas, take-profit, timeout, session states, and REST endpoints. Load this instead of re-reading docs/kss.md when working on app/kss/. Saves tokens by giving the exact math in one place.
---

# KSS Pyramid DCA — Canonical Spec

> **FROZEN.** `app/kss/pyramid.py` math must never change. Build features AROUND
> it. `tests/app/test_kss_invariants.py` locks the formulas/states/source_refs —
> if it fails, restore pyramid.py.

Core dataclass: `app/kss/pyramid.py::PyramidSession` (preserved verbatim from original).
Persistence/lifecycle authority: `app/kss/service.py` (DB-backed, no in-memory manager dict).

## Parameters
`symbol, entry_price>0, distance_pct in (0,100), max_waves>=1, isolated_fund>0, tp_pct>0, timeout_x_min>0, gap_y_min>=0`.

## Live wave formulas (geometric, increasing qty)
- `pip_size = pip_multiplier × minQty` (pip_multiplier default 2.0; minQty from Binance exchange info).
- `qty(n) = (n + 1) × pip_size`, rounded to `stepSize`, floored at `minQty`.
- `price(n) = entry_price × (1 - distance_pct/100)^n`, rounded to price precision.
- `estimate_total_cost(N) = Σ price(n)·qty(n)` for n in [0, N).

## Take profit
- Triggers when `market_price ≥ avg_price × (1 + tp_pct/100)`.
- Action: MARKET SELL the full `total_filled_qty`; status → TP_TRIGGERED.

## On fill
Update wave→filled, recompute `avg_price = total_cost / total_filled_qty`. Then: check TP → check timeout → else queue next wave (if `n+1 < max_waves` and `next_cost ≤ remaining_fund`).

## Timeout
Stop new waves when time since last fill `> timeout_x_min` AND gap between last two fills `< gap_y_min`.

## States
`PENDING → ACTIVE → (TP_TRIGGERED | STOPPED | TIMEOUT) → COMPLETED`.

## Preview projection — DIFFERENT ON PURPOSE
`POST /api/kss/preview` uses a simplified projection (tests depend on it; do NOT change to geometric):
- `qty_per_wave = isolated_fund / max_waves / entry_price` (equal each wave).
- `target_price(n) = entry_price × (1 - distance_pct/100 × n)` (LINEAR drop).
- `avg_price_after`, `tp_price_after = avg × (1 + tp_pct/100)`, cumulative qty/cost, `price_range_pct`.

## Order source_ref convention
`pyramid:{session_id}:wave:{wave_num}` for waves, `pyramid:{session_id}:tp` for the TP sell.

## Endpoints (`/api/kss`)
`POST /preview` · `POST /sessions` · `POST /sessions/{id}/start` · `POST /sessions/{id}/stop` · `PATCH /sessions/{id}` · `GET /sessions/{id}` · `GET /sessions` · `GET /summary` · `DELETE /sessions/{id}` · `POST /sessions/{id}/check-tp`.

Every generated order goes through the pending-order approval queue — KSS never executes directly.
