#!/usr/bin/env bash
# Validate and deploy the AgentCore runtime with an explicit, portable region.
#
# The pinned AgentCore CLI reads runtime environment variables from
# agentcore.json. This wrapper swaps in a temporary manifest for the duration
# of validate/deploy, then atomically restores the tracked manifest.

set -euo pipefail

SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
ROOT_DIR=$(CDPATH='' cd -- "$SCRIPT_DIR/.." && pwd)
CORE_DIR="$ROOT_DIR/coreAgent"
MANIFEST_DIR="$CORE_DIR/agentcore"
MANIFEST="$MANIFEST_DIR/agentcore.json"
TARGETS="$MANIFEST_DIR/aws-targets.json"
SUPPORTED_REGIONS="$SCRIPT_DIR/agentcore_cli_regions.txt"
LOCK_DIR="$MANIFEST_DIR/.agentcore-deploy.lock"
BACKUP="$LOCK_DIR/agentcore.json.original"
RENDERED=""
BACKUP_READY=0

die() {
  echo "deploy-agent: $*" >&2
  exit 2
}

is_safe_regular_file() {
  [[ -f "$1" && ! -L "$1" ]]
}

command -v jq >/dev/null 2>&1 || die "jq is required"
command -v agentcore >/dev/null 2>&1 || die "AgentCore CLI is required"
is_safe_regular_file "$MANIFEST" || die "agentcore.json must be a regular, non-symlink file"
is_safe_regular_file "$TARGETS" || die "run make aws-configure to create a safe aws-targets.json"
is_safe_regular_file "$SUPPORTED_REGIONS" || die "the pinned AgentCore CLI region list is missing or unsafe"

AWS_REGION="${AWS_REGION:-}"
[[ "$AWS_REGION" =~ ^[a-z]{2}(-[a-z0-9]+)+-[0-9]+$ ]] || \
  die "AWS_REGION is required and must be a valid region"
grep -Fxq -- "$AWS_REGION" "$SUPPORTED_REGIONS" || \
  die "AWS_REGION is not supported by the pinned AgentCore CLI; see scripts/agentcore_cli_regions.txt"

target_region=$(jq -er \
  'select(type == "array" and length == 1) | .[0] |
   select((.account | type == "string" and test("^[0-9]{12}$")) and
          (.region | type == "string")) | .region' \
  "$TARGETS") || die "aws-targets.json must contain exactly one valid AWS target"
[[ "$target_region" == "$AWS_REGION" ]] || \
  die "AWS_REGION does not match the sole target in aws-targets.json"

jq -e \
  '.runtimes | type == "array" and length == 1 and
   (.[0].envVars == null or (.[0].envVars | type == "array"))' \
  "$MANIFEST" >/dev/null || \
  die "agentcore.json must contain exactly one runtime and an optional envVars array"

GITHUB_APP_ID="${GITHUB_APP_ID:-}"
DOMAIN_NAME="${DOMAIN_NAME:-}"
AGENTCORE_MEMORY_ID="${AGENTCORE_MEMORY_ID:-}"
AGENTCORE_SEMANTIC_STRATEGY_ID="${AGENTCORE_SEMANTIC_STRATEGY_ID:-}"
AGENTCORE_USER_PREF_STRATEGY_ID="${AGENTCORE_USER_PREF_STRATEGY_ID:-}"
DEPLOY_EXPERIMENTAL_SANDBOX="${DEPLOY_EXPERIMENTAL_SANDBOX:-false}"

if [[ -n "$GITHUB_APP_ID" && ! "$GITHUB_APP_ID" =~ ^[0-9]+$ ]]; then
  die "GITHUB_APP_ID must be numeric"
fi
case "$DEPLOY_EXPERIMENTAL_SANDBOX" in
  true)
    SANDBOX_ENABLED=1
    ;;
  false|"")
    SANDBOX_ENABLED=""
    ;;
  *)
    die "DEPLOY_EXPERIMENTAL_SANDBOX must be true or false"
    ;;
