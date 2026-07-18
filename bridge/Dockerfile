# Multi-stage build for the FastAPI bridge service.
# Build context: bridge/ (not repo root).

# ---- Stage 1: install dependencies with uv ----
FROM python:3.13-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.11.24 /uv /usr/local/bin/uv

WORKDIR /app

# Copy dependency files first for layer caching
COPY pyproject.toml uv.lock ./

# Install production dependencies only (no dev/test deps).
# --frozen: use lockfile exactly, no resolution.
# --no-dev: skip pytest, pytest-asyncio, etc.
# --no-install-project: don't try to install the bridge package itself
#   (source isn't copied yet; we just need the deps in .venv).
RUN uv sync --frozen --no-dev --no-install-project

# Copy source
COPY bridge/ ./bridge/

# ---- Stage 2: slim runtime image ----
FROM python:3.13-slim

RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --home-dir /app --create-home \
      --shell /usr/sbin/nologin app

WORKDIR /app

COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --from=builder --chown=app:app /app/bridge /app/bridge

ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8000

USER 10001:10001

# Two workers: enough for concurrent Slack webhook + OAuth without
# over-provisioning the 0.25 vCPU Fargate task.
CMD ["uvicorn", "bridge.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
