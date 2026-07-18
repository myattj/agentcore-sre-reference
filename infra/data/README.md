# AWS infrastructure

This CDK application defines the durable data, observability, and optional
service infrastructure for the archived AgentCore reference project. Nothing in
this directory should be treated as a one-click production deployment.

See the [root README](../../README.md) for project status and architecture.

## Stacks

The CDK app can define five stacks. Context determines which optional stacks are
included.

| Deployed stack name | Created when | Main resources |
|---|---|---|
| <code>AgentCore-coreAgent-data-&lt;region&gt;</code> (<code>DataStack</code>) | Always | Five DynamoDB tables and three least-purpose IAM policies |
| <code>AgentCore-coreAgent-observability-&lt;region&gt;</code> (<code>ObservabilityStack</code>) | Always | CloudWatch dashboard, alarms, and SNS topic |
| <code>AgentCore-coreAgent-gateway-&lt;region&gt;</code> (<code>GatewayStack</code>) | <code>bridgePublicUrl</code> is set | AgentCore Gateway support resources and interceptor Lambda |
| <code>AgentCore-coreAgent-services-&lt;region&gt;</code> (<code>ServicesStack</code>) | <code>agentRuntimeArn</code> is set | VPC, ECS cluster, ALB, bridge service, and onboarding service |
| <code>AgentCore-coreAgent-sandbox-&lt;region&gt;</code> (<code>SandboxStack</code>) | Sandbox secret and VPC contexts are set | Sandbox task resources, job table, logs, security group, and IAM policy |

The full deployment contains six DynamoDB tables: five from
<code>AgentCore-coreAgent-data-&lt;region&gt;</code> and
<code>sandbox_jobs</code> from the optional sandbox stack.

### Data tables

| Table | Purpose | Removal policy |
|---|---|---|
| <code>tenants</code> | Tenant agent configuration | Retain |
| <code>workspace_to_tenant</code> | Slack workspace lookup | Retain |
| <code>audit_log</code> | Agent and tool audit events | Retain, with TTL support |
| <code>processed_events</code> | Slack idempotency records | Retain, with TTL |
| <code>dashboards</code> | Temporary dashboard payloads | Destroy, with seven-day TTL |
| <code>sandbox_jobs</code> | Optional sandbox coordination | Retain, with TTL |

The base stack also creates separate AgentCore, bridge, and onboarding data
access policies. The sandbox stack adds its own access policy.

## Prerequisites

- Node.js 22
- AWS CDK v2 credentials for the target account and region
- Docker when building service images
- Python 3.13 and <code>uv</code> for provisioning helpers
- A deployed AgentCore runtime for the optional services stack
- AWS Secrets Manager entries for Slack and bridge secrets

AgentCore, Bedrock model, Gateway, and Memory availability varies by AWS region.
Verify current support and pricing before choosing a region.

## Build and synthesize

~~~bash
cd infra/data
npm ci
npm run build
bash scripts/build_interceptor_zip.sh
CDK_DEFAULT_ACCOUNT=000000000000 npx cdk synth --quiet
~~~

The default synth covers <code>AgentCore-coreAgent-data-us-west-2</code> and
<code>AgentCore-coreAgent-observability-us-west-2</code>. Add context values to
inspect optional stacks; the CI workflow contains reproducible synthetic
examples. If you set <code>-c region=...</code>, replace the region suffix in
the stack names below.

## Deploy the base stacks

~~~bash
cd infra/data
npx cdk bootstrap
npx cdk deploy \
  AgentCore-coreAgent-data-us-west-2 \
  AgentCore-coreAgent-observability-us-west-2
~~~

Set <code>-c alarmEmail=you@example.com</code> if you want alarm email
subscriptions. After deploying the agent runtime, attach the generated data
policy with [scripts/attach_agent_policy.sh](scripts/attach_agent_policy.sh).

## Optional deployment contexts

### AgentCore Gateway

Supply <code>bridgePublicUrl</code> to include
<code>AgentCore-coreAgent-gateway-&lt;region&gt;</code> and, optionally,
<code>gatewayJwtIssuer</code> for JWT configuration. Build the interceptor
package before synth or deploy.

The CDK stack prepares support resources; create or update the actual AgentCore
Gateway and its SSM coordinates separately:

~~~bash
cd infra/data
bash scripts/build_interceptor_zip.sh
npx cdk deploy AgentCore-coreAgent-gateway-us-west-2 \
  -c bridgePublicUrl=https://agent.example.com
uv run --with boto3 python scripts/provision_gateway.py
~~~

### Bridge and onboarding services

<code>AgentCore-coreAgent-services-&lt;region&gt;</code> requires
<code>agentRuntimeArn</code> plus Slack and bridge shared-secret ARNs. A
certificate and domain are optional only for local/synthetic stack validation;
the production workflow requires both and refuses an HTTP deployment. The
reference stack routes the bridge and onboarding service
through one public origin because the Slack OAuth callback sets the
host-scoped HttpOnly onboarding cookie. GitHub App and sandbox contexts are
also optional.

