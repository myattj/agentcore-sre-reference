#!/usr/bin/env bash
# Thin launcher for scripts/smoke.py that runs it inside the bridge venv
# (which has httpx + boto3 + the bridge package on its path).
set -euo pipefail

REPO_ROOT="$( cd "$( dirname "${BASH_SOURCE[0]}" )/.." && pwd )"
PYTHON="$REPO_ROOT/bridge/.venv/bin/python"

if [[ ! -x "$PYTHON" ]]; then
  echo "error: $PYTHON not found." >&2
  echo "       run \`cd bridge && uv sync\` first." >&2
  exit 1
fi

exec "$PYTHON" "$REPO_ROOT/scripts/smoke.py" "$@"
