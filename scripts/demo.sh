#!/usr/bin/env bash
set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
BRIDGE_PORT=${BRIDGE_PORT:-8000}
WEB_PORT=${WEB_PORT:-3000}
CHECK_ONLY=0
OPEN_BROWSER=1
TEMP_DIR=""
BRIDGE_PID=""
WEB_PID=""
TOKEN="51c17c51c17c51c17c51c17c51c17c51"

usage() {
  cat <<'EOF'
Usage: scripts/demo.sh [options]

Starts a completely local Agent demo: FastAPI bridge + Next.js UI + a
realistic synthetic incident dashboard. No AgentCore CLI, AWS credentials,
Bedrock model, Slack app, or Docker daemon is needed.

Options:
  --bridge-port PORT  Bridge port (default: 8000; env: BRIDGE_PORT)
  --web-port PORT     Web port (default: 3000; env: WEB_PORT)
  --no-open           Do not open a browser automatically
  --check             Verify both services and exit cleanly (for CI/tests)
  -h, --help          Show this help
EOF
}

is_port() {
  case "$1" in
    ''|*[!0-9]*) return 1 ;;
    *) [ "$1" -ge 1 ] 2>/dev/null && [ "$1" -le 65535 ] 2>/dev/null ;;
  esac
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --bridge-port)
      [ "$#" -ge 2 ] || { printf 'demo: --bridge-port needs a value\n' >&2; exit 2; }
      BRIDGE_PORT=$2
      shift 2
      ;;
    --web-port)
      [ "$#" -ge 2 ] || { printf 'demo: --web-port needs a value\n' >&2; exit 2; }
      WEB_PORT=$2
      shift 2
      ;;
    --no-open) OPEN_BROWSER=0; shift ;;
    --check) CHECK_ONLY=1; OPEN_BROWSER=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) printf 'demo: unknown argument: %s\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
done

if ! is_port "$BRIDGE_PORT" || ! is_port "$WEB_PORT"; then
  printf 'demo: ports must be integers from 1 through 65535\n' >&2
  exit 2
fi
if [ "$BRIDGE_PORT" = "$WEB_PORT" ]; then
  printf 'demo: bridge and web ports must be different\n' >&2
  exit 2
fi

PYTHON_BIN=""
if command -v python3.13 >/dev/null 2>&1; then
  PYTHON_BIN=$(command -v python3.13)
elif command -v python3 >/dev/null 2>&1 && python3 -c 'import sys; raise SystemExit(sys.version_info[:2] != (3, 13))'; then
  PYTHON_BIN=$(command -v python3)
fi
if [ -z "$PYTHON_BIN" ]; then
  printf 'demo: Python 3.13 is required; run make doctor.\n' >&2
  exit 1
fi
if [ ! -x "$ROOT_DIR/bridge/.venv/bin/uvicorn" ] || [ ! -x "$ROOT_DIR/onboarding/node_modules/.bin/next" ]; then
  printf 'demo: dependencies are not installed; run make setup first.\n' >&2
  exit 1
fi

TEMP_DIR=$(mktemp -d "${TMPDIR:-/tmp}/agent-demo.XXXXXX")

cleanup() {
  trap - EXIT INT TERM
  if [ -n "$WEB_PID" ]; then
    kill "$WEB_PID" 2>/dev/null || true
  fi
  if [ -n "$BRIDGE_PID" ]; then
    kill "$BRIDGE_PID" 2>/dev/null || true
  fi
  if [ -n "$WEB_PID" ]; then
    wait "$WEB_PID" 2>/dev/null || true
  fi
  if [ -n "$BRIDGE_PID" ]; then
    wait "$BRIDGE_PID" 2>/dev/null || true
  fi
  if [ -n "$TEMP_DIR" ] && [ -d "$TEMP_DIR" ]; then
    rm -r -- "$TEMP_DIR"
  fi
}
trap cleanup EXIT
trap 'exit 0' INT TERM

"$PYTHON_BIN" "$ROOT_DIR/scripts/create_demo_dashboard.py" \
  --source "$ROOT_DIR/examples/dashboard.sample.json" \
  --output-dir "$TEMP_DIR/dashboards" \
  --token "$TOKEN" >/dev/null

printf 'Starting local bridge on http://127.0.0.1:%s ...\n' "$BRIDGE_PORT"
(
  cd "$ROOT_DIR/bridge"
  exec env \
    LOCAL_DEV=1 \
    AWS_EC2_METADATA_DISABLED=true \
    AWS_REGION=us-west-2 \
    DASHBOARD_LOCAL_DIR="$TEMP_DIR/dashboards" \
    LOCAL_AGENT_URL="http://127.0.0.1:9" \
    ONBOARDING_BASE_URL="http://127.0.0.1:$WEB_PORT" \
    .venv/bin/uvicorn bridge.main:app --host 127.0.0.1 --port "$BRIDGE_PORT"
) >"$TEMP_DIR/bridge.log" 2>&1 &
BRIDGE_PID=$!

printf 'Starting local web UI on http://127.0.0.1:%s ...\n' "$WEB_PORT"
(
  cd "$ROOT_DIR/onboarding"
  exec env \
    BRIDGE_URL="http://127.0.0.1:$BRIDGE_PORT" \
    NEXT_PUBLIC_BRIDGE_INSTALL_URL="http://127.0.0.1:$BRIDGE_PORT/slack/install" \
    node_modules/.bin/next dev --hostname 127.0.0.1 --port "$WEB_PORT"
) >"$TEMP_DIR/web.log" 2>&1 &
WEB_PID=$!

wait_for_url() {
  label=$1
  url=$2
  log_file=$3
  attempts=0
  while [ "$attempts" -lt 120 ]; do
    if "$PYTHON_BIN" - "$url" <<'PY' >/dev/null 2>&1
import sys
import urllib.request

with urllib.request.urlopen(sys.argv[1], timeout=1) as response:
    raise SystemExit(response.status != 200)
PY
    then
      return 0
    fi
    attempts=$((attempts + 1))
    sleep 0.25
  done
  printf 'demo: %s did not become ready at %s\n' "$label" "$url" >&2
  printf '%s log:\n' "$label" >&2
  tail -n 40 "$log_file" >&2 || true
  return 1
}

wait_for_url "bridge" "http://127.0.0.1:$BRIDGE_PORT/healthz" "$TEMP_DIR/bridge.log"
DASHBOARD_URL="http://127.0.0.1:$WEB_PORT/d/$TOKEN"
wait_for_url "web UI" "$DASHBOARD_URL" "$TEMP_DIR/web.log"

printf '\nAgent is ready — no cloud credentials were used.\n'
printf 'Dashboard: %s\n' "$DASHBOARD_URL"

if [ "$CHECK_ONLY" -eq 1 ]; then
  printf 'Local demo check passed; shutting down both services.\n'
  exit 0
fi

if [ "$OPEN_BROWSER" -eq 1 ]; then
  if command -v open >/dev/null 2>&1; then
    open "$DASHBOARD_URL" >/dev/null 2>&1 || true
  elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$DASHBOARD_URL" >/dev/null 2>&1 || true
  fi
fi

printf 'Press Ctrl-C to stop both services.\n'
while kill -0 "$BRIDGE_PID" 2>/dev/null && kill -0 "$WEB_PID" 2>/dev/null; do
  sleep 1
done
printf 'demo: a service exited unexpectedly\n' >&2
tail -n 20 "$TEMP_DIR/bridge.log" >&2 || true
tail -n 20 "$TEMP_DIR/web.log" >&2 || true
exit 1
