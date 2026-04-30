#!/usr/bin/env bash
# Production startup: validate env → migrate → serve.
set -e

echo "[startup] Checking environment..."

# Fail fast if critical env vars are missing
if [ -z "$APP_SECRET_KEY" ]; then
    echo "ERROR: APP_SECRET_KEY is not set. Refusing to start." >&2
    exit 1
fi

if [ "${#APP_SECRET_KEY}" -lt 32 ]; then
    echo "ERROR: APP_SECRET_KEY must be at least 32 characters." >&2
    exit 1
fi

mkdir -p /app/data /app/data/uploads

echo "[startup] Running database migrations..."
alembic upgrade head

echo "[startup] Starting API server (workers=${WORKERS:-4})..."
exec gunicorn src.findmy.api.main:app \
    -k uvicorn.workers.UvicornWorker \
    -w "${WORKERS:-4}" \
    -b "0.0.0.0:${PORT:-8000}" \
    --timeout 60 \
    --access-logfile - \
    --error-logfile -
