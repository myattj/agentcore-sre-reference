# Phase B sandbox container.
#
# Built by infra/data/lib/sandbox-stack.ts via DockerImageAsset (which
# auto-creates an ECR repo and pushes on `cdk deploy`). The container
# is a one-shot Fargate task: ECS spawns it via run_task with a
# TASK_ID env override, the entrypoint reads the matching row from
# the sandbox_jobs DDB table, opens a PR, writes the result back, and
# exits.
#
# First slice ships a DUMMY entrypoint (`add a line to README, open
# a test PR titled "AgentCore Reference: test PR (<task_id>) — please ignore"`).
# v2 swaps in a real Claude Agent SDK loop. The Dockerfile here is
# deliberately minimal — only what the dummy needs — so v2 can layer
# in `anthropic`, `uv`, `node`, etc. without disturbing the
# proven plumbing.
#
# Why python:3.13-slim and not 3.12: matches the agent + bridge venvs
# (CLAUDE.md gotcha #7). Don't drift the Python minor versions across
# the platform — Strands' SDK pins 3.13.

FROM python:3.13-slim

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

# Install Python deps in their own layer so changes to entrypoint.py
# don't bust the deps cache.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# scm_github.py is a MIRROR of coreAgent/app/coreAgent/scm_github.py
# (gotcha #13: bridge/agent/sandbox each have their own venv, can't
# cross-import). The image is rebuilt by `cdk deploy` whenever the
# file changes, so the mirror stays in sync as long as the source is
# updated and a deploy is run.
COPY scm_github.py .
COPY entrypoint.py .

# Run as non-root for general hygiene. The container does git +
# subprocess work in /tmp which is world-writable; nothing in /app
# needs to be written to.
RUN useradd --create-home --shell /bin/bash sandbox \
    && chown -R sandbox:sandbox /app
USER sandbox

CMD ["python", "-u", "entrypoint.py"]
