#!/usr/bin/env bash
# Thin launcher for scripts/testenv/bootstrap.py that runs it inside
# the bridge venv (where httpx + boto3 + slack-sdk live alongside the
# bridge package so `from bridge.slack_oauth import make_session_token`
# works for minting PATCH session tokens).
#
# See scripts/testenv/README.md for the one-time manual steps
# (Slack install, channels, GitHub App).
set -euo pipefail

REPO_ROOT="$( cd "$( dirname "${BASH_SOURCE[0]}" )/.." && pwd )"
PYTHON="$REPO_ROOT/bridge/.venv/bin/python"

if [[ ! -x "$PYTHON" ]]; then
  echo "error: $PYTHON not found." >&2
  echo "       run \`cd bridge && uv sync\` first." >&2
  exit 1
fi

cd "$REPO_ROOT"
exec "$PYTHON" -m scripts.testenv.bootstrap "$@"
