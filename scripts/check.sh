#!/usr/bin/env bash
set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
QUICK=0
PYTHON_BIN=""

usage() {
  cat <<'EOF'
Usage: scripts/check.sh [--quick]

Runs the repository's local validation gates with locked dependencies. Nothing
is deployed and tests are configured not to contact AWS, Slack, or GitHub.

  --quick     Skip CDK synth and the no-cloud service startup check.
  -h, --help  Show this help.

Run `make setup` once before this command.
EOF
}

case "${1:-}" in
  "") ;;
  --quick) QUICK=1 ;;
  -h|--help) usage; exit 0 ;;
  *) printf 'check: unknown argument: %s\n' "$1" >&2; usage >&2; exit 2 ;;
esac
if [ "$#" -gt 1 ]; then
  printf 'check: too many arguments\n' >&2
  exit 2
fi

run() {
  label=$1
  shift
  printf '\n==> %s\n' "$label"
  "$@"
}

run_in() {
  label=$1
  directory=$2
  shift 2
  printf '\n==> %s\n' "$label"
  (cd "$ROOT_DIR/$directory" && "$@")
}

if [ ! -x "$ROOT_DIR/bridge/.venv/bin/pytest" ] || [ ! -d "$ROOT_DIR/onboarding/node_modules" ]; then
  printf 'check: dependencies are missing; run make setup first.\n' >&2
  exit 1
fi

if command -v python3.13 >/dev/null 2>&1; then
  PYTHON_BIN=$(command -v python3.13)
elif command -v python3 >/dev/null 2>&1 && python3 -c 'import sys; raise SystemExit(sys.version_info[:2] != (3, 13))'; then
  PYTHON_BIN=$(command -v python3)
else
  printf 'check: Python 3.13 is required; run make doctor.\n' >&2
  exit 1
fi

run "local tooling tests" "$PYTHON_BIN" -m unittest discover -s "$ROOT_DIR/scripts/tests" -v
run "shell syntax" bash -n \
  "$ROOT_DIR/scripts/setup.sh" \
  "$ROOT_DIR/scripts/doctor.sh" \
  "$ROOT_DIR/scripts/demo.sh" \
  "$ROOT_DIR/scripts/check.sh"
run_in "bridge tests" bridge uv run --frozen pytest
run_in "core agent tests" coreAgent/app/coreAgent uv run --frozen pytest
run_in "core metrics tests" coreAgent/app/coreAgent uv run --frozen python -m unittest test_metrics
run_in "Gateway interceptor tests" workers/gateway_interceptor uv run --frozen pytest
run_in "sandbox tests" infra/sandbox uv run --frozen python -m pytest -p no:cacheprovider tests
run_in "synthetic incident seed tests" . uv run --python 3.13 --with requests==2.32.5 python -m unittest discover -s seed/tests -v
run_in "onboarding authentication tests" onboarding npm test
run_in "onboarding production build" onboarding env NEXT_PUBLIC_BRIDGE_INSTALL_URL=https://ci.test/slack/install npm run build
run_in "generated AgentCore CDK build" coreAgent/agentcore/cdk npm run build
run_in "generated AgentCore CDK tests" coreAgent/agentcore/cdk npm test -- --runInBand
run_in "generated AgentCore CDK format" coreAgent/agentcore/cdk npm run format:check
run_in "hand-authored CDK build" infra/data npm run build

if [ "$QUICK" -eq 0 ]; then
  run_in "Gateway interceptor bundle" infra/data bash scripts/build_interceptor_zip.sh
  run_in "base CDK synth" infra/data env CDK_DEFAULT_ACCOUNT=000000000000 npx cdk synth --quiet
  run_in "Gateway CDK synth" infra/data env CDK_DEFAULT_ACCOUNT=000000000000 \
    npx cdk synth AgentCore-coreAgent-gateway-us-west-2 --quiet \
    --context bridgePublicUrl=https://bridge.example.test
  run_in "certificate-free services CDK synth" infra/data env CDK_DEFAULT_ACCOUNT=000000000000 \
    npx cdk synth AgentCore-coreAgent-services-us-west-2 --quiet \
    --context agentRuntimeArn=arn:aws:bedrock-agentcore:us-west-2:000000000000:runtime/example \
    --context slackSecretsArn=arn:aws:secretsmanager:us-west-2:000000000000:secret:slack-abc123 \
    --context bridgeSecretsArn=arn:aws:secretsmanager:us-west-2:000000000000:secret:bridge-abc123
  run_in "HTTPS services CDK synth" infra/data env CDK_DEFAULT_ACCOUNT=000000000000 \
    npx cdk synth AgentCore-coreAgent-services-us-west-2 --quiet \
    --context agentRuntimeArn=arn:aws:bedrock-agentcore:us-west-2:000000000000:runtime/example \
    --context certificateArn=arn:aws:acm:us-west-2:000000000000:certificate/00000000-0000-0000-0000-000000000000 \
    --context domainName=example.test \
    --context slackSecretsArn=arn:aws:secretsmanager:us-west-2:000000000000:secret:slack-abc123 \
    --context bridgeSecretsArn=arn:aws:secretsmanager:us-west-2:000000000000:secret:bridge-abc123 \
    --context sandboxSecretsArn=arn:aws:secretsmanager:us-west-2:000000000000:secret:sandbox-abc123
  run_in "sandbox CDK synth" infra/data env CDK_DEFAULT_ACCOUNT=000000000000 \
    npx cdk synth AgentCore-coreAgent-sandbox-us-west-2 --quiet \
    --context sandboxSecretsArn=arn:aws:secretsmanager:us-west-2:000000000000:secret:sandbox-abc123 \
    --context sandboxVpcId=vpc-00000000000000000 \
    --context sandboxAvailabilityZones=us-west-2a,us-west-2b \
    --context sandboxPublicSubnetIds=subnet-00000000000000001,subnet-00000000000000002 \
    --context sandboxClusterName=example-cluster \
    --context sandboxClusterArn=arn:aws:ecs:us-west-2:000000000000:cluster/example-cluster \
    --context sandboxDomainName=example.test \
    --context sandboxGithubAppId=123456
  run "no-cloud demo startup" "$ROOT_DIR/scripts/demo.sh" --check --no-open
fi

REPO_HAS_GIT=0
if git_root=$(git -C "$ROOT_DIR" rev-parse --show-toplevel 2>/dev/null) && \
   [ "$git_root" = "$ROOT_DIR" ]; then
  REPO_HAS_GIT=1
fi

if command -v gitleaks >/dev/null 2>&1; then
  if [ "$REPO_HAS_GIT" -eq 1 ]; then
    run_in "gitleaks secret scan (full history)" . gitleaks git --redact --no-banner
  else
    run "gitleaks secret scan (source tree)" \
      gitleaks dir --redact --no-banner "$ROOT_DIR"
  fi
else
  printf '\n==> gitleaks secret scan\n'
  printf 'Skipped locally (optional); CI runs the mandatory full-history scan.\n'
fi

if [ "$REPO_HAS_GIT" -eq 1 ]; then
  run_in "Git whitespace check" . git diff --check
else
  printf '\n==> Git whitespace check\n'
  printf 'Skipped (source archive has no .git metadata).\n'
fi
printf '\nAll requested local validation gates passed.\n'
