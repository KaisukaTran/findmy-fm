#!/usr/bin/env bash
# Run Alembic migrations to head.
# Usage:
#   ./scripts/migrate.sh              # upgrade to head
#   ./scripts/migrate.sh downgrade -1 # rollback one step
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR/.."

mkdir -p data

ACTION="${1:-upgrade}"
TARGET="${2:-head}"

alembic "$ACTION" "$TARGET"
