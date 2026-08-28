# FINDMY-FM — working notes for Claude Code

Paper-trading simulator for the **KSS Pyramid DCA** strategy. One package (`app/`), one
SQLite file. Real-money execution is wired but ships OFF.

## Load these instead of re-reading source

`.claude/skills/` holds the durable context — load on demand, do not duplicate it here:
`fm-conventions` (layout, idioms, commit format), `kss-spec` (the frozen strategy math),
`htmx-dashboard` (UI patterns), `security-checklist` (review pass), `context-engineering`
(the default working discipline).

## Invariants — never break these

- **`app/kss/pyramid.py` math is FROZEN.** Build guards/config *around* it.
  `tests/app/test_kss_invariants.py` locks the formulas.
- **Exits are never gated.** A SELL that reduces risk is never blocked by the circuit
  breaker, the notional cap, or the Guardian. Slowing an exit is the one unforgivable bug.
- **Everything goes through the approval queue.** No path executes an order directly.
- **K-2:** a take-profit may never realize below the true aggregate cost basis + fees.
- **Secrets** live only in `settings` as `SecretStr`, and are never logged or serialized.

## Two worktrees, two instances

| | paper | live |
|---|---|---|
| path / branch | `d:\FINDMY` @ `dev` or a feature branch | `d:\FINDMY-live` @ `live` |
| port · DB · lock | 8000 · `data/findmy.db` · 8801 | 8001 · `data/live.db` · 8802 |
| launcher | `scripts/run_paper.ps1` | `scripts/run_live.ps1` |

Develop and test in **paper**; `live` only ever receives reviewed merges. Details in
`docs/dual-instance.md`.

**`live` has diverged from `main`** (dynamic exits, pyramid-up, the 90s position guard, the
WS feed, `app/clock.py:utcnow()` replacing `datetime.utcnow()`, auth-on-by-default). Base new
work on whichever branch it will actually run on, and expect a real merge — an auto-merge that
looks clean has already produced a `NameError` once.

## Live execution: two models, one switch

`MAKER_ORDERS=false` → legacy: wait for the market to reach a limit, then send a marketable
order. `MAKER_ORDERS=true` → the resting model (task 1.5): rungs and the take-profit sit on
the exchange in advance and the venue fills them; `orders.reconcile_live_orders` books the
fill. Everything in that model is gated by `orders.resting_model_active()` = live **and**
maker, so paper is untouched either way.

Current state, next steps and every design decision: **`docs/plan/live-readiness-plan.md`**
(read it before touching the live path). Testnet setup and the two harnesses
(`scripts/testnet_check.py`, `scripts/testnet_e2e.py`): `docs/testnet-setup.md`.

## Checks

```bash
pytest tests/app -c tests/app/pytest.ini      # the suite for app/
ruff check app tests/app                       # pre-existing failures in files you did not touch are not yours
```

Before claiming a change is green, run the suite on the branch you are actually targeting and
compare failures against that branch's own baseline.

## Where a session can and cannot run

A **cloud session** (Claude Code on the web) runs in an isolated container with a fresh clone:
no access to `d:\`, and its egress proxy blocks `binance.com` / `testnet.binance.vision`. It
can write code, run the offline test suite, and push — it cannot run the testnet harnesses or
drive the live instance. Anything that must reach the exchange, or touch the real worktrees,
belongs in a **local session** on the trading machine.
