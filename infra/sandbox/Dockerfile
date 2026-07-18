# PR sandbox container with a Claude agent loop.
#
# Built by infra/data/lib/sandbox-stack.ts via DockerImageAsset (which
# auto-creates an ECR repo and pushes on `cdk deploy`). The container
# is a one-shot Fargate task: ECS spawns it via run_task with a
# TASK_ID env override, the entrypoint reads the matching row from
# the sandbox_jobs DDB table, runs a Claude agent loop to make code
# changes, opens a PR, writes the result back, and exits.
#
# The Claude agent (agent.py) uses the Anthropic API directly (not
# Bedrock) for prompt caching support. ANTHROPIC_API_KEY is injected
# from Secrets Manager at container start.
#
# Why python:3.13-slim and not 3.12: matches the agent + bridge venvs
# (CLAUDE.md gotcha #7). Don't drift the Python minor versions across
# the platform — Strands' SDK pins 3.13.

FROM python:3.13-slim

# Keep dependency resolution identical between local tests and the image.
# Pin the installer itself and tell uv to use the Python already in the base
# image rather than downloading another interpreter.
COPY --from=ghcr.io/astral-sh/uv:0.11.24 /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

# git for clone/branch/commit/push.
# ca-certificates for HTTPS to api.github.com, dynamodb, secretsmanager.
# Everything else stays out of the image to keep it small and the
# blast radius small (no `sudo`, no `curl`, no `apt-get` at runtime).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        git \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install runtime-only Python dependencies in their own layer so changes to
# worker code do not invalidate the dependency cache. `--frozen` makes the
# checked-in lock authoritative; the pytest extra is intentionally omitted.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
ENV PATH="/app/.venv/bin:${PATH}"

# scm_github.py is a MIRROR of coreAgent/app/coreAgent/scm_github.py
# (gotcha #13: bridge/agent/sandbox each have their own venv, can't
# cross-import). The image is rebuilt by `cdk deploy` whenever the
# file changes, so the mirror stays in sync as long as the source is
# updated and a deploy is run.
COPY scm_github.py .
COPY agent.py .
COPY entrypoint.py .

# Run as non-root for general hygiene. The container does git +
# subprocess work in /tmp which is world-writable; nothing in /app
# needs to be written to.
RUN useradd --create-home --shell /bin/bash sandbox \
    && chown -R sandbox:sandbox /app
USER sandbox

CMD ["python", "-u", "entrypoint.py"]
