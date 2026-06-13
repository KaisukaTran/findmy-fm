# FINDMY-FM — Lean Paper-Trading Simulator

> A from-scratch, lean trading simulator built around the **KSS Pyramid DCA**
> strategy, with a **multi-agent scanner**, optional **full-auto** execution, a
> server-rendered **HTMX dashboard**, and a deliberately-gated **live trading**
> path that is **shipped OFF**.

**Version:** 2.0.0 · **Stack:** FastAPI + SQLAlchemy + Jinja2/HTMX/Alpine ·
**Storage:** one SQLite file · **License:** MIT · **Status:** paper by default

---

## What it is

FINDMY-FM is a single Python package (`app/`) that paper-trades the **KSS Pyramid
DCA** strategy: it builds a position with progressively larger buy "waves" as
price dips, then takes profit above the running average. A decision layer of
deterministic quant agents scans the market, backtests each pair, and (semi- or
fully-automatically) opens KSS sessions for pairs that clear every safety gate.

Everything runs on **public exchange data** (via `ccxt`, no API key) and executes
as **paper trades** unless an operator explicitly arms the live path. Every order
— manual or strategy-generated — passes through one approval queue.

This `app/` package is a **lean v2 rebuild** of an earlier multi-service design
(`src/findmy/` + `services/`, now legacy/reference). See
[docs/REBUILD.md](docs/REBUILD.md) for what changed and why.

### Key features

- **KSS Pyramid DCA** — wave-based DCA with take-profit, stop-loss, trailing
  stop, timeout, and a ≤30-day session deadline. Math is frozen in
  `app/kss/pyramid.py`. See [docs/kss.md](docs/kss.md).
- **Multi-agent scanner** — `trend`/`dip`/`volatility`/`liquidity`/`ml` agents +
  a backtested win-rate gate decide which pairs to trade. See
  [docs/AGENTS.md](docs/AGENTS.md).
- **Automation, gated** — semi-auto (you approve) or full-auto (auto-approved,
  still risk-checked), guarded by a **circuit breaker**, an optional **AI
  Guardian** veto layer, and a **loss-streak block**.
- **Order safety invariant** — nothing bypasses `pending_orders`; orders only
  execute after `approve`. Risk checks annotate, they never block queuing.
- **HTMX dashboard** — server-rendered partials, tight CSP, zero-JS SVG charts
  (equity curve, win/loss, per-session ladder), a P&L calendar, and a pending
  queue with bulk/auto approval.
- **Telegram remote control** — alerts + `/status /summary /pending /positions
  /kss /fullauto /pause /resume /freeze /reset`. See
  [docs/telegram-setup.md](docs/telegram-setup.md).
- **Live trading path** — wired through `ccxt` private endpoints, capped per
  order, breaker-gated, and **OFF by default**. See [docs/go-live.md](docs/go-live.md).

---

## Quick start

```bash
# 1. Virtual environment
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

# 2. Install runtime dependencies
pip install -r requirements-app.txt

# 3. (Optional) configure — all defaults work for the local demo
cp .env.example .env               # then edit if you want auth, Telegram, etc.

# 4. Run
uvicorn app.main:app --reload --port 8000
```

Open **http://localhost:8000** — create a KSS session with a $10,000 isolated
fund, click **Start**, and watch the waves build. Interactive API docs are at
`/docs`.

The database (`data/findmy.db`) and its five tables are created automatically on
first start.

---

## Tests & linting

```bash
pytest tests/app -c tests/app/pytest.ini    # isolated from the legacy suite
ruff check app tests/app
```

`tests/app/` is its own pytest rootdir (own `conftest.py` + throwaway SQLite DB),
so it never loads the legacy root config.

---

## Project layout

