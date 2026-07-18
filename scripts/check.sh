#!/usr/bin/env bash
set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
. "$ROOT_DIR/scripts/offline_aws_env.sh"
QUICK=0
PYTHON_BIN=""
SYNTH_APP="env CDK_DEFAULT_ACCOUNT=000000000000 CDK_DEFAULT_REGION=us-west-2 node dist/bin/data.js"

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

run_offline_cdk() {
  label=$1
  shift
  printf '\n==> %s\n' "$label"
  (
    cd "$ROOT_DIR/infra/data"
    run_offline_aws "$@"
  )
}

cleanup_source_scan() {
  if [ -n "${SOURCE_SCAN_WORK:-}" ] && [ -d "$SOURCE_SCAN_WORK" ]; then
    rm -r -- "$SOURCE_SCAN_WORK"
  fi
  SOURCE_SCAN_WORK=""
}

scan_source_archive() {
  SOURCE_SCAN_WORK=$(mktemp -d "${TMPDIR:-/tmp}/agent-source-scan.XXXXXX")
  source_scan_tree="$SOURCE_SCAN_WORK/tree"
  mkdir "$source_scan_tree"
  trap cleanup_source_scan EXIT
  trap 'cleanup_source_scan; exit 130' HUP INT TERM

  # A downloaded source archive has no Git index to distinguish source from
  # files created by setup and build commands. Copy only source-like content
  # into a temporary tree so generated local secrets and dependency/build
  # artifacts cannot become false positives. Keep .env.example files: they are
  # documentation and should still be scanned.
  (
    cd "$ROOT_DIR"
    tar -cf "$SOURCE_SCAN_WORK/source.tar" \
      --exclude-from="$ROOT_DIR/.source-scan-excludes" \
      .
  )
  tar -xf "$SOURCE_SCAN_WORK/source.tar" -C "$source_scan_tree"
  rm -f -- "$SOURCE_SCAN_WORK/source.tar"

  source_scan_status=0
  gitleaks dir --redact --no-banner "$source_scan_tree" || source_scan_status=$?
  cleanup_source_scan
  trap - EXIT HUP INT TERM
  return "$source_scan_status"
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
  "$ROOT_DIR/scripts/check.sh" \
  "$ROOT_DIR/scripts/offline_aws_env.sh" \
  "$ROOT_DIR/scripts/deploy_agent.sh" \
  "$ROOT_DIR/scripts/resolve_agent_runtime.sh" \
  "$ROOT_DIR/infra/data/scripts/aws_region.sh" \
  "$ROOT_DIR/infra/data/scripts/attach_agent_policy.sh" \
  "$ROOT_DIR/infra/data/scripts/check_portability.sh" \
  "$ROOT_DIR/infra/data/scripts/deploy_sandbox.sh"
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
run_in "hand-authored CDK tests" infra/data npm test

if [ "$QUICK" -eq 0 ]; then
  run_in "Gateway interceptor bundle" infra/data bash scripts/build_interceptor_zip.sh
  run_offline_cdk "base CDK synth" npx cdk synth --quiet \
    --app "$SYNTH_APP" \
    --context region=us-west-2
  run_offline_cdk "Gateway CDK synth" \
    npx cdk synth AgentCore-coreAgent-gateway-us-west-2 --quiet \
    --app "$SYNTH_APP" \
    --context region=us-west-2 \
    --context bridgePublicUrl=https://bridge.example.test
  run_offline_cdk "certificate-free services CDK synth" \
    npx cdk synth AgentCore-coreAgent-services-us-west-2 --quiet \
    --app "$SYNTH_APP" \
    --context region=us-west-2 \
    --context agentRuntimeArn=arn:aws:bedrock-agentcore:us-west-2:000000000000:agent/00000000-0000-0000-0000-000000000001:1 \
    --context slackSecretsArn=arn:aws:secretsmanager:us-west-2:000000000000:secret:slack-abc123 \
    --context bridgeSecretsArn=arn:aws:secretsmanager:us-west-2:000000000000:secret:bridge-abc123
  run_offline_cdk "HTTPS services CDK synth" \
    npx cdk synth AgentCore-coreAgent-services-us-west-2 --quiet \
    --app "$SYNTH_APP" \
    --context region=us-west-2 \
    --context agentRuntimeArn=arn:aws:bedrock-agentcore:us-west-2:000000000000:agent/00000000-0000-0000-0000-000000000001:1 \
    --context certificateArn=arn:aws:acm:us-west-2:000000000000:certificate/00000000-0000-0000-0000-000000000000 \
    --context domainName=example.test \
    --context slackSecretsArn=arn:aws:secretsmanager:us-west-2:000000000000:secret:slack-abc123 \
    --context bridgeSecretsArn=arn:aws:secretsmanager:us-west-2:000000000000:secret:bridge-abc123 \
    --context sandboxSecretsArn=arn:aws:secretsmanager:us-west-2:000000000000:secret:sandbox-abc123
  run_offline_cdk "sandbox CDK synth" \
    npx cdk synth AgentCore-coreAgent-sandbox-us-west-2 --quiet \
    --app "$SYNTH_APP" \
    --context region=us-west-2 \
    --context sandboxSecretsArn=arn:aws:secretsmanager:us-west-2:000000000000:secret:sandbox-abc123 \
    --context sandboxVpcId=vpc-00000000000000000 \
    --context sandboxAvailabilityZones=us-west-2a,us-west-2b \
    --context sandboxPublicSubnetIds=subnet-00000000000000001,subnet-00000000000000002 \
    --context sandboxClusterName=example-cluster \
    --context sandboxClusterArn=arn:aws:ecs:us-west-2:000000000000:cluster/example-cluster \
    --context sandboxDomainName=example.test \
    --context sandboxGithubAppId=123456

  run_in "partition portability CDK synth" infra/data bash scripts/check_portability.sh
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
    run "gitleaks secret scan (source tree)" scan_source_archive
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
