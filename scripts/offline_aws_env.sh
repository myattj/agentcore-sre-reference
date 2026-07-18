#!/usr/bin/env bash

# Run a command with no ambient AWS target or credential sources. Callers must
# provide any synthetic CDK account and region values in the command itself.
run_offline_aws() {
  env \
    -u AWS_ACCESS_KEY_ID \
    -u AWS_SECRET_ACCESS_KEY \
    -u AWS_SESSION_TOKEN \
    -u AWS_SECURITY_TOKEN \
    -u AWS_PROFILE \
    -u AWS_DEFAULT_PROFILE \
    -u AWS_ROLE_ARN \
    -u AWS_WEB_IDENTITY_TOKEN_FILE \
    -u AWS_CONTAINER_CREDENTIALS_RELATIVE_URI \
    -u AWS_CONTAINER_CREDENTIALS_FULL_URI \
    -u AWS_CONTAINER_AUTHORIZATION_TOKEN \
    -u AWS_REGION \
    -u AWS_DEFAULT_REGION \
    -u CDK_DEFAULT_ACCOUNT \
    -u CDK_DEFAULT_REGION \
    AWS_CONFIG_FILE=/dev/null \
    AWS_SHARED_CREDENTIALS_FILE=/dev/null \
    AWS_EC2_METADATA_DISABLED=true \
    "$@"
}
