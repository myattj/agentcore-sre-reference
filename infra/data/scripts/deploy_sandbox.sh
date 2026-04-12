#!/usr/bin/env bash
#
# Two-pass deploy for the Phase B sandbox stack.
#
# SandboxStack needs five values from ServicesStack (VPC ID, AZs, public
# subnets, cluster name + ARN) plus the sandbox secret ARN and the
# public domain. We can't use Fn.importValue for the VPC because
# Vpc.fromVpcAttributes requires availabilityZones to be a real list
# at synth time, not a CFN token. So this script:
#
#   1. Deploys ServicesStack first with the sandboxSecretsArn context,
#      so it picks up the new exports + the /internal/* listener rule
#      + the SANDBOX_CALLBACK_SECRET injection into the bridge.
#   2. Reads the resulting CloudFormation outputs.
#   3. Deploys SandboxStack with all the values threaded in via
#      --context flags.
#   4. (Optional) Runs attach_agent_policy.sh to attach both the
#      AgentCoreDataAccess and the new AgentCoreSandboxAccess managed
#      policies to the agent runtime role.
#
# Idempotent — re-running after no changes is safe (CDK reports
# "no changes" per stack and exits 0).
#
# Required env / context (passed in via -e or -c flags):
#   AGENT_RUNTIME_ARN     — AgentCore Runtime ARN (already set in your
#                           normal deploy ritual)
#   SLACK_SECRETS_ARN     — Slack Secrets Manager secret ARN
#   BRIDGE_SECRETS_ARN    — Bridge Secrets Manager secret ARN
#   CERTIFICATE_ARN       — ACM cert ARN (optional, but required for
#                           the sandbox to be reachable on agent.example.com)
#   DOMAIN_NAME           — e.g. agent.example.com. Required for sandbox
#                           — the SANDBOX_CALLBACK_URL env var the
#                           sandbox container POSTs to.
#   SANDBOX_SECRETS_ARN   — agentcore/services/sandbox secret ARN
#                           (created out-of-band via
#                           `aws secretsmanager create-secret`).
#
# Optional:
#   REGION                — default us-west-2
#   ATTACH_POLICY         — set to "1" to also run attach_agent_policy.sh
#                           after sandbox deploy. Default 0 (run it
#                           manually so you can review).
#
# Exit codes:
#   0 — both stacks deployed (or no-op) successfully
#   1 — required env var missing
#   2 — services stack outputs not extractable
#   3 — sandbox deploy failed

set -euo pipefail

REGION="${REGION:-us-west-2}"
DATA_STACK="AgentCore-coreAgent-data-${REGION}"
SERVICES_STACK="AgentCore-coreAgent-services-${REGION}"
SANDBOX_STACK="AgentCore-coreAgent-sandbox-${REGION}"

cd "$(dirname "$0")/.."

# -----------------------------------------------------------------------
# 1. Validate inputs
# -----------------------------------------------------------------------
missing=()
[[ -z "${AGENT_RUNTIME_ARN:-}" ]]   && missing+=("AGENT_RUNTIME_ARN")
[[ -z "${SLACK_SECRETS_ARN:-}" ]]   && missing+=("SLACK_SECRETS_ARN")
[[ -z "${BRIDGE_SECRETS_ARN:-}" ]]  && missing+=("BRIDGE_SECRETS_ARN")
[[ -z "${SANDBOX_SECRETS_ARN:-}" ]] && missing+=("SANDBOX_SECRETS_ARN")
[[ -z "${DOMAIN_NAME:-}" ]]         && missing+=("DOMAIN_NAME")

