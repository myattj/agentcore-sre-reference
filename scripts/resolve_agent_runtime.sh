#!/usr/bin/env bash
# Resolve one READY AgentCore Runtime in the active account and region.
#
# Required environment:
#   AWS_REGION
# Optional environment:
#   RUNTIME_NAME          defaults to <manifest name>_<single runtime name>
#   RUNTIME_ARN_OVERRIDE  selects one exact runtime when names are duplicated
#
# The selected ARN is the only value written to stdout. Diagnostics go to
# stderr so callers can safely use command substitution.

set -euo pipefail

SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
DEFAULT_MANIFEST="$SCRIPT_DIR/../coreAgent/agentcore/agentcore.json"
AWS_REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-}}"
RUNTIME_NAME="${RUNTIME_NAME:-}"
RUNTIME_ARN_OVERRIDE="${RUNTIME_ARN_OVERRIDE:-}"

if [[ ! "$AWS_REGION" =~ ^[a-z]{2}(-[a-z0-9]+)+-[0-9]+$ ]]; then
  echo "resolve-agent-runtime: AWS_REGION is required and must be a valid region" >&2
  exit 2
fi
command -v aws >/dev/null 2>&1 || {
  echo "resolve-agent-runtime: aws CLI is required" >&2
  exit 2
}
command -v jq >/dev/null 2>&1 || {
  echo "resolve-agent-runtime: jq is required" >&2
  exit 2
}

if [[ -z "$RUNTIME_NAME" ]]; then
  if [[ ! -r "$DEFAULT_MANIFEST" ]]; then
    echo "resolve-agent-runtime: AgentCore manifest is missing; set RUNTIME_NAME" >&2
    exit 2
  fi
  project_name=$(jq -er \
    '.name | select(type == "string" and length > 0)' \
    "$DEFAULT_MANIFEST") || {
      echo "resolve-agent-runtime: AgentCore manifest project name is invalid" >&2
      exit 2
    }
  runtime_component=$(jq -er \
    '.runtimes | select(type == "array" and length == 1) | .[0].name | select(type == "string" and length > 0)' \
    "$DEFAULT_MANIFEST") || {
      echo "resolve-agent-runtime: set RUNTIME_NAME when the manifest does not contain exactly one runtime" >&2
      exit 2
    }
  RUNTIME_NAME="${project_name}_${runtime_component}"
fi
if [[ ! "$RUNTIME_NAME" =~ ^[A-Za-z][A-Za-z0-9_]{0,47}$ ]]; then
  echo "resolve-agent-runtime: RUNTIME_NAME is invalid" >&2
  exit 2
fi
commercial_region_pattern='^(af-south|ap-(east|northeast|south|southeast)|ca-(central|west)|eu-(central|north|south|west)|il-central|me-(central|south)|mx-central|sa-east|us-(east|west))-[0-9]+$'
govcloud_region_pattern='^us-gov-(east|west)-[0-9]+$'

identity_json=$(aws sts get-caller-identity \
  --region "$AWS_REGION" \
  --output json \
  --no-cli-pager)
account_id=$(jq -r '.Account // empty' <<<"$identity_json")
caller_arn=$(jq -r '.Arn // empty' <<<"$identity_json")
if [[ ! "$account_id" =~ ^[0-9]{12}$ ]] || [[ ! "$caller_arn" =~ ^arn:[^:]+: ]]; then
  echo "resolve-agent-runtime: STS returned an invalid identity" >&2
  exit 1
fi
partition="${caller_arn#arn:}"
partition="${partition%%:*}"
case "$partition" in
  aws)
    if [[ ! "$AWS_REGION" =~ $commercial_region_pattern ]]; then
      echo "resolve-agent-runtime: STS partition does not match AWS_REGION" >&2
      exit 1
    fi
    ;;
  aws-us-gov)
    if [[ ! "$AWS_REGION" =~ $govcloud_region_pattern ]]; then
      echo "resolve-agent-runtime: STS partition does not match AWS_REGION" >&2
      exit 1
    fi
    ;;
  *)
    echo "resolve-agent-runtime: unsupported AWS partition: $partition" >&2
    exit 1
    ;;
esac

runtimes_json=$(aws bedrock-agentcore-control list-agent-runtimes \
  --region "$AWS_REGION" \
  --output json \
  --no-cli-pager)
runtime_rows=$(jq -c --arg name "$RUNTIME_NAME" \
  '.agentRuntimes[]? | select(.agentRuntimeName == $name) | {
    agentRuntimeArn, agentRuntimeId, agentRuntimeVersion
  }' \
  <<<"$runtimes_json" | LC_ALL=C sort -u)
runtime_count=$(awk 'NF { count += 1 } END { print count + 0 }' <<<"$runtime_rows")

