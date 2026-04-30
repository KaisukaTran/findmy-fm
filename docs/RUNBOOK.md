# FindMy-FM Operations Runbook

## Pre-Flight Checklist (Before Go-Live)

Run `python scripts/preflight_check.py` and confirm all checks pass.

### Manual verification steps

1. **Environment variables**
   - `APP_SECRET_KEY` — at least 32 chars, not default value
   - `DATABASE_URL` — points to production SQLite/Postgres file
   - `SOT_DATABASE_URL` — same as DATABASE_URL or separate SOT db
   - `BINANCE_API_KEY` / `BINANCE_API_SECRET` — set for live mode; leave blank for paper
   - `LIVE_TRADING_DRY_RUN` — set to `false` only when ready for real orders

2. **Database**
   ```bash
   alembic current          # should show: 0011 (head)
   alembic upgrade head     # apply any pending migrations
   ```

3. **Admin user**
   ```bash
   python scripts/seed_admin.py   # creates admin if missing
   ```
   Change the default password immediately after first login.

4. **Config sanity**
   ```bash
   python -c "from src.findmy.config import settings; print(settings.json(indent=2))"
   ```
   Verify: `max_position_size_pct`, `max_daily_loss_pct`, `max_orders_per_minute`.

---

## Smoke Tests (Post-Deploy)

Run after every deployment before routing real traffic.

```bash
# Health check
curl -sf http://localhost:8000/health | python -m json.tool

# Auth
TOKEN=$(curl -sf -X POST http://localhost:8000/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"<pwd>"}' | python -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

# Pending orders list
curl -sf http://localhost:8000/api/pending -H "Authorization: Bearer $TOKEN"

# Circuit breaker status
curl -sf http://localhost:8000/api/circuit-breaker/status -H "Authorization: Bearer $TOKEN"

# Summary
curl -sf http://localhost:8000/api/summary -H "Authorization: Bearer $TOKEN"
```

All calls should return HTTP 200. `health` status should be `ok` or `degraded` (not `unhealthy`).

---

## Startup

```bash
# Development / single-worker
uvicorn findmy.api.main:app --host 0.0.0.0 --port 8000 --reload

# Production (multi-worker, DB-backed halt flag)
bash scripts/start_prod.sh
# or directly:
gunicorn findmy.api.main:app \
  -w 4 -k uvicorn.workers.UvicornWorker \
  --bind 0.0.0.0:8000 \
  --access-logfile - --error-logfile -
```

---

## Emergency Halt

**Stop all order execution immediately:**

```bash
curl -X POST http://localhost:8000/api/emergency/stop \
  -H "Authorization: Bearer $ADMIN_TOKEN"
```

This sets `halt=true` in the `system_state` DB table, visible to all gunicorn workers.

**Resume after investigation:**

```bash
curl -X POST http://localhost:8000/api/emergency/resume \
  -H "Authorization: Bearer $ADMIN_TOKEN"
```

**Verify halt state:**

```bash
curl http://localhost:8000/api/system/status
```

---

## Rollback Procedure

### Application rollback

```bash
# Roll back to previous Docker image
docker stop findmy-fm
docker run -d --name findmy-fm-rollback \
  --env-file .env \
  -v $(pwd)/data:/app/data \
  -p 8000:8000 \
  findmy-fm:<previous-tag>
```

### Database rollback

```bash
# Downgrade one revision
alembic downgrade -1

# Downgrade to specific revision
alembic downgrade 0010
```

> **Note:** Revision 0011 (`downgrade` is a no-op). Downgrading past it will not restore the CHECK constraints; data is preserved.

### Config rollback

Restore `.env` from backup. Restart the service. Re-run smoke tests.

---

## Monitoring

- **Prometheus metrics:** `http://localhost:8000/metrics`
- **Alert rules:** `monitoring/alerts.yml`
- **Key alerts:**
  - `EmergencyHaltActivated` — halt flag is on
  - `HighUnrealizedLoss` — position drawdown > 5%
  - `PendingOrdersBacklog` — > 50 pending orders
  - `CircuitBreakerTripping` — repeated circuit breaker violations

---

## Common Issues

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| `CHECK constraint failed: status` | Migrating from old schema | Run `alembic upgrade head` (applies 0011) |
| `no such table: system_state` | Alembic not run | `alembic upgrade head` |
| Orders stuck in PENDING | Halt active | Check `/api/system/status`, resume if safe |
| `ModuleNotFoundError: findmy` | Wrong PYTHONPATH | Add `src/` to path or install package |
| 401 on all API calls | Expired/wrong SECRET_KEY | Ensure `APP_SECRET_KEY` matches key used to sign tokens |
