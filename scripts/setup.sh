#!/usr/bin/env bash
set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
ENV_ONLY=0

usage() {
  cat <<'EOF'
Usage: scripts/setup.sh [--env-only]

Creates safe local env files, then installs every dependency represented by a
checked-in uv.lock or package-lock.json. It does not contact AWS or Slack and
does not deploy anything.

Options:
  --env-only  Create/verify local env files without installing dependencies.
  -h, --help  Show this help.
EOF
}

case "${1:-}" in
  "") ;;
  --env-only) ENV_ONLY=1 ;;
  -h|--help) usage; exit 0 ;;
  *) printf 'setup: unknown argument: %s\n' "$1" >&2; usage >&2; exit 2 ;;
esac
if [ "$#" -gt 1 ]; then
  printf 'setup: too many arguments\n' >&2
  usage >&2
  exit 2
fi

find_python() {
  if command -v python3.13 >/dev/null 2>&1; then
    command -v python3.13
  elif command -v python3 >/dev/null 2>&1 && python3 -c 'import sys; raise SystemExit(sys.version_info[:2] != (3, 13))'; then
    command -v python3
  else
    return 1
  fi
}

PYTHON_BIN=$(find_python || true)
if [ -z "$PYTHON_BIN" ]; then
  printf 'setup: Python 3.13 is required. Run make doctor for an exact fix.\n' >&2
  exit 1
fi

printf 'Configuring local-only environment files...\n'
"$PYTHON_BIN" "$ROOT_DIR/scripts/bootstrap_local_env.py" --root "$ROOT_DIR"

if [ "$ENV_ONLY" -eq 1 ]; then
  exit 0
fi

"$ROOT_DIR/scripts/doctor.sh"

sync_uv() {
  label=$1
  directory=$2
  extra=$3
  printf '\nInstalling %s (frozen uv lock)...\n' "$label"
  (cd "$ROOT_DIR/$directory" && uv sync --frozen --extra "$extra")
}

sync_npm() {
  label=$1
  directory=$2
  printf '\nInstalling %s (npm clean install)...\n' "$label"
  (cd "$ROOT_DIR/$directory" && npm ci --no-audit --no-fund)
}

sync_uv "bridge" "bridge" "dev"
sync_uv "core agent" "coreAgent/app/coreAgent" "test"
sync_uv "Gateway interceptor" "workers/gateway_interceptor" "dev"
sync_uv "pull-request sandbox" "infra/sandbox" "test"
sync_npm "onboarding UI" "onboarding"
sync_npm "hand-authored CDK" "infra/data"
sync_npm "generated AgentCore CDK" "coreAgent/agentcore/cdk"

printf '\nSetup complete. No AWS or Slack API was contacted.\n'
printf 'Next: make demo  # local bridge + web UI + synthetic incident dashboard\n'
