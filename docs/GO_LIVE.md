# Go-Live Checklist

Status as of latest commit on `claude/review-progress-todos-X5uh0`.

## Code state
- 38/38 integration tests passing
- 12 Alembic migrations (latest: `0012_ai_agent_tables`)
- AI agent infrastructure complete (Phases A–E)
- Claude Design theme applied to dashboard

---

## P0 — Must-do before production

| # | Task | Where | Notes |
|---|------|-------|-------|
| 1 | Set `APP_SECRET_KEY` to a strong random 32+ char string | `.env` | run `python -c "import secrets; print(secrets.token_urlsafe(48))"` |
| 2 | Set `ANTHROPIC_API_KEY` from console.anthropic.com | `.env` | required for AI agent to start |
| 3 | Configure exchange credentials | `.env` | `BROKER_API_KEY`, `BROKER_API_SECRET` (Binance) |
| 4 | Run `python scripts/preflight_check.py` | shell | must report all green |
| 5 | Apply migrations: `alembic upgrade head` | shell | brings DB to 0012 |
| 6 | Seed an admin user: `python scripts/seed_admin.py` | shell | change default password immediately after first login |
| 7 | Remove demo users (`trader1`, `trader2`) from DB | SQL | preflight script warns about these |
| 8 | Verify TLS termination (nginx/Cloudflare) | infra | API must not be reachable over plain HTTP |
| 9 | Set up backup of `data/findmy_fm_paper.db` | cron/infra | daily snapshot to off-host storage |
| 10 | Run integration tests in staging: `pytest tests/integration/ --no-cov` | CI | all 38 must pass |

## P0 — AI agent specific (paper mode validation)

| # | Task | Notes |
|---|------|-------|
| 11 | Set `LIVE_TRADING=false` (default) and `LIVE_TRADING_DRY_RUN=true` | paper mode + Binance testnet |
| 12 | Start AI agent in paper mode via dashboard | "Start AI Agent" button on AI tab |
| 13 | Run for ≥ 7 days (`AI_PAPER_MIN_DAYS`) | watch decision log for sane reasoning |
| 14 | Confirm no error spam in `/api/ai/decisions` | no `ERROR` actions repeating on the same symbol |
| 15 | Verify `/api/ai/status` shows reasonable `today.spent_usdt` | within `AI_MAX_SPEND_USDT × 10` daily cap |
| 16 | Add at least one consultant agent (`technical` or `llm`) | reduces single-model risk |
| 17 | Use `/api/ai/promote-to-live` to flip `mode=live` | requires paper-day gate to pass |
| 18 | Then set `LIVE_TRADING=true` env var and restart server | activates real exchange execution |

## P1 — Recommended before scaling

| # | Task | Why |
|---|------|-----|
| 19 | Wire trade close events into `paper_report.estimated_win_rate` | currently `None`, eligibility check passes prematurely |
| 20 | Add Prometheus alert for AI errors > N/min | catch silent loop crashes |
| 21 | Tune `AI_LOOP_INTERVAL_SECONDS` based on cost/latency | default 300s ≈ $X/day at current Claude pricing |
| 22 | Set `AI_WATCHLIST` env var with your preferred symbols | default uses top-5 liquid pairs |
| 23 | Adjust `AI_MAX_SPEND_USDT` and `AI_DAILY_TARGET_PCT` | match your account size and risk |
| 24 | Add Sentry / error reporting for unhandled exceptions | surface prod issues fast |
| 25 | Configure log shipping (`/var/log/findmy/*.log` → ELK/Loki) | structured JSON logs already enabled |

## P2 — Quality of life

- [ ] Replace `confirm()`/`prompt()` dialogs in dashboard with proper modals
- [ ] Add CSRF token to mutating fetch calls
- [ ] Migrate FastAPI `on_event` → `lifespan` (deprecation warnings)
- [ ] Build a "manual signal injection" endpoint for testing AI consensus without paying for Claude calls
- [ ] Auto-stop AI agent when emergency halt is triggered (currently it just skips iterations)
- [ ] Trade-close PnL tracker → realistic win-rate / drawdown for promotion gate

---

## Rollback path

If anything goes wrong post-promotion:

```bash
# 1. Emergency halt: stops all order approval/execution
curl -X POST http://prod/api/emergency-stop -H "Authorization: Bearer $ADMIN_TOKEN"

# 2. Stop AI agent loop
curl -X POST http://prod/api/ai/stop -H "Authorization: Bearer $ADMIN_TOKEN"

# 3. Revert mode to paper
sqlite3 data/findmy_fm_paper.db "UPDATE ai_agent_state SET value='paper' WHERE key='mode'"

# 4. If a bad migration is suspected
alembic downgrade -1

# 5. Resume after fix
curl -X POST http://prod/api/emergency-resume -H "Authorization: Bearer $ADMIN_TOKEN"
```

---

## Known limitations

1. **Win-rate tracking is a stub.** `paper_report.estimated_win_rate` returns `None` until trade-close events are wired (requires `services/ts/` to emit close events into `ai_decision_log`).
2. **`promote_to_live` only flips DB mode**, not the global `LIVE_TRADING` env var. You still need to set `LIVE_TRADING=true` and restart for real exchange calls.
3. **Single-tenant only.** No per-user fund isolation; AI shares the system equity.
4. **No order amendment / cancellation.** Once submitted, an order runs to fill or expiry.
5. **Watchlist is static** (env var or default 5 symbols). No dynamic discovery.
