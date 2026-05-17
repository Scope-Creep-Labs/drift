# syntax=docker/dockerfile:1.7

# ---- Builder ----
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build
COPY drift-agent/pyproject.toml ./
RUN pip install --upgrade pip wheel \
 && pip install --prefix=/install \
        "fastapi>=0.115.0" \
        "uvicorn[standard]>=0.32.0" \
        "anthropic>=0.40.0" \
        "httpx>=0.27.0" \
        "pydantic>=2.9.0" \
        "pydantic-settings>=2.6.0" \
        "numpy>=2.1.0" \
        "scipy>=1.14.0" \
        "pyyaml>=6.0" \
        "sqlalchemy[asyncio]>=2.0.30" \
        "asyncpg>=0.29" \
        "alembic>=1.13" \
        "boto3>=1.34" \
        "prometheus-client>=0.20" \
        "cryptography>=43.0"

# ---- Runtime ----
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

# Non-root user
RUN groupadd --system app && useradd --system --gid app --create-home app

WORKDIR /app

COPY --from=builder /install /usr/local
COPY drift-agent/app ./app
COPY drift-agent/alembic.ini ./
COPY drift-agent/alembic ./alembic
COPY drift-agent/entrypoint.sh ./
# Edge-agent scripts are served from /api/deploy/agent/* so a fresh box
# can curl-bootstrap. Lives at the repo top-level under edge-agent/ and
# is copied in here at build time.
COPY edge-agent /opt/edge-agent
RUN chmod +x entrypoint.sh

USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/healthz', timeout=2)" || exit 1

# entrypoint.sh runs `alembic upgrade head` (best-effort — skipped if
# DRIFT_PG_URL is unset) then execs uvicorn.
CMD ["./entrypoint.sh"]
