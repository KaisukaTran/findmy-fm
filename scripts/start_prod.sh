#!/usr/bin/env bash
# Production startup: migrate then serve.
set -e
echo "[startup] Running database migrations..."
alembic upgrade head
echo "[startup] Starting API server..."
exec gunicorn src.findmy.api.main:app \
    -k uvicorn.workers.UvicornWorker \
    -w "${WORKERS:-4}" \
    -b "0.0.0.0:${PORT:-8000}" \
    --timeout 60 \
    --access-logfile - \
    --error-logfile -
