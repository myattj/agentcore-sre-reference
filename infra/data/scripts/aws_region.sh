#!/usr/bin/env bash

# Resolve the deployment region consistently for the Bash deployment helpers.
# An explicit REGION wins, followed by the standard AWS SDK environment names,
# the active AWS CLI profile, and finally the example fallback.
resolve_aws_region() {
  local selected_region profile profile_region
  selected_region="${REGION:-${AWS_REGION:-${AWS_DEFAULT_REGION:-}}}"
  if [[ -z "$selected_region" ]] && command -v aws >/dev/null 2>&1; then
    profile="${AWS_PROFILE:-${AWS_DEFAULT_PROFILE:-}}"
    if [[ -n "$profile" ]]; then
      if ! profile_region=$(aws configure get region --profile "$profile" 2>/dev/null); then
        echo "ERROR: could not read region from AWS profile: $profile" >&2
        return 1
      fi
    else
      if ! profile_region=$(aws configure get region 2>/dev/null); then
        echo "ERROR: could not read region from the default AWS profile" >&2
        return 1
      fi
    fi
    selected_region="$profile_region"
  fi
  selected_region="${selected_region:-us-west-2}"
  if [[ ! "$selected_region" =~ ^[a-z]{2}(-[a-z0-9]+)+-[0-9]+$ ]]; then
    echo "ERROR: invalid AWS region: $selected_region" >&2
    return 1
  fi
  printf '%s\n' "$selected_region"
}