The referenced Secrets Manager values must be JSON objects with these exact
keys; ECS resolves every listed key before starting either task:

<code>agentcore/services/slack</code>:

~~~json
{
  "SLACK_CLIENT_ID": "…",
  "SLACK_CLIENT_SECRET": "…",
  "SLACK_SIGNING_SECRET": "…",
  "SLACK_APP_ID": "A…"
}
~~~

<code>agentcore/services/bridge</code>:

~~~json
{
  "BRIDGE_OAUTH_STATE_SECRET": "at least 32 random characters",
  "BRIDGE_GATEWAY_JWT_PRIVATE_KEY_PEM": "-----BEGIN PRIVATE KEY-----\n…",
  "ADMIN_SECRET": "a separate high-entropy operator secret"
}
~~~

<code>SLACK_APP_ID</code> is required so reaction feedback can prove that both
the signed event and reacted message belong to this app; missing identity fails
closed. <code>ADMIN_SECRET</code> protects operator routes and signs the
operator-session cookie. Never reuse either HMAC secret as a Slack credential.

Avoid putting secret JSON directly in shell history or process arguments. This
Unix example writes a mode-0600 temporary file, passes only its path to the AWS
CLI, and removes it on exit. Replace <code>create-secret</code> with
<code>put-secret-value --secret-id …</code> when rotating an existing secret.

~~~bash
umask 077
SERVICE_SECRET_FILE=$(mktemp)
cleanup_service_secret() {
  rm -f "$SERVICE_SECRET_FILE"
  unset SLACK_CLIENT_ID SLACK_CLIENT_SECRET SLACK_SIGNING_SECRET SLACK_APP_ID
}
trap cleanup_service_secret EXIT
trap 'exit 130' HUP INT TERM

read -rp 'Slack client ID: ' SLACK_CLIENT_ID
read -rsp 'Slack client secret: ' SLACK_CLIENT_SECRET; printf '\n'
read -rsp 'Slack signing secret: ' SLACK_SIGNING_SECRET; printf '\n'
read -rp 'Slack app ID: ' SLACK_APP_ID
export SLACK_CLIENT_ID SLACK_CLIENT_SECRET SLACK_SIGNING_SECRET SLACK_APP_ID
python3.13 - <<'PY' >"$SERVICE_SECRET_FILE"
import json, os
print(json.dumps({key: os.environ[key] for key in (
    "SLACK_CLIENT_ID", "SLACK_CLIENT_SECRET", "SLACK_SIGNING_SECRET", "SLACK_APP_ID"
)}))
PY
aws secretsmanager create-secret \
  --name agentcore/services/slack \
  --secret-string "file://$SERVICE_SECRET_FILE"

read -rp 'Gateway JWT private-key file: ' GATEWAY_KEY_FILE
python3.13 - "$GATEWAY_KEY_FILE" <<'PY' >"$SERVICE_SECRET_FILE"
import json, secrets, sys
from pathlib import Path
print(json.dumps({
    "BRIDGE_OAUTH_STATE_SECRET": secrets.token_hex(32),
    "BRIDGE_GATEWAY_JWT_PRIVATE_KEY_PEM": Path(sys.argv[1]).read_text(),
    "ADMIN_SECRET": secrets.token_urlsafe(48),
}))
PY
aws secretsmanager create-secret \
  --name agentcore/services/bridge \
  --secret-string "file://$SERVICE_SECRET_FILE"
~~~

Inspect [bin/data.ts](bin/data.ts) for the authoritative context names and
validation rules before deploying.

### Sandbox workers

Use [scripts/deploy_sandbox.sh](scripts/deploy_sandbox.sh) rather than invoking
the sandbox stack ad hoc. The wrapper validates the runtime, VPC, secret, domain,
and GitHub App inputs and can attach the resulting policy when requested.
The worker's Python dependencies are resolved by the checked-in
[sandbox lockfile](../sandbox/uv.lock); both local tests and the Docker image use
that frozen environment.

### Temporary dashboards

The dashboard table is always present, but publishing is optional. Outside local
development, configure the agent with an HTTPS <code>DASHBOARD_BASE_URL</code>.
Dashboard URLs contain unguessable bearer tokens; anyone with a URL can view its
contents until expiration, so never publish secrets there.

## Deployment policy

The repository's GitHub Actions workflow validates pull requests and pushes,
but production deployment runs only through manual dispatch with explicit
confirmation and requires <code>CERTIFICATE_ARN</code> plus
<code>DOMAIN_NAME</code>. The workflow deploys data and observability, the agent
runtime, services, and the optional sandbox. Memory provisioning and the actual
AgentCore Gateway remain separate operator steps.

Review removal policies, account and region settings, secret handling, IAM
scope, domains, TLS, alarms, and expected spend before deploying this archived
example.