if (( ${#missing[@]} )); then
  echo "ERROR: missing required env vars: ${missing[*]}" >&2
  echo >&2
  echo "Example invocation:" >&2
  echo "  AGENT_RUNTIME_ARN=arn:aws:bedrock-agentcore:... \\" >&2
  echo "  SLACK_SECRETS_ARN=arn:aws:secretsmanager:... \\" >&2
  echo "  BRIDGE_SECRETS_ARN=arn:aws:secretsmanager:... \\" >&2
  echo "  SANDBOX_SECRETS_ARN=arn:aws:secretsmanager:... \\" >&2
  echo "  CERTIFICATE_ARN=arn:aws:acm:... \\" >&2
  echo "  DOMAIN_NAME=agent.example.com \\" >&2
  echo "  bash infra/data/scripts/deploy_sandbox.sh" >&2
  exit 1
fi

CONTEXT_FLAGS=(
  "--context" "agentRuntimeArn=$AGENT_RUNTIME_ARN"
  "--context" "slackSecretsArn=$SLACK_SECRETS_ARN"
  "--context" "bridgeSecretsArn=$BRIDGE_SECRETS_ARN"
  "--context" "sandboxSecretsArn=$SANDBOX_SECRETS_ARN"
  "--context" "domainName=$DOMAIN_NAME"
)

if [[ -n "${CERTIFICATE_ARN:-}" ]]; then
  CONTEXT_FLAGS+=("--context" "certificateArn=$CERTIFICATE_ARN")
fi

# -----------------------------------------------------------------------
# 2. Deploy ServicesStack first so its new outputs (cluster + VPC +
#    subnets) exist before we try to read them.
# -----------------------------------------------------------------------
echo "==> Pass 1: deploying $SERVICES_STACK (gets new exports + /internal/* + SANDBOX_CALLBACK_SECRET)"
npm run deploy -- "$SERVICES_STACK" "${CONTEXT_FLAGS[@]}" --require-approval never

# -----------------------------------------------------------------------
# 3. Read ServicesStack outputs
# -----------------------------------------------------------------------
echo
echo "==> Reading $SERVICES_STACK outputs"
get_output() {
  local key="$1"
  aws cloudformation describe-stacks \
    --region "$REGION" \
    --stack-name "$SERVICES_STACK" \
    --query "Stacks[0].Outputs[?OutputKey=='$key'].OutputValue" \
    --output text 2>/dev/null
}

VPC_ID="$(get_output VpcId)"
AZS="$(get_output VpcAvailabilityZones)"
SUBNETS="$(get_output VpcPublicSubnetIds)"
CLUSTER_NAME="$(get_output ClusterName)"
CLUSTER_ARN="$(get_output ClusterArn)"

for var in VPC_ID AZS SUBNETS CLUSTER_NAME CLUSTER_ARN; do
  val="${!var}"
  if [[ -z "$val" || "$val" == "None" ]]; then
    echo "ERROR: could not read $var from $SERVICES_STACK outputs." >&2
    echo "       Did the pass-1 deploy succeed?" >&2
    exit 2
  fi
done

echo "  VpcId:                 $VPC_ID"
echo "  AvailabilityZones:     $AZS"
echo "  PublicSubnetIds:       $SUBNETS"
echo "  ClusterName:           $CLUSTER_NAME"
echo "  ClusterArn:            $CLUSTER_ARN"

# -----------------------------------------------------------------------
# 4. Deploy SandboxStack with the extracted values
# -----------------------------------------------------------------------
SANDBOX_CONTEXT_FLAGS=(
  "${CONTEXT_FLAGS[@]}"
  "--context" "sandboxVpcId=$VPC_ID"
  "--context" "sandboxAvailabilityZones=$AZS"
  "--context" "sandboxPublicSubnetIds=$SUBNETS"
  "--context" "sandboxClusterName=$CLUSTER_NAME"
  "--context" "sandboxClusterArn=$CLUSTER_ARN"
  "--context" "sandboxDomainName=$DOMAIN_NAME"
)

echo
echo "==> Pass 2: deploying $SANDBOX_STACK"
if ! npm run deploy -- "$SANDBOX_STACK" "${SANDBOX_CONTEXT_FLAGS[@]}" --require-approval never; then
  echo "ERROR: $SANDBOX_STACK deploy failed." >&2
  exit 3
fi

# -----------------------------------------------------------------------
# 5. Optional policy attach
# -----------------------------------------------------------------------
echo
if [[ "${ATTACH_POLICY:-0}" == "1" ]]; then
  echo "==> Attaching managed policies to agent role"
  bash "$(dirname "$0")/attach_agent_policy.sh"
else
  echo "==> Skipping attach_agent_policy.sh (set ATTACH_POLICY=1 to run)."
  echo "    To attach manually:"
  echo "      bash infra/data/scripts/attach_agent_policy.sh"
fi

echo
echo "==> Done. Sandbox stack outputs:"
aws cloudformation describe-stacks \
  --region "$REGION" \
  --stack-name "$SANDBOX_STACK" \
  --query 'Stacks[0].Outputs[*].[OutputKey,OutputValue]' \
  --output table