legacy_arn_pattern="^arn:${partition}:bedrock-agentcore:${AWS_REGION}:${account_id}:runtime/[A-Za-z][A-Za-z0-9_]{0,99}-[A-Za-z0-9]{10}$"
versioned_arn_pattern="^arn:${partition}:bedrock-agentcore:${AWS_REGION}:${account_id}:agent/[A-Fa-f0-9]{8}-[A-Fa-f0-9]{4}-[A-Fa-f0-9]{4}-[A-Fa-f0-9]{4}-[A-Fa-f0-9]{12}:[1-9][0-9]{0,4}$"

is_expected_runtime_arn() {
  [[ "$1" =~ $legacy_arn_pattern || "$1" =~ $versioned_arn_pattern ]]
}

if [[ -n "$RUNTIME_ARN_OVERRIDE" ]]; then
  if ! is_expected_runtime_arn "$RUNTIME_ARN_OVERRIDE"; then
    echo "resolve-agent-runtime: override is not a Runtime ARN in the active account and region" >&2
    exit 1
  fi
  selected_rows=$(jq -c --arg arn "$RUNTIME_ARN_OVERRIDE" \
    'select(.agentRuntimeArn == $arn)' <<<"$runtime_rows")
  selected_count=$(awk 'NF { count += 1 } END { print count + 0 }' <<<"$selected_rows")
  if [[ "$selected_count" -ne 1 ]]; then
    echo "resolve-agent-runtime: override does not identify a runtime named $RUNTIME_NAME" >&2
    exit 1
  fi
  selected_row="$selected_rows"
elif [[ "$runtime_count" -eq 1 ]]; then
  selected_row="$runtime_rows"
else
  echo "resolve-agent-runtime: expected exactly one runtime named $RUNTIME_NAME; found $runtime_count" >&2
  echo "Set RUNTIME_ARN_OVERRIDE to select an exact matching ARN." >&2
  exit 1
fi

runtime_arn=$(jq -r '.agentRuntimeArn // empty' <<<"$selected_row")
runtime_id=$(jq -r '.agentRuntimeId // empty' <<<"$selected_row")
runtime_version=$(jq -r '.agentRuntimeVersion // empty | tostring' <<<"$selected_row")
if ! is_expected_runtime_arn "$runtime_arn"; then
  echo "resolve-agent-runtime: discovered ARN has an unexpected account, region, or resource ID" >&2
  exit 1
fi
if [[ ! "$runtime_id" =~ ^[A-Za-z][A-Za-z0-9_]{0,99}-[A-Za-z0-9]{10}$ ]] || \
   [[ ! "$runtime_version" =~ ^[1-9][0-9]{0,4}$ ]]; then
  echo "resolve-agent-runtime: discovered runtime has an invalid ID or version" >&2
  exit 1
fi
if [[ "$runtime_arn" =~ $legacy_arn_pattern ]] && \
   [[ "${runtime_arn##*/}" != "$runtime_id" ]]; then
  echo "resolve-agent-runtime: legacy ARN resource does not match agentRuntimeId" >&2
  exit 1
fi
if [[ "$runtime_arn" =~ $versioned_arn_pattern ]] && \
   [[ "${runtime_arn##*:}" != "$runtime_version" ]]; then
  echo "resolve-agent-runtime: versioned ARN does not match agentRuntimeVersion" >&2
  exit 1
fi
runtime_json=$(aws bedrock-agentcore-control get-agent-runtime \
  --agent-runtime-id "$runtime_id" \
  --agent-runtime-version "$runtime_version" \
  --region "$AWS_REGION" \
  --output json \
  --no-cli-pager)
actual_arn=$(jq -r '.agentRuntimeArn // empty' <<<"$runtime_json")
actual_id=$(jq -r '.agentRuntimeId // empty' <<<"$runtime_json")
actual_version=$(jq -r '.agentRuntimeVersion // empty | tostring' <<<"$runtime_json")
actual_name=$(jq -r '.agentRuntimeName // empty' <<<"$runtime_json")
actual_status=$(jq -r '.status // empty' <<<"$runtime_json")
if [[ "$actual_arn" != "$runtime_arn" ]] || \
   [[ "$actual_id" != "$runtime_id" ]] || \
   [[ "$actual_version" != "$runtime_version" ]] || \
   [[ "$actual_name" != "$RUNTIME_NAME" ]] || \
   [[ "$actual_status" != "READY" ]]; then
  echo "resolve-agent-runtime: runtime failed identity/readiness validation (name=$actual_name status=$actual_status)" >&2
  exit 1
fi

echo "Resolved $actual_name runtime in $AWS_REGION." >&2
printf '%s\n' "$runtime_arn"
