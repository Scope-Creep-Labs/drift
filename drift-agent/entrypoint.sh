#!/usr/bin/env sh
set -e

# Run migrations only when a Postgres URL is configured. Existing
# observability/alert tools work fine without the deploy schema.
if [ -n "${DRIFT_PG_URL:-}" ]; then
  echo "[entrypoint] running alembic upgrade head against ${DRIFT_PG_URL%@*}@..."
  alembic upgrade head
else
  echo "[entrypoint] DRIFT_PG_URL not set; skipping migrations"
fi

exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --no-access-log
