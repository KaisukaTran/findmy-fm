---
name: fm-conventions
description: Coding conventions, file layout, and commit format for the FINDMY-FM lean rebuild. Load when writing or reviewing app/ code so you match the project's structure and idioms. Replaces the bloated copilot-instructions for on-demand use.
---

# FINDMY-FM Conventions (lean rebuild)

## Layout (single package, single DB)
```
app/
  main.py     app factory, lifespan (create tables), middleware, mounts
  config.py   pydantic-settings, SecretStr for secrets
  db.py       one SQLite engine, SessionLocal, get_db() dependency, Base
  models.py   all ORM models (PendingOrder, Fill, Position, KssSession, KssWave)
  schemas.py  Pydantic v2 request/response
  market.py   Binance public prices + exchange info + TTL cache
  risk.py     pip sizing + position/daily-loss checks
  orders.py   queue → approve/reject → paper execute → Fill/Position
  kss/        pyramid.py (verbatim), service.py, routes.py
  routes.py   core API + dashboard + /ws
  templates/  static/
```

## Rules
- Routes are thin: parse (Pydantic) → call domain function → return schema. No SQL/business logic in routes.
- DB access via `get_db()` dependency or `SessionLocal()` in a try/finally; commit explicitly; never leave sessions open.
- Facts are append-only: insert Fills, never edit history; Position is updated state derived from fills.
- All money/qty as float for the demo; round prices to symbol precision, qty to stepSize.
- Type hints on every function; concise docstrings (what + why, not how).
- Secrets: only via `settings` (SecretStr). Never log or serialize secret values.
- Imports use the `app.` package root (no `src.findmy` / `services.` legacy paths).

## Testing
- Tests live in `tests/app/`; deterministic, no live network — mock `app.market` price/exchange calls.
- Keep `tests/app/test_kss.py` green (formula contract). Add a test per new branch.

## Tooling
- Format: `black app/ tests/app`  ·  Lint: `ruff check app/ tests/app`  ·  Types: `mypy app/ --ignore-missing-imports`.

## Commits
`feat(module): …` `fix(module): …` `docs(module): …` `refactor(module): …` `test(module): …`. Small, verifiable commits (1-3 files).
