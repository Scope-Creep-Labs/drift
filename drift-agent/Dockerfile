# syntax=docker/dockerfile:1.7

# ---- Builder ----
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build
COPY pyproject.toml ./
RUN pip install --upgrade pip wheel \
 && pip install --prefix=/install \
        "fastapi>=0.115.0" \
        "uvicorn[standard]>=0.32.0" \
        "anthropic>=0.40.0" \
        "httpx>=0.27.0" \
        "pydantic>=2.9.0" \
        "pydantic-settings>=2.6.0" \
        "numpy>=2.1.0" \
        "scipy>=1.14.0"

# ---- Runtime ----
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

# Non-root user
RUN groupadd --system app && useradd --system --gid app --create-home app

WORKDIR /app

COPY --from=builder /install /usr/local
COPY app ./app

USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/healthz', timeout=2)" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
