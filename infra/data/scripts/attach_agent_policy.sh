#!/usr/bin/env bash
#
# Attach the agent runtime's IAM execution role to all the managed
# policies it needs:
#   - AgentCoreDataAccess        (from data-stack.ts)
#   - AgentCoreSandboxAccess     (from sandbox-stack.ts, Phase B —
#                                 only attached if the sandbox stack is
#                                 deployed; gracefully skipped if not)
#
# `agentcore deploy` creates an IAM role for the agent runtime inside the
# `@aws/agentcore-cdk` L3 construct. We can't reference that role from our
# sibling data stack (different CDK app, can't cross-stack-ref an opaque
# L3 resource), so this script discovers the role by describing the
# agent's CloudFormation stack resources and then attaches each managed
# policy to it.
#
# Idempotent: skips the attach if a policy is already present.
#
# Usage:
#   bash infra/data/scripts/attach_agent_policy.sh
#
# Environment:
#   REGION        — default us-west-2
#   AGENT_STACK   — default AgentCore-coreAgent-default  (see aws-targets.json)
#   DATA_STACK    — default AgentCore-coreAgent-data-<region>
#   SANDBOX_STACK — default AgentCore-coreAgent-sandbox-<region>
#
# Exit codes:
#   0 — attached (or already attached)
#   1 — could not discover the agent role; manual attach needed (see output)
#   2 — required AWS CLI / jq not installed

set -euo pipefail

REGION="${REGION:-us-west-2}"
AGENT_STACK="${AGENT_STACK:-AgentCore-coreAgent-default}"
DATA_STACK="${DATA_STACK:-AgentCore-coreAgent-data-${REGION}}"
SANDBOX_STACK="${SANDBOX_STACK:-AgentCore-coreAgent-sandbox-${REGION}}"

command -v aws >/dev/null 2>&1 || { echo "aws CLI not installed"; exit 2; }
command -v jq  >/dev/null 2>&1 || { echo "jq not installed"; exit 2; }

echo "Region:         $REGION"
echo "Agent stack:    $AGENT_STACK"
echo "Data stack:    $DATA_STACK"
echo "Sandbox stack: $SANDBOX_STACK"
echo

# -----------------------------------------------------------------------
# 1. Resolve all managed policy ARNs we want attached.
#
# Helper: read an output by name from a stack. Returns empty if either
# the stack or the output is missing — non-fatal so we can support
# environments where the sandbox stack hasn't been deployed yet.
# -----------------------------------------------------------------------
read_stack_output() {
  local stack="$1"
  local key="$2"
  aws cloudformation describe-stacks \
    --region "$REGION" \
    --stack-name "$stack" \
    --query "Stacks[0].Outputs[?OutputKey=='$key'].OutputValue" \
    --output text 2>/dev/null
}

# Required: AgentCoreDataAccess from data-stack.
DATA_POLICY_ARN="$(read_stack_output "$DATA_STACK" "AgentDataAccessPolicyArn")"
if [[ -z "$DATA_POLICY_ARN" || "$DATA_POLICY_ARN" == "None" ]]; then
  echo "ERROR: Could not read AgentDataAccessPolicyArn from $DATA_STACK." >&2
  echo "       Did you run 'npm run deploy' in infra/data/ yet?" >&2
  exit 1
fi

# Optional: AgentCoreSandboxAccess from sandbox-stack. Soft-skip if the
# sandbox stack isn't deployed yet — pre-Phase-B environments still work.
SANDBOX_POLICY_ARN="$(read_stack_output "$SANDBOX_STACK" "AgentSandboxAccessPolicyArn")"

# Build the list of (stack, arn) pairs we'll attach. Empty entries are
# filtered out below so the loop only iterates over real ARNs.
POLICY_ARNS=("$DATA_POLICY_ARN")
POLICY_LABELS=("AgentCoreDataAccess (data-stack)")
if [[ -n "$SANDBOX_POLICY_ARN" && "$SANDBOX_POLICY_ARN" != "None" ]]; then
  POLICY_ARNS+=("$SANDBOX_POLICY_ARN")
  POLICY_LABELS+=("AgentCoreSandboxAccess (sandbox-stack)")
