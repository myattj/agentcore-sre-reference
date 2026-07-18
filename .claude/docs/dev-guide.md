# Development guide

> This is reference documentation for an archived example. Start with the
> [root README](../../README.md) and do not assume its AWS resources are still
> deployed.

## Prerequisites

- Python 3.13
- <code>uv</code>
- Bedrock AgentCore CLI
- Node.js 22 and npm
- AWS credentials with access to a supported Bedrock model for real agent calls

The bridge and core agent have independent Python environments. Do not install
one service's dependencies into the other or cross-import their modules.

## Local development

For the no-cloud path, start at the repository root with <code>make doctor</code>,
<code>make setup</code>, and <code>make demo</code>. The three-service loop below
is for real Bedrock and AgentCore development.

Run the three services in separate terminals.

### 1. Core agent

~~~bash
cd coreAgent/app/coreAgent
uv sync --frozen --extra test
cd ../..
AGENT_LOCAL_STORES=1 agentcore dev --logs
~~~

The AgentCore development server listens on <http://127.0.0.1:8080>.
<code>AGENT_LOCAL_STORES=1</code> loads tenant, workspace, and memory fixtures
from the repository-level <code>examples/</code> directory. Model invocations still use Amazon
Bedrock.

### 2. Bridge

~~~bash
cd bridge
uv sync --frozen --extra dev
../scripts/setup.sh --env-only
uv run uvicorn bridge.main:app --reload --port 8000 --env-file .env.local
~~~

The tracked [bridge environment example](../../bridge/.env.example) sets
<code>LOCAL_DEV=1</code> and routes invocations to port 8080. In bridge local
mode, workspace mapping is read from JSON, event deduplication is in memory, and
<code>/debug/message</code> is available.

The two local flags are intentionally different:

- Agent: <code>AGENT_LOCAL_STORES=1</code>
- Bridge: <code>LOCAL_DEV=1</code>

The AgentCore CLI reserves <code>LOCAL_DEV</code>; do not use it as the agent's
fixture switch.

### 3. Onboarding UI

~~~bash
cd onboarding
../scripts/setup.sh --env-only
npm ci
npm run dev
~~~

The UI listens on <http://localhost:3000> and calls the bridge at
<http://localhost:8000>. The tracked
[onboarding environment example](../../onboarding/.env.example) documents all
required and optional values.

## Smoke test

~~~bash
curl http://localhost:8000/healthz

curl -X POST http://localhost:8000/debug/message \
  -H 'Content-Type: application/json' \
  -d '{"workspace_id":"demo-ws","user_id":"u1","text":"Summarize the current incident"}'
~~~

The debug request exercises the bridge-to-agent path and may incur Bedrock
usage. Slack OAuth and signed webhook flows require credentials from a Slack app
you own.

## Validation commands

~~~bash
# Bridge
cd bridge
uv sync --extra dev
uv run pytest

# Core agent
cd ../coreAgent/app/coreAgent
uv sync --extra test
uv run pytest
uv run python -m unittest test_metrics

# Onboarding
cd ../../../onboarding
npm ci
NEXT_PUBLIC_BRIDGE_INSTALL_URL=https://ci.test/slack/install npm run build

# Infrastructure synth
cd ../infra/data
npm ci
npm run build
bash scripts/build_interceptor_zip.sh
CDK_DEFAULT_ACCOUNT=000000000000 npx cdk synth --quiet
~~~

The CI workflow also tests the Gateway interceptor, sandbox worker, generated
AgentCore CDK, and optional CDK stack combinations.

## Production reference path

Production deployment is manual-only through GitHub Actions and requires
<code>deploy_production=true</code>. The workflow deploys the data and
observability stacks, AgentCore runtime, Fargate services, and optional sandbox.
It does not automatically provision AgentCore Memory or the actual Gateway.

Typical operator sequence:

1. Deploy <code>DataStack</code> and <code>ObservabilityStack</code>.
2. Validate and deploy the AgentCore runtime.
3. Attach the generated AgentCore data policy.
4. Provision Memory and configure its IDs if memory is enabled.
5. Optionally provision Gateway targets and deploy Fargate services.
6. Optionally deploy the PR sandbox with its wrapper script.
7. Configure your own Slack app, domains, TLS certificate, secrets, alarms, and
   budget controls.

See [infra/data/README.md](../../infra/data/README.md) for current stacks and
contexts. AgentCore and Bedrock availability varies by AWS region, so verify the
current AWS documentation before deployment.

## Environment map

| Service | Important variables |
|---|---|
| Core agent | <code>AGENT_LOCAL_STORES</code>, table names, Memory IDs, <code>METRICS_NAMESPACE</code>, sandbox and dashboard settings |
| Bridge | Slack credentials, <code>BRIDGE_OAUTH_STATE_SECRET</code>, <code>LOCAL_AGENT_URL</code>, table names, <code>ADMIN_SECRET</code>, optional GitHub and sandbox settings |
| Onboarding | <code>BRIDGE_URL</code>, shared <code>BRIDGE_OAUTH_STATE_SECRET</code>, public install URL, optional admin secret and GitHub App slug |
| CDK | Account and region plus stack-specific context values documented in <code>infra/data/bin/data.ts</code> |

Never commit real credentials. Use local ignored environment files and AWS
Secrets Manager for deployed services.
