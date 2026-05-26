# syntax=docker/dockerfile:1.7

# ---- Builder ----
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build
COPY drift-agent/pyproject.toml ./
# --ignore-installed forces install into /install even when the package is
# already satisfied by the base image's system Python (e.g. `packaging` is
# bundled with pip itself, so a vanilla `pip install --prefix=/install
# packaging` silently no-ops and the runtime stage never gets the files
# when it copies /install → /usr/local).
RUN pip install --upgrade pip wheel \
 && pip install --prefix=/install --ignore-installed \
        "fastapi>=0.115.0" \
        "uvicorn[standard]>=0.32.0" \
        "litellm>=1.63.0" \
        "packaging>=24.0" \
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
        "cryptography>=43.0" \
        "bcrypt>=4.2"

# ---- Runtime ----
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

# docker CLI + compose plugin — used by the admin/updates surface to
# `docker compose pull && up -d` drift-agent and drift-frontend from
# inside this container. Talks to the host's daemon over a mounted
# /var/run/docker.sock (see docker-compose.yml). 'app' user is added to
# a group whose gid matches the host's docker.sock group at runtime
# via entrypoint.sh; we don't bake a fixed gid here because it varies
# across host distros.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl gnupg \
 && install -m 0755 -d /etc/apt/keyrings \
 && curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg \
 && chmod a+r /etc/apt/keyrings/docker.gpg \
 && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian $(. /etc/os-release && echo \"$VERSION_CODENAME\") stable" > /etc/apt/sources.list.d/docker.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends docker-ce-cli docker-compose-plugin \
 && apt-get clean && rm -rf /var/lib/apt/lists/*

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
# Default-apps seeded on first startup (see app/deploy/seed.py). Each
# subdir under default-apps/ becomes an App row + initial revision in
# the new install's DB, with the directory's contents as the bundle.
COPY drift-agent/default-apps /opt/drift-defaults/apps
# Pre-create the bundle storage path with `app:app` ownership so an
# empty named volume mounted here inherits writable perms on first up
# (docker copies the image directory's ownership into a fresh volume).
RUN mkdir -p /var/lib/drift/bundles && chown -R app:app /var/lib/drift
RUN chmod +x entrypoint.sh

USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/healthz', timeout=2)" || exit 1

# entrypoint.sh runs `alembic upgrade head` (best-effort — skipped if
# DRIFT_PG_URL is unset) then execs uvicorn.
CMD ["./entrypoint.sh"]