else
  echo "Note: $SANDBOX_STACK not found — skipping AgentCoreSandboxAccess attach."
  echo "      Run 'bash infra/data/scripts/deploy_sandbox.sh' first if you" \
       "want Phase B propose_pr support."
fi
echo

echo "Will attach ${#POLICY_ARNS[@]} managed policy(ies):"
for i in "${!POLICY_ARNS[@]}"; do
  echo "  - ${POLICY_LABELS[$i]}"
  echo "    $(printf '%s' "${POLICY_ARNS[$i]}")"
done
echo

# -----------------------------------------------------------------------
# 2. Discover the agent's IAM execution role from its CFN stack resources.
# -----------------------------------------------------------------------
# The agentcore-cdk L3 construct creates one or more IAM roles; the one we
# want is the execution role attached to the AgentCore Runtime. Its
# logical ID contains "ExecutionRole" or "AgentRuntimeRole" depending on
# the library version. We list all IAM::Role resources in the stack and
# prefer the one whose logical ID matches; if nothing matches, we fall
# back to the first role and warn loudly.

ROLES_JSON="$(
  aws cloudformation list-stack-resources \
    --region "$REGION" \
    --stack-name "$AGENT_STACK" \
    --query "StackResourceSummaries[?ResourceType=='AWS::IAM::Role']" \
    --output json 2>/dev/null || echo "[]"
)"

if [[ "$ROLES_JSON" == "[]" || -z "$ROLES_JSON" ]]; then
  echo "ERROR: Could not list stack resources for $AGENT_STACK." >&2
  echo "       Either the stack name is wrong or 'agentcore deploy' hasn't" >&2
  echo "       been run yet." >&2
  echo >&2
  echo "Manual fallback:" >&2
  echo "  1. Find the agent's execution role in the IAM console" >&2
  echo "     (look for a role whose trust policy lets bedrock-agentcore.amazonaws.com assume it)." >&2
  echo "  2. Run:" >&2
  echo "       aws iam attach-role-policy --role-name <role-name> \\\\" >&2
  echo "           --policy-arn $POLICY_ARN" >&2
  exit 1
fi

ROLE_NAME="$(
  echo "$ROLES_JSON" \
  | jq -r '
    (map(select(.LogicalResourceId | test("(?i)(executionrole|runtimerole|agentrole)"))) | first | .PhysicalResourceId) //
    (.[0] | .PhysicalResourceId)
  '
)"

if [[ -z "$ROLE_NAME" || "$ROLE_NAME" == "null" ]]; then
  echo "ERROR: Could not identify the agent execution role." >&2
  echo "Available IAM roles in $AGENT_STACK:" >&2
  echo "$ROLES_JSON" | jq -r '.[] | "  - \(.LogicalResourceId) -> \(.PhysicalResourceId)"' >&2
  exit 1
fi

echo "Agent role:    $ROLE_NAME"
echo

# -----------------------------------------------------------------------
# 3. Attach each policy (idempotent — skip if already attached).
# -----------------------------------------------------------------------
ATTACHED_NOW=0
ALREADY_ATTACHED=0
for i in "${!POLICY_ARNS[@]}"; do
  arn="${POLICY_ARNS[$i]}"
  label="${POLICY_LABELS[$i]}"
  if aws iam list-attached-role-policies \
       --role-name "$ROLE_NAME" \
       --query "AttachedPolicies[?PolicyArn=='$arn']" \
       --output text \
     | grep -q "$arn"; then
    echo "  $label: already attached, skipping."
    ALREADY_ATTACHED=$((ALREADY_ATTACHED + 1))
    continue
  fi
  echo "  $label: attaching..."
  aws iam attach-role-policy \
    --role-name "$ROLE_NAME" \
    --policy-arn "$arn"
  echo "  $label: attached."
  ATTACHED_NOW=$((ATTACHED_NOW + 1))
done

echo
echo "Done. Newly attached: $ATTACHED_NOW. Already-attached: $ALREADY_ATTACHED."
