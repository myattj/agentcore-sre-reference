#!/usr/bin/env bash
# Thin launcher for scripts/dev_up.py that runs it inside the bridge venv
# (which has httpx and the bridge package on its path).
set -euo pipefail

REPO_ROOT="$( cd "$( dirname "${BASH_SOURCE[0]}" )/.." && pwd )"
PYTHON="$REPO_ROOT/bridge/.venv/bin/python"

if [[ ! -x "$PYTHON" ]]; then
  echo "error: $PYTHON not found." >&2
  echo "       run \`cd bridge && uv sync\` first." >&2
  exit 1
fi

# PYTHONUNBUFFERED so the script's stdout is visible immediately when
# launched from a non-TTY shell (e.g. CI / wrapped subprocess). On a real
# terminal, Python auto-line-buffers, so this is a no-op there.
exec env PYTHONUNBUFFERED=1 "$PYTHON" "$REPO_ROOT/scripts/dev_up.py" "$@"
