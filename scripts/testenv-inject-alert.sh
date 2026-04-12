#!/usr/bin/env bash
# Thin launcher for scripts/testenv/inject_alert.py — same venv as the
# bootstrap launcher. See that script for why.
set -euo pipefail

REPO_ROOT="$( cd "$( dirname "${BASH_SOURCE[0]}" )/.." && pwd )"
PYTHON="$REPO_ROOT/bridge/.venv/bin/python"

if [[ ! -x "$PYTHON" ]]; then
  echo "error: $PYTHON not found." >&2
  echo "       run \`cd bridge && uv sync\` first." >&2
  exit 1
fi

cd "$REPO_ROOT"
exec "$PYTHON" -m scripts.testenv.inject_alert "$@"