esac
DASHBOARD_BASE_URL=""
if [[ -n "$DOMAIN_NAME" ]]; then
  DASHBOARD_BASE_URL="https://$DOMAIN_NAME"
fi

umask 077
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  die "another deployment or stale lock exists at $LOCK_DIR"
fi
chmod 700 "$LOCK_DIR"
printf '%s\n' "$$" > "$LOCK_DIR/pid"

cleanup() {
  status=$?
  trap - EXIT HUP INT TERM
  restore_failed=0

  if [[ -n "$RENDERED" && -e "$RENDERED" ]]; then
    rm -f -- "$RENDERED" || true
  fi
  if [[ "$BACKUP_READY" -eq 1 && -f "$BACKUP" ]]; then
    if ! mv -f -- "$BACKUP" "$MANIFEST"; then
      echo "deploy-agent: ERROR: could not restore $MANIFEST; original remains at $BACKUP" >&2
      restore_failed=1
    fi
  fi
  if [[ "$restore_failed" -eq 0 ]]; then
    rm -f -- "$LOCK_DIR/pid" || true
    rmdir "$LOCK_DIR" 2>/dev/null || true
  else
    status=1
  fi
  exit "$status"
}

on_signal() {
  exit "$1"
}

trap cleanup EXIT
trap 'on_signal 129' HUP
trap 'on_signal 130' INT
trap 'on_signal 143' TERM

RENDERED=$(mktemp "$LOCK_DIR/agentcore.json.rendered.XXXXXX")
jq \
  --arg aws_region "$AWS_REGION" \
  --arg github_app_id "$GITHUB_APP_ID" \
  --arg dashboard_base_url "$DASHBOARD_BASE_URL" \
  --arg memory_id "$AGENTCORE_MEMORY_ID" \
  --arg semantic_id "$AGENTCORE_SEMANTIC_STRATEGY_ID" \
  --arg user_pref_id "$AGENTCORE_USER_PREF_STRATEGY_ID" \
  --arg sandbox_enabled "$SANDBOX_ENABLED" \
  '
    def optional_env($name; $value):
      if $value == "" then [] else [{name: $name, value: $value}] end;
    [
      "AWS_REGION",
      "GITHUB_APP_ID",
      "DASHBOARD_BASE_URL",
      "AGENTCORE_MEMORY_ID",
      "AGENTCORE_SEMANTIC_STRATEGY_ID",
      "AGENTCORE_USER_PREF_STRATEGY_ID",
      "ENABLE_EXPERIMENTAL_PR_SANDBOX"
    ] as $managed |
    .runtimes[0].envVars = (
      ((.runtimes[0].envVars // []) |
        map(select(.name as $name | ($managed | index($name) | not)))) +
      [{name: "AWS_REGION", value: $aws_region}] +
      optional_env("GITHUB_APP_ID"; $github_app_id) +
      optional_env("DASHBOARD_BASE_URL"; $dashboard_base_url) +
      optional_env("AGENTCORE_MEMORY_ID"; $memory_id) +
      optional_env("AGENTCORE_SEMANTIC_STRATEGY_ID"; $semantic_id) +
      optional_env("AGENTCORE_USER_PREF_STRATEGY_ID"; $user_pref_id) +
      optional_env("ENABLE_EXPERIMENTAL_PR_SANDBOX"; $sandbox_enabled)
    )
  ' \
  "$MANIFEST" > "$RENDERED"
jq -e . "$RENDERED" >/dev/null

# Mark the backup as expected before the first rename so a deferred signal
# cannot land between moving the original and enabling restoration.
BACKUP_READY=1
mv -- "$MANIFEST" "$BACKUP"
mv -- "$RENDERED" "$MANIFEST"
RENDERED=""

export AWS_REGION
AWS_DEFAULT_REGION="$AWS_REGION"
export AWS_DEFAULT_REGION

(
  cd "$CORE_DIR"
  agentcore validate
  agentcore deploy "$@"
)
