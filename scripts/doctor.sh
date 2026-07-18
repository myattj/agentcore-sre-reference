#!/usr/bin/env bash
set -u

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)

usage() {
  cat <<'EOF'
Usage: scripts/doctor.sh

Checks the tools used by Agent's local setup, demo, and validation scripts.
Required-tool failures produce a non-zero exit. Optional cloud tools never do.

Required: Bash, Git, Python 3.13, uv, Node.js 22+, npm
Required for self-hosting: AWS CLI, Docker, jq, OpenSSL
Optional for direct/manual work: AgentCore CLI, CDK, GitHub CLI, gitleaks
EOF
}

if [ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ]; then
  usage
  exit 0
fi
if [ "$#" -ne 0 ]; then
  printf 'doctor: unknown argument: %s\n' "$1" >&2
  usage >&2
  exit 2
fi

required_failures=0
has_make=0

ok() {
  printf '  [ok]      %-14s %s\n' "$1" "$2"
}

missing() {
  printf '  [missing] %-14s %s\n' "$1" "$2"
  required_failures=$((required_failures + 1))
}

optional() {
  if command -v "$2" >/dev/null 2>&1; then
    version=$($3 2>/dev/null | head -n 1 || true)
    ok "$1" "${version:-installed}"
  else
    printf '  [optional] %-13s %s\n' "$1" "$4"
  fi
}

check_agentcore() {
  minimum_version="0.24.1"
  install_hint="npm install -g @aws/agentcore@$minimum_version"
  if ! command -v agentcore >/dev/null 2>&1; then
    printf '  [optional] %-13s %s\n' "AgentCore" "install only for the live agent loop: $install_hint"
    return
  fi

  output=$(agentcore --version 2>/dev/null | head -n 1 || true)
  version=$(printf '%s' "$output" | grep -Eo '[0-9]+\.[0-9]+\.[0-9]+' | head -n 1 || true)
  if [ -n "$version" ] && "$PYTHON_BIN" -c '
import sys
found = tuple(int(part) for part in sys.argv[1].split("."))
required = tuple(int(part) for part in sys.argv[2].split("."))
raise SystemExit(found < required)
' "$version" "$minimum_version"; then
    ok "AgentCore" "$output"
  else
    printf '  [update]  %-13s found %s; this repository validates with %s (%s)\n' \
      "AgentCore" "${output:-unknown version}" "$minimum_version" "$install_hint"
  fi
}

printf 'Agent local development doctor\n'
printf 'Repository: %s\n\n' "$ROOT_DIR"
printf 'Required for setup, demo, and checks\n'

bash_major=${BASH_VERSINFO[0]:-0}
if [ "$bash_major" -ge 3 ]; then
  ok "Bash" "$BASH_VERSION"
else
  missing "Bash" "need Bash 3+; macOS ships it, or install with: brew install bash"
fi

if command -v git >/dev/null 2>&1; then
  ok "Git" "$(git --version)"
else
  missing "Git" "install from https://git-scm.com/downloads"
fi

PYTHON_BIN=""
if command -v python3.13 >/dev/null 2>&1; then
  PYTHON_BIN=$(command -v python3.13)
elif command -v python3 >/dev/null 2>&1 && python3 -c 'import sys; raise SystemExit(sys.version_info[:2] != (3, 13))' 2>/dev/null; then
  PYTHON_BIN=$(command -v python3)
fi
if [ -n "$PYTHON_BIN" ]; then
  ok "Python" "$($PYTHON_BIN --version 2>&1)"
else
  missing "Python" "need exactly 3.13; macOS: brew install python@3.13; Linux: https://www.python.org/downloads/"
fi

if command -v uv >/dev/null 2>&1; then
  ok "uv" "$(uv --version 2>&1)"
else
  missing "uv" "install with: curl -LsSf https://astral.sh/uv/install.sh | sh"
fi

if command -v node >/dev/null 2>&1; then
  node_version=$(node --version 2>&1)
  node_major=$(printf '%s' "$node_version" | sed 's/^v//' | cut -d. -f1)
  if [ "${node_major:-0}" -ge 22 ] 2>/dev/null; then
    ok "Node.js" "$node_version"
  else
    missing "Node.js" "found $node_version, need 22+; with nvm: nvm install 22 && nvm use 22"
  fi
else
  missing "Node.js" "need 22+; install from https://nodejs.org/ or run: nvm install 22"
fi

if command -v npm >/dev/null 2>&1; then
  ok "npm" "$(npm --version 2>&1)"
else
  missing "npm" "install Node.js 22 from https://nodejs.org/ (npm is included)"
fi

printf '\nConvenience wrapper (the scripts work without it)\n'
if command -v make >/dev/null 2>&1; then
  has_make=1
  ok "Make" "$(make --version 2>&1 | head -n 1)"
else
  printf '  [optional] %-13s %s\n' "Make" "use ./scripts/*.sh directly; macOS: xcode-select --install; Debian/Ubuntu: sudo apt install make"
fi

printf '\nOptional for the full AWS/Slack deployment path\n'
optional "AWS CLI" "aws" "aws --version" "install: https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html"
check_agentcore
optional "AWS CDK" "cdk" "cdk --version" "not required globally; infra checks use the locked npx version"
optional "Docker" "docker" "docker --version" "install only for container/deployment work: https://docs.docker.com/get-docker/"
optional "jq" "jq" "jq --version" "install for self-hosted runtime configuration: https://jqlang.github.io/jq/download/"
optional "OpenSSL" "openssl" "openssl version" "install for self-hosted Gateway JWT key generation: https://www.openssl.org/"
optional "GitHub CLI" "gh" "gh --version" "install only for pull-request workflows: https://cli.github.com/"
optional "gitleaks" "gitleaks" "gitleaks version" "CI always scans secrets; local install: https://github.com/gitleaks/gitleaks#installing"

printf '\n'
if [ "$required_failures" -ne 0 ]; then
  printf 'Doctor found %s required-tool problem(s). Fix the lines above, then rerun make doctor.\n' "$required_failures" >&2
  exit 1
fi
if [ "$has_make" -eq 1 ]; then
  printf 'Ready for local work: make setup, make demo, and make check.\n'
  printf 'For AWS deployment, resolve any optional AWS CLI, Docker, jq, or OpenSSL lines, then run make self-host.\n'
else
  printf 'Ready for: ./scripts/setup.sh, ./scripts/demo.sh, and ./scripts/check.sh.\n'
fi