```
app/
  main.py        app factory + lifespan (create tables) + security + routers
  config.py      pydantic-settings (SecretStr for secrets) — all env knobs
  db.py          single SQLite engine, SessionLocal, get_db(), init_db()
  models.py      PendingOrder, Fill, Position, KssSession, KssWave (+ more)
  market.py      public prices + exchange info + TTL cache
  orders.py      queue -> approve/reject -> paper/live execute -> Fill/Position
  execution.py   paper + gated live order placement
  portfolio.py   read views: positions / trades / summary
  risk.py        pip sizing + position/daily-loss checks
  routes.py      JSON API + dashboard (HTMX partials) + /ws
  security.py    API-key dependency, security headers, CORS, rate limiting
  scanner.py     multi-agent scan pipeline (universe -> backtest -> vote -> act)
  scheduler.py   background scan/manage loop (off by default)
  circuit.py     circuit breaker (drawdown / daily-loss / consecutive-loss)
  guardian.py    optional LLM veto layer over auto-approvals
  notify.py      Telegram notifier + command poller
  charts.py      server-rendered SVG charts (CSP-safe, zero JS)
  agents/        deterministic quant agents + aggregator + ml/backtest agents
  kss/           pyramid.py (frozen math) · service.py (DB authority) · routes.py
  orchestrator/  OPUS mode (advanced, independent full-auto; paper, off)
  ta/            technical-analysis evidence bundle (3 tiers)
  templates/     dashboard.html + partials/
  static/        htmx.min.js, alpine.min.js (CSP build), app.js, style.css

docs/            documentation (see docs/README.md)
tests/app/       the v2 test suite
data/            SQLite database
scripts/         operational helpers (observation harness, replays)
```

> `src/findmy/` and `services/` are the **legacy v1** tree, kept for reference
> only. They are not used by `app/`.

---

## Configuration

All settings live in `app/config.py` and load from environment variables / a
local `.env`. **[`.env.example`](.env.example) is the canonical, fully-commented
reference** — copy it to `.env` and override only what you need. Highlights:

| Variable | Default | Purpose |
|----------|---------|---------|
| `REQUIRE_AUTH` | `false` | Enforce the `X-API-Key` header on write endpoints. |
| `API_KEY` | `dev-key` | Shared key for mutation endpoints when auth is on. |
| `DATABASE_URL` | `sqlite:///./data/findmy.db` | Single SQLite database. |
| `LIVE_EXCHANGE` / `DATA_EXCHANGE` | `kraken` | Public `ccxt` ids for prices / backtest history. |
| `MIN_WIN_RATE` / `MIN_CONFIDENCE` | `80` / `70` | Scanner gates a pair must clear to trade. |
| `AUTO_TRADE` | `false` | Full-auto: auto-approve qualifying KSS orders. |
| `SCHEDULER_ENABLED` | `false` | Run the background scan/manage loop. |
| `FULL_AUTO` | `false` | Master switch: scheduler + auto-trade + auto-approve together. |
| `LIVE_TRADING` | `false` | **Master switch for REAL-money orders** (paper everywhere when off). |
| `TELEGRAM_ENABLED` | `false` | Telegram alerts + command poller. |

Secrets use `SecretStr` and are never logged. **Never commit `.env`.**

---

## Safety model

- **Approval queue** — every order is inserted into `pending_orders` and only
  executes after `approve`. Nothing bypasses it.
- **Risk checks annotate, never block** — position-size and daily-loss checks
  attach a note; you keep final judgment at approval.
- **Circuit breaker** — freezes auto-approvals on excess drawdown, daily loss, or
  a consecutive-loss streak; auto-rearms after a cooldown.
- **Live trading is OFF by default** — even when armed, live BUYs are capped per
  order (`LIVE_MAX_ORDER_NOTIONAL`) and breaker-gated; SELL exits are never gated.
  See the [Go-Live runbook](docs/go-live.md).

---

## Documentation

| Document | What it covers |
|----------|----------------|
| [docs/REBUILD.md](docs/REBUILD.md) | v2 architecture, what changed from v1, and the agent/skill workflow. |
| [docs/kss.md](docs/kss.md) | KSS Pyramid DCA strategy: formulas, lifecycle, endpoints. |
| [docs/AGENTS.md](docs/AGENTS.md) | Multi-agent scanner + backtested auto-trading pipeline. |
| [docs/go-live.md](docs/go-live.md) | Operator runbook for arming real-money execution. |
| [docs/telegram-setup.md](docs/telegram-setup.md) | Telegram bot setup + command reference. |
| [docs/README.md](docs/README.md) | Full documentation index. |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Contribution workflow and standards. |
| [CHANGELOG.md](CHANGELOG.md) | Release history. |

---

## Disclaimer

This project is for **research and educational purposes only**. It is **not
financial advice**. The live trading path places real orders only when an
operator deliberately arms it; do not enable it without thorough testing and your
own risk management.

---

## License

MIT — see [LICENSE](LICENSE).
