# FINDMY-FM — Telegram Status & Control Plan

Created: 2026-06-13 · Builds on the existing `app/notify.py` (sender + command poller).

---

## Spec delta (product contract)

Target: `docs/spec` (no root spec.md in repo) — recorded here as the Telegram surface contract.

The Telegram bot is the operator's remote eyes + hands. It must, for the **single authorised
chat only** (the existing `telegram_chat_id` auth boundary):

1. **Overview** — equity, cash, market value, realized + unrealized P&L (USDT).
2. **Open state** — pending orders, open positions, active KSS sessions.
3. **Push important events** — trades (fills), risk (SL/trailing/breaker/guardian/loss-streak),
   and important audit-log entries. Not every scan — only "important", with a kill switch.
4. **Full-Auto on/off** — toggle full-auto from chat.

Already shipped (reuse, do not rebuild): `notify.send()`, the poller with the chat-id auth
boundary, `/status /pause /resume /freeze /reset /help`, the `TELEGRAM` toggle + "Thử" button,
and auto-push for **breaker FROZEN** (`circuit.py`) and **guardian veto** (`scheduler.py`).

**Req #4 is effectively DONE today**: `/resume` enables full-auto + scheduler, `/pause` disables
both. This plan only adds a clearer `/fullauto on|off` alias + inline buttons.

## Validation (non-trivial gate)

- `team_validation_mode: manual-pass` — subagents not used (per session policy; evaluated the
  5 perspectives inline). **Product**: maps 1:1 to the 4 requirements; reuses existing surface.
  **Architecture**: all additions live in `app/notify.py` (read commands + formatters) and a
  thin push hook from `orders`/`audit`; no new dependency, no new framework. **Security**: the
  chat-id auth boundary already gates commands; pushes carry only the operator's own portfolio
  data; the bot token stays `SecretStr`, never logged (keep that invariant). **QA**: command
  formatters + push triggers are pure/mockable — `notify.send` is monkeypatched in tests (no
  network). **Skeptic**: main risk is alert spam → gated by `telegram_notify_*` flags + a
  digest cadence, default conservative.
- Wheel-reinvention check: no new lib — reuse httpx + the poller. Confirmed against
  `[[full-auto-master-switch]]` and the existing notify tests (`tests/app/test_notify.py`).
- Lint/format baseline: ruff is configured (`pyproject.toml`); tests run via `tests/app`. No
  setup task needed.
- Gates in DoD: unit tests (mock `notify.send`) + ruff green; offline-safe (send() returns
  False when disabled, so tests never touch Telegram).

---

## Phase 1: Read commands — overview + open state (req #1, #2)  [tdd:required]

| Task | Content | DoD | Depends | Status |
|------|---------|-----|---------|--------|
| 1.1 | Add `/summary` (and fold the key figures into `/status`): equity, cash, market value, realized + unrealized P&L from `portfolio.summary_view`. | `handle_command("/summary")` returns lines containing equity + both P&L figures; unit test on a seeded DB | - | cc:TODO |
| 1.2 | Add `/pending`, `/positions`, `/kss` read commands — each formats ≤15 rows (symbol/side/qty/price/PnL) or "none". | each returns the rows for a fixture and "none" when empty; unit test | 1.1 | cc:TODO |
| 1.3 | Add `/fullauto on\|off` alias → `full_auto_on`/`full_auto_off` + scheduler; refresh `/help`. | `/fullauto on` sets full_auto True, `/fullauto off` False; unit test | - | cc:TODO |

## Phase 2: Event push — trades / risk / important logs (req #3)  [tdd:required]

| Task | Content | DoD | Depends | Status |
|------|---------|-----|---------|--------|
| 2.1 | Push on fill: after `orders.approve_order` records a Fill, send a one-line alert (side/qty/symbol/price/realized) gated by new `telegram_notify_trades` (default true). | a BUY+SELL fill each call `notify.send` (mocked) with the symbol; unit test | - | cc:TODO |
| 2.2 | Push on risk events: route `audit.log` entries of category **risk** (SL/trailing/loss-streak/veto) to `notify.send`, gated by `telegram_notify_risk` (default true). Breaker-freeze + guardian-veto already push — dedupe. | a risk audit event triggers one push; no double-send for breaker; unit test | - | cc:TODO |
| 2.3 | Severity/throttle: only trades + risk + breaker push; never per-scan. A simple per-symbol cooldown so a chatty DCA wave can't flood. | a burst of N fills in M seconds yields ≤K pushes; unit test on the throttle | 2.1, 2.2 | cc:TODO |

## Phase 3: Periodic digest (req #1, optional cadence)  [tdd:required]

| Task | Content | DoD | Depends | Status |
|------|---------|-----|---------|--------|
| 3.1 | Digest builder: equity + today's realized P&L + open-position/session counts; pushed every `telegram_digest_hours` (0 = off, default 0). Driven from the scheduler tick. | `build_digest(db)` returns text with equity + day P&L; scheduler calls it only on cadence; unit test on the builder | 1.1 | cc:TODO |

## Phase 4: UX + docs + gate  [tdd:skip:ui-docs]

| Task | Content | DoD | Depends | Status |
|------|---------|-----|---------|--------|
| 4.1 | Inline-keyboard buttons on `/status` for quick actions (Full-Auto on/off, freeze/reset) — optional polish. | buttons post the matching command; manual check | 1.3 | cc:TODO |
| 4.2 | `docs/telegram-setup.md` runbook: BotFather → token, get chat_id, `.env`, restart, verify. | runbook committed | - | cc:DONE (this session) |
| 4.3 | Final gate: tests + ruff green; offline-safe (no real Telegram in tests); manual `/status` + "Thử" check. | all gates green | all | cc:TODO |

---

## New config (all default-safe)
- `telegram_notify_trades: bool = true` — push on each fill.
- `telegram_notify_risk: bool = true` — push on risk-category audit events.
- `telegram_digest_hours: int = 0` — periodic digest cadence; 0 = off.

## Out of scope
- Multi-user / multiple chat ids (single authorised chat only — keeps the auth boundary simple).
- Rich charts in Telegram (text only; the dashboard owns charts).
