#!/usr/bin/env bash
# Exercise partition portability and fail-closed CDK context validation.
# Assumes `npm ci`, `npm run build`, and the interceptor bundle have already run.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
INFRA_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
ROOT_DIR="$(cd "$INFRA_DIR/../.." && pwd)"
. "$ROOT_DIR/scripts/offline_aws_env.sh"
TMP_BASE="${TMPDIR:-/tmp}"
TMP_BASE="${TMP_BASE%/}"
WORK_DIR=$(mktemp -d "$TMP_BASE/agent-portability-synth.XXXXXX")
GOV_APP='env CDK_DEFAULT_ACCOUNT=000000000000 CDK_DEFAULT_REGION=us-gov-west-1 node dist/bin/data.js'
COMMERCIAL_APP='env CDK_DEFAULT_ACCOUNT=000000000000 CDK_DEFAULT_REGION=us-west-2 node dist/bin/data.js'

cleanup() {
  case "$WORK_DIR" in
    "$TMP_BASE"/agent-portability-synth.*) rm -r -- "$WORK_DIR" ;;
    *) echo "check-portability: refusing to remove unexpected path: $WORK_DIR" >&2 ;;
  esac
}
trap cleanup EXIT
trap 'exit 130' HUP INT TERM

cd "$INFRA_DIR"

run_offline_aws npx cdk synth AgentCore-coreAgent-data-us-gov-west-1 --quiet \
  --output "$WORK_DIR/data" \
  --app "$GOV_APP" \
  --context region=us-gov-west-1
run_offline_aws npx cdk synth AgentCore-coreAgent-services-us-gov-west-1 --quiet \
  --output "$WORK_DIR/services" \
  --app "$GOV_APP" \
  --context region=us-gov-west-1 \
  --context agentRuntimeArn=arn:aws-us-gov:bedrock-agentcore:us-gov-west-1:000000000000:agent/00000000-0000-0000-0000-000000000001:1 \
  --context slackSecretsArn=arn:aws-us-gov:secretsmanager:us-gov-west-1:000000000000:secret:slack-abc123 \
  --context bridgeSecretsArn=arn:aws-us-gov:secretsmanager:us-gov-west-1:000000000000:secret:bridge-abc123

commercial_matches=$(find "$WORK_DIR" -name '*.template.json' -type f \
  -exec grep -Hn 'arn:aws:' {} \; || true)
if [[ -n "$commercial_matches" ]]; then
  printf '%s\n' "$commercial_matches" >&2
  echo "check-portability: commercial ARN leaked into GovCloud templates" >&2
  exit 1
fi
gov_matches=$(find "$WORK_DIR" -name '*.template.json' -type f \
  -exec grep -Hl 'arn:aws-us-gov:' {} \; || true)
if [[ -z "$gov_matches" ]]; then
  echo "check-portability: GovCloud services template did not retain GovCloud ARNs" >&2
  exit 1
fi

# Required services contexts must fail before a template can be emitted.
if run_offline_aws npx cdk synth AgentCore-coreAgent-services-us-west-2 --quiet \
  --output "$WORK_DIR/missing-context" \
  --app "$COMMERCIAL_APP" \
  --context region=us-west-2 \
  --context agentRuntimeArn=arn:aws:bedrock-agentcore:us-west-2:000000000000:agent/00000000-0000-0000-0000-000000000001:1 \
  >"$WORK_DIR/missing-context.log" 2>&1; then
  echo "check-portability: services synth accepted missing secret contexts" >&2
  exit 1
fi
if ! grep -q 'slackSecretsArn context is required' "$WORK_DIR/missing-context.log"; then
  cat "$WORK_DIR/missing-context.log" >&2
  echo "check-portability: services synth failed for an unexpected reason" >&2
  exit 1
fi

echo "Partition portability and fail-closed context checks passed."
