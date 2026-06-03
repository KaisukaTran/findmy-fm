# FINDMY-FM v2 — Lean Rebuild

A from-scratch, tinh-gọn rebuild of FINDMY-FM that keeps the product purpose
(paper-trading simulator centered on the **KSS Pyramid DCA** strategy) while
removing the over-engineering of the original (split SOT/TS services + two DBs,
L1/L2 cache, full JWT, Prometheus, an in-memory KSS manager alongside the DB).

## What changed

| Area | Old | New (`app/`) |
|------|-----|--------------|
| Packages | `src/findmy/` + `services/` (55 files) | single `app/` package |
| Database | `sot.db` + `ts.db` | one `data/findmy.db` (5 tables) |
| Caching | L1/L2 cache service | one TTL cache in `app/market.py` |
| Auth | JWT + refresh + scopes | one API key (`X-API-Key`), opt-in |
| KSS state | DB **and** in-memory manager dict | DB only — `app/kss/service.py` is the single authority |
| UI | server HTML + ad-hoc JS | HTMX partials + Alpine (CSP build) + tight CSP |
| Metrics | Prometheus | dropped (just `/health`) |

The KSS strategy math is **preserved verbatim** in `app/kss/pyramid.py` (only
import paths changed). The dashboard "Preview Pyramid" keeps its original
equal-qty / linear-price projection (distinct from the live geometric waves).

## Layout

```
app/
  main.py      factory + lifespan (create tables) + security + static + routers
  config.py    pydantic-settings (SecretStr for secrets)
  db.py        single SQLite engine, SessionLocal, get_db(), init_db()
  models.py    PendingOrder, Fill, Position, KssSession, KssWave
  market.py    Binance public prices + exchange info + TTL cache
  risk.py      pip sizing + position/daily-loss checks
  orders.py    queue -> approve/reject -> paper execute -> Fill/Position (+ KSS hook)
  portfolio.py read views: positions / trades / summary
  routes.py    JSON API + dashboard (HTMX partials) + /ws
  security.py  API-key dependency, security headers, CORS, rate limiting
  kss/         pyramid.py (verbatim) · service.py (DB authority) · routes.py
  templates/   dashboard.html + partials/
  static/      htmx.min.js, alpine.min.js (CSP build), app.js, style.css
```

## Run

```bash
python -m venv .venv && .venv/Scripts/activate        # Windows
pip install -r requirements-app.txt
uvicorn app.main:app --reload --port 8000
# open http://localhost:8000  → create a $10,000 KSS session, Start, watch waves
```

## Test

```bash
pytest tests/app -c tests/app/pytest.ini    # 38 tests, isolated from legacy suite
ruff check app tests/app
```

`tests/app/` is its own pytest rootdir so it does not load the legacy root
`conftest.py`/plugins. A throwaway SQLite DB is configured in its conftest.

## Order safety invariant

Every order — manual or KSS-generated — is inserted into `pending_orders` and
only executes after `approve`. Nothing bypasses the queue. Risk checks attach a
note but never block queuing (the user keeps final judgment at approval).

## Multi-agent + skills workflow

This rebuild ships a Claude Code agent team under `.claude/` designed for
token-efficient development and maintenance:

**Agents** (`.claude/agents/`): `kss-architect` (opus, design/review),
`backend-builder` (sonnet, FastAPI/SQLAlchemy), `kss-strategy` (sonnet, strategy
correctness), `frontend-htmx` (sonnet, dashboard), `security-reviewer` (opus,
audits), `test-runner` (haiku, runs checks and reports tersely).

**Skills** (`.claude/skills/`, progressive disclosure — loaded only when needed):
`kss-spec` (canonical formulas), `fm-conventions` (code layout/idioms),
`htmx-dashboard` (UI patterns), `security-checklist` (review checklist).

Token principles: mechanical work on sonnet/haiku, architecture/security on opus;
agents return summaries (not file dumps); skills replace a bloated always-on
context. Structure informed by community collections (hesreallyhim/awesome-claude-code,
VoltAgent/awesome-claude-code-subagents).

> Legacy `src/findmy/` + `services/` remain in the tree for reference until the
> lean app is adopted; they can be removed in a final cleanup commit.
