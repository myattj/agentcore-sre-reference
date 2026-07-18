#!/usr/bin/env bash
#
# Attach the agent runtime's IAM execution role to all the managed
# policies it needs:
#   - AgentCoreDataAccess        (from data-stack.ts)
#   - AgentCoreSandboxAccess     (from sandbox-stack.ts — only attached
#                                 if the sandbox stack is
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
#   ATTACH_SANDBOX_POLICY — set to 1 only after explicit sandbox review
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
ATTACH_SANDBOX_POLICY="${ATTACH_SANDBOX_POLICY:-0}"

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

# Build the list of (stack, arn) pairs we'll attach. Empty entries are
# filtered out below so the loop only iterates over real ARNs.
POLICY_ARNS=("$DATA_POLICY_ARN")
POLICY_LABELS=("AgentCoreDataAccess (data-stack)")
if [[ "$ATTACH_SANDBOX_POLICY" == "1" ]]; then
  SANDBOX_POLICY_ARN="$(read_stack_output "$SANDBOX_STACK" "AgentSandboxAccessPolicyArn")"
  if [[ -z "$SANDBOX_POLICY_ARN" || "$SANDBOX_POLICY_ARN" == "None" ]]; then
    echo "ERROR: ATTACH_SANDBOX_POLICY=1 but $SANDBOX_STACK has no sandbox policy." >&2
    exit 1
  fi
  POLICY_ARNS+=("$SANDBOX_POLICY_ARN")
  POLICY_LABELS+=("AgentCoreSandboxAccess (experimental sandbox-stack)")
else
  echo "Experimental sandbox policy: disabled (set ATTACH_SANDBOX_POLICY=1 only after review)."
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
# The agentcore-cdk L3 construct may create multiple IAM roles, and logical
# IDs can change between CLI releases. Identify the execution role by its
# trust policy instead: exactly one stack role must allow the AgentCore
# service principal. Ambiguity or an unreadable trust policy fails closed.

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
  echo "           --policy-arn $DATA_POLICY_ARN" >&2
  exit 1
fi

TRUSTED_ROLES=()
while IFS= read -r candidate_role; do
  [[ -n "$candidate_role" ]] || continue
  if ! role_json="$(
    aws iam get-role \
      --role-name "$candidate_role" \
      --output json 2>/dev/null
  )"; then
    echo "ERROR: Could not inspect trust policy for role $candidate_role." >&2
    exit 1
  fi
  if echo "$role_json" | jq -e \
    --arg service "bedrock-agentcore.amazonaws.com" '
      [
        .Role.AssumeRolePolicyDocument.Statement[]?
        | select(.Effect == "Allow")
        | .Principal.Service?
        | if type == "array" then .[] else . end
      ]
      | any(. == $service)
    ' >/dev/null; then
    TRUSTED_ROLES+=("$candidate_role")
  fi
done < <(echo "$ROLES_JSON" | jq -r '.[].PhysicalResourceId // empty')

if [[ ${#TRUSTED_ROLES[@]} -ne 1 ]]; then
  echo "ERROR: Expected exactly one AgentCore-assumable role in $AGENT_STACK;" >&2
  echo "       found ${#TRUSTED_ROLES[@]}. Refusing to attach any policy." >&2
  echo "Available IAM roles in $AGENT_STACK:" >&2
  echo "$ROLES_JSON" | jq -r '.[] | "  - \(.LogicalResourceId) -> \(.PhysicalResourceId)"' >&2
  if [[ ${#TRUSTED_ROLES[@]} -gt 0 ]]; then
    echo "Roles trusting bedrock-agentcore.amazonaws.com:" >&2
    printf '  - %s\n' "${TRUSTED_ROLES[@]}" >&2
  fi
  exit 1
fi

ROLE_NAME="${TRUSTED_ROLES[0]}"

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
